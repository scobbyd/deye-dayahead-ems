"""What the LP is told to minimise, and the plant it is told about: the pack constants, the payload, the
quadratic-loss stress pricing, the rebalance schedule, the Growatt cut rule, and the live knob layer."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timezone

from .grid import stamp_z, STEP_H, STEP_MIN, step_times
from .plant import PLANT
from .series import GROWATT_SHARE, PV_P10_MIX


CAPACITY_KWH = float(PLANT["battery"]["capacity_kwh"])   # = config.json battery_nominal_energy_capacity / 1000;


# Counterfactual replay (2026-09-04). The scoreboard used to settle the plan
# lane on the plan's OWN forecasts and the realised lane on the incumbent's
# utility meter, so the headline gap mixed forecast error into plan quality and
# the two lanes could drift apart on tariff as well. replay_day rebuilds the
# plan's battery decisions against the day that actually happened, and the
# realised lane is re-settled in the plan's archived tariff frame, so all four
# lanes share one price vector (spec of 2026-09-04).
ETA_BRIDGE = float(PLANT["inverter"]["eta_bridge"])      # measured Deye DC<->AC bridge efficiency (the site loss model)


GRID_CAP_W = int(PLANT["grid"]["cap_w"])      # = config.json maximum_power_from_grid / _to_grid (3 x 25 A)


METER_DRIFT_EUR = 0.25  # realised cash, plan frame vs utility meter: flag past this


SOC_MIN, SOC_MAX = float(PLANT["battery"]["soc_min"]), float(PLANT["battery"]["soc_max"])   # = config.json battery min/max state of charge


# Fixed horizon-end SOC (2026-09-02): soc_final = soc_init coupled the
# day's end to plan-time drift (a 13:00 plan at 90% forced the far midnight
# back to 90%). A fixed target at the cushion threshold ends every day at
# the natural resting floor, keeps scored days comparable, and blocks the
# end-of-horizon dump below the cushion. Fully free is not an EMHASS option
# (an omitted soc_final falls back to battery_target_state_of_charge).
# 2026-09-02 later: raised to 0,50: mid-band keeps optionality for the
# unseen future beyond the horizon (room to sell an evening spike or buy a
# night dip either way).
SOC_FINAL_TARGET = float(PLANT["battery"]["soc_final_target"])


def build_payload(t0, n, soc_init, soc_final, pv_w, buy, sell) -> dict:
    for name, arr in (("pv_power_forecast", pv_w), ("load_cost_forecast", buy),
                      ("prod_price_forecast", sell)):
        if len(arr) != n:
            raise ValueError(f"{name} has {len(arr)} items, horizon is {n}")
    return {"optimization_time_step": STEP_MIN, "prediction_horizon": n,
            "soc_init": round(float(soc_init), 4), "soc_final": round(float(soc_final), 4),
            "pv_power_forecast": list(pv_w), "load_cost_forecast": list(buy),
            "prod_price_forecast": list(sell)}


def step_cost(p_grid_w: float, buy: float, sell: float) -> float:
    imp, exp = max(p_grid_w, 0.0), max(-p_grid_w, 0.0)
    return (buy * imp - sell * exp) * STEP_H / 1000.0


def plan_cost(rows) -> float:
    """A plan's own cash in its own prices, EUR over its rows."""
    return round(sum(step_cost(float(r["P_grid"]), float(r["unit_load_cost"]), float(r["unit_prod_price"]))
                     for r in rows), 4)


def rows_to_gross(rows: list[dict], micro_w, micro_cut=None) -> list[dict]:
    """Back to GROSS before anything sees these rows, in place. The split is a
    payload detail, not a change of units: EMHASS solved on main-only PV and a
    load net of the must-take half, and putting that half back into BOTH P_PV
    and P_Load restores the site's real quantities while leaving P_PV - P_Load,
    and so the whole balance, exactly as solved. virtual_day, the replay lane,
    the scoreboard and the charts then need no migration and no pre/post-split
    branch. On a CUT step the Growatt was never in the load, so its forecast
    goes into P_PV (available) and P_PV_curtailment (declined) instead:
    available minus declined stays the harvest and every balance downstream
    holds."""
    for k, (r, m) in enumerate(zip(rows, micro_w)):
        r["P_PV"] = round(float(r["P_PV"]) + m, 1)
        if micro_cut is not None and micro_cut[k]:
            r["P_PV_curtailment"] = round(float(r.get("P_PV_curtailment") or 0.0) + m, 1)
        else:
            r["P_Load"] = round(float(r["P_Load"]) + m, 1)
    return rows


