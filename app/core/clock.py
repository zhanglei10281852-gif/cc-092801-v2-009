from __future__ import annotations

import os
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


class ClockRegistry:
    """进程级时钟来源。

    生产环境使用系统时钟；演练/测试环境可以通过 ``TOWNSHIP_FIXED_NOW`` 环境变量
    固定时间，或直接注入 :class:`FrozenClock`，便于在固定时钟下通过 API 复现
    维护窗口跨重启的阶段恢复。
    """

    def __init__(self) -> None:
        self._override: Clock | None = None

    def get(self) -> Clock:
        if self._override is not None:
            return self._override
        raw = os.getenv("TOWNSHIP_FIXED_NOW", "").strip()
        if raw:
            return FrozenClock(datetime.fromisoformat(raw))
        return SystemClock()

    def override(self, clock: Clock | None) -> None:
        self._override = clock

    def reset(self) -> None:
        self._override = None


clock_registry = ClockRegistry()


def domain_clock(clock: Clock | None) -> Clock:
    return clock or clock_registry.get()
