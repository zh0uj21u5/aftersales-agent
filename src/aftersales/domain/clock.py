"""Injected, timezone-aware deterministic domain time."""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

FIXED_NOW = datetime.fromisoformat("2026-11-15T12:00:00+08:00")


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FixedClock:
    instant: datetime = FIXED_NOW

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None or self.instant.utcoffset() is None:
            raise ValueError("Clock must be timezone-aware")

    def now(self) -> datetime:
        return self.instant


class WallClock:
    def now(self) -> datetime:
        return datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8)))
