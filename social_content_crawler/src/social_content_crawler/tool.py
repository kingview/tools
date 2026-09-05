from __future__ import annotations

from .diagnostics import logged

import asyncio
import hashlib
import json
import mimetypes
import re
import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from pydantic import HttpUrl, TypeAdapter, ValidationError

from .contracts import (
    AuditEvent,
    DownloadedArtifact,
    DownloadInput,
    DownloadMode,
    DownloadOutput,
    MediaInfo,
    ToolSpec,
    TelegramDownloadScope,
)
from .errors import CrawlerError, ErrorCode
from .ports import AuditSink, DownloaderBackend, RateLimiter, ToolContext, UrlPolicy


TOOL_SPEC = ToolSpec(
    name="social.download_media",
    version="1.9.2",
    description=(
        "Extract metadata or download social-media media; registered BitBrowser "
        "sessions use the Profile's proxy route as well as its platform cookies."
    ),
    input_schema=DownloadInput.model_json_schema(),
    output_schema=DownloadOutput.model_json_schema(),
    category="read",
    side_effect=False,
    risk_level="medium",
    timeout_seconds=900,
    max_retries=2,
    idempotent=True,
    supports_dry_run=True,
    required_permissions=["social_content.read", "media.download"],
    policy_tags=["public-or-authorized-content", "opaque-session-reference", "domain-allowlist", "audited"],
    rate_limit_bucket="social-media-public-read",
    requires_approval=False,
)