def cut_payload(payload: dict, load_gross_w, micro_w, today_set: set) -> dict:
    """The second-pass payload of the Growatt cut: the must-take half leaves
    today's remaining load entries (tomorrow keeps it), everything else as the
    uncut solve had it."""
    out = dict(payload)
    out["load_power_forecast"] = [round(l - (0.0 if k in today_set else m), 1)
                                  for k, (l, m) in enumerate(zip(load_gross_w, micro_w))]
    return out


def restamp(rows: list[dict], t0, n: int) -> list[dict]:
    """naive-mpc-optim anchors its grid at NOW, so a re-solve of a past horizon
    comes back stamped with today's clock. The rows are purely POSITIONAL over
    the runtime lists, so restamp them onto the horizon they were solved for;
    day_slice can then cut the scored day out exactly as it does for the plan
    lane."""
    for k, t in enumerate(step_times(t0, n)):
        rows[k]["timestamp"] = stamp_z(t)
    return rows


# Quadratic-loss pricing, split by stage (2026-09-02). The measured
# arbitrage quad LOSS_QUAD (the site loss model) crosses bridge +
# port on every leg, so it splits: Q_PORT (cells + pack cabling + battery-port
# DC-DC, a function of battery current) goes on battery_stress_cost; Q_BRIDGE
# (DC-AC conduction, a function of AC current) goes on inverter_stress_cost.
# Every path then pays exactly the stages it crosses: PV->battery pays the
# port only (DC-coupled advantage), PV->grid the bridge only, arbitrage both.
# Split estimated 75/25 from the Deye DC-AC efficiency curve plus measured
# pack/cable R; pin it by re-fitting the loss model with two coefficients
# (PV-only high-power hours vs arbitrage hours).
# Per the EMHASS docs the stress unit cost is the penalty per kWh at NOMINAL
# power, quadratic below, so u = (q x P_nom_kW) x price level.
# STAR REWIRE (2026-09-07). Q_PORT was 0,0037 against a daisy-chain port
# of 8,8 mOhm, and port R / V^2 (8,8e-3 / 51,2^2 = 0,00336 kW/kW^2) was nearly
# all of it. The 5 Sep star rewire measured 6,55 mOhm on 38 h of data (CI 5,7
# to 7,8), which is 0,0025 of pure ohmic. Re-pinned conservatively to 0,003,
# with the loss model's loss_quad 0,00483 -> 0,004 in step; the bridge did not
# change. Revisit on the 13 Sep full-window port figure.
Q_PORT = float(PLANT["inverter"]["q_port_kw_per_kw2"])      # kW lost per (kW battery power)^2 (0,0037 before the star rewire)


Q_BRIDGE = float(PLANT["inverter"]["q_bridge_kw_per_kw2"])   # kW lost per (kW AC power)^2; Q_PORT+Q_BRIDGE ~ 0,0042 (was 0,00483)


P_NOM_BATT_KW = float(PLANT["battery"]["p_nom_kw"])   # = config.json battery charge/discharge power max


P_NOM_INV_KW = float(PLANT["inverter"]["p_nom_kw"])    # = config.json inverter_ac_output_max


def stress_costs(buy, sell) -> tuple[float, float]:
    """(battery, inverter) stress unit costs, priced at the horizon's mean
    price level so the knobs follow the tariff regime and the day's prices."""
    if not buy:
        return 0.0, 0.0
    pi = sum((b + s) / 2.0 for b, s in zip(buy, sell)) / len(buy)
    u_batt = max(round(P_NOM_BATT_KW * Q_PORT * pi, 5), 0.0)
    u_inv = max(round(P_NOM_INV_KW * Q_BRIDGE * pi, 5), 0.0)
    return u_batt, u_inv


