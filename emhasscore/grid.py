"""The 15-minute plan grid: step arithmetic through UTC so DST days come out as 92 or 100 steps."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


STEP_MIN = 15


STEP_H = STEP_MIN / 60.0


def _parse_ts(s: str) -> datetime:
    """ISO 8601 with offset or trailing Z (EMHASS writes '...:00.000Z')."""
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def ceil_step(dt: datetime, step_min: int = STEP_MIN) -> datetime:
    """Round up to the next step boundary; a value already on the boundary stays.
    Matches EMHASS method_ts_round 'last' (pandas ceil)."""
    dt = dt.replace(microsecond=0)
    rem = (dt.minute % step_min) * 60 + dt.second
    return dt if rem == 0 else dt + timedelta(seconds=step_min * 60 - rem)


def expected_steps(day: date, tz: str) -> int:
    z = ZoneInfo(tz)
    a = datetime.combine(day, time(0), tzinfo=z).astimezone(timezone.utc)
    b = datetime.combine(day + timedelta(days=1), time(0), tzinfo=z).astimezone(timezone.utc)
    return round((b - a).total_seconds() / (STEP_MIN * 60))


def horizon(now: datetime, tz: str, days_ahead: int = 1) -> tuple[datetime, int]:
    """t0 = ceil(now, 15 min) in tz; n = steps from t0 to 24:00 of now's local
    date + days_ahead (default tomorrow; 2 = the day after, fed by epex
    predicted prices and Solcast day-3 PV). t0 never depends on days_ahead."""
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    t0 = ceil_step(local)
    end = datetime.combine(local.date() + timedelta(days=1 + days_ahead), time(0), tzinfo=z)
    n = round((end.astimezone(timezone.utc) - t0.astimezone(timezone.utc)).total_seconds()
              / (STEP_MIN * 60))
    return t0, n


def step_times(t0: datetime, n: int) -> list[datetime]:
    z = t0.tzinfo
    base = t0.astimezone(timezone.utc)
    return [(base + timedelta(minutes=STEP_MIN * k)).astimezone(z) for k in range(n)]


def local_midnight(day: date, tz: str) -> datetime:
    """00:00 local of `day`, tz-aware."""
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(tz))


def step_index(t: datetime, t0: datetime) -> int:
    """Steps from t0 to t on the UTC grid (negative before t0). Through UTC, so
    the DST fold does not turn a 15-minute gap into a wall-clock hour."""
    return round((t.astimezone(timezone.utc) - t0.astimezone(timezone.utc)).total_seconds() / (STEP_MIN * 60))


def stamp_z(t: datetime) -> str:
    """A row timestamp the way EMHASS writes them: UTC with a trailing '.000Z'."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _slot(dt: datetime) -> int:
    """Wall-clock 15-minute slot of the local day, 0..95."""
    return (dt.hour * 60 + dt.minute) // STEP_MIN