class SocialMediaDownloadTool:
    def __init__(
        self,
        *,
        backend: DownloaderBackend,
        audit_sink: AuditSink,
        rate_limiter: RateLimiter,
        url_policy: UrlPolicy,
        output_root: Path,
        allowed_domains: Iterable[str],
    ) -> None:
        self._backend = backend
        self._audit_sink = audit_sink
        self._rate_limiter = rate_limiter
        self._url_policy = url_policy
        self._output_root = output_root.resolve()
        self._allowed_domains = frozenset(
            domain.lower().strip().lstrip(".") for domain in allowed_domains if domain.strip()
        )
        if not self._allowed_domains:
            raise ValueError("allowed_domains cannot be empty")

    @property
    def spec(self) -> ToolSpec:
        return TOOL_SPEC

    @logged("social-content", "social.download_media")
    async def execute(self, request: DownloadInput, context: ToolContext) -> DownloadOutput:
        input_hash = _hash_model(request)
        output: DownloadOutput | None = None
        error: CrawlerError | None = None
        output_directory: Path | None = None
        try:
            for url in request.urls:
                await asyncio.to_thread(
                    self._url_policy.validate, str(url), self._allowed_domains
                )
            await self._rate_limiter.acquire(
                f"{TOOL_SPEC.rate_limit_bucket}:{_host(request.urls[0])}",
                context.tenant_id,
            )
            output_directory = self._create_output_directory(context, input_hash, request)
            raw_items = await asyncio.to_thread(self._backend.run, request, output_directory)
            artifacts = _artifacts(output_directory)
            maximum_bytes = request.max_file_size_mb * 1024 * 1024
            if any(artifact.size_bytes > maximum_bytes for artifact in artifacts):
                raise CrawlerError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "downloaded artifact exceeds the configured file-size limit",
                )
            maximum_total_bytes = request.max_total_size_mb * 1024 * 1024
            counted_artifacts = [
                artifact
                for artifact in artifacts
                if Path(artifact.path).name != "telegram-channel-manifest.jsonl"
            ]
            if sum(artifact.size_bytes for artifact in counted_artifacts) > maximum_total_bytes:
                raise CrawlerError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "downloaded artifacts exceed the configured total-size limit",
                )
            output = DownloadOutput(
                items=[_normalize_info(item) for item in raw_items],
                artifacts=artifacts,
                output_directory=(
                    str(output_directory) if request.mode is DownloadMode.DOWNLOAD else None
                ),
                network_route=(
                    str(getattr(self._backend, "last_network_route", "direct"))
                ),
                checkpoint_path=getattr(self._backend, "last_checkpoint_path", None),
                completed=bool(getattr(self._backend, "last_completed", True)),
                stop_reason=str(getattr(self._backend, "last_stop_reason", "completed")),
                scanned_count=int(getattr(self._backend, "last_scanned_count", len(raw_items))),
            )
            return output
        except CrawlerError as exc:
            error = exc
            if output_directory is not None and not (request.session_ref or '').startswith('sess_telegram_') and request.telegram_scope is not TelegramDownloadScope.CHANNEL:
                shutil.rmtree(output_directory, ignore_errors=True)
            raise
        except Exception as exc:
            error = CrawlerError(
                ErrorCode.DOWNLOAD_FAILED,
                "unexpected downloader failure",
                retryable=False,
            )
            if output_directory is not None and not (request.session_ref or '').startswith('sess_telegram_') and request.telegram_scope is not TelegramDownloadScope.CHANNEL:
                shutil.rmtree(output_directory, ignore_errors=True)
            raise error from exc
        finally:
            await self._audit_sink.record(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    trace_id=context.trace_id,
                    workflow_run_id=context.workflow_run_id,
                    agent_run_id=context.agent_run_id,
                    actor_type=context.actor_type,
                    actor_id=context.actor_id,
                    event_type="tool.failed" if error else "tool.succeeded",
                    tool_name=TOOL_SPEC.name,
                    tool_version=TOOL_SPEC.version,
                    input_hash=input_hash,
                    output_hash=_hash_model(output) if output else None,
                    error_code=error.code if error else None,
                    created_at=datetime.now(UTC),
                )
            )

    def _create_output_directory(
        self,
        context: ToolContext,
        input_hash: str,
        request: DownloadInput,
    ) -> Path:
        tenant = re.sub(r"[^A-Za-z0-9_.-]", "_", context.tenant_id)[:64] or "tenant"
        if request.telegram_scope is TelegramDownloadScope.CHANNEL:
            stable_payload = json.dumps(
                {
                    "session_ref": request.session_ref,
                    "url": str(request.urls[0]),
                    "media_format": request.media_format,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            stable_hash = hashlib.sha256(stable_payload.encode()).hexdigest()[:24]
            invocation = f"telegram-channel-{stable_hash}"
        elif (request.session_ref or '').startswith('sess_telegram_'):
            invocation = f'telegram-messages-{input_hash[:24]}'
            # Older releases used a random suffix. Reuse only this exact
            # request-hash prefix within the same tenant, never scan by title.
            tenant_root = self._output_root / tenant
            legacy = [path for path in tenant_root.glob(f'{input_hash[:12]}-*')
                      if path.is_dir() and not path.is_symlink()
                      and re.fullmatch(re.escape(input_hash[:12]) + r'-[a-f0-9]{12}', path.name)]
            if legacy:
                invocation = max(legacy, key=lambda path:path.stat().st_mtime).name
        else:
            invocation = f"{input_hash[:12]}-{uuid.uuid4().hex[:12]}"
        destination = (self._output_root / tenant / invocation).resolve()
        if not destination.is_relative_to(self._output_root):
            raise CrawlerError(ErrorCode.CONFIGURATION_ERROR, "unsafe output directory")
        destination.mkdir(
            parents=True,
            exist_ok=request.telegram_scope is TelegramDownloadScope.CHANNEL or (request.session_ref or '').startswith('sess_telegram_'),
        )
        return destination


def _normalize_info(info: dict[str, Any]) -> MediaInfo:
    source_url = str(info.get("__source_url") or info.get("webpage_url") or "")
    media_id = str(info.get("id") or hashlib.sha256(source_url.encode()).hexdigest()[:24])
    return MediaInfo(
        source_url=source_url,
        webpage_url=_http_url(info.get("webpage_url")),
        extractor=str(info.get("extractor_key") or info.get("extractor") or "unknown"),
        media_id=media_id,
        title=_text(info.get("title"), 2_000),
        description=_text(info.get("description"), 20_000),
        uploader=_text(info.get("uploader"), 1_000),
        uploader_id=_text(info.get("uploader_id"), 1_000),
        upload_date=_upload_date(info.get("upload_date")),
        duration_seconds=_number(info.get("duration")),
        thumbnail_url=_http_url(info.get("thumbnail")),
        view_count=_integer(info.get("view_count")),
        like_count=_integer(info.get("like_count")),
        repost_count=_integer(info.get("repost_count")),
        comment_count=_integer(info.get("comment_count")),
    )


def _artifacts(directory: Path) -> list[DownloadedArtifact]:
    artifacts = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix == ".part":
            continue
        artifacts.append(
            DownloadedArtifact(
                path=str(path.resolve()),
                size_bytes=path.stat().st_size,
                sha256=_hash_file(path),
                media_type=mimetypes.guess_type(path.name)[0],
            )
        )
    return artifacts


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_model(value: Any) -> str:
    if value is None:
        payload = b"null"
    elif hasattr(value, "model_dump_json"):
        payload = value.model_dump_json(exclude_none=True).encode()
    else:
        payload = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _host(url: HttpUrl) -> str:
    return (url.host or "unknown").lower()


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return None
    try:
        TypeAdapter(HttpUrl).validate_python(value)
    except ValidationError:
        return None
    return value


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _upload_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
