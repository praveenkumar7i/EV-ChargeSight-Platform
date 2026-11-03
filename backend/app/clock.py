"""Time helpers.

Two clocks exist:
* the **simulation clock** (SimState.sim_time) used by simulated chargers and
  their tickets, so ages and MTTA/MTTR are consistent with simulated events;
* the **wall clock** (UTC now) used for real/imported data.

All datetimes are naive UTC internally; the API serialises them with a 'Z'.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 08:00 IST on 23 Oct 2026 expressed as naive UTC.
SIM_EPOCH = datetime(2026, 10, 23, 2, 30, 0)
IST = timezone(timedelta(hours=5, minutes=30))


def wall_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return to_naive_utc(dt).isoformat(timespec="seconds") + "Z"


def minutes_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0