def loss_adjustment(rows) -> float:
    """EUR the quadratic-loss pricing collects as MONEY in the LP objective but
    the meter pays as ENERGY: the plan's flows read ~optimistic by exactly the
    quadratic loss. Charge it back so planned_eur is comparable with a realised
    day whose meters pay real losses. Each slot's lost kWh is priced at the
    slot's mid price ((buy+sell)/2), the same frame stress_costs uses; the
    linear part of the loss model is already inside the etas and needs nothing."""
    tot = 0.0
    for r in rows:
        pb = float(r.get("P_batt") or 0.0) / 1000.0
        ph = r.get("P_hybrid_inverter")
        if ph is None:
            ph = float(r.get("P_Load") or 0.0) - float(r.get("P_grid") or 0.0)
        ph = float(ph) / 1000.0
        price = (float(r["unit_load_cost"]) + float(r["unit_prod_price"])) / 2.0
        tot += (Q_PORT * pb * pb + Q_BRIDGE * ph * ph) * STEP_H * price
    return round(tot, 4)


# Rebalancing stressor (2026-09-02). EMHASS has no native "days since
# the pack was balanced" state, but every SOC knob is runtime-overridable, so
# run_plan carries the dynamic: fresh after a full charge the high-SOC dwell
# penalty is at full strength; it fades to zero over REBALANCE_TARGET_DAYS;
# past the target an active pull ramps in (deficit threshold 1,0 with a small
# cost, so every kWh below full is charged per hour and the planner books a
# top-up in the cheapest slots). Since 2026-09-15 the clock is the SETTLED
# pack's (rebalance.py): it resets when the virtual pack has sat at or above
# REBALANCE_FULL_LEVEL for REBALANCE_DWELL_H, and nothing on record counts as
# overdue, not as relaxed. The low band gets the same dwell treatment
# statically: DEFICIT_BASE below 20% keeps an overnight cushion priced, not
# fenced (brief dips stay allowed).
SURPLUS_BASE = 0.005            # EUR/kWh/h above battery_soc_surplus_threshold


DEFICIT_BASE = (0.20, 0.01)     # (threshold, EUR/kWh/h) for the 10-20% band


REBALANCE_TARGET_DAYS = 7.0


REBALANCE_PULL = 0.003          # full pull strength at 2x target


REBALANCE_FULL_LEVEL = 0.995


REBALANCE_DWELL_H = 2.0         # hours the settled pack must sit at or above the level for a full to count


def rebalance_schedule(days_since_full, surplus_base: float = SURPLUS_BASE,
                       deficit_threshold: float = DEFICIT_BASE[0], deficit_cost: float = DEFICIT_BASE[1]):
    """Runtime SOC-knob overrides for the rebalancing dynamic. None (no full
    charge on record) counts as OVERDUE, twice the target: the pull at full
    strength, because a pack with no balance on record is assumed to need one
    (2026-09-15; until then None read as exactly the target, relaxed).
    The bases are parameters since 2026-09-07 so the live knob layer can move them."""
    d = 2.0 * REBALANCE_TARGET_DAYS if days_since_full is None else float(days_since_full)
    surplus = round(float(surplus_base) * max(0.0, 1.0 - d / REBALANCE_TARGET_DAYS), 5)
    if d > REBALANCE_TARGET_DAYS:
        ramp = min(1.0, (d - REBALANCE_TARGET_DAYS) / REBALANCE_TARGET_DAYS)
        thr, cost = 1.0, round(REBALANCE_PULL * ramp, 5)
    else:
        thr, cost = float(deficit_threshold), float(deficit_cost)
    return {"battery_soc_surplus_cost": surplus,
            "battery_soc_deficit_threshold": thr,
            "battery_soc_deficit_cost": cost}


