from __future__ import annotations

import os
import tempfile
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import BinaryIO, Iterator

from .errors import CrawlerError, ErrorCode
from .browser_lock_contract import OPERATION_LOCK_DIRECTORY, operation_key


class ProfileTaskCoordinator:
    """Serialize all work targeting one BitBrowser Profile.

    The in-process lock coordinates Tool backends in the MCP server. The small
    lock file also prevents a standalone Tool GUI and SocialAgent from driving
    the same profile at the same time.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(tempfile.gettempdir()) / OPERATION_LOCK_DIRECTORY
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    @contextmanager
    def hold(
        self,
        api_url: str,
        profile_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> Iterator[None]:
        key = operation_key(api_url,profile_id)
        with self._guard:
            local_lock = self._locks[key]
        if not local_lock.acquire(timeout=max(0.0, timeout_seconds)):
            raise _profile_busy_error()

        stream: BinaryIO | None = None
        try:
            stream = (self.root / f"{key}.lock").open("a+b")
            if os.name != "nt":
                os.chmod(stream.name, 0o600)
            if not _acquire_file_lock(stream, timeout_seconds):
                raise _profile_busy_error()
            try:
                yield
            finally:
                _release_file_lock(stream)
        finally:
            if stream is not None:
                stream.close()
            local_lock.release()


def _profile_busy_error() -> CrawlerError:
    return CrawlerError(
        ErrorCode.SESSION_BUSY,
        "该比特浏览器窗口正在执行另一个任务，请等待当前任务完成后再试。",
        retryable=True,
    )


def _acquire_file_lock(stream: BinaryIO, timeout_seconds: float) -> bool:
    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if stream.tell() == 0 and stream.read(1) == b"":
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if monotonic() >= deadline:
                return False
            sleep(0.05)


def _release_file_lock(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


GLOBAL_PROFILE_TASK_COORDINATOR = ProfileTaskCoordinator()
