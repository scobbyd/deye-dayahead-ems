"""The wrapper's _actuals_15min and the golden fixtures' window_actuals, offline
and fed from a CSV of 5-minute statistics instead of the recorder.

day_actuals and window_actuals are tests/golden/fixtures.py's actuals_for_day
and window_actuals line for line, with the statistics passed in instead of
read from the module global, plus the one thing the fixtures leave out: the
wrapper's curtailment fallback (_fallback_mask) is BOTH halves of the site
rule, SOC above 95 % AND switch.inverter_export_surplus off at the step
start, read from the switch's state history. The recorder's statistics do not
carry that history, so it comes in as `export_pts`, the switch's transitions
(export_states_from_csv); without it the mask is SOC-only, as in the wrapper
when the switch has no recorded state. On 2026-09-03 and 09-05 the SOC-only
mask fired on 5 and 50 window steps against the live rule's 2 and 29, and the
hindsight lane was off by 0,016 and 0,125 EUR; with the history it matches to
the cent (task-3-report.md).

The switch's states exist ONLY as recorder state history, about ten days of
purge window, never as long-term statistics: the CSV holds what the recorder
still had on 2026-09-11 (from 08-20), refresh_export_states extends it before
a window ages out, and the summer frame (March to August) cannot carry the
live mask at all and drops to SOC alone there, over-firing by about half the
steps on the two days measured."""
import csv
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

TZ = "Europe/Amsterdam"
# wrapper constants (ha/pyscript/emhass_shadow.py): what each series is called.
# The reference plant's entity ids: rename to yours, in this one map.
PV, LOAD, IMP, EXP = "sensor.total_pv_power", "sensor.inverter_load_ups_power", \
    "sensor.p1reader2_p1_reader_2_power_consumed", "sensor.p1reader2_p1_reader_2_power_returned"
BATT, SOC, CURT, MICRO = "sensor.inverter_battery_power", "sensor.inverter_battery", \
    "sensor.pv_curtailment_active", "sensor.inverter_microinverter_power"
COLS = {PV: "total_pv_power", LOAD: "inverter_load_ups_power", IMP: "p1reader2_p1_reader_2_power_consumed",
        EXP: "p1reader2_p1_reader_2_power_returned", BATT: "inverter_battery_power", SOC: "inverter_battery",
        CURT: "pv_curtailment_active", MICRO: "inverter_microinverter_power"}
MAX_COL = "total_pv_power_max"
# the wrapper's entity map (ha/pyscript/emhass_shadow.py), keyed as the fixtures key it
ENT = {"pv_w": PV, "load_w": LOAD, "imp": IMP, "exp": EXP, "batt_dc_w": BATT, "soc_pct": SOC,
       "curtailed": CURT, "micro_w": MICRO}
# MAX_PV_GAPS and PV_CURTAIL_SOC_PCT are read from `core` by the callers, never re-declared here
EXPORT_SW = "switch.inverter_export_surplus"      # the other half of the site rule (ACTUAL_EXPORT_SW)


