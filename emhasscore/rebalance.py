"""The rebalancing clock, kept on the SETTLED pack.

Until 2026-09-15 `days_since_full` read the plan-of-record chain: the clock
reset when a plan's past rows showed SOC_opt at or above 99,5 %, whether or
not the virtual pack got there (the clamp margin, a dull afternoon, a Growatt
cut), and it reset the instant the plan touched full, so the surplus penalty
returned at full strength and the planner left 100 % at once: over 208
replayed days 5 to 14 % of the resets had no settled full behind them and the
median stay at full was 1 to 2 h, the lower quartile under an hour on the
taxed lanes. The BMS balances the cells while they sit at the top, so the
clock now counts what the pack actually did:

  - a full charge COUNTS when the settled trajectory (virtual_day's soc_pct,
    the past half) has sat at or above REBALANCE_FULL_LEVEL for at least the
    dwell (REBALANCE_DWELL_H, live knob rebalance_dwell_h), consecutive
    quarters, across midnight if need be;
  - until then the clock keeps counting, so the pull that took the pack up
    holds it there for the dwell before the surplus penalty sends it back
    down;
  - no full on record (a fresh install, a state file lost) counts as OVERDUE,
    not as relaxed: the pack is assumed to need a balance until it has had
    one (2026-09-15).

The state is a small dict the caller persists (the wrapper in
/config/emhass/rebalance.json, the replay walk in its state.json):

  last_full     ISO instant the last qualifying dwell completed, or None
  run_quarters  consecutive settled quarters at or above the level so far
  run_end       ISO end instant of the last quarter counted in the run
  seen          ISO end instant of the newest settled quarter processed

`update` is idempotent over a re-read of the same settled day (it only
consumes quarters after `seen`), and a quarter that is not the immediate
successor of `run_end` starts a new run, so a gap in the settlement cannot
bridge two half-dwells.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .grid import STEP_MIN
from .objective import REBALANCE_DWELL_H, REBALANCE_FULL_LEVEL, REBALANCE_TARGET_DAYS

STATE_KEYS = ("last_full", "run_quarters", "run_end", "seen")


def empty_state() -> dict:
    return {"last_full": None, "run_quarters": 0, "run_end": None, "seen": None}


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def update(state: dict | None, slice_start: str | datetime, soc_pct: list, n_past: int,
           level_pct: float = REBALANCE_FULL_LEVEL * 100.0, dwell_h: float = REBALANCE_DWELL_H,
           step_min: int = STEP_MIN) -> dict:
    """Fold a settled slice into the clock: soc_pct[k] is the pack AFTER step k,
    so quarter k ends at slice_start + (k + 1) steps; only the first `n_past`
    steps are settled, the rest are the plan's own. Returns the new state."""
    st = dict(empty_state(), **{k: v for k, v in (state or {}).items() if k in STATE_KEYS})
    t0 = slice_start if isinstance(slice_start, datetime) else datetime.fromisoformat(str(slice_start))
    step = timedelta(minutes=step_min)
    need = max(1, int(round(float(dwell_h) * 60.0 / step_min)))
    seen, run_end = _parse(st["seen"]), _parse(st["run_end"])
    run = int(st["run_quarters"] or 0)
    last_full = _parse(st["last_full"])
    for k in range(min(int(n_past), len(soc_pct))):
        v = soc_pct[k]
        end = t0 + step * (k + 1)
        if seen is not None and end <= seen:
            continue
        seen = end
        if v is None:
            run, run_end = 0, None
            continue
        if float(v) >= float(level_pct):
            run = run + 1 if (run_end is not None and end - run_end == step) else 1
            run_end = end
            if run >= need and (last_full is None or end > last_full):
                last_full = end
        else:
            run, run_end = 0, None
    return {"last_full": _iso(last_full), "run_quarters": run, "run_end": _iso(run_end), "seen": _iso(seen)}


def days_since(state: dict | None, now: datetime) -> float | None:
    """Days since the last qualifying full, or None when there is none on
    record (which the schedule treats as overdue)."""
    lf = _parse((state or {}).get("last_full"))
    if lf is None:
        return None
    return max(0.0, (now - lf).total_seconds() / 86400.0)


def overdue_days() -> float:
    """What a clock with nothing on record counts as: twice the target, the
    pull at full strength."""
    return 2.0 * REBALANCE_TARGET_DAYS
