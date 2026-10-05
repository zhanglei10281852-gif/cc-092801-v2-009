from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_storage(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_storage(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(slots=True)
class SystemClock:
    def now(self) -> datetime:
        return utc_now()


@dataclass(slots=True)
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, **values: int) -> datetime:
        self.current += timedelta(**values)
        return self.current


_clock_override: Clock | None = None


def current_clock() -> Clock:
    """返回当前生效时钟；测试与演示可通过 set_clock_override 注入固定时钟。"""
    return _clock_override if _clock_override is not None else SystemClock()


def set_clock_override(clock: Clock | None) -> None:
    global _clock_override
    _clock_override = clock
