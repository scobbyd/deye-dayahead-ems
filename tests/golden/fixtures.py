"""Inputs for the golden cases.

Everything the wrapper (pyscript/emhass_shadow.py) would hand the core, rebuilt
offline from committed fixtures:

  plans/                 archived plan documents copied verbatim from the VM's
                         /config/emhass/plans (2026-09-02..07), chosen to cover a
                         negative-price curtailment day (09-05), the PV split
                         arriving mid-day (09-06), the P50 archive and the knob
                         layer arriving (09-07), a replay document beside its
                         source, and pre-split / pre-profile documents
  synthetic/plans/       documents build.py fabricates for what the archive lacks:
                         the autumn DST day (100 steps), a non-Optimal solve, a
                         plan with the Growatt cut firing, a legacy .json file
  actuals_5min.json.gz   the recorder's 5-minute statistics for the eight
                         measured entities over the same days, in the WS row
                         shape (start ms, mean, max) that the wrapper receives
  nordpool.json          Nord Pool rows per day, the raw get_prices_for_date rows
  scores.csv             the VM's scores.csv as of 2026-09-07

The solver is stubbed IN PROCESS (StubSolver): a deterministic greedy policy
that charges on surplus, discharges in the dearer half of the horizon, and
curtails surplus at negative sell prices. It is not EMHASS; it only has to be
the same every time, and to produce rows the core has to reason about.
"""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import shutil
import sys
import tempfile
import warnings
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)
ROOT = os.path.dirname(os.path.abspath(__file__))
PLANS = os.path.join(ROOT, "plans")
SYN_PLANS = os.path.join(ROOT, "synthetic", "plans")
SYN_ACTUALS = os.path.join(ROOT, "synthetic", "actuals.json")
EXPECTED = os.path.join(ROOT, "expected")
ACTUALS_5MIN = os.path.join(ROOT, "actuals_5min.json.gz")
NORDPOOL = os.path.join(ROOT, "nordpool.json")
SCORES = os.path.join(ROOT, "scores.csv")

# the wrapper's entity map (pyscript/emhass_shadow.py)
ENT = {"pv_w": "sensor.total_pv_power", "load_w": "sensor.inverter_load_ups_power",
       "imp": "sensor.p1reader2_p1_reader_2_power_consumed",
       "exp": "sensor.p1reader2_p1_reader_2_power_returned",
       "batt_dc_w": "sensor.inverter_battery_power", "soc_pct": "sensor.inverter_battery",
       "curtailed": "sensor.pv_curtailment_active", "micro_w": "sensor.inverter_microinverter_power"}
MAX_GAPS = 8
PV_CURTAIL_SOC_PCT = 95.0
LAST_RUN = {"status": "ok", "action": "naive-mpc-optim", "timestamp": "2026-09-07T12:00:00Z",
            "duration_total_seconds": 0.1, "emhass_version": "stub"}


def local(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=Z)


def canonical(obj) -> str:
    """One byte layout for a result, whatever the container types."""
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, (set, frozenset)):
            return sorted(o)
        raise TypeError(f"not JSON-able: {type(o).__name__}")
    return json.dumps(obj, sort_keys=True, indent=1, allow_nan=False, default=default) + "\n"


# ---- measured series --------------------------------------------------------------

_RAW = None


def raw_stats() -> dict:
    """{entity_id: [{start, mean, max}, ...]} as recorder/statistics_during_period returns them."""
    global _RAW
    if _RAW is None:
        with gzip.open(ACTUALS_5MIN, "rt", encoding="utf-8") as f:
            packed = json.load(f)
        _RAW = {eid: [{"start": r[0], "mean": r[1], "max": r[2]} for r in rows] for eid, rows in packed.items()}
    return _RAW


def actuals_for_day(core, day: date, gap_upto: int | None = None) -> dict:
    """The wrapper's _actuals_15min, offline: {pv_w, load_w, grid_w, batt_dc_w, soc_pct,
    curtailed, micro_w, pv_peak_w}, a series dropped when it has more than MAX_GAPS
    held steps. The curtailment sensor only records from 2026-09-05 14:00, so
    earlier days take the SOC-only fallback mask (the wrapper reads the export
    switch's history too, which has no offline equivalent)."""
    st = raw_stats()
    got = {}
    for key, eid in ENT.items():
        vals, gaps = core.fifteen_min_series(day, TZ, st.get(eid), gap_upto)
        if gaps <= MAX_GAPS:
            got[key] = vals
    pk, pk_gaps = core.fifteen_min_series(day, TZ, st.get(ENT["pv_w"]), gap_upto, "max", "max")
    if pk_gaps <= MAX_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp", None), got.pop("exp", None)
    if imp is not None and exp is not None:
        got["grid_w"] = [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(len(imp))]
    if got.get("curtailed") is None and got.get("soc_pct") is not None:
        got["curtailed"] = [1.0 if float(s) > PV_CURTAIL_SOC_PCT else 0.0 for s in got["soc_pct"]]
    return got


