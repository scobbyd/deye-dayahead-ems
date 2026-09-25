"""One command: what a battery of size X on a hybrid of size Y would have earned
on your frame, per day, month and year, from the knowledge ladder.

    python -m backtest.quickcheck --frame data/frame.csv [--plant plant.json]
        [--capacity 48.2[,24.1,...]] [--inverter 12] [--lanes P1,F_da] [--start D --end D]
        [--tariff plant|2027|supplier] [--supplier-daily CSV] [--out runs/quickcheck]

What it does, in order:
  1. loads plant.json (--plant, else the search order in emhasscore.plant) and
     the frame (frame_schema.load: a bad frame stops here with the defect named);
  2. keeps the requested lanes whose columns the frame carries, says which F
     lanes it skips and why (a forecast column absent or empty);
  3. runs ladder.run for every capacity in --capacity (a sweep: one ladder CSV
     per capacity under --out, resumable), the LP's pack and inverter patched
     through backtest.hardware and the settlement's pack passed along;
  4. prints per capacity the block summary (ladder.print_blocks: one block per
     plant value, shared days only) and, from the same rows through
     backtest.yearly, a yearly table and a monthly table with: cash with
     battery, cash without battery (the no-battery baseline), the battery's
     gain, kWh through the pack (discharged) and EUR per kWh cycled. Money in
     the printed tables is EUR earned (positive = money in), European
     decimals (1.234,56).

`eur` in the ladder CSV is money OUT of the house (lower is better); the
tables here flip the sign so a gain reads as a positive number. A lane's
yearly row only sums the days that lane settled; the block summary is the
place where lanes are compared on the same days.

Needs the emhass library (pip install -r requirements.txt); exits 2 with the
hint when it is missing.
"""
from __future__ import annotations

import argparse
import os
import sys


def _eu(v, decimals: int = 2) -> str:
    """European number: comma decimal, point thousands ("1.234,56"); "" for NaN."""
    try:
        if v is None or v != v:
            return ""
        return f"{float(v):,.{decimals}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    except (TypeError, ValueError):
        return str(v)


def ladder_to_sidecar(rows, lane_prefix: str = "") -> "pd.DataFrame":
    """The ladder CSV's ok rows in the metrics sidecar's shape (backtest.yearly
    reads it): cash_eur = eur, nobatt_cash_eur = nobatt_eur, batt_gain_eur =
    nobatt - eur (positive = the battery lowered money out), batt_out_kwh =
    discharged_kwh, batt_in_kwh from the cycle count. Grid and PV energies
    the ladder does not carry are 0."""
    import pandas as pd
    from backtest import yearly
    ok = rows[rows["status"] == "ok"].copy()
    out = pd.DataFrame({"lane": lane_prefix + ok["lane"].astype(str), "source": "rec", "date": ok["day"].astype(str),
                        "cash_eur": ok["eur"].astype(float), "nobatt_cash_eur": ok["nobatt_eur"].astype(float),
                        "batt_gain_eur": ok["nobatt_eur"].astype(float) - ok["eur"].astype(float),
                        "batt_out_kwh": ok["discharged_kwh"].astype(float), "cycles": ok["cycles"].astype(float),
                        "pv_kwh": ok["pv_kwh"].astype(float), "pv_curtailed_kwh": ok["curtailed_kwh"].astype(float),
                        "load_kwh": ok["load_kwh"].astype(float)})
    for c in yearly.SUMS:
        if c not in out.columns:
            out[c] = 0.0
    return out


def yearly_table(side, capacity: float) -> "pd.DataFrame":
    """One row per lane over every day the lane settled."""
    from backtest import yearly
    if not len(side):
        return side.iloc[:0]
    lanes = sorted(side["lane"].unique())
    t = yearly.summarize(side, lanes, side["date"].min(), side["date"].max())
    t.insert(0, "capacity_kwh", capacity)
    t["eur_per_kwh"] = t["batt_gain_eur"] / t["batt_out_kwh"].where(t["batt_out_kwh"] > 0)
    t["cycles_per_day"] = t["cycles"] / t["days"]
    return t


