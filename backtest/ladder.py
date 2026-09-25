"""The knowledge ladder: one plan per day per lane, settled on the measured day
through the live accounting, so the difference between lanes is the value of
what each one knew.

Every lane solves ONE plan at 00:00 local of day D from the measured SOC at
that step, soc_final 0,5 at the horizon end, real 15-minute day-ahead prices
for the whole horizon (price knowledge is held equal across lanes), the
plant's own stress costs and the relaxed rebalance knobs, and the Growatt as
must-take exactly as the live planner and the hindsight lane hand it to
EMHASS: main-string PV in the PV list, the Growatt as negative load, and
rows_to_gross after the solve. The frame's PV columns are all MAIN-string
already (pv_pot_main_w, fc_pv_*_main_w), so nothing is subtracted from them;
only the load list loses micro_w.

Settlement is the hindsight lane's (scoring._hindsight_settle) with one
argument changed: replay_day's curtailment repair runs on the day's TRUE
potential (pv_pot_main_w + micro_w) for every lane. _hindsight_settle passes
the plan's own P_PV there, which is the repaired potential in the hindsight
lane and so the same series for the P lanes here, but for an F lane it would
be the forecast, and replay_day would then credit every watt of over-forecast
as free export. The live plan lane settles forecast plans on the reconstructed
potential (scoring._replayed_lane), never on the plan's PV; so does this.

One plant limit is enforced here that replay_day leaves to the plan: the
12 kW hybrid bridge (ruling 2026-09-11). replay_day applies the plan's
commanded battery to the measured day as it stands, and a plan that
under-forecast the evening sun commands a discharge that, next to the REAL
PV, would put up to 19 kW through a 12 kW bridge; P1, which knows the sun,
is bridge-capped inside its own LP. So before the day is settled the
commanded battery power is clamped per step against the day's true MAIN
harvest, b in [-12 kW - harvest, 12 kW - harvest] with harvest =
max(0, pv_pot_main_w - the plan's own curtailment), the grid absorbs the
difference and the pack keeps the energy that did not move (_bridge_cap).
Main only, because the Growatt sits on the gen port and never crosses the
Deye's DC-AC bridge, and the LP's own constraint (optimization.py, the DC
bus balance p_pv - p_pv_curtailment + p_sto against inverter_ac_output_max)
sees main-string PV alone; capping on the gross potential would have
docked P1 at every step its LP legitimately saturated the bridge while the
Growatt was producing (8 to 9 steps a day on the June smoke). The hindsight
lane never needs the cap because its plan is feasible on the real day by
construction. The settlement does price the quadratic stage losses
(replay_day's loss term); what it lacked was this hard cap and the LP's
SOC deficit and surplus costs, which it still does not price. Measured
after the cap on the March to September rerun: P1 settles worse than an F
lane on 31 of 665 (day, lane) pairs, all within 0,08 EUR but three F_da
days (13 March 0,76, 18 August 0,20, 3 May 0,19).

Plant blocks (ruling 2026-09-11). A frame may carry quarters whose record
belongs to an older plant (the frame's `plant` column reads `modelled` there:
the current plant's model laid on a grid trace the old plant produced). A
ladder day there would be a what-if, settled with the old plant's own battery
flows removed, so the lanes are only run on the measured plant by default and
the summary prints one block per plant value and never a mixed total. On the
reference frame the current strings and the microinverter only produce from
local 2026-02-08; the default range there is 2026-02-15 (the first week of
the new plant left out; 15 February itself is a 16-hour recorder gap) to
2026-09-06 (P2 needs D+2 inside the frame), and a run with --start 2026-01-12
fills the modelled block (the inverter's statistics start 2026-01-07 14:00Z).
default_range() derives the same rule from any frame: the first full measured
day plus a week, the last full day minus the longest lane's horizon.

The reference ladder rows were computed on the frame of the day-ahead-price
suspect mask and are pending the decision on whether the P and F lanes move
to the closed-loop settlement or are retired; the frame has since moved to
the supplier file's final contract (the imbalance-price mask, meter and
battery as a reference only), and this open-loop settlement still reads
grid_w and batt_dc_w as actuals.

An optional comparison: a supplier's own daily payout (--supplier-daily, a
CSV with columns day,payout_eur, money IN to the house) is joined per day
into the CSV as supplier_payout_eur (empty where absent) and totalled over
each block's shared days next to the lanes, sign flipped to money out. It is
a reference, not a lane: on the reference plant the incumbent EMS's payout
included the aFRR volumes and the imbalance settlement the lanes do not
model, and its tariff (no energy tax, 0,02 EUR/kWh supplier fee and feed-in
fee, 21 % BTW, so buy and sell sit 2 x 0,02 x 1,21 apart, verified
2026-09-01) was not the plant's own. Off by default.

Tariffs: `plant` is PLANT["tariff"] from plant.json (the reference: no
energy tax, 0,019 EUR/kWh supplier fee, 21 % BTW, 0,019 feed-in fee).
`2027` is the Dutch post-salderen frame (energy tax on every imported kWh,
no feed-in fee), a named alternative for a what-if. `supplier` is the
incumbent EMS's tariff above, for settling the lanes in the frame a
--supplier-daily payout was earned in.
"""
import argparse
import csv
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from emhasscore import objective, scoring
from emhasscore.archive import day_slice
from emhasscore.deye import ETA_C, ETA_D
from emhasscore.grid import expected_steps, STEP_H
from emhasscore.objective import (CAPACITY_KWH, ETA_BRIDGE, P_NOM_INV_KW, SOC_MAX, SOC_MIN, build_payload,
                                  rebalance_schedule, restamp, rows_to_gross, step_cost, stress_costs)