def window_actuals(core, t0: datetime, n: int) -> dict | None:
    """The wrapper's _window_actuals: {pv_w, grid_w, batt_dc_w, curtailed, soc_pct,
    micro_w, pv_peak_w} over n steps from t0."""
    st = raw_stats()
    got = {}
    for key in ("pv_w", "imp", "exp", "batt_dc_w", "curtailed", "soc_pct", "micro_w"):
        vals, gaps = core.window_series(t0, n, st.get(ENT[key]))
        if gaps > MAX_GAPS:
            if key in ("curtailed", "micro_w"):
                got[key] = None
                continue
            return None
        got[key] = vals
    if got.get("curtailed") is None:
        got["curtailed"] = [1.0 if float(s) > PV_CURTAIL_SOC_PCT else 0.0 for s in got["soc_pct"]]
    pk, pk_gaps = core.window_series(t0, n, st.get(ENT["pv_w"]), None, "max", "max")
    if pk_gaps <= MAX_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp"), got.pop("exp")
    got["grid_w"] = [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(n)]
    return got


def load_days(core, today: date, n_days: int = 7) -> dict:
    """The wrapper's _load_history_days: {date: W per step} for the n complete days before today."""
    rows = raw_stats().get(ENT["load_w"])
    out = {}
    first = today - timedelta(days=n_days)
    for k in range(n_days):
        day = first + timedelta(days=k)
        vals, gaps = core.fifteen_min_series(day, TZ, rows)
        if gaps <= MAX_GAPS:
            out[day] = vals
    return out


def nordpool(day: date) -> list:
    with open(NORDPOOL) as f:
        return json.load(f).get(day.isoformat()) or []


# ---- Solcast rows rebuilt from an archived document ---------------------------------

