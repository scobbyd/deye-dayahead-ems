"""The ladder rungs over the closed replay: emhasscore.ladder.ladder_run,
unmodified, with the stand-in actual's own virtual days as the "measured"
day (the closed ruling: no meter, no supplier battery).

inputs[day]["day"] carries the frame's load_w / micro_w and, from the
settled slice, pv_w = pv_peak_w = p_pv_w with curtailed all zero, soc_pct,
grid_w (= p_grid_w) and batt_dc_w (= p_batt_w moved to the DC side by the
inverse of replay_day's conversion). The rungs' sun is the stand-in's own
available PV (p_pv_w: the repaired potential on suspect steps, measured
elsewhere), so pv_potential returns it unchanged and effective_load = load +
losses exactly: the actual rung's replay is the identity. win1 / win2 are
the same over D..D+1 and D..D+2.

Residual: on steps the stand-in declined PV, the rungs see the declined
amount as a load-and-PV pair, so a rung can curtail it and import at a
negative price; ceilings are optimistic by that amount on those steps, the
actual rung is unaffected.

A seed row for the day before the first scored day carries every rung's
soc_end_pct = the stand-in's settled end, so no rung resets on day one.
After the run every row gets "rec" in flags, and ladder_rec_supplier.csv adds
supplier_eur (-sum(net_result_eur), ladder sign: negative = earnings; empty
when the frame has no net_result_eur column) and supplier_da_eur (the
frame's measured grid_w priced with the stand-in's buy / sell).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

import pandas as pd

from backtest import hardware as hwm
from backtest import pricefill as pf
from backtest import replay
from backtest import replay_inputs as ri
from emhasscore.ladder import LADDER_COLUMNS, RUNGS, ladder_run, read_ladder, upsert_ladder
from emhasscore.objective import ETA_BRIDGE
from emhasscore.scoring import cash_in_frame

BASE_URL = "http://library"


def batt_dc_from_ac(p_batt_w, eta: float = ETA_BRIDGE) -> list[float]:
    """replay_day reads b_ac = b_dc x eta when discharging (b_dc >= 0) and
    b_dc / eta when charging; this is its inverse."""
    return [round(float(b) / eta, 1) if float(b) >= 0 else round(float(b) * eta, 1) for b in p_batt_w]


def _day_slice(slices: pd.DataFrame, day: date) -> pd.DataFrame | None:
    d = slices[slices["date"] == day.isoformat()].sort_values("ts_utc")
    return d if len(d) else None


def _window(slices: pd.DataFrame, df: pd.DataFrame, day: date, days: int, tz: str) -> dict | None:
    """The wrapper's _window_actuals shape over `days` local days from `day`,
    the rungs' sun on the stand-in's own basis: pv_w = pv_peak_w = the
    settled slice's p_pv_w, curtailed all zero (see module docstring)."""
    keys = {"pv_w": [], "load_w": [], "micro_w": [], "curtailed": [], "pv_peak_w": [], "soc_pct": [],
            "grid_w": [], "batt_dc_w": []}
    for k in range(days):
        d = day + timedelta(days=k)
        sl = _day_slice(slices, d)
        if sl is None:
            return None
        act = ri.actuals(df, d, tz)
        for key in ("load_w", "micro_w"):
            keys[key] += act[key]
        pv = [round(float(v), 1) for v in sl["p_pv_w"]]
        keys["pv_w"] += pv
        keys["pv_peak_w"] += pv
        keys["curtailed"] += [0.0] * len(pv)
        keys["soc_pct"] += [float(v) for v in sl["soc_pct"]]
        keys["grid_w"] += [round(float(v), 1) for v in sl["p_grid_w"]]
        keys["batt_dc_w"] += batt_dc_from_ac(sl["p_batt_w"].tolist())
    return keys


def day_inputs(slices: pd.DataFrame, df: pd.DataFrame, day: date, tz: str = ri.TZ) -> dict:
    return {"day": _window(slices, df, day, 1, tz), "win1": _window(slices, df, day, 2, tz),
            "win2": _window(slices, df, day, 3, tz)}


def seed_row(slices: pd.DataFrame, day_before: date) -> dict:
    sl = _day_slice(slices, day_before)
    if sl is None:
        raise ValueError(f"no settled slice for {day_before}")
    end = float(sl["soc_now_pct"].iloc[0])
    row = {k: None for k in LADDER_COLUMNS}
    row.update(date=day_before.isoformat(), soc_start_pct=float(sl["soc_start_pct"].iloc[0]), flags="rec;seed")
    for r in RUNGS:
        row[f"{r}_status"], row[f"{r}_soc_end_pct"], row[f"{r}_solves"] = "ok", end, 0
    return row


def np_rows_for(days: list[str], da: dict, tz: str) -> list:
    ds = sorted(date.fromisoformat(d) for d in days)
    rows = []
    d = ds[0] - timedelta(days=1)
    while d <= ds[-1] + timedelta(days=3):
        rows += pf.nordpool_rows(d, da, tz)
        d += timedelta(days=1)
    return rows


