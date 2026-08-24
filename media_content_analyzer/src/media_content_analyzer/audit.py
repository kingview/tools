from __future__ import annotations

import threading
from pathlib import Path

from .contracts import AuditEvent


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonLinesAuditSink:
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    async def record(self, event: AuditEvent) -> None:
        line = event.model_dump_json() + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