from emhasscore.plant import PLANT
from emhasscore.series import tariff as tariff_frame
from emhasscore.slices import compact_slice

from . import frame_schema

TZ = str(PLANT["site"]["timezone"])
LAMBDA_FRAC, SOC_FINAL = 0.9, float(PLANT["battery"]["soc_final_target"])
MICRO_SHARE = float(PLANT["pv"]["micro_share"])
BRIDGE_W = P_NOM_INV_KW * 1000.0      # the hybrid bridge, config.json inverter_ac_output_max / _input_max (as shipped;
#                                       _bridge_cap reads objective.P_NOM_INV_KW at call time so hardware.apply moves it)
PLANT_TARIFF = {k: float(PLANT["tariff"][k]) for k in ("energy_tax", "supplier_fee", "btw_pct", "feedin_fee")}
LIVE_TARIFF = PLANT_TARIFF             # the name the settlement code and the tests use for the plant's own tariff
TARIFF_2027 = {"energy_tax": 0.11, "supplier_fee": 0.02, "btw_pct": 21.0, "feedin_fee": 0.0}   # NL post-salderen frame
TARIFF_SUPPLIER = {"energy_tax": 0.0, "supplier_fee": 0.02, "btw_pct": 21.0, "feedin_fee": 0.02}   # the incumbent EMS's buy and sell, 2 x 0,02 x 1,21 apart
TARIFFS = {"plant": PLANT_TARIFF, "2027": TARIFF_2027, "supplier": TARIFF_SUPPLIER}
MEASURED_LEAD_DAYS = 7                 # the measured block starts a week after the plant's first full day
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
LANES = {
    "P0":    {"days": 1, "pv": ["pv_pot_main_w"], "load": "load_w"},
    "P1":    {"days": 2, "pv": ["pv_pot_main_w"], "load": "load_w"},
    "P2":    {"days": 3, "pv": ["pv_pot_main_w"], "load": "load_w"},
    "F_da":  {"days": 2, "pv": ["fc_pv_om24_main_w", "fc_pv_om48_main_w"], "load": "fc_load_ma7_w"},
    "F_sol": {"days": 2, "pv": ["fc_pv_solcast_p50_main_w"], "load": "fc_load_ma7_w"},
    "F_mix": {"days": 2, "pv": ["mix"], "load": "fc_load_ma7_w"},
    "F_om0": {"days": 2, "pv": ["fc_pv_om0_main_w"], "load": "fc_load_ma7_w"},
}
MIX_COLS = ["fc_pv_solcast_p50_main_w", "fc_pv_solcast_p10_main_w"]
MEASURED = ["pv_pot_main_w", "pv_main_w", "micro_w", "load_w", "grid_w", "batt_dc_w", "soc_pct", "da_eur_kwh"]
FIELDS = ["day", "lane", "status", "eur", "cash_eur", "soc_term_eur", "loss_eur", "pv_kwh", "load_kwh",
          "curtailed_kwh", "soc_start_pct", "soc_end_pct",
          "cycles", "discharged_kwh", "nobatt_eur", "batt_value_eur", "eur_per_kwh", "soc_above_90_pct", "soc_below_15_pct",
          "capped_steps", "supplier_payout_eur", "n", "seconds", "message"]


