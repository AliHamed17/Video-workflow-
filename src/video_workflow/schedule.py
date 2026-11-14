"""Posting slots: every finished video gets the next free slot, and all platforms go out together.

``publish.schedule.slots`` are local times (``timezone``); at most ``max_per_day`` videos per day.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import ScheduleConfig


def _parse(slot: str) -> time:
    hours, minutes = slot.strip().split(":")
    return time(int(hours), int(minutes))


def next_slot(config: ScheduleConfig, taken: Iterable[datetime], now: datetime | None = None,
              horizon_days: int = 60) -> datetime | None:
    """The earliest free slot after ``now`` (UTC), or None when no slots are configured."""
    if not config.slots:
        return None
    tz = ZoneInfo(config.timezone)
    now = (now or datetime.now(UTC)).astimezone(tz)
    taken_local = [t.astimezone(tz) for t in taken]
    per_day: dict[date, int] = {}
    for t in taken_local:
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
    taken_set = {t.replace(second=0, microsecond=0) for t in taken_local}
    slots = sorted(_parse(s) for s in config.slots)
    for day_offset in range(horizon_days):
        day = now.date() + timedelta(days=day_offset)
        if per_day.get(day, 0) >= config.max_per_day:
            continue
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=tz)
            if candidate > now and candidate not in taken_set:
                return candidate.astimezone(UTC)
    return None


def is_due(scheduled_for: datetime | None, now: datetime | None = None) -> bool:
    return scheduled_for is None or (now or datetime.now(UTC)) >= scheduled_for
