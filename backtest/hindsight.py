"""Reproduce the live hindsight lane offline, day by day, and compare with scores.csv.

Everything emhasscore.scoring.hindsight_day needs is what the live wrapper
(ha/pyscript/emhass_shadow.py::_hindsight) hands it: the plan of record from the
archive, the plan's whole horizon and the scored day as 15-minute actuals (the
curtailment mask built from the export switch's history, as the wrapper builds
it), the Nord Pool rows for every calendar day the horizon touches, the pack
capacity and the lambda fraction. Only the solve is different: the library
in-process instead of the add-on over HTTP.

The reference is the live scoreboard's scores.csv as synced on 2026-09-11
(golden/scores_2026-09-11.csv), not golden/scores.csv: that older snapshot
(09-07) carries hindsight values from the core as it was then, and the live
board was rescored by hand with the current core after it, so the older
numbers cannot be reached by today's code and the older file stays as the
other golden cases' input.

Inputs live in data/ (gitignored), your synced archive: the recorder's
5-minute statistics CSV (backtest.actuals.raw_stats_from_csv's layout), the
export switch's state history CSV, the Nord Pool rows per day as JSON, the
plan archive directory (a copy of /config/emhass/plans) and the scoreboard
CSV. main() reports which are missing and exits 2 instead of tracing back.

The core is reached through the emhass_core facade, as every test does: the
bare emhasscore package exports nothing, and the facade re-imports the package
on first load, so one import path keeps patch_solve and hindsight_day on the
same module objects."""
import argparse, csv, json, os, sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import emhass_core as core
except ImportError:                     # the facade lives in ha/pyscript_helpers; the tests put it on the path
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ha", "pyscript_helpers"))
    import emhass_core as core
from .actuals import raw_stats_from_csv, day_actuals, window_actuals, export_states_from_csv
from .solver import LibrarySolver, patch_solve

TZ = "Europe/Amsterdam"
CAPACITY_KWH, LAMBDA_FRAC = 48.2, 0.9
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))          # your synced archive, gitignored
DEFAULT_CSV = os.path.join(DATA, "recorder_5min.csv")              # 5-minute statistics, one column per entity
DEFAULT_EXPORT_SW = os.path.join(DATA, "export_switch.csv")        # ts_utc,state of the export switch
DEFAULT_NORDPOOL = os.path.join(DATA, "nordpool.json")             # {day: nordpool.get_prices_for_date rows}
DEFAULT_ARCHIVE = os.path.join(DATA, "emhass_plans")               # a copy of /config/emhass/plans
GOLD = os.path.join(HERE, "..", "tests", "golden")
DEFAULT_SCORES = os.path.join(DATA, "scores_live.csv")             # the live scoreboard as synced


def load_nordpool(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def np_rows_for(doc: dict, nordpool: dict) -> list:
    t0 = datetime.fromisoformat(doc["t0"]).astimezone(ZoneInfo(TZ))
    end = (t0 + timedelta(minutes=15 * int(doc["n"]))).date()
    rows, d = [], t0.date()
    while d <= end:
        rows += nordpool.get(d.isoformat()) or []
        d += timedelta(days=1)
    return rows


def live_eur(scores_csv: str, day_iso: str):
    with open(scores_csv) as f:
        for r in csv.DictReader(f):
            if r.get("date") == day_iso and r.get("hindsight_status") == "ok" and r.get("hindsight_eur"):
                return float(r["hindsight_eur"])
    return None


def reproduce(day_iso: str, archive_dir: str, stats: dict, nordpool: dict, scores_csv: str,
              solver=None, export_pts: list | None = None) -> dict:
    """One day of the live lane, offline: {day, status, eur, live_eur, delta, seconds, message}.
    `export_pts` is the export switch's history (export_states_from_csv); without
    it the curtailment mask is SOC-only and a repaired day will not match."""
    day = date.fromisoformat(day_iso)
    found = core.plan_for_day(archive_dir, day, TZ)
    if not found:
        return {"day": day_iso, "status": "no_plan", "eur": None, "live_eur": live_eur(scores_csv, day_iso), "delta": None}
    doc = found[0]
    t0, n = datetime.fromisoformat(doc["t0"]).astimezone(ZoneInfo(TZ)), int(doc["n"])
    win = window_actuals(stats, core, t0, n, export_pts)
    act = day_actuals(stats, core, day, None, export_pts)
    solver = solver or LibrarySolver()
    with patch_solve(core.hindsight_day, solver):
        out = core.hindsight_day(archive_dir, "http://library", day_iso, TZ, np_rows_for(doc, nordpool),
                                 win, act, CAPACITY_KWH, LAMBDA_FRAC)
    ref = live_eur(scores_csv, day_iso)
    eur = out.get("eur")
    return {"day": day_iso, "status": out.get("status"), "eur": eur, "live_eur": ref,
            "delta": (None if eur is None or ref is None else round(eur - ref, 4)),
            "seconds": out.get("seconds"), "message": out.get("message")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="reproduce the live hindsight lane offline")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--nordpool", default=DEFAULT_NORDPOOL)
    ap.add_argument("--scores", default=DEFAULT_SCORES)
    ap.add_argument("--export-switch", default=DEFAULT_EXPORT_SW,
                    help="the export switch's state history CSV (ts_utc,state); '' for the SOC-only mask")
    a = ap.parse_args(argv)
    missing = [p for p in (a.csv, a.nordpool, a.scores) if not os.path.isfile(p)]
    if not os.path.isdir(a.archive):
        missing.append(a.archive)
    if missing:
        print("hindsight needs your synced archive under data/ (see the module docstring); missing: "
              + ", ".join(missing), file=sys.stderr)
        return 2
    stats, npd, solver = raw_stats_from_csv(a.csv), load_nordpool(a.nordpool), LibrarySolver()
    pts = export_states_from_csv(a.export_switch) if a.export_switch else None
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    print(f"{'day':<12}{'status':<16}{'offline':>10}{'live':>10}{'delta':>9}")
    while d <= end:
        r = reproduce(d.isoformat(), a.archive, stats, npd, a.scores, solver, pts)
        f = lambda v: "" if v is None else f"{v:.4f}"
        print(f"{r['day']:<12}{r['status']:<16}{f(r['eur']):>10}{f(r['live_eur']):>10}{f(r['delta']):>9}")
        d += timedelta(days=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