def _horizon(df, day: date, days: int):
    """(t0, n, window): the horizon grid from 00:00 local of `day` over `days`
    local days, DST days at their real step count; window rows the frame does
    not cover come back NaN."""
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(TZ))
    n = _steps(day, days)
    idx = pd.date_range(t0.astimezone(ZoneInfo("UTC")), periods=n, freq="15min", name="ts_utc")
    return t0, n, df.reindex(idx)


def _steps(day: date, days: int) -> int:
    return sum(expected_steps(day + timedelta(days=k), TZ) for k in range(days))


def _pv_columns(spec) -> list:
    return MIX_COLS if spec["pv"] == ["mix"] else list(spec["pv"])


def _pv_list(win, spec, day: date) -> list:
    """The lane's main-string PV over the horizon, W."""
    if spec["pv"] == ["mix"]:
        return (0.8 * win[MIX_COLS[0]] + 0.2 * win[MIX_COLS[1]]).tolist()
    if len(spec["pv"]) == 1:
        return win[spec["pv"][0]].tolist()
    # day-ahead: the scored day from the 24 h lane, the rest from the 48 h lane
    first = win.index.tz_convert(TZ).date == day
    return win[spec["pv"][1]].where(~first, win[spec["pv"][0]]).tolist()


def _doc(t0, n, soc_init, tariff) -> dict:
    """The archived document's keys that _hindsight_settle and compact_slice
    read; the settlement overrides rows, pv_micro_w and the status keys itself."""
    return {"plan_ts": t0.isoformat(), "t0": t0.isoformat(), "n": n, "tz": TZ, "soc_init": soc_init,
            "soc_final": SOC_FINAL, "tariff": dict(tariff), "n_predicted_steps": 0, "pv_gap_steps": 0,
            "growatt_share": MICRO_SHARE, "knobs": {}, "prices_predicted": False, "optim_status": "Optimal",
            "soc_source": "measured", "load_source": "frame"}


def _day_actuals(df, day: date) -> dict:
    """The wrapper's _actuals_15min shape from the frame's day D: gross PV and
    the gross potential (the frame's PV columns are main-string), the load, the
    meter, the pack's DC power and SOC, the Growatt, and the suspect mask as
    the curtailment flag."""
    d = _horizon(df, day, 1)[2]
    return {"pv_w": (d["pv_main_w"] + d["micro_w"]).tolist(), "load_w": d["load_w"].tolist(),
            "grid_w": d["grid_w"].tolist(), "batt_dc_w": d["batt_dc_w"].tolist(), "micro_w": d["micro_w"].tolist(),
            "soc_pct": d["soc_pct"].tolist(), "curtailed": d["suspect"].astype(float).tolist(),
            "pv_peak_w": (d["pv_pot_main_w"] + d["micro_w"]).tolist(),
            "pot_main_w": d["pv_pot_main_w"].tolist()}          # the bridge's own PV, for _bridge_cap