# When to switch the Growatt off (2026-09-06, rule fixed 2026-09-07). The
# gen port has one lever and it is binary. The rule is a hysteresis on the
# REMAINING-TODAY figures of an UNCUT solve:
#
#   cut     when planned curtailment >= AUX_CUT_ON_RATIO x remaining Growatt output
#           and at least AUX_CUT_ON_MIN_KWH
#   re-enable when the ratio falls below AUX_CUT_OFF_RATIO OR the remaining
#           curtailment falls below AUX_CUT_OFF_MIN_KWH, whichever comes first
#
# Evaluated on every solve; the state carries over from the newest plan of the
# same day and resets at midnight. It must be evaluated on the uncut solve
# because a cut plan's own curtailment is lower by roughly the Growatt's output,
# which would flip the rule every solve. So a refresh inside the regime solves
# twice: uncut to decide, cut to plan. The cut runs from the solve to 24:00
# today; tomorrow's plans decide tomorrow.
#
# On the 09-05 plan of record the rule would have fired from 11:00 (10,39 kWh
# curtailment against 6,49 kWh of Growatt left, ratio 1,60). The earlier
# advisory feasibility calculation (gain from spill vs loss of stored energy)
# never fired on a plan lane and is retired; the ratio rule was chosen knowing
# it can cost stored energy on a day the pack runs behind, because the settled
# chain re-evaluates it every solve.
AUX_CUT_ON_RATIO = 1.5


AUX_CUT_ON_MIN_KWH = 2.0


AUX_CUT_OFF_RATIO = 1.2


AUX_CUT_OFF_MIN_KWH = 1.0


def aux_cut_decision(curtail_kwh: float, aux_kwh: float, active: bool,
                     on_ratio: float = AUX_CUT_ON_RATIO, on_min_kwh: float = AUX_CUT_ON_MIN_KWH,
                     off_ratio: float = AUX_CUT_OFF_RATIO, off_min_kwh: float = AUX_CUT_OFF_MIN_KWH) -> dict:
    """Cut on / stay cut / re-enable, from the remaining-today planned curtailment
    (an UNCUT solve) and the remaining-today Growatt forecast. No Growatt left
    to cut means not cut, whatever the curtailment. The thresholds are
    parameters so the A/B walk can move them; production uses the constants."""
    c, a = max(0.0, float(curtail_kwh)), max(0.0, float(aux_kwh))
    ratio = (c / a) if a > 0 else (math.inf if c > 0 else 0.0)
    if a <= 0:
        cut = False
    elif not active:
        cut = c >= float(on_min_kwh) and ratio >= float(on_ratio)
    else:
        cut = not (c < float(off_min_kwh) or ratio < float(off_ratio))
    return {"active": cut, "was_active": bool(active), "curtail_kwh": round(c, 3), "aux_kwh": round(a, 3),
            "ratio": (round(ratio, 3) if ratio != math.inf else None),
            "on_ratio": float(on_ratio), "on_min_kwh": float(on_min_kwh),
            "off_ratio": float(off_ratio), "off_min_kwh": float(off_min_kwh)}


#
# Every knob the A/B walk can move is also a helper in HA (2026-09-07: "a
# button that pushes the settings to the live EMS", and one that copies the live
# settings into the tester). The wrapper reads the helpers each solve into
# inp["knobs"]; a missing or unseeded helper falls back to the constant here, so
# the code still carries the defaults. Knobs the add-on only reads at boot
# (capacity, efficiencies, hybrid model, solver) are plant model and stay out.

