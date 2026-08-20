from __future__ import annotations

import asyncio
from collections import defaultdict
from time import monotonic

from .contracts import AuditEvent


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class LocalRateLimiter:
    """Single-process MVP limiter; replace with a Valkey-backed port in production."""

    def __init__(self, minimum_interval_seconds: float = 1.0) -> None:
        self._minimum_interval = minimum_interval_seconds
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_call: dict[str, float] = {}

    async def acquire(self, bucket: str, tenant_id: str) -> None:
        key = f"{tenant_id}:{bucket}"
        async with self._locks[key]:
            remaining = self._minimum_interval - (monotonic() - self._last_call.get(key, 0.0))
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_call[key] = monotonic()