def monthly_table(side, capacity: float) -> "pd.DataFrame":
    from backtest import yearly
    if not len(side):
        return side.iloc[:0]
    lanes = sorted(side["lane"].unique())
    m = yearly.monthly(side, lanes, side["date"].min(), side["date"].max())
    if not len(m):
        return m
    m.insert(0, "capacity_kwh", capacity)
    m["eur_per_kwh"] = m["batt_gain_eur"] / m["batt_out_kwh"].where(m["batt_out_kwh"] > 0)
    return m


YEAR_COLS = [("capacity_kwh", "kWh", 1), ("days", "days", 0), ("from", "from", None), ("to", "to", None),
             ("earned_eur", "cash batt EUR", 2), ("nobatt_earned_eur", "cash nobatt EUR", 2),
             ("batt_gain_eur", "gain EUR", 2), ("gain_per_day", "gain/day", 2),
             ("batt_out_kwh", "kWh cycled", 0), ("eur_per_kwh", "EUR/kWh", 3), ("cycles_per_day", "cycles/day", 2)]
MONTH_COLS = [("capacity_kwh", "kWh", 1), ("lane", "lane", None), ("month", "month", None), ("days", "days", 0),
              ("earned_eur", "cash batt EUR", 2), ("nobatt_earned_eur", "cash nobatt EUR", 2),
              ("batt_gain_eur", "gain EUR", 2), ("batt_out_kwh", "kWh cycled", 0), ("eur_per_kwh", "EUR/kWh", 3)]


def format_table(t, cols, index_name: str | None = "lane") -> str:
    """A fixed-width text table with European decimals."""
    import pandas as pd
    if not len(t):
        return "(no settled days)"
    rows = []
    for idx, r in t.iterrows():
        row = {} if index_name is None else {index_name: str(idx)}
        for c, label, dec in cols:
            v = r.get(c) if c in t.columns else None
            row[label] = _eu(v, dec) if dec is not None else ("" if v is None or (isinstance(v, float) and v != v) else str(v))
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.to_string(index=False, justify="right")


