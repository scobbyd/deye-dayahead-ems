"""One year of the replay in one table, over the metrics sidecar.

A year need not be one run: on the reference plant the winter was a
pretend-plant frame (the current plant modelled on the previous winter,
7 November to 15 February) and the rest the delivered plant record, so a
yearly lane may be a chain of runs. A lane spec is `Label=run[,run...]`: the
runs in order, each covering the days up to the first day the next one has,
the last one to the end of the span. `run@date` ends that run on `date`
instead, which is how the chained lanes take the pretend winter exactly as
far as the pretend frame goes (@2026-02-15) rather than as far as the next
run happens to start. A single-run lane is just `Label=run`; without --lane
every lane in the CSV is its own single-run lane, and without --span the
span is the CSV's own.

    python -m backtest.yearly [--span 2025-11-07 2026-09-03] [--csv ...] [--monthly]
        [--lane "2026 salderen=foresight_winter,foresight"] [--lane "PV 25 %=foresight_pv25_year"]

Prints one row per lane: days covered, the cash (as EUR earned, the sidecar's
sign flipped), the same house without a battery, the pack's gain, the grid
and pack energy, the cycles, and the gain per day. --monthly adds the per
month table per lane. Days a lane does not have are reported, never filled:
a lane short of the span is marked with its own first and last day.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from backtest import metrics as mx

SUMS = ["cash_eur", "nobatt_cash_eur", "batt_gain_eur", "grid_import_kwh", "grid_export_kwh",
        "grid_to_batt_kwh", "batt_in_kwh", "batt_out_kwh", "pv_kwh", "pv_curtailed_kwh", "load_kwh", "cycles"]
EARNED = {"cash_eur": "earned_eur", "nobatt_cash_eur": "nobatt_earned_eur"}


def parse_lane(spec: str) -> tuple[str, list[str]]:
    label, _, runs = spec.partition("=")
    if not runs:
        label, runs = spec, spec
    return label, [r.strip() for r in runs.split(",") if r.strip()]


def lane_days(df: pd.DataFrame, runs: list[str], first: str, last: str) -> pd.DataFrame:
    """The chained rows over [first, last]: run k covers up to its own `@date`
    when it has one, else up to the day before run k+1's first scored day."""
    scored = df[df["cash_eur"].notna() & (df["date"] >= first) & (df["date"] <= last)]
    names = [r.split("@", 1)[0] for r in runs]
    out = []
    for k, run in enumerate(runs):
        name, _, cut = run.partition("@")
        rows = scored[scored["lane"] == name]
        if cut:
            keep = rows[rows["date"] <= cut]
        elif k + 1 < len(runs):
            nxt = scored[scored["lane"] == names[k + 1]]["date"].min()
            keep = rows if pd.isna(nxt) else rows[rows["date"] < nxt]
        else:
            keep = rows
        out.append(keep)
    got = pd.concat(out) if out else scored.iloc[:0]
    return got.drop_duplicates(subset="date", keep="first").sort_values("date")


def summarize(df: pd.DataFrame, lanes: list[str], first: str, last: str) -> pd.DataFrame:
    rows = {}
    for spec in lanes:
        label, runs = parse_lane(spec)
        d = lane_days(df, runs, first, last)
        if not len(d):
            continue
        s = d[SUMS].sum()
        r = {"days": len(d), "from": d["date"].min(), "to": d["date"].max()}
        r["earned_eur"] = -s["cash_eur"]
        r["nobatt_earned_eur"] = -s["nobatt_cash_eur"]
        r.update({c: s[c] for c in SUMS if c not in EARNED})
        r["gain_per_day"] = s["batt_gain_eur"] / len(d)
        r["gain_per_cycle"] = s["batt_gain_eur"] / s["cycles"] if s["cycles"] else float("nan")
        rows[label] = r
    return pd.DataFrame(rows).T


def monthly(df: pd.DataFrame, lanes: list[str], first: str, last: str) -> pd.DataFrame:
    out = []
    for spec in lanes:
        label, runs = parse_lane(spec)
        d = lane_days(df, runs, first, last).copy()
        if not len(d):
            continue
        d["month"] = d["date"].str[:7]
        g = d.groupby("month")[SUMS].sum()
        g.insert(0, "days", d.groupby("month")["date"].size())
        g["earned_eur"] = -g.pop("cash_eur")
        g["nobatt_earned_eur"] = -g.pop("nobatt_cash_eur")
        g.insert(0, "lane", label)
        out.append(g.reset_index())
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the replay's lanes as one year")
    ap.add_argument("--lane", action="append", default=[], metavar="LABEL=run[,run]",
                    help="a yearly lane: the runs in order, earlier runs covering the days before the next one starts")
    ap.add_argument("--span", nargs=2, default=None, metavar=("FIRST", "LAST"), help="default: the CSV's own span")
    ap.add_argument("--csv", default=mx.ALL_CSV)
    ap.add_argument("--monthly", action="store_true")
    ap.add_argument("--out", default=None, help="write the lane table as CSV as well")
    a = ap.parse_args(argv)
    raw = pd.read_csv(a.csv, dtype={"date": str})
    raw = raw[raw["source"] == "rec"]
    lanes = a.lane or sorted(raw["lane"].unique())
    first, last = a.span if a.span else (raw["date"].min(), raw["date"].max())
    t = summarize(raw, lanes, first, last)
    pd.set_option("display.width", 240)
    print(t.round(1).to_string())
    if a.monthly:
        print()
        print(monthly(raw, lanes, first, last).round(1).to_string(index=False))
    if a.out:
        t.to_csv(a.out, index_label="lane")
        print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
