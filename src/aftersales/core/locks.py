"""Process-local session exclusion without serializing independent sessions."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol


class SessionBusy(Exception):
    pass


class SessionLock(Protocol):
    def acquire(
        self,
        session_id: str,
        timeout: float = 0,
    ) -> AbstractAsyncContextManager[None]: ...


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class InMemorySessionLock:
    """Only turns and other mutations of the same session contend."""

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def acquire(self, session_id: str, timeout: float = 0) -> AsyncIterator[None]:
        entry = self._entries.setdefault(session_id, _LockEntry())
        entry.users += 1
        try:
            if timeout <= 0 and entry.lock.locked():
                raise SessionBusy(session_id)
            try:
                async with asyncio.timeout(max(0, timeout)):
                    await entry.lock.acquire()
            except TimeoutError as exc:
                raise SessionBusy(session_id) from exc
            try:
                yield
            finally:
                entry.lock.release()
        finally:
            entry.users -= 1
            if not entry.users:
                self._entries.pop(session_id)