def _bridge_cap(hs: dict, pot_main, capacity_kwh: float = CAPACITY_KWH) -> int:
    """Clamp the plan's commanded battery power per step so the hybrid bridge
    never carries more than BRIDGE_W in either direction next to the day's
    real main-string harvest (ruling 2026-09-11, on the LP's own basis):

        harvest  = max(0, pot_main - pv_curtail_w)              the plan's own curtailment
        b_capped = clip(b, -BRIDGE_W - harvest, BRIDGE_W - harvest)    W, + = discharge

    where b is the command replay_day derives, P_Load - (P_PV - curtail) -
    P_grid, which at the AC node is P_hybrid_inverter minus the harvest, so
    this is exactly the constraint the LP enforced on its own PV
    (inverter_ac_output_max on p_pv - p_pv_curtailment + p_sto): a P lane is
    never capped, an F lane only where its forecast error commanded more than
    the bridge could carry next to the real sun. Main string only, because
    the Growatt is on the gen port and does not cross the bridge. The grid
    absorbs the difference (p_grid_w moves by b - b_capped, so the derived
    command IS the capped one), p_batt_w and cost_eur follow, and the pack
    keeps the energy that did not move: the SOC trajectory shifts by the
    withheld discharge through eta_d and the withheld charge through eta_c,
    held inside the pack's limits the way integrate_soc holds them. Edits hs
    in place; returns the number of capped steps. The bridge is
    objective.P_NOM_INV_KW at call time (hardware.apply moves it for a sweep)."""
    n = hs["n"]
    bridge_w = float(objective.P_NOM_INV_KW) * 1000.0
    pg, pb, soc = list(hs["p_grid_w"]), list(hs["p_batt_w"]), list(hs["soc_pct"])
    capped, shift_kwh = 0, 0.0
    for i in range(n):
        b = hs["p_load_w"][i] - (hs["p_pv_w"][i] - hs["pv_curtail_w"][i]) - pg[i]
        h = max(0.0, float(pot_main[i]) - float(hs["pv_curtail_w"][i]))
        d = b - min(max(b, -bridge_w - h), bridge_w - h)      # + = discharge withheld, - = charge withheld
        if abs(d) > 0.05:
            capped += 1
            pg[i] = round(pg[i] + d, 1)
            pb[i] = round(pb[i] - d, 1)
            shift_kwh += d * STEP_H / 1000.0 / ETA_D if d > 0 else d * STEP_H / 1000.0 * ETA_C
        if shift_kwh:
            s = soc[i] + shift_kwh / capacity_kwh * 100.0
            if s > SOC_MAX * 100.0:
                shift_kwh, s = (SOC_MAX * 100.0 - soc[i]) * capacity_kwh / 100.0, SOC_MAX * 100.0
            elif s < SOC_MIN * 100.0:
                shift_kwh, s = (SOC_MIN * 100.0 - soc[i]) * capacity_kwh / 100.0, SOC_MIN * 100.0
            soc[i] = round(s, 2)
    if capped:
        hs.update(p_grid_w=pg, p_batt_w=pb, soc_pct=soc,
                  cost_eur=[round(step_cost(pg[i], hs["buy"][i], hs["sell"][i]), 5) for i in range(n)])
    return capped


def _settle(rows, doc: dict, day: date, mic, act: dict, cap: bool = True,
            capacity_kwh: float = CAPACITY_KWH) -> tuple:
    """scoring._hindsight_settle line for line, with two changes: replay_day's
    curtailment repair runs on the day's true potential instead of the plan's
    own P_PV (an F lane's p_pv_w is a forecast, not the potential; see the
    module docstring), and the commanded battery is clamped to the 12 kW
    bridge against the true main-string harvest first (_bridge_cap;
    `cap=False` skips it for a comparison run). Returns (status, hs, rep); hs
    and rep None unless ok, hs carrying capped_steps."""
    sl = day_slice(rows, day, TZ, doc["soc_init"])
    if not sl or sl["n"] != expected_steps(day, TZ):
        return "short_slice", None, None
    hs = compact_slice(sl, dict(doc, n_predicted_steps=0, pv_gap_steps=0, rows=rows,
                                optim_status="Optimal", soc_source="hindsight",
                                pv_micro_w=[float(m) for m in mic], micro_cut=None, pv_p50_w=None))
    hs["capped_steps"] = _bridge_cap(hs, act["pot_main_w"], capacity_kwh) if cap else 0
    lam = scoring.lambda_for(hs["sell"], LAMBDA_FRAC)
    rep = scoring.replay_day(hs, act["grid_w"], act["batt_dc_w"], act["pv_w"],
                             capacity_kwh, lam, pv_pot_w=act["pv_peak_w"])
    if rep["status"] == "no_actuals":
        return "no_actuals", None, None
    return "ok", hs, rep


def plan_payload(df, day: date, lane: str, tariff=LIVE_TARIFF):
    """(t0, n, mic, payload) for one lane's plan at 00:00 local of `day`, or
    None when the frame lacks a value the lane needs somewhere in the horizon.
    The PV list is the lane's main-string column as is; the load list is the
    lane's load column minus the Growatt; the pack starts at the measured SOC
    of the first step and is pinned to SOC_FINAL at the horizon end."""
    spec = LANES[lane]
    t0, n, win = _horizon(df, day, spec["days"])
    need = MEASURED + [spec["load"]] + _pv_columns(spec)
    if win[need].isna().any().any():
        return None
    mic = [round(float(v), 1) for v in win["micro_w"]]
    pv = [round(max(0.0, float(p)), 1) for p in _pv_list(win, spec, day)]
    load = [round(float(v) - m, 1) for v, m in zip(win[spec["load"]], mic)]
    buy, sell = tariff_frame([float(v) for v in win["da_eur_kwh"]], **tariff)
    soc_init = round(float(win["soc_pct"].iloc[0]) / 100.0, 4)
    payload = build_payload(t0, n, soc_init, SOC_FINAL, pv, buy, sell)
    payload["load_power_forecast"] = load
    payload["battery_stress_cost"], payload["inverter_stress_cost"] = stress_costs(buy, sell)
    payload.update(rebalance_schedule(None))
    return t0, n, mic, payload


