"""Timezone-aware bucket periods; period identity is fixed at admission."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class PeriodWindow:
    key: str
    start: datetime | None
    end: datetime | None


def period_window(
    period: Literal["call", "run", "day", "month"], timezone: str, now: datetime
) -> PeriodWindow:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("period_instant_must_be_timezone_aware")
    if period in {"call", "run"}:
        return PeriodWindow(period, None, None)
    if period not in {"day", "month"}:
        raise ValueError("period_invalid")
    zone = ZoneInfo(timezone)
    day = now.astimezone(zone).date()
    if period == "day":
        start = datetime.combine(day, time.min, zone)
        end = datetime.combine(day + timedelta(days=1), time.min, zone)
    else:
        start = datetime(day.year, day.month, 1, tzinfo=zone)
        end = (
            datetime(day.year + 1, 1, 1, tzinfo=zone)
            if day.month == 12
            else datetime(day.year, day.month + 1, 1, tzinfo=zone)
        )
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    return PeriodWindow(f"{period}:{start.isoformat()}/{end.isoformat()}", start, end)
