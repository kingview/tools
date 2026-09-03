"""Development diagnostics, vendored verbatim in the independently installed tools.

Only stdlib dependencies. No request bodies, local variables or source lines are
logged. Each process owns a rotating JSONL file so plugins never race on rotation.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
RETAIN_PROCESS_LOGS = 10
_context = contextvars.ContextVar("diagnostic_context", default={})
_handlers: dict[tuple[str, str, int], logging.Logger] = {}
_lock = threading.RLock()
_secrets: set[str] = set()
_default_root: Path | None = None
_FIELDS = {"task_id", "conversation_id", "execution_id", "trace_id", "agent_run_id",
           "workflow_run_id", "plugin_id", "tool", "stage", "thread", "input_hash",
           "step_id", "step_item_id", "tool_call_id"}
TRANSPORT_KEY = "com.socialagent/diagnostics"
_TRANSPORT_FIELDS = {"task_id", "conversation_id", "execution_id", "trace_id",
                     "step_id", "step_item_id", "tool_call_id"}
_ERROR_ATTRIBUTE = "_social_agent_diagnostic"
_SENSITIVE = re.compile(r"key|password|secret|token|cookie|authorization|credential", re.I)


def register_secrets(*values: str) -> None:
    with _lock:
        _secrets.update(value for value in values if isinstance(value, str) and len(value) >= 4)


def redact(text: str) -> str:
    with _lock:
        secrets = _secrets | {v for k, v in os.environ.items() if _SENSITIVE.search(k) and len(v) >= 4}
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    # Drop cookie/header values as a unit (including cookies whose names are not known).
    text = re.sub(r"(?im)\b(?:set-cookie|cookie|authorization)\s*[:=]\s*[^\r\n]+", "[REDACTED HEADER]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)([\"']?[\w-]*(?:token|secret|password|api[_-]?key|cookie|authorization|credential)[\w-]*[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}", "[REDACTED KEY]", text)

    def safe_url(match):
        try:
            value = urlsplit(match.group())
            host = value.netloc.rsplit("@", 1)[-1]
            return urlunsplit((value.scheme, host, value.path, "[REDACTED]" if value.query else "", ""))
        except ValueError:
            return "[REDACTED URL]"

    text = re.sub(r"(?:https?|wss?|socks[45]h?)://[^\s\"'<>]+", safe_url, text)
    return text[:16_000]


def log_directory(state_root: Path | None = None) -> Path:
    if override := os.getenv("SOCIAL_AGENT_LOG_DIR"):
        return Path(override).expanduser().resolve()
    root = state_root or _default_root
    if root is None and (value := os.getenv("SOCIAL_AGENT_STATE_ROOT")):
        root = Path(value)
    if root is not None:
        return Path(root).expanduser().resolve() / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "SocialAgent"
    return Path(os.getenv("LOCALAPPDATA") or os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state") / "SocialAgent" / "logs"


def current_context() -> dict[str, str]:
    return dict(_context.get())


def transport_context(values: object) -> dict[str, str]:
    """Diagnostics only: no paths, credentials, policy grants or arbitrary data."""
    if not isinstance(values, dict):
        return {}
    return {k: v for k, v in values.items() if k in _TRANSPORT_FIELDS
            and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", v)}


@contextmanager
def diagnostic_context(*, replace: bool = False, **values):
    token = _context.set({**({} if replace else _context.get()),
                         **{k: str(v) for k, v in values.items() if k in _FIELDS and v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


class _PrivateRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # Let record_exception report a safe notice and avoid marking an unwritten
        # stack as persisted. Never dump logging's record/arguments on stderr.
        raise OSError("diagnostic log write failed")

    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8")


def _prune_inactive_logs(directory: Path, component: str) -> None:
    # Signal-0 liveness checks are POSIX-specific. Other platforms retain process
    # groups, while still rotating each process file by size.
    if os.name != "posix":
        return
    candidates = []
    for path in directory.glob(f"{component}-*.jsonl"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue  # Another process may have pruned the same file.
    files = [path for _, path in sorted(candidates, reverse=True)]
    for path in files[RETAIN_PROCESS_LOGS - 1:]:
        match = re.fullmatch(re.escape(component) + r"-(\d+)\.jsonl", path.name)
        if not match:
            continue
        try:
            os.kill(int(match[1]), 0)
        except ProcessLookupError:
            for old in [path, *(path.with_name(f"{path.name}.{i}") for i in range(1, BACKUP_COUNT + 1))]:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass  # Retention failure must not prevent a new error log.
        except (PermissionError, OSError):
            pass


def _logger(directory: Path, component: str) -> logging.Logger:
    component = re.sub(r"[^A-Za-z0-9_-]", "_", component)[:80]
    key = (str(directory), component, os.getpid())
    with _lock:
        if key not in _handlers:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _prune_inactive_logs(directory, component)
            logger = logging.Logger(f"diagnostics.{component}.{os.getpid()}", logging.ERROR)
            logger.propagate = False
            handler = _PrivateRotatingHandler(directory / f"{component}-{os.getpid()}.jsonl",
                maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            _handlers[key] = logger
        return _handlers[key]


def safe_exception_message(exc: BaseException) -> str:
    # Framework wrappers often embed str(ValidationError), including input_value,
    # in their own message. Scrub the wrapper as well as its nested cause.
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "ValidationError" and callable(getattr(current, "errors", None)):
            return "Structured output/input validation failed; see validation_errors in diagnostic log"
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return redact(str(exc))


def _exception_data(exc: BaseException, seen: set[int] | None = None) -> dict:
    seen = set() if seen is None else seen
    if id(exc) in seen or len(seen) >= 16:
        return {"type": type(exc).__name__, "truncated": True}
    seen.add(id(exc))
    data = {"type": type(exc).__name__, "message": safe_exception_message(exc), "stack": [
        {"file": redact(frame.filename), "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(exc.__traceback__, limit=80)
    ]}
    if type(exc).__name__ == "ValidationError" and callable(getattr(exc, "errors", None)):
        # str(ValidationError) embeds input values; never persist it.
        data["message"] = "Structured output/input validation failed"
        data["validation_errors"] = [{"field": [redact(str(part)) for part in error["loc"]], "type": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)[:50]]
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)):
        data["exceptions"] = [_exception_data(item, seen) for item in nested[:16]]
    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None:
        data["cause"] = _exception_data(cause, seen)
    return data


def _error_reference(exc: BaseException) -> dict | None:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        ref = getattr(exc, _ERROR_ATTRIBUTE, None)
        if isinstance(ref, dict) and re.fullmatch(r"err_[0-9a-f]{32}", str(ref.get("error_id", ""))):
            return ref
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return None


def link_remote_error(exc: BaseException, metadata: object) -> None:
    """Link an MCP failure to its originating log without trusting message text."""
    if isinstance(metadata, dict) and re.fullmatch(r"err_[0-9a-f]{32}", str(metadata.get("error_id", ""))):
        setattr(exc, _ERROR_ATTRIBUTE, {"error_id": metadata["error_id"], "persisted": True,
                                      "seen": set(), "remote": True})


def record_exception(component: str, stage: str, exc: BaseException, *, state_root: Path | None = None, **context) -> str | None:
    """Best effort: disk/logging failure must not mask the actual task failure."""
    try:
        with _lock:
            ref = _error_reference(exc) or {"error_id": f"err_{uuid.uuid4().hex}", "persisted": False, "seen": set()}
            fields = {k: redact(str(v)) for k, v in {**_context.get(), **context}.items() if k in _FIELDS and v is not None}
            location = (str(log_directory(state_root)), component, stage, tuple(sorted(fields.items())))
            if location in ref["seen"]:
                return ref["error_id"]
            record = {"time": datetime.now(timezone.utc).isoformat(), "level": "ERROR",
                "component": component, "pid": os.getpid(), "stage": stage,
                "event": "exception_propagated" if ref["persisted"] else "exception",
                "error_id": ref["error_id"], "context": fields,
                "exception": {"type": type(exc).__name__} if ref["persisted"] else _exception_data(exc)}
            _logger(log_directory(state_root), component).error("%s", json.dumps(record, ensure_ascii=False))
            ref["persisted"] = True
            ref["seen"].add(location)
            setattr(exc, _ERROR_ATTRIBUTE, ref)
            return ref["error_id"]
    except Exception:
        try:
            sys.stderr.write("SocialAgent: unable to persist diagnostic log.\n")
        except Exception:
            pass


def logged(component: str, stage: str):
    """Log chained failures at a public Tool boundary; never serialize arguments."""
    def decorate(function):
        signature = inspect.signature(function, eval_str=True)

        def metadata(args, kwargs):
            try:
                ctx = signature.bind(*args, **kwargs).arguments.get("context")
                return {key: getattr(ctx, key) for key in _FIELDS if hasattr(ctx, key)}
            except TypeError:
                return {}

        @functools.wraps(function)
        async def async_wrapper(*args, **kwargs):
            with diagnostic_context(**{**metadata(args, kwargs), "tool": stage}):
                try:
                    return await function(*args, **kwargs)
                except Exception as exc:
                    record_exception(component, stage, exc)
                    raise

        @functools.wraps(function)
        def sync_wrapper(*args, **kwargs):
            with diagnostic_context(**{**metadata(args, kwargs), "tool": stage}):
                try:
                    return function(*args, **kwargs)
                except Exception as exc:
                    record_exception(component, stage, exc)
                    raise

        wrapper = async_wrapper if inspect.iscoroutinefunction(function) else sync_wrapper
        wrapper.__signature__ = signature
        wrapper.__annotations__ = {key: parameter.annotation for key, parameter in signature.parameters.items()}
        wrapper.__annotations__["return"] = signature.return_annotation
        return wrapper
    return decorate


def install_exception_hooks(component: str, state_root: Path | None = None) -> None:
    global _default_root
    _default_root = state_root
    sys.excepthook = lambda kind, exc, tb: record_exception(component, "unhandled.main", exc.with_traceback(tb))
    threading.excepthook = lambda args: record_exception(component, "unhandled.thread",
        args.exc_value.with_traceback(getattr(args, "exc_traceback", args.exc_value.__traceback__)),
        thread=args.thread.name if args.thread else None)


def log_async_exception(loop, context) -> None:
    exc = context.get("exception") or RuntimeError(str(context.get("message", "Async task failed")))
    record_exception("agent", "unhandled.async", exc)