LIVE_KNOBS = {
    # name: (default, helper entity id)
    "stress_scale": (1.0, "input_number.emhass_stress_scale"),
    "surplus_base": (SURPLUS_BASE, "input_number.emhass_surplus_base"),
    "deficit_threshold": (DEFICIT_BASE[0], "input_number.emhass_deficit_threshold"),
    "deficit_cost": (DEFICIT_BASE[1], "input_number.emhass_deficit_cost"),
    "soc_final": (SOC_FINAL_TARGET, "input_number.emhass_soc_final"),
    "weight_battery_discharge": (0.01, "input_number.emhass_weight_battery_discharge"),
    "batt_power_max_w": (12500.0, "input_number.emhass_batt_power_max_w"),
    "soc_min": (SOC_MIN, "input_number.emhass_soc_min"),
    "soc_max": (SOC_MAX, "input_number.emhass_soc_max"),
    "soc_target": (0.0, "input_number.emhass_soc_target"),                # 0 = off
    "soc_target_at": ("17:00", "input_datetime.emhass_soc_target_at"),
    "aux_cut_on_ratio": (AUX_CUT_ON_RATIO, "input_number.emhass_aux_cut_on_ratio"),
    "aux_cut_on_min_kwh": (AUX_CUT_ON_MIN_KWH, "input_number.emhass_aux_cut_on_min_kwh"),
    "aux_cut_off_ratio": (AUX_CUT_OFF_RATIO, "input_number.emhass_aux_cut_off_ratio"),
    "aux_cut_off_min_kwh": (AUX_CUT_OFF_MIN_KWH, "input_number.emhass_aux_cut_off_min_kwh"),
    "pv_p10_mix": (PV_P10_MIX, "input_number.emhass_pv_p10_mix"),
    "growatt_share": (GROWATT_SHARE, "input_number.emhass_growatt_share"),
    "rebalance_dwell_h": (REBALANCE_DWELL_H, "input_number.emhass_rebalance_dwell_h"),
}


def knobs(inp_knobs: dict | None) -> dict:
    """Defaults overlaid with whatever the wrapper read; a None value keeps the default."""
    out = {k: d for k, (d, _e) in LIVE_KNOBS.items()}
    for k, v in (inp_knobs or {}).items():
        if k in out and v is not None:
            out[k] = v
    return out


def soc_target_timestep(t0: datetime, n: int, day: date, at: str) -> int | None:
    """Index of the wall-clock `at` on `day` inside the horizon from t0, or None
    when it lies at or before t0 or beyond the horizon."""
    try:
        hh, mm = str(at).split(":")[:2]
        target = datetime.combine(day, time(int(hh), int(mm)), tzinfo=t0.tzinfo)
    except (ValueError, AttributeError):
        return None
    k = round((target.astimezone(timezone.utc) - t0.astimezone(timezone.utc)).total_seconds() / (STEP_MIN * 60))
    return k if 0 < k < n else None


def apply_knobs(payload: dict, kn: dict, t0: datetime, n: int, day: date) -> dict:
    """The runtime keys a knob set adds to a solve payload. Stress costs are
    scaled in place (they are price-scaled per solve upstream); the SOC target
    lands only when its time is ahead inside the horizon on `day`."""
    payload["battery_stress_cost"] = round(float(payload.get("battery_stress_cost", 0.0)) * float(kn["stress_scale"]), 5)
    payload["inverter_stress_cost"] = round(float(payload.get("inverter_stress_cost", 0.0)) * float(kn["stress_scale"]), 5)
    payload["soc_final"] = round(float(kn["soc_final"]), 4)
    payload["weight_battery_discharge"] = float(kn["weight_battery_discharge"])
    payload["battery_charge_power_max"] = float(kn["batt_power_max_w"])
    payload["battery_discharge_power_max"] = float(kn["batt_power_max_w"])
    payload["battery_minimum_state_of_charge"] = float(kn["soc_min"])
    payload["battery_maximum_state_of_charge"] = float(kn["soc_max"])
    payload.pop("soc_target", None)
    payload.pop("soc_target_timestep", None)
    if float(kn["soc_target"]) > 0:
        k = soc_target_timestep(t0, n, day, kn["soc_target_at"])
        if k is not None:
            payload["soc_target"] = round(float(kn["soc_target"]), 4)
            payload["soc_target_timestep"] = k
    return payload


def cut_thresholds(kn: dict) -> dict:
    return {"on_ratio": float(kn["aux_cut_on_ratio"]), "on_min_kwh": float(kn["aux_cut_on_min_kwh"]),
            "off_ratio": float(kn["aux_cut_off_ratio"]), "off_min_kwh": float(kn["aux_cut_off_min_kwh"])}
