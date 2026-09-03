from __future__ import annotations

from .diagnostics import install_exception_hooks, record_exception

import argparse
import asyncio
import hashlib
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from typing import Sequence

from .adapters import DEFAULT_OLLAMA_MODEL
from .contracts import AnalyzeContentInput, ArtifactRef
from .errors import AnalyzerError
from .ports import ToolContext
from .runtime import build_local_tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-content-analyzer",
        description="Analyze downloaded images, audio, and videos locally.",
    )
    parser.add_argument("artifacts", nargs="+", help="Media files to analyze")
    parser.add_argument("--media-root", type=Path, help="Allowed media root")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".media-content-analyzer",
        help="Cache, audit, and temporary-work directory",
    )
    parser.add_argument("--post-text", help="Associated social post text")
    parser.add_argument("--source-url", help="Original public post URL")
    parser.add_argument("--language", dest="language_hint", help="Language hint, e.g. zh or en")
    parser.add_argument("--max-keyframes", type=int, default=24)
    parser.add_argument("--max-duration", type=int, default=3_600)
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument(
        "--model-base-url",
        help=(
            "Ollama/LiteLLM/vLLM OpenAI-compatible URL "
            "(default: http://127.0.0.1:11434/v1)"
        ),
    )
    parser.add_argument("--model", default=DEFAULT_OLLAMA_MODEL)
    parser.add_argument("--model-api-key")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--no-asr", action="store_true")
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--force", action="store_true", help="Ignore the analysis cache")
    parser.add_argument("--output", type=Path, help="Write JSON result to this file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    install_exception_hooks("media-content")
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (AnalyzerError, ValueError) as exc:
        record_exception("media-content", "cli.handled", exc)
        code = getattr(exc, "code", "invalid_input")
        print(f"ERROR [{code}]: {exc}", file=sys.stderr)
        return 2


async def _run(args: argparse.Namespace) -> int:
    paths = [Path(value).expanduser().resolve(strict=True) for value in args.artifacts]
    media_root = args.media_root.expanduser().resolve() if args.media_root else _common_parent(paths)
    manifests = [
        ArtifactRef(
            path=str(path),
            size_bytes=path.stat().st_size,
            sha256=_sha256(path),
            media_type=mimetypes.guess_type(path.name)[0],
        )
        for path in paths
    ]
    request = AnalyzeContentInput(
        artifacts=manifests,
        post_text=args.post_text,
        source_url=args.source_url,
        language_hint=args.language_hint,
        run_ocr=not args.no_ocr,
        transcribe_audio=not args.no_asr,
        run_vision_model=not args.no_vision,
        max_keyframes=args.max_keyframes,
        max_video_duration_seconds=args.max_duration,
        force_reanalyze=args.force,
    )
    tool = build_local_tool(
        allowed_media_root=media_root,
        state_root=args.state_root,
        enable_ocr=not args.no_ocr,
        enable_asr=not args.no_asr,
        enable_vision=not args.no_vision,
        model_base_url=args.model_base_url,
        model_name=args.model,
        model_api_key=args.model_api_key,
        whisper_model=args.whisper_model,
    )
    result = await tool.execute(
        request,
        ToolContext(
            tenant_id="local",
            trace_id=uuid.uuid4().hex,
            actor_type="user",
            actor_id=os.getenv("USER") or os.getenv("USERNAME") or "local-user",
        ),
    )
    rendered = result.model_dump_json(indent=2)
    if args.output:
        args.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
        print(str(args.output))
    else:
        print(rendered)
    return 0


def _common_parent(paths: Sequence[Path]) -> Path:
    return Path(os.path.commonpath([str(path.parent) for path in paths])).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