def run_day(df, day: date, lane: str, solver, tariff=LIVE_TARIFF, cap: bool = True,
            capacity_kwh: float | None = None) -> dict:
    """One lane's plan for `day`, solved and settled; the CSV row as a dict.
    `capacity_kwh` is the settled pack's size (default the plant's); the LP's
    own pack is the solver's (hardware.patch_solver for a sweep).

    Every PV column in the frame is MAIN-string only (pv_main_w is the Deye's
    own string sensor, pv_pot_main_w its potential, the fc_pv_*_main_w are
    the first Solcast site less the microinverter's share plus the other
    sites, or the main plant model), so the LP's PV list is the lane's column
    as is and the microinverter leaves the load list only; subtracting
    micro_w from the PV list as well would have handed the LP main minus
    micro and rows_to_gross would only have restored main."""
    capacity_kwh = float(capacity_kwh) if capacity_kwh is not None else float(CAPACITY_KWH)
    out = {"day": day.isoformat(), "lane": lane, "n": _steps(day, LANES[lane]["days"]), "status": "", "message": ""}
    built = plan_payload(df, day, lane, tariff)
    if built is None:
        out["status"] = "no_data"
        return out
    t0, n, mic, payload = built
    t = time.monotonic()
    res = solver("http://library", payload)
    out["seconds"] = round(time.monotonic() - t, 1)
    if not res["ok"]:
        out["status"], out["message"] = "solve_failed", res.get("message", "")
        return out
    rows = res["rows"]
    if len(rows) != n or str(rows[0].get("optim_status")) != "Optimal":
        out["status"] = "not_optimal"
        out["message"] = f"{len(rows)} rows, {rows[0].get('optim_status') if rows else 'none'}"
        return out
    rows = rows_to_gross(restamp(rows, t0, n), mic)
    act = _day_actuals(df, day)
    status, hs, rep = _settle(rows, _doc(t0, n, payload["soc_init"], tariff), day, mic, act, cap, capacity_kwh)
    out["status"] = status
    if status != "ok":
        return out
    out["capped_steps"] = int(hs["capped_steps"])
    out.update(eur=float(rep["eur"]), cash_eur=float(rep["cash"]), soc_term_eur=float(rep["soc_term"]),
               loss_eur=float(rep["loss"]), pv_kwh=round(sum(act["pv_w"]) / 4000.0, 3),
               load_kwh=round(sum(act["load_w"]) / 4000.0, 3),
               curtailed_kwh=round(sum(hs["pv_curtail_w"]) / 4000.0, 3),
               soc_start_pct=float(hs["soc_start_pct"]), soc_end_pct=float(hs["soc_pct"][-1]))
    out.update(_metrics(rep, hs, act, capacity_kwh))
    return out


def _settled_batt_w(hs) -> list:
    """The battery series the settlement charged, W, + = discharge. replay_day
    returns aggregates only (cash, soc_term, loss, eur, peaks, cap_steps) and
    no per-step series, so this is the plan's commanded P_batt from the compact
    slice, the very series replay_day takes for a settled slice. It differs
    from replay_day's row-balance battery only where the 12 kW bridge cap
    binds (up to 133 W on such a step)."""
    return [float(v) for v in hs["p_batt_w"]]


