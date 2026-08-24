from __future__ import annotations

import json
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .contracts import WatermarkRegion
from .errors import AnalyzerError, ErrorCode


class HighQualityVideoRepairBackend(Protocol):
    name: str

    def repair(
        self,
        *,
        source: Path,
        destination: Path,
        regions: Sequence[WatermarkRegion],
        moving: bool,
        tracked_regions: Sequence[bool] | None = None,
    ) -> None: ...


class CommandVideoRepairBackend:
    """Adapter for a JSON-contract video-inpainting sidecar.

    The configured command receives a JSON request path via ``--request`` and
    must create the exact ``output_path`` named in that request. No shell is
    used, so configuration cannot inject shell operators.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        runner=subprocess.run,
        timeout_seconds: int = 7_200,
        progress_callback: Callable[[int, str], None] | None = None,
        progress_poll_seconds: float = 0.25,
    ) -> None:
        if not command:
            raise ValueError("high-quality repair command cannot be empty")
        self._command = list(command)
        self._runner = runner
        self._timeout_seconds = timeout_seconds
        self._progress_callback = progress_callback
        self._progress_poll_seconds = progress_poll_seconds
        self._last_progress_signature: tuple[int, str] | None = None
        self._last_name = "external-video-inpainting-worker"

    @property
    def name(self) -> str:
        return self._last_name

    def repair(
        self,
        *,
        source: Path,
        destination: Path,
        regions: Sequence[WatermarkRegion],
        moving: bool,
        tracked_regions: Sequence[bool] | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request_path = destination.parent / f".repair-{uuid.uuid4().hex}.json"
        progress_path = destination.parent / f".repair-progress-{uuid.uuid4().hex}.json"
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.2",
                    "input_path": str(source.resolve()),
                    "output_path": str(destination.resolve()),
                    "progress_path": str(progress_path.resolve()),
                    "moving": moving,
                    "regions": [
                        {
                            **item.model_dump(mode="json"),
                            "tracked": (
                                bool(tracked_regions[index])
                                if tracked_regions is not None
                                else moving
                            ),
                        }
                        for index, item in enumerate(regions)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        stop_polling = threading.Event()
        progress_thread = None
        if self._progress_callback is not None:
            progress_thread = threading.Thread(
                target=self._poll_progress,
                args=(progress_path, stop_polling),
                name="video-repair-progress",
                daemon=True,
            )
            progress_thread.start()
        try:
            completed = self._runner(
                [*self._command, "--request", str(request_path)],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        finally:
            stop_polling.set()
            if progress_thread is not None:
                progress_thread.join(timeout=max(1.0, self._progress_poll_seconds * 4))
            self._emit_progress(progress_path)
            request_path.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)
        if completed.returncode != 0 or not destination.is_file():
            destination.unlink(missing_ok=True)
            detail = (
                completed.stderr
                or completed.stdout
                or "high-quality repair worker created no output"
            ).strip()[-1_000:]
            raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, detail)
        try:
            payload = json.loads((completed.stdout or "").strip().splitlines()[-1])
            worker = str(
                payload.get("worker") or "external-video-inpainting-worker"
            ).strip()
            device = str(payload.get("device") or "").strip()
            self._last_name = f"{worker}:{device}" if device else worker
        except (IndexError, TypeError, ValueError):
            self._last_name = "external-video-inpainting-worker"

    def _poll_progress(self, progress_path: Path, stop: threading.Event) -> None:
        while not stop.wait(self._progress_poll_seconds):
            self._emit_progress(progress_path)

    def _emit_progress(self, progress_path: Path) -> None:
        if self._progress_callback is None or not progress_path.is_file():
            return
        try:
            payload = json.loads(progress_path.read_text(encoding="utf-8"))
            value = max(0, min(100, int(payload["percent"])))
            message = str(payload["message"])
            signature = (value, message)
            if signature == self._last_progress_signature:
                return
            self._last_progress_signature = signature
            try:
                self._progress_callback(value, message)
            except Exception:
                return
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return