def raw_stats_from_csv(path: str) -> dict:
    """{entity_id: [{"start": ms, "mean": float, "max": float|None}, ...]}"""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    out = {}
    for eid, col in COLS.items():
        if col not in df.columns:
            continue
        mx = df[MAX_COL] if (eid == PV and MAX_COL in df.columns) else None
        rows = []
        for i, (ts, v) in enumerate(df[col].items()):
            if pd.isna(v):
                continue
            rows.append({"start": int(ts.value // 10**6), "mean": float(v),
                         "max": (float(mx.iloc[i]) if mx is not None and not pd.isna(mx.iloc[i]) else None)})
        out[eid] = rows
    return out


def export_states_from_csv(path: str) -> list:
    """[(epoch seconds, state), ...] sorted: the export switch's state changes as
    HA's history endpoint lists them (every state, "unavailable" included, so the
    walk below filters exactly as the wrapper does)."""
    pts = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            pts.append((datetime.fromisoformat(r["ts_utc"]).timestamp(), r["state"]))
    pts.sort()
    return pts


def merge_export_states(existing: list, history: list, start: str) -> list:
    """The CSV's rows after a fetch: `existing` (ts_utc, state) rows plus the
    decoded /api/history/period response (a list per entity of states with
    last_changed and state), merged on (timestamp, state) so nothing is ever
    replaced and a repeated fetch adds nothing. The endpoint also returns the
    state AT `start` stamped on `start` itself (a marker, not a transition, and
    a different one for every start); that row is dropped so repeated runs with
    different starts merge to the same file. Sorted, as the file is."""
    t_start = datetime.fromisoformat(start)
    rows = {(str(x["last_changed"]), str(x["state"])) for lst in history for x in lst
            if datetime.fromisoformat(x["last_changed"]) != t_start}
    rows |= {(str(ts), str(st)) for ts, st in existing}
    return sorted(rows)


def refresh_export_states(path: str, base_url: str, token: str, start: str, end: str,
                          entity: str = EXPORT_SW, timeout: int = 60) -> int:
    """Refetch the switch's state history from HA and merge it into `path`, the
    call the CSV was first built with (2026-09-11, start 2026-08-20T00:00:00+00:00):

        GET {base_url}/api/history/period/{start}?filter_entity_id={entity}
            &end_time={end}&minimal_response&no_attributes
        Authorization: Bearer {token}          (HA_LLAT in the project .env)

    A run every week or so keeps the file growing past the recorder's purge
    window (merge_export_states has the rules). Returns the row count written.
    requests is imported here so the pure-data module needs nothing beyond
    pandas."""
    import requests
    r = requests.get(f"{base_url.rstrip('/')}/api/history/period/{start}",
                     params={"filter_entity_id": entity, "end_time": end,
                             "minimal_response": "", "no_attributes": ""},
                     headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    existing = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            existing = [(x["ts_utc"], x["state"]) for x in csv.DictReader(f)]
    out = merge_export_states(existing, r.json(), start)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_utc", "state"])
        w.writerows(out)
    return len(out)


def export_off(pts: list, t0: datetime, n: int) -> list | None:
    """The wrapper's _export_off: 1,0 per step where the switch was OFF at the step
    start. state_changes_during_period(a, b, include_start_time_state=True) hands
    the wrapper the state at `a` plus the changes inside (a, b); the same set is
    cut out of the transition list here. None when nothing is recorded for the
    window, which is the caller's signal to drop back to the SOC half alone."""
    a = t0.astimezone(timezone.utc)
    b = a + timedelta(minutes=15 * n)
    ta, tb = a.timestamp(), b.timestamp()
    before = [p for p in pts if p[0] <= ta]
    hist = ([(ta, before[-1][1])] if before else []) + [p for p in pts if ta < p[0] < tb]
    pts = [(ts, st) for ts, st in hist if st in ("on", "off")]
    pts.sort()
    if not pts:
        return None
    out, j, cur = [], 0, pts[0][1]
    for i in range(n):
        ts = (a + timedelta(minutes=15 * i)).timestamp()
        while j < len(pts) and pts[j][0] <= ts:
            cur = pts[j][1]
            j += 1
        out.append(1.0 if cur == "off" else 0.0)
    return out


def fallback_mask(pts: list | None, t0: datetime, n: int, soc_pct: list, curtail_soc_pct: float) -> list:
    """The wrapper's _fallback_mask: both halves of the rule when the switch has a
    history, SOC alone when it has none (or none was given). `curtail_soc_pct`
    is core.PV_CURTAIL_SOC_PCT, passed so this module never carries its own."""
    off = export_off(pts, t0, n) if pts else None
    m = min(n, len(soc_pct))
    if off is None:
        return [1.0 if float(soc_pct[i]) > curtail_soc_pct else 0.0 for i in range(m)]
    return [1.0 if (float(soc_pct[i]) > curtail_soc_pct and off[i] >= 0.5) else 0.0
            for i in range(m)]


def day_actuals(stats: dict, core, day: date, gap_upto: int | None = None, export_pts: list | None = None) -> dict:
    """The wrapper's _actuals_15min, offline: {pv_w, load_w, grid_w, batt_dc_w, soc_pct,
    curtailed, micro_w, pv_peak_w}, a series dropped when it has more than core.MAX_PV_GAPS
    held steps. The curtailment sensor only records from 2026-09-05 14:00, so
    earlier days take the fallback mask: SOC and the export switch's history when
    `export_pts` carries it, SOC alone otherwise."""
    st = stats
    got = {}
    for key, eid in ENT.items():
        vals, gaps = core.fifteen_min_series(day, TZ, st.get(eid), gap_upto)
        if gaps <= core.MAX_PV_GAPS:
            got[key] = vals
    pk, pk_gaps = core.fifteen_min_series(day, TZ, st.get(ENT["pv_w"]), gap_upto, "max", "max")
    if pk_gaps <= core.MAX_PV_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp", None), got.pop("exp", None)
    if imp is not None and exp is not None:
        got["grid_w"] = [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(len(imp))]
    if got.get("curtailed") is None and got.get("soc_pct") is not None:
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(TZ))
        got["curtailed"] = fallback_mask(export_pts, midnight, len(got["soc_pct"]), got["soc_pct"],
                                         core.PV_CURTAIL_SOC_PCT)
    return got


def window_actuals(stats: dict, core, t0: datetime, n: int, export_pts: list | None = None) -> dict | None:
    """The wrapper's _window_actuals: {pv_w, grid_w, batt_dc_w, curtailed, soc_pct,
    micro_w, pv_peak_w} over n steps from t0."""
    st = stats
    got = {}
    for key in ("pv_w", "imp", "exp", "batt_dc_w", "curtailed", "soc_pct", "micro_w"):
        vals, gaps = core.window_series(t0, n, st.get(ENT[key]))
        if gaps > core.MAX_PV_GAPS:
            if key in ("curtailed", "micro_w"):
                got[key] = None
                continue
            return None
        got[key] = vals
    if got.get("curtailed") is None:
        got["curtailed"] = fallback_mask(export_pts, t0, n, got["soc_pct"], core.PV_CURTAIL_SOC_PCT)
    pk, pk_gaps = core.window_series(t0, n, st.get(ENT["pv_w"]), None, "max", "max")
    if pk_gaps <= core.MAX_PV_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp"), got.pop("exp")
    got["grid_w"] = [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(n)]
    return got