def _grid_without_battery(act: dict, sell) -> list:
    """The measured day with the real battery's AC contribution removed: the
    grid trace the site would have shown with no battery at all, in the same
    difference form replay_day uses, repair included (ruling 2026-09-11):

        G_nobatt = G_meas + B_meas_ac - repair
        repair   = max(0, pv_pot - pv_meas) where sell >= 0, else 0

    The DC-to-AC step is replay_day's own, verbatim: eta x B_dc discharging,
    B_dc / eta charging. The repair is the lane's: where the real array was
    held back because the real pack was full, a battery-less site would have
    exported that energy, so the baseline gets it too or batt_value_eur is
    biased on every curtailed day. On a negative-price step it gets nothing: a
    battery-less site with this inverter would curtail there under the
    zero-export rule, the same lever the lanes have."""
    eta = float(ETA_BRIDGE)
    out = []
    for g, b_dc, pot, pv, s in zip(act["grid_w"], act["batt_dc_w"], act["pv_peak_w"], act["pv_w"], sell):
        b_dc = float(b_dc)
        b_ac = b_dc * eta if b_dc >= 0 else b_dc / eta
        repair = max(0.0, float(pot) - float(pv)) if float(s) >= 0.0 else 0.0
        out.append(float(g) + b_ac - repair)
    return out


def _metrics(rep, hs, act, capacity_kwh: float = CAPACITY_KWH) -> dict:
    """The site's operational metrics per scored day: equivalent full cycles
    (charge plus discharge throughput over twice the capacity), the battery's
    own value against a no-battery accounting baseline (no solve: the measured
    day with the real battery's AC flows removed and the curtailment repair
    applied on non-negative sell steps, see _grid_without_battery, priced in
    the lane's frame), value per discharged kWh (None below 0,05 kWh), and the
    share of steps the settled pack spent at or above 90 % and at or below
    15 %."""
    b = _settled_batt_w(hs)
    dis = sum(max(v, 0.0) for v in b) / 4000.0
    chg = sum(max(-v, 0.0) for v in b) / 4000.0
    soc = [float(v) for v in hs["soc_pct"]]
    nobatt = float(scoring.cash_in_frame(_grid_without_battery(act, hs["sell"]), hs["buy"], hs["sell"]))
    value = float(rep["eur"]) - nobatt
    return {"cycles": round((dis + chg) / (2.0 * capacity_kwh), 4), "discharged_kwh": round(dis, 3),
            "nobatt_eur": round(nobatt, 4), "batt_value_eur": round(value, 4),
            "eur_per_kwh": (round(value / dis, 4) if dis > 0.05 else None),
            "soc_above_90_pct": round(100.0 * sum(1 for v in soc if v >= 90.0) / len(soc), 1),
            "soc_below_15_pct": round(100.0 * sum(1 for v in soc if v <= 15.0) / len(soc), 1)}


def supplier_payout(path: str | None) -> dict:
    """{ISO day: payout_eur} from a supplier's daily table (columns day and
    payout_eur; `date` is accepted for day), money IN to the house; empty
    when no path is given or the file is absent."""
    if not path or not os.path.exists(path):
        return {}
    t = pd.read_csv(path)
    day_col = "day" if "day" in t.columns else "date"
    t = t[t["payout_eur"].notna()]
    return {str(d): float(v) for d, v in zip(t[day_col], t["payout_eur"])}


def run(df, start: str, end: str, lanes, out_csv: str, solver=None, tariff=LIVE_TARIFF, resume=True, cap=True,
        payout=None, capacity_kwh: float | None = None):
    """One row per (day, lane) appended to out_csv as it settles; with resume
    the rows already there are skipped, so an interrupted run picks up where
    it stopped. Every row already in the file counts as done, the non-ok
    ones (no_data, not_optimal, solve_failed, error) included, so a rebuilt
    frame or a changed settlement needs a fresh CSV (resume=False, or delete
    it), not a resume. A day whose run_day raises is written as status
    "error" with the exception's text and the loop goes on. Every row carries
    the day's supplier payout (supplier_payout_eur, from `payout`, a
    {day: EUR} dict; empty where absent or when none is given).
    `capacity_kwh` is the settled pack (see run_day)."""
    from .solver import LibrarySolver, patch_solve      # lazy: keeps this module (and --summary,
    # the stub-solver tests) importable on an interpreter without the emhass library
    solver = solver or LibrarySolver()
    payout = payout or {}
    done = set()
    if resume and os.path.exists(out_csv):
        with open(out_csv, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is not None and reader.fieldnames != FIELDS:
                raise ValueError(f"{out_csv} has header {reader.fieldnames}, expected {FIELDS}; "
                                  "an older file (e.g. without capped_steps) cannot be resumed, "
                                  "rerun with resume=False or delete it")
            done = {(r["day"], r["lane"]) for r in reader}
    with open(out_csv, "a" if resume else "w", newline="") as f, patch_solve(scoring.hindsight_day, solver):
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if f.tell() == 0:
            w.writeheader()
        d, last = date.fromisoformat(start), date.fromisoformat(end)
        while d <= last:
            for lane in lanes:
                if (d.isoformat(), lane) in done:
                    continue
                try:
                    r = run_day(df, d, lane, solver, tariff, cap, capacity_kwh)
                except Exception as e:      # noqa: BLE001 - one bad day must not abort the season
                    r = {"day": d.isoformat(), "lane": lane, "status": "error",
                         "message": f"{type(e).__name__}: {e}"[:300]}
                r["supplier_payout_eur"] = payout.get(d.isoformat())
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})
                f.flush()
                print(f"{r['day']} {lane:<6} {r['status']:<12} {r.get('eur', ''):>9} {r.get('seconds', '')}", flush=True)
            d += timedelta(days=1)