def run_capacity(df, lanes, start, end, capacity: float, inverter: float, out_dir: str, tariff: dict,
                 payout: dict, resume: bool = True) -> str:
    """One ladder run for one pack size; returns the CSV path."""
    from backtest import hardware, ladder
    from backtest.solver import LibrarySolver
    hw = hardware.normalize({"capacity_kwh": capacity, "inverter_kw": inverter})
    hardware.apply(hw)
    label = hardware.label(hw) or "_plant"
    solver = LibrarySolver(data_dir=os.path.join(out_dir, "emhass" + label))
    hardware.patch_solver(solver, hw)
    out_csv = os.path.join(out_dir, f"ladder{label}.csv")
    ladder.run(df, start, end, lanes, out_csv, solver=solver, tariff=tariff, resume=resume, payout=payout,
               capacity_kwh=capacity)
    hardware.apply(hardware.DEFAULT)
    return out_csv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="what a battery would have earned on your frame, from the knowledge ladder")
    ap.add_argument("--frame", required=True, help="the 15-minute frame CSV (backtest/frame_schema.py)")
    ap.add_argument("--plant", default=None, help="plant.json (default: the search order in emhasscore.plant)")
    ap.add_argument("--capacity", default=None,
                    help="pack size(s) in kWh, comma-separated for a sweep (default: the plant's)")
    ap.add_argument("--inverter", type=float, default=None, help="hybrid inverter AC nominal, kW (default: the plant's)")
    ap.add_argument("--lanes", default="P1,F_da", help="comma-separated lanes; F lanes without columns are skipped")
    ap.add_argument("--start", default=None, help="first local day (default: the frame's measured plant plus a week)")
    ap.add_argument("--end", default=None, help="last local day (default: the frame's last full day minus the horizon)")
    ap.add_argument("--tariff", default="plant", help="plant (plant.json), 2027 (the Dutch post-salderen frame) or supplier (the incumbent EMS tariff)")
    ap.add_argument("--supplier-daily", default=None, help="optional comparison: a supplier's daily payout CSV (day,payout_eur)")
    ap.add_argument("--out", default=os.path.join("runs", "quickcheck"), help="run directory (ladder CSVs, solver data)")
    ap.add_argument("--fresh", action="store_true", help="ignore ladder CSVs already in --out instead of resuming them")
    a = ap.parse_args(argv)

    if a.plant:
        if not os.path.isfile(a.plant):
            print(f"plant file not found: {a.plant}", file=sys.stderr)
            return 2
        os.environ["EMHASS_PLANT_JSON"] = os.path.abspath(a.plant)
    try:
        import emhass  # noqa: F401
    except ImportError:
        print("the emhass library is not importable: pip install -r requirements.txt (emhass==0.18.2) "
              "in the interpreter you run this with", file=sys.stderr)
        return 2
    import pandas as pd
    from emhasscore import plant as plant_mod
    from backtest import frame_schema, ladder

    tariffs = ladder.TARIFFS
    if a.tariff not in tariffs:
        print(f"--tariff: choose from {', '.join(sorted(tariffs))}", file=sys.stderr)
        return 2
    print(f"plant: {plant_mod.SOURCE or 'built-in defaults (no plant.json found)'}")
    try:
        df = frame_schema.load(a.frame)
    except (FileNotFoundError, ValueError) as e:
        print(f"frame: {e}", file=sys.stderr)
        return 2
    print(f"frame: {a.frame}, {len(df)} quarters {df.index[0]:%Y-%m-%d %H:%M}Z .. {df.index[-1]:%Y-%m-%d %H:%M}Z")

    want = [x.strip() for x in a.lanes.split(",") if x.strip()]
    unknown = [x for x in want if x not in ladder.LANES]
    if unknown:
        print(f"--lanes: unknown {', '.join(unknown)}; choose from {', '.join(ladder.LANES)}", file=sys.stderr)
        return 2
    have, missing = frame_schema.available_lanes(df, {k: ladder.LANES[k] for k in want})
    for lane in want:
        if lane in missing:
            print(f"skip {lane}: the frame has no {', '.join(missing[lane])}")
    if not have:
        print("no lane can run on this frame", file=sys.stderr)
        return 2
    try:
        start, end = ladder.default_range(df, have)
    except ValueError as e:
        print(f"frame: {e}", file=sys.stderr)
        return 2
    start, end = a.start or start, a.end or end
    caps = [float(x) for x in a.capacity.split(",")] if a.capacity else [float(ladder.CAPACITY_KWH)]
    inverter = float(a.inverter) if a.inverter is not None else float(ladder.P_NOM_INV_KW)
    print(f"lanes: {', '.join(have)}; days {start} .. {end}; capacities {', '.join(f'{c:g}' for c in caps)} kWh "
          f"on a {inverter:g} kW hybrid; tariff {a.tariff}")
    os.makedirs(a.out, exist_ok=True)
    payout = ladder.supplier_payout(a.supplier_daily)
    blocks = ladder.blocks_for(df)

    years, months = [], []
    for cap in caps:
        print(f"\n### {cap:g} kWh pack, {inverter:g} kW hybrid")
        csv_path = run_capacity(df, have, start, end, cap, inverter, a.out, tariffs[a.tariff], payout, resume=not a.fresh)
        print(f"\nblock summary ({os.path.relpath(csv_path)}):")
        ladder.print_blocks(csv_path, blocks)
        rows = pd.read_csv(csv_path)
        side = ladder_to_sidecar(rows)
        years.append(yearly_table(side, cap))
        months.append(monthly_table(side, cap))

    pd.set_option("display.width", 250)
    print("\n## yearly: every day each lane settled (EUR earned, positive = money in)")
    year = pd.concat(years) if years else pd.DataFrame()
    print(format_table(year, YEAR_COLS))
    print("\n## monthly")
    month = pd.concat(months, ignore_index=True) if months else pd.DataFrame()
    print(format_table(month, MONTH_COLS, index_name=None))
    if len(year):
        year.to_csv(os.path.join(a.out, "yearly.csv"))
        month.to_csv(os.path.join(a.out, "monthly.csv"), index=False)
        print(f"\nwrote {os.path.join(a.out, 'yearly.csv')} and monthly.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
