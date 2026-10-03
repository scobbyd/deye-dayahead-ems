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
    file) reads as OVERDUE: the pack is assumed to need a balance;
  - THE PULL LATCHES (2026-10-03, `latch`): once the clock reaches
    REBALANCE_PULL_DAY (or reads overdue) it stays at least REBALANCE_PULL_DAY
    plus the days since it latched, so the end target, the surplus-off and the
    pull hold and the pull keeps stepping up, until REBALANCE_RELEASE_H of
    counted top time fall inside the last REBALANCE_RELEASE_WINDOW_H. The
    release is a balance: from then on the clock reads at most the time since
    it, so older stretches cannot drag it straight back past day 14.

The state is a small dict the caller persists (the wrapper in
/config/emhass/rebalance.json, the replay walk in its state.json):
  runs       [[start, end], ...] the counted stretches inside the lookback, oldest first
  run_start  ISO start of the stretch in progress (counted or not yet), or None
  run_end    ISO end of the last quarter in that stretch, or None
  seen       ISO end of the newest settled quarter processed
  latch_since  ISO instant the pull latched, or None
  released     ISO instant the last latch released (a balance), or None
`update` is idempotent over a re-read of the same settled day (it only
consumes quarters after `seen`), and a quarter that is not the immediate
successor of `run_end` starts a new stretch, so a gap in the settlement cannot
bridge two half-stretches.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .grid import STEP_MIN
from .objective import (REBALANCE_BUDGET_H, REBALANCE_DWELL_H, REBALANCE_LOOKBACK_D, REBALANCE_PULL_DAY,
                        REBALANCE_RELEASE_H, REBALANCE_RELEASE_WINDOW_H, REBALANCE_SOC_FINAL_DAY,
                        REBALANCE_SURPLUS_OFF_DAY, rebalance_clock_days)

STATE_KEYS = ("runs", "run_start", "run_end", "seen", "latch_since", "released")


def empty_state() -> dict:
    return {"runs": [], "run_start": None, "run_end": None, "seen": None, "latch_since": None, "released": None}


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
            "run_end": _iso(run_end), "seen": _iso(seen), "latch_since": st["latch_since"], "released": st["released"]}


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


def _days(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 86400.0)


def raw_days(state: dict | None, now: datetime, budget_h: float = REBALANCE_BUDGET_H) -> float | None:
    """The budget clock before the latch: days since the newest `budget_h` hours
    of counted top time began, at most the days since the last release; None
    when neither exists."""
    t = budget_start(state, budget_h)
    rel = _parse(load_state(state)["released"])
    ds = [_days(x, now) for x in (t, rel) if x is not None]
    return min(ds) if ds else None


def days_since(state: dict | None, now: datetime, budget_h: float = REBALANCE_BUDGET_H) -> float | None:
    """The clock the schedule reads: the raw clock, held at REBALANCE_PULL_DAY
    plus the latch's age while the pull is latched; None (overdue) when there
    is nothing on record and no latch."""
    d = raw_days(state, now, budget_h)
    since = _parse(load_state(state)["latch_since"])
    if since is None:
        return d
    held = REBALANCE_PULL_DAY + _days(since, now)
    return held if d is None else max(d, held)


def hours_within(state: dict | None, now: datetime, window_h: float = REBALANCE_RELEASE_WINDOW_H) -> float:
    """Counted top time inside the last `window_h` hours, hours."""
    a0 = now - timedelta(hours=float(window_h))
    tot = 0.0
    for a, b in load_state(state)["runs"]:
        a, b = max(_parse(a), a0), min(_parse(b), now)
        if b > a:
            tot += (b - a).total_seconds()
    return round(tot / 3600.0, 2)


def latch(state: dict | None, now: datetime, release_h: float = REBALANCE_RELEASE_H,
          window_h: float = REBALANCE_RELEASE_WINDOW_H, budget_h: float = REBALANCE_BUDGET_H) -> dict:
    """One latch step at `now`: a latched pull releases once `release_h` of
    counted top time lie inside the last `window_h` (the release is a balance);
    an unlatched clock latches at REBALANCE_PULL_DAY or when overdue."""
    st = load_state(state)
    if st["latch_since"] is not None:
        if hours_within(st, now, window_h) >= float(release_h):
            st["latch_since"], st["released"] = None, _iso(now)
    else:
        d = raw_days(st, now, budget_h)
        if d is None or d >= REBALANCE_PULL_DAY:
            st["latch_since"] = _iso(now)
    return st


def top_hours(state: dict | None) -> float:
    """Counted top time inside the lookback, hours (for the published state)."""
    return round(sum((_parse(b) - _parse(a)).total_seconds() for a, b in load_state(state)["runs"]) / 3600.0, 2)


def overdue_days() -> float:
    """What a clock with nothing on record counts as: the pull at its ceiling."""
    return rebalance_clock_days(None)


def published(state: dict | None, now: datetime, budget_h: float = REBALANCE_BUDGET_H) -> dict:
    """The two numbers the dashboard plots (2026-10-03): `top_hours` is the
    counted top time inside the lookback ("balanced, 30 d"); `clock_days` is
    the clock, or the lookback when the budget is not in it (overdue: the
    graph keeps a value instead of a hole); `phase` names the schedule stage
    the clock is in."""
    d = days_since(state, now, budget_h)
    clock = rebalance_clock_days(d)
    if d is None:
        phase = "overdue"
    elif clock >= REBALANCE_PULL_DAY:
        phase = "pulling"
    elif clock >= REBALANCE_SOC_FINAL_DAY:
        phase = "end target"
    elif clock >= REBALANCE_SURPLUS_OFF_DAY:
        phase = "surplus off"
    else:
        phase = "fresh"
    st = load_state(state)
    t = budget_start(state, budget_h)
    return {"top_hours": top_hours(state), "clock_days": round(d, 2) if d is not None else REBALANCE_LOOKBACK_D,
            "phase": phase, "budget_start": _iso(t), "stretches": len(st["runs"]),
            "last_stretch_end": st["runs"][-1][1] if st["runs"] else None,
            "latch_since": st["latch_since"], "released": st["released"],
            "release_hours": hours_within(state, now)}