def summarise(out_csv: str, start=None, end=None) -> pd.DataFrame:
    """Per-lane totals over the days EVERY lane settled between `start` and
    `end` (ISO days or dates, both inclusive, None = open), so the lanes are
    compared on one set of days. A lane in the file with no ok day in the
    range does not count toward "every lane": it is dropped from the
    shared-day set and named on stdout, because silently shrinking the set
    to the lanes that happened to settle would change what the table
    compares. The supplier payout over the same shared days sits in the
    table's attrs: `supplier_payout_eur` (money in, the supplier's sign),
    `days` and `days_with_payout`."""
    t = pd.read_csv(out_csv)
    if start is not None:
        t = t[t["day"] >= str(start)]
    if end is not None:
        t = t[t["day"] <= str(end)]
    ok = t[t.status == "ok"]
    dropped = sorted(set(t["lane"]) - set(ok["lane"]))
    if dropped:
        print(f"summarise: no ok day for {', '.join(dropped)}; shared days are over the other lanes")
    days = ok.groupby("day")["lane"].nunique()
    full = days[days == ok["lane"].nunique()].index
    g = ok[ok.day.isin(full)].groupby("lane")
    s = g.agg(days=("eur", "size"), total_eur=("eur", "sum"), mean_eur=("eur", "mean"),
              cash_eur=("cash_eur", "sum"), loss_eur=("loss_eur", "sum"),
              batt_value_eur=("batt_value_eur", "sum"), discharged_kwh=("discharged_kwh", "sum"),
              cycles_per_day=("cycles", "mean"), soc_above_90_pct=("soc_above_90_pct", "mean"),
              soc_below_15_pct=("soc_below_15_pct", "mean"))
    s["eur_per_kwh"] = s["batt_value_eur"] / s["discharged_kwh"].where(s["discharged_kwh"] > 0)
    if "P1" in s.index:
        s["vs_P1_eur"] = s["total_eur"] - s.loc["P1", "total_eur"]
    s = s.round(3)
    pay = pd.to_numeric(t.drop_duplicates("day").set_index("day").get("supplier_payout_eur"), errors="coerce") \
        if "supplier_payout_eur" in t.columns else pd.Series(dtype=float)
    pay = pay.reindex(full)
    s.attrs.update(days=int(len(full)), days_with_payout=int(pay.notna().sum()),
                   supplier_payout_eur=(round(float(pay.sum()), 3) if pay.notna().any() else None),
                   first_day=(str(full.min()) if len(full) else None), last_day=(str(full.max()) if len(full) else None))
    return s


def _eu(v: float, decimals: int = 2) -> str:
    """European number: comma decimal, point thousands ("1.096,85")."""
    return f"{v:,.{decimals}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def plant_days(df) -> dict:
    """{plant value: sorted full local days} from the frame's `plant` column
    (frame_schema.full_local_days per value)."""
    return {p: frame_schema.full_local_days(df, TZ, p) for p in sorted(set(df["plant"]))}


def blocks_for(df=None) -> dict:
    """{block name: (start, end)} for print_blocks, from the frame's plant
    column: `measured plant` from its first full day plus MEASURED_LEAD_DAYS
    (open-ended), `modelled plant` up to the day before the measured plant's
    first full day, only when the frame has modelled rows. Without a frame:
    one open block, everything measured."""
    if df is None or "plant" not in df.columns or set(df["plant"]) <= {"measured"}:
        return {"measured plant": (None, None)}
    days = plant_days(df)
    meas = days.get("measured") or []
    mod = days.get("modelled") or []
    out = {}
    if mod:
        out["modelled plant"] = (None, (meas[0] - timedelta(days=1)) if meas else None)
    if meas:
        out["measured plant"] = (meas[0] + timedelta(days=MEASURED_LEAD_DAYS), None)
    return out