def merge_flags(csv_path: str, before: dict[str, str]) -> int:
    """Union each date's flags in `before` into the row currently on disk,
    idempotently: a flag already present is not re-added, and a date with
    nothing in `before` is left untouched. Returns the number of rows changed.

    The one flag-merging code path, used both to restore what ladder_day
    wiped on a chained (already-ok) day and to add "rec" everywhere."""
    n = 0
    for r in read_ladder(csv_path):
        cur = [f for f in (r.get("flags") or "").split(";") if f]
        add = [f for f in (before.get(r["date"]) or "").split(";") if f]
        merged = cur + [f for f in add if f not in cur]
        if merged != cur:
            r["flags"] = ";".join(merged)
            upsert_ladder(csv_path, r)
            n += 1
    return n


def add_rec_flag(csv_path: str) -> int:
    dates = [r["date"] for r in read_ladder(csv_path)]
    return merge_flags(csv_path, {d: "rec" for d in dates})


def supplier_join(csv_path: str, slices: pd.DataFrame, df: pd.DataFrame, out_path: str, tz: str = ri.TZ) -> pd.DataFrame:
    lad = pd.read_csv(csv_path, dtype={"flags": str})
    has_net = "net_result_eur" in df.columns
    b_eur, b_da = [], []
    for d_iso, flags in zip(lad["date"], lad["flags"].fillna("")):
        # The seed row carries no plan of its own: it exists only to chain
        # every rung's SOC into day one, and pricing it against the supplier
        # would score a day the ladder never walked.
        if "seed" in flags.split(";"):
            b_eur.append(None)
            b_da.append(None)
            continue
        d = date.fromisoformat(d_iso)
        a, b = ri.day_bounds(d, tz)
        fr = df.loc[(df.index >= a) & (df.index < b)]
        sl = _day_slice(slices, d)
        b_eur.append(-float(fr["net_result_eur"].sum()) if has_net and len(fr) else None)
        if sl is not None and len(fr) == len(sl):
            b_da.append(round(cash_in_frame(fr["grid_w"].astype(float).tolist(), sl["buy"].tolist(), sl["sell"].tolist()), 4))
        else:
            b_da.append(None)
    lad["supplier_eur"], lad["supplier_da_eur"] = b_eur, b_da
    lad.to_csv(out_path, index=False)
    return lad


def run(run_dir: str, first: date, last: date, solver=None, df: pd.DataFrame | None = None, tz: str = ri.TZ) -> dict:
    from backtest.solver import LibrarySolver, patch_solve
    from emhasscore import ladder as core_ladder
    slices = replay.load_slices(run_dir)
    meta_path = os.path.join(run_dir, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    df = df if df is not None else ri.load_frame(ri.frame_path(meta.get("frame")))   # the walk's own frame file
    df = ri.scale_pv(df, float(meta.get("pv_scale", 1.0)))        # the rungs meter the walk's plant
    hw = hwm.normalize(meta.get("hardware"))                        # and run the walk's pack and inverter
    hwm.apply(hw)
    csv_path = os.path.join(run_dir, "ladder_rec.csv")
    days = [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]
    inputs = {d: day_inputs(slices, df, date.fromisoformat(d), tz) for d in days}
    if not read_ladder(csv_path):
        upsert_ladder(csv_path, seed_row(slices, first - timedelta(days=1)))
    # ladder_day rewrites flags="" for a day whose rungs are all already ok
    # (core ladder.py:411,417,471), which would otherwise drop *_caps /
    # *_reset / actual_seam history on every resume. Snapshot what is on
    # disk now and merge it back after the core run touches the file.
    before = {r["date"]: r.get("flags") or "" for r in read_ladder(csv_path)}
    solver = solver or LibrarySolver(data_dir=os.path.join(run_dir, "emhass_data"))
    hwm.patch_solver(solver, hw)
    with patch_solve(core_ladder._run_rung, solver):
        out = ladder_run(os.path.join(run_dir, "plans"), csv_path, BASE_URL, days, tz,
                         np_rows_for(days, pf.load_da(), tz), inputs, capacity_kwh=hw["capacity_kwh"])
    merge_flags(csv_path, before)
    added = add_rec_flag(csv_path)
    supplier_join(csv_path, slices, df, os.path.join(run_dir, "ladder_rec_supplier.csv"), tz)
    return {"days": len(days), "solves": out["solves"], "rec_added": added, "summary": out["summary"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ladder rungs over a closed replay run")
    ap.add_argument("--mode", required=True, choices=pf.MODES)
    ap.add_argument("--first", default="2026-08-01")
    ap.add_argument("--last", default="2026-09-02")
    ap.add_argument("--run-dir", default=None)
    a = ap.parse_args(argv)
    out = run(a.run_dir or os.path.join(ri.REPLAY_DIR, a.mode), date.fromisoformat(a.first), date.fromisoformat(a.last))
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
