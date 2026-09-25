"""One row per lane per day over a replay run: the energy, the money, the
pack and the controller, next to a supplier's same day when one is given. The
sidecar the dashboard page and the report read; the live ladder file keeps
its own schema untouched.

Every row carries `lane` (the run label: foresight, foresight_omni2, ...) and
`source` ("rec" for a replay walk; a later live extractor writes "live" rows
in the same shape), so one file spans the archive seam and a chart switches
lanes on a column. Derived entirely from the run's slices.parquet, its
ladder_rec_supplier.csv, walk.log and, optionally, a supplier's daily table;
nothing is decided at walk time.

Money (EUR, ladder sign: negative = earned):
  cash_eur / loss_eur / eur     the actual rung's day from the ladder
  nobatt_cash_eur               the same house with no battery: load - available PV at the day's prices
  batt_gain_eur                 nobatt_cash - cash: what the pack earned that day
  charge_cost_eur               energy the pack absorbed, valued where it came from (grid at buy, sun at sell)
  discharge_value_eur           energy the pack released, valued where it went (load at buy, export at sell)
  charge_cost_per_kwh / discharge_value_per_kwh / captured_spread (their difference), AC side
  supplier_net_eur (ladder sign) and supplier_da_eur for the same day (empty without a supplier table)
  rung_*_eur                    the ladder's open-loop rungs that day, for the graphs

    python -m backtest.metrics --run-dir <dir> --lane foresight [--first .. --last ..] [--supplier-daily CSV]
Appends or replaces the lane's rows in runs/replay/replay_metrics.csv and
writes <run-dir>/metrics.csv. --supplier-daily is a CSV with columns day,
net_eur (the supplier's settlement, money in) and optionally da_eur (the
same day's flows at day-ahead, ladder sign).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd

from backtest import replay
from backtest import replay_inputs as ri
from emhasscore.grid import STEP_H
from emhasscore.ladder import RUNGS
from emhasscore.objective import CAPACITY_KWH, step_cost

ALL_CSV = os.path.join(ri.REPLAY_DIR, "replay_metrics.csv")
COLUMNS = ["lane", "source", "date",
           "cash_eur", "loss_eur", "eur", "nobatt_cash_eur", "batt_gain_eur",
           "charge_cost_eur", "discharge_value_eur", "charge_cost_per_kwh", "discharge_value_per_kwh",
           "captured_spread", "supplier_net_eur", "supplier_da_eur",
           "batt_in_kwh", "batt_out_kwh", "cycles", "grid_import_kwh", "grid_export_kwh",
           "grid_to_batt_kwh", "pv_kwh", "pv_curtailed_kwh", "load_kwh",
           "soc_start_pct", "soc_end_pct", "soc_min_pct", "soc_max_pct", "h_above_90", "h_below_15",
           "clamp_writes", "solves_ok", "solves_failed",
           "buy_mean", "sell_mean", "buy_spread", "status"] + [f"rung_{r}_eur" for r in RUNGS if r != "actual"]

_LOG = re.compile(r"^\S+ (\S+) .*ok=(True|False)")


def solves_by_day(run_dir: str, tz: str = ri.TZ) -> dict[str, tuple[int, int]]:
    """{local date: (ok, failed)} from walk.log; a tick's day is its local day.
    A resumed walk retries a failed tick (it was never archived) and logs it
    again, so each tick counts once, with its last outcome."""
    ticks: dict[str, bool] = {}
    path = os.path.join(run_dir, "walk.log")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        for line in f:
            m = _LOG.match(line)
            if m:
                ticks[m.group(1)] = m.group(2) == "True"
    out: dict[str, list[int]] = {}
    for t, ok in ticks.items():
        d = pd.Timestamp(t).tz_convert(ZoneInfo(tz)).date().isoformat()
        out.setdefault(d, [0, 0])[0 if ok else 1] += 1
    return {d: (c[0], c[1]) for d, c in out.items()}


def day_metrics(sl: pd.DataFrame, lad: dict | None, bal: dict | None, solves: tuple[int, int] | None,
                lane: str, source: str = "rec", capacity_kwh: float = CAPACITY_KWH) -> dict:
    """One day. `sl` is the day's settled quarters (oldest first), `lad` its
    ladder row (ladder_rec_supplier.csv), `bal` the supplier's row for the day
    ({"supplier_net_eur": money in, "da_eur": ladder sign}) or None."""
    kwh = lambda w: round(float(sum(w)) * STEP_H / 1000.0, 3)
    b = sl["p_batt_w"].astype(float).to_numpy()          # + = discharge, AC side
    g = sl["p_grid_w"].astype(float).to_numpy()          # + = import
    pv = sl["p_pv_w"].astype(float).to_numpy()           # available sun
    cut = sl["pv_curtail_w"].fillna(0.0).astype(float).to_numpy()
    load = sl["p_load_w"].astype(float).to_numpy()
    buy = sl["buy"].astype(float).to_numpy()
    sell = sl["sell"].astype(float).to_numpy()
    soc = sl["soc_pct"].astype(float).to_numpy()
    imp, exp = g.clip(min=0), (-g).clip(min=0)
    ch, dis = (-b).clip(min=0), b.clip(min=0)
    # charging: what came from the grid is the overlap of import and charge, the rest is sun
    ch_grid = [min(c, i) for c, i in zip(ch, imp)]
    ch_pv = ch - ch_grid
    charge_cost = sum(cg * bp + cp * sp for cg, cp, bp, sp in zip(ch_grid, ch_pv, buy, sell)) * STEP_H / 1000.0
    # discharging: what left through the meter is the overlap of export and discharge, the rest fed the load
    dis_exp = [min(d, e) for d, e in zip(dis, exp)]
    dis_load = dis - dis_exp
    discharge_value = sum(de * sp + dl * bp for de, dl, bp, sp in zip(dis_exp, dis_load, buy, sell)) * STEP_H / 1000.0
    nobatt = sum(step_cost(float(l - p), bp, sp) for l, p, bp, sp in zip(load, pv, buy, sell))
    cash = float(lad["actual_cash"]) if lad and lad.get("actual_cash") is not None else None
    batt_in, batt_out = kwh(ch), kwh(dis)
    row = {"lane": lane, "source": source, "date": str(sl["date"].iloc[0]),
           "cash_eur": cash, "loss_eur": lad.get("actual_loss") if lad else None,
           "eur": lad.get("actual_eur") if lad else None,
           "nobatt_cash_eur": round(nobatt, 4),
           "batt_gain_eur": round(nobatt - cash, 4) if cash is not None else None,
           "charge_cost_eur": round(charge_cost, 4), "discharge_value_eur": round(discharge_value, 4),
           "charge_cost_per_kwh": round(charge_cost / batt_in, 4) if batt_in else None,
           "discharge_value_per_kwh": round(discharge_value / batt_out, 4) if batt_out else None,
           "captured_spread": (round(discharge_value / batt_out - charge_cost / batt_in, 4)
                               if batt_in and batt_out else None),
           "supplier_net_eur": -float(bal["supplier_net_eur"]) if bal else None,
           "supplier_da_eur": (float(bal["da_eur"]) if bal and bal.get("da_eur") is not None else None),
           "batt_in_kwh": batt_in, "batt_out_kwh": batt_out, "cycles": round(batt_out / capacity_kwh, 3),
           "grid_import_kwh": kwh(imp), "grid_export_kwh": kwh(exp), "grid_to_batt_kwh": kwh(ch_grid),
           "pv_kwh": kwh(pv - cut), "pv_curtailed_kwh": kwh(cut), "load_kwh": kwh(load),
           "soc_start_pct": round(float(sl["soc_start_pct"].iloc[0]), 2), "soc_end_pct": round(float(soc[-1]), 2),
           "soc_min_pct": round(float(soc.min()), 2), "soc_max_pct": round(float(soc.max()), 2),
           "h_above_90": round(float((soc > 90).sum()) * STEP_H, 2),
           "h_below_15": round(float((soc < 15).sum()) * STEP_H, 2),
           "clamp_writes": int(sl["clamp_writes"].iloc[0]) if "clamp_writes" in sl else None,
           "solves_ok": solves[0] if solves else None, "solves_failed": solves[1] if solves else None,
           "buy_mean": round(float(buy.mean()), 5), "sell_mean": round(float(sell.mean()), 5),
           "buy_spread": round(float(buy.max() - buy.min()), 5),
           "status": lad.get("actual_status") if lad else "no_ladder"}
    for r in RUNGS:
        if r != "actual":
            row[f"rung_{r}_eur"] = lad.get(f"{r}_eur") if lad else None
    return row


def supplier_rows(path: str | None) -> dict:
    """{ISO day: {"supplier_net_eur", "da_eur"}} from a supplier's daily CSV
    (columns day, net_eur[, da_eur]); empty without a file."""
    if not path or not os.path.exists(path):
        return {}
    t = pd.read_csv(path)
    day_col = "day" if "day" in t.columns else "date"
    out = {}
    for r in t.to_dict("records"):
        if r.get("net_eur") is None or pd.isna(r.get("net_eur")):
            continue
        da = r.get("da_eur")
        out[str(r[day_col])] = {"supplier_net_eur": float(r["net_eur"]),
                                "da_eur": None if da is None or pd.isna(da) else float(da)}
    return out


def run(run_dir: str, lane: str, first: date | None = None, last: date | None = None,
        all_csv: str = ALL_CSV, tz: str = ri.TZ, supplier: dict | None = None) -> pd.DataFrame:
    slices = replay.load_slices(run_dir)
    lad_path = os.path.join(run_dir, "ladder_rec_supplier.csv")
    if not os.path.exists(lad_path):
        lad_path = os.path.join(run_dir, "ladder_rec.csv")
    lad = pd.read_csv(lad_path, dtype={"flags": str}) if os.path.exists(lad_path) else pd.DataFrame()
    lad_by = {r["date"]: r for r in lad.to_dict("records") if "seed" not in str(r.get("flags") or "")}
    days = sorted(d for d in slices["date"].unique() if d in lad_by)
    if first:
        days = [d for d in days if d >= first.isoformat()]
    if last:
        days = [d for d in days if d <= last.isoformat()]
    if not days:
        return pd.DataFrame(columns=COLUMNS)
    meta_path = os.path.join(run_dir, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    cap = float((meta.get("hardware") or {}).get("capacity_kwh", CAPACITY_KWH))
    supplier = supplier or {}
    solves = solves_by_day(run_dir, tz)
    rows = []
    for d in days:
        sl = slices[slices["date"] == d].sort_values("ts_utc")
        rows.append(day_metrics(sl, lad_by.get(d), supplier.get(d), solves.get(d), lane, capacity_kwh=cap))
    out = pd.DataFrame(rows, columns=COLUMNS)
    out.to_csv(os.path.join(run_dir, "metrics.csv"), index=False)
    if os.path.exists(all_csv):
        prev = pd.read_csv(all_csv)
        prev = prev[~((prev["lane"] == lane) & (prev["date"].isin(out["date"])))]
        out_all = pd.concat([prev, out], ignore_index=True)
    else:
        out_all = out
    out_all.sort_values(["lane", "date"]).to_csv(all_csv, index=False)
    return out


def monthly(df: pd.DataFrame) -> pd.DataFrame:
    """The roll-up: sums for energy and money, throughput-weighted per-kWh
    figures, hours summed, days counted."""
    g = df.assign(month=df["date"].str[:7]).groupby(["lane", "month"])
    s = g[["cash_eur", "loss_eur", "eur", "nobatt_cash_eur", "batt_gain_eur", "charge_cost_eur",
           "discharge_value_eur", "supplier_net_eur", "supplier_da_eur", "batt_in_kwh", "batt_out_kwh", "cycles",
           "grid_import_kwh", "grid_export_kwh", "grid_to_batt_kwh", "pv_kwh", "pv_curtailed_kwh", "load_kwh",
           "h_above_90", "h_below_15", "clamp_writes", "solves_ok", "solves_failed"]].sum(min_count=1)
    s["days"] = g.size()
    s["charge_cost_per_kwh"] = s["charge_cost_eur"] / s["batt_in_kwh"]
    s["discharge_value_per_kwh"] = s["discharge_value_eur"] / s["batt_out_kwh"]
    s["captured_spread"] = s["discharge_value_per_kwh"] - s["charge_cost_per_kwh"]
    per_kwh = ["charge_cost_per_kwh", "discharge_value_per_kwh", "captured_spread"]
    return s.round(2).assign(**{c: s[c].round(4) for c in per_kwh})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--lane", required=True)
    ap.add_argument("--first", default=None)
    ap.add_argument("--last", default=None)
    ap.add_argument("--supplier-daily", default=None, help="optional: a supplier's daily CSV (day,net_eur[,da_eur])")
    a = ap.parse_args(argv)
    df = run(a.run_dir, a.lane, date.fromisoformat(a.first) if a.first else None,
             date.fromisoformat(a.last) if a.last else None, supplier=supplier_rows(a.supplier_daily))
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    m = monthly(df)
    print(m[["days", "cash_eur", "loss_eur", "batt_gain_eur", "supplier_net_eur", "batt_in_kwh", "batt_out_kwh",
             "cycles", "grid_to_batt_kwh", "charge_cost_per_kwh", "discharge_value_per_kwh", "captured_spread",
             "h_above_90", "h_below_15", "solves_failed"]].to_string())
    print(f"{len(df)} days -> {os.path.join(a.run_dir, 'metrics.csv')} and {ALL_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
