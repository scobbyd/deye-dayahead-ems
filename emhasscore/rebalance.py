"""The rebalancing clock: how long ago the pack last had its budget of time at the top.

History. Until 2026-09-15 `days_since_full` read the plan-of-record chain and
reset the instant a plan touched full. From 2026-09-15 the clock counted one
consecutive dwell of the settled SOC at or above 99,5 %. Both read the SOC, and
the SOC is the wrong instrument: pack 3 reports 99,5 % on few days while its
cells sit exactly as high as packs 1/2 (its full-charge voltage is set higher),
and the inverter SOC follows the mean of the three.

The clock since 2026-10-03:
  - TIME AT THE TOP is a settled quarter whose series is at or above the level:
    the bank voltage at or above REBALANCE_TOP_V live (the PACE packs bleed
    their high cells only in the knee), the SOC at or above REBALANCE_FULL_LEVEL
    for a pack known only by its SOC (the replay's virtual pack);
  - a STRETCH counts only once it has lasted REBALANCE_DWELL_H (live knob
    rebalance_dwell_h, 1 h): a half-hour touch of the top does nothing for the
    cells and must not reset anything;
  - the CLOCK is the age of the newest REBALANCE_BUDGET_H hours of counted top
    time, looking back from now across as many stretches as it takes. Summer
    tops of 1 to 4 h a day keep it at a few days; in winter one long night
    hold resets it, or two shorter holds add up. A hold keeps the clock high
    until the whole budget is in, so the pull holds the pack at the top for
    the budget and lets go after it (the hysteresis the site asked for);
  - nothing counted inside REBALANCE_LOOKBACK_D (a fresh install, a lost state
    file) reads as OVERDUE: the pack is assumed to need a balance.

The state is a small dict the caller persists (the wrapper in
/config/emhass/rebalance.json, the replay walk in its state.json):
  runs       [[start, end], ...] the counted stretches inside the lookback, oldest first
  run_start  ISO start of the stretch in progress (counted or not yet), or None
  run_end    ISO end of the last quarter in that stretch, or None
  seen       ISO end of the newest settled quarter processed
`update` is idempotent over a re-read of the same settled day (it only
consumes quarters after `seen`), and a quarter that is not the immediate
successor of `run_end` starts a new stretch, so a gap in the settlement cannot
bridge two half-stretches.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .grid import STEP_MIN
from .objective import REBALANCE_BUDGET_H, REBALANCE_DWELL_H, REBALANCE_LOOKBACK_D, rebalance_clock_days

STATE_KEYS = ("runs", "run_start", "run_end", "seen")


def empty_state() -> dict:
    return {"runs": [], "run_start": None, "run_end": None, "seen": None}


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def load_state(state: dict | None) -> dict:
    """The state with every key present; a state from before 2026-10-03 (the
    single-dwell clock: last_full, run_quarters) carries nothing this clock
    can use and starts empty."""
    st = empty_state()
    st.update({k: v for k, v in (state or {}).items() if k in STATE_KEYS})
    st["runs"] = [list(r) for r in (st["runs"] or [])]
    return st


def update(state: dict | None, slice_start: str | datetime, series: list, n_past: int, level: float,
           dwell_h: float = REBALANCE_DWELL_H, lookback_d: float = REBALANCE_LOOKBACK_D,
           step_min: int = STEP_MIN) -> dict:
    """Fold a settled slice into the clock. series[k] is the quarter ending at
    slice_start + (k + 1) steps (a 15-minute mean: the bank voltage live, the
    SOC % in the replay); only the first `n_past` are settled. A quarter at or
    above `level` is time at the top; None (an unsettled hole) ends a stretch.
    Returns the new state."""
    st = load_state(state)
    t0 = slice_start if isinstance(slice_start, datetime) else datetime.fromisoformat(str(slice_start))
    step = timedelta(minutes=step_min)
    need = timedelta(hours=float(dwell_h))
    seen, run_start, run_end = _parse(st["seen"]), _parse(st["run_start"]), _parse(st["run_end"])
    runs = [[_parse(a), _parse(b)] for a, b in st["runs"]]
    for k in range(min(int(n_past), len(series))):
        end = t0 + step * (k + 1)
        if seen is not None and end <= seen:
            continue
        seen = end
        v = series[k]
        if v is None or float(v) < float(level):
            run_start = run_end = None
            continue
        if run_end is None or end - run_end != step:
            run_start = end - step
        run_end = end
        if run_end - run_start >= need:
            if runs and runs[-1][0] == run_start:
                runs[-1][1] = run_end                  # the stretch in progress grows
            else:
                runs.append([run_start, run_end])
    if seen is not None:
        runs = [r for r in runs if r[1] > seen - timedelta(days=float(lookback_d))]
    return {"runs": [[_iso(a), _iso(b)] for a, b in runs], "run_start": _iso(run_start),
            "run_end": _iso(run_end), "seen": _iso(seen)}


def budget_start(state: dict | None, budget_h: float = REBALANCE_BUDGET_H) -> datetime | None:
    """The instant from which the counted top time up to the newest stretch adds
    up to `budget_h`, walking the stretches back from the newest; None when the
    lookback does not hold that much."""
    left = timedelta(hours=float(budget_h))
    for a, b in reversed(load_state(state)["runs"]):
        a, b = _parse(a), _parse(b)
        if b - a >= left:
            return b - left
        left -= b - a
    return None


def days_since(state: dict | None, now: datetime, budget_h: float = REBALANCE_BUDGET_H) -> float | None:
    """The clock: days since the newest `budget_h` hours of counted top time
    began, or None when the lookback does not hold them (which the schedule
    treats as overdue)."""
    t = budget_start(state, budget_h)
    if t is None:
        return None
    return max(0.0, (now - t).total_seconds() / 86400.0)


def top_hours(state: dict | None) -> float:
    """Counted top time inside the lookback, hours (for the published state)."""
    return round(sum((_parse(b) - _parse(a)).total_seconds() for a, b in load_state(state)["runs"]) / 3600.0, 2)


def overdue_days() -> float:
    """What a clock with nothing on record counts as: the pull at its ceiling."""
    return rebalance_clock_days(None)