def default_range(df, lanes=None) -> tuple:
    """(start, end) ISO days the lanes run on by default: the measured
    plant's first full day plus MEASURED_LEAD_DAYS, and the last full day
    minus the longest lane's horizon beyond D (P2 needs D+2 inside the
    frame). Raises ValueError when the frame has no full measured day."""
    meas = frame_schema.full_local_days(df, TZ, "measured")
    if not meas:
        raise ValueError("the frame has no full local day with plant = measured and every required column present")
    horizon = max(LANES[l]["days"] for l in (lanes or LANES)) - 1
    start = meas[0] + timedelta(days=MEASURED_LEAD_DAYS)
    end = meas[-1] - timedelta(days=horizon)
    if end < start:
        start = meas[0]
    if end < start:
        raise ValueError(f"the frame's measured days ({meas[0]} .. {meas[-1]}) are too few for a {horizon + 1}-day lane")
    return start.isoformat(), end.isoformat()


def print_blocks(out_csv: str, blocks: dict | None = None):
    """One block per plant (blocks_for), each its own shared-day table with
    the supplier payout over the same days next to it when the CSV carries
    one; never a mixed total."""
    blocks = blocks or {"measured plant": (None, None)}
    with pd.option_context("display.width", 200):
        for name, (start, end) in blocks.items():
            s = summarise(out_csv, start, end)
            a = s.attrs
            if a["days"] == 0:
                print(f"== {name}: no rows\n")
                continue
            print(f"== {name}: {a['days']} shared days {a['first_day']} .. {a['last_day']}"
                  + (" (a what-if: the current plant's model on the old plant's grid trace)" if name.startswith("modelled") else ""))
            print(s.to_string())
            if a["supplier_payout_eur"] is not None:
                print(f"supplier payout over {a['days_with_payout']} of these days: {_eu(a['supplier_payout_eur'])} EUR in, "
                      f"i.e. {_eu(-a['supplier_payout_eur'])} EUR as money out next to total_eur "
                      f"(a supplier's settlement may include aFRR and imbalance volumes the lanes do not model)")
            print()


def statuses(out_csv: str) -> pd.DataFrame:
    """Status counts per lane, every row of the CSV."""
    t = pd.read_csv(out_csv)
    return t.groupby(["lane", "status"]).size().unstack(fill_value=0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="knowledge ladder over a backtest frame")
    ap.add_argument("--start", default=None, help="first local day (default: the frame's measured plant plus a week)")
    ap.add_argument("--end", default=None, help="last local day (default: the frame's last full day minus the horizon)")
    ap.add_argument("--lanes", default=",".join(LANES))
    ap.add_argument("--tariff", choices=sorted(TARIFFS), default="plant")
    ap.add_argument("--frames", default=os.path.join(DATA, "frame.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "runs", "ladder.csv"))
    ap.add_argument("--supplier-daily", default=None, metavar="CSV",
                    help="optional comparison: a supplier's daily payout (columns day,payout_eur, money in)")
    ap.add_argument("--summary", action="store_true", help="read --out and print the block tables, no solving")
    a = ap.parse_args(argv)
    lanes = [x.strip() for x in a.lanes.split(",") if x.strip()]
    unknown = [x for x in lanes if x not in LANES]
    if unknown or not lanes:
        ap.error(f"--lanes: unknown {', '.join(unknown) or '(empty)'}; choose from {', '.join(LANES)}")
    from .frames import load
    if a.summary:
        blocks = blocks_for(load(a.frames)) if os.path.exists(a.frames) else None
        print_blocks(a.out, blocks)
        with pd.option_context("display.width", 200):
            print(statuses(a.out).to_string())
        return
    df = load(a.frames)
    start, end = default_range(df, lanes)
    start, end = a.start or start, a.end or end
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    run(df, start, end, lanes, a.out, tariff=TARIFFS[a.tariff], payout=supplier_payout(a.supplier_daily))


if __name__ == "__main__":
    main()