def solcast_from_doc(doc: dict, day: date, field_p50="pv_p50_w", field_p10="pv_p10_w", scale=1.0) -> list:
    """Half-hourly Solcast rows for `day` from a document's positional P50/P10 series
    (gross W per step): the archive keeps the series, not the rows, so the run_plan
    cases feed the planner the sun its own plan saw. `scale` builds the ESE site."""
    t0 = datetime.fromisoformat(doc["t0"]).astimezone(Z)
    p50, p10, p90 = doc.get(field_p50) or [], doc.get(field_p10) or [], doc.get("pv_p90_w") or []
    if not p50:
        p50 = [float(r["P_PV"]) for r in doc["rows"]]
    rows = []
    for k, t in enumerate(_step_times(t0, len(p50))):
        if t.date() != day or (t.minute % 30 and k > 0):
            continue
        # a horizon starting at :15/:45 still gets the half-hour that contains it
        start = t.replace(minute=(t.minute // 30) * 30)
        row = {"period_start": start.isoformat(), "pv_estimate": round(float(p50[k]) * scale / 1000.0, 4)}
        if k < len(p10):
            row["pv_estimate10"] = round(float(p10[k]) * scale / 1000.0, 4)
        if k < len(p90):
            row["pv_estimate90"] = round(float(p90[k]) * scale / 1000.0, 4)
        rows.append(row)
    return rows


def _step_times(t0, n):
    base = t0.astimezone(timezone.utc)
    return [(base + timedelta(minutes=15 * k)).astimezone(t0.tzinfo) for k in range(n)]


# ---- the solver stub ---------------------------------------------------------------

class StubSolver:
    """A deterministic stand-in for core.solve: same signature, same result shape.

    Greedy policy per step: surplus charges the pack (up to the payload's power cap
    and the headroom); the remainder exports, or is CURTAILED when the sell price is
    negative; a deficit discharges in steps whose buy price is at or above the
    horizon's median, and imports otherwise. Rows are stamped from `t0` exactly as
    naive-mpc-optim stamps them from the ceil of now."""

    def __init__(self, t0: datetime, status: str = "Optimal", curtail: bool = True,
                 eta_c: float = 0.961, eta_d: float = 0.957, capacity_kwh: float = 48.2):
        self.t0, self.status, self.curtail = t0, status, curtail
        self.eta_c, self.eta_d, self.cap_kwh = eta_c, eta_d, capacity_kwh
        self.calls: list[dict] = []

    def __call__(self, base_url: str, payload: dict, timeout: int = 180) -> dict:
        self.calls.append(dict(payload))
        n = int(payload["prediction_horizon"])
        pv = [float(v) for v in payload["pv_power_forecast"]]
        load = [float(v) for v in (payload.get("load_power_forecast") or [400.0] * n)]
        buy = [float(v) for v in payload["load_cost_forecast"]]
        sell = [float(v) for v in payload["prod_price_forecast"]]
        soc = float(payload["soc_init"])
        smin = float(payload.get("battery_minimum_state_of_charge", 0.10))
        smax = float(payload.get("battery_maximum_state_of_charge", 1.00))
        # The add-on bounds the port discharge at eff_dis * max and the port
        # charge at max itself (measured on the live plans of 2026-09-29).
        eta_c = float(payload.get("battery_charge_efficiency", self.eta_c))
        eta_d = float(payload.get("battery_discharge_efficiency", self.eta_d))
        ccap = float(payload.get("battery_charge_power_max", 12500.0))
        dcap = float(payload.get("battery_discharge_power_max", 12500.0)) * eta_d
        med = sorted(buy)[n // 2]
        step_kwh = self.cap_kwh * 1000.0 / 0.25          # W that moves the whole pack in one step
        rows = []
        for k, t in enumerate(_step_times(self.t0, n)):
            surplus = pv[k] - load[k]
            charge = discharge = curtail = 0.0
            headroom = max(0.0, smax - soc) * step_kwh / eta_c
            floor = max(0.0, soc - smin) * step_kwh * eta_d
            if surplus > 0:
                charge = min(ccap, surplus, headroom)
                rest = surplus - charge
                if sell[k] < 0 and self.curtail:
                    curtail, grid = rest, 0.0
                else:
                    grid = -rest
            else:
                need = -surplus
                if buy[k] >= med:
                    discharge = min(dcap, need, floor)
                grid = need - discharge
            soc += charge * 0.25 / 1000.0 * eta_c / self.cap_kwh
            soc -= discharge * 0.25 / 1000.0 / eta_d / self.cap_kwh
            soc = min(max(soc, smin), smax)
            p_batt = round(discharge - charge, 4)
            grid = round(grid, 4)
            cost = (buy[k] * max(grid, 0.0) - sell[k] * max(-grid, 0.0)) * 0.25 / 1000.0
            rows.append({"timestamp": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                         "P_PV": round(pv[k], 4), "P_Load": round(load[k], 4),
                         "P_PV_curtailment": round(curtail, 4),
                         "P_grid_pos": max(grid, 0.0), "P_grid_neg": min(grid, 0.0), "P_grid": grid,
                         "P_batt": p_batt, "SOC_opt": round(soc, 10),
                         "P_hybrid_inverter": round(load[k] - grid, 4),
                         "unit_load_cost": buy[k], "unit_prod_price": sell[k],
                         "cost_profit": round(-cost, 10), "optim_status": self.status})
        return {"ok": True, "rows": rows, "last_run": dict(LAST_RUN), "seconds": 0.0}


@contextlib.contextmanager
def patched_solve(fn, stub):
    """Swap `solve` for `stub` in every loaded module that binds the add-on's
    solve (found by identity, so a module that imported it under `from .addon
    import solve` is covered wherever it lives), plus the module defining `fn`.
    While patched, the HTTP layer raises: a solve that slips past the stub must
    fail at once, not sleep 30 s per retry against http://stub."""
    import emhasscore.addon as addon
    orig = addon.solve
    with warnings.catch_warnings():
        # numpy/scipy keep deprecated lazy submodules whose getattr warns once emhass is
        # loaded in the session; the scan is read-only. Deliberate twin of
        # backtest//solver.py::patch_solve, kept apart so this suite runs
        # on an interpreter without the emhass library.
        warnings.simplefilter("ignore", DeprecationWarning)
        mods = {m for m in list(sys.modules.values()) if m is not None and getattr(m, "solve", None) is orig}
    mods.add(sys.modules[fn.__module__])
    saved = {m: getattr(m, "solve", None) for m in mods}
    post_saved = addon.emhass_post

    def no_http(*a, **k):
        raise RuntimeError("an unpatched solve reached the HTTP layer")
    for m in mods:
        setattr(m, "solve", stub)
    addon.emhass_post = no_http
    try:
        yield stub
    finally:
        addon.emhass_post = post_saved
        for m, old in saved.items():
            if old is None:
                delattr(m, "solve")
            else:
                setattr(m, "solve", old)


@contextlib.contextmanager
def tmp_archive(src: str = PLANS):
    """A throwaway copy of an archive for the cases that write to it. Copies keep
    their mtimes, so plan_heads' cache sees the same files."""
    d = tempfile.mkdtemp(prefix="emhass-golden-")
    try:
        dst = os.path.join(d, "plans")
        shutil.copytree(src, dst)
        yield dst
    finally:
        shutil.rmtree(d, ignore_errors=True)


def strip_paths(obj, roots: tuple[str, ...]):
    """Replace absolute fixture / temp paths in a result by their basename, so the
    expected files do not depend on where the tree was checked out."""
    if isinstance(obj, dict):
        return {k: strip_paths(v, roots) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_paths(v, roots) for v in obj]
    if isinstance(obj, tuple):
        return [strip_paths(v, roots) for v in obj]
    if isinstance(obj, str) and any(obj.startswith(r) for r in roots):
        return "<archive>/" + os.path.basename(obj)
    return obj
