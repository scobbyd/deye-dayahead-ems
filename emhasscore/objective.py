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
# lanes share one price vector. Spec:
# an internal design note
ETA_BRIDGE = float(PLANT["inverter"]["eta_bridge"])      # = an internal tool eta_bridge (measured Deye DC<->AC)


GRID_CAP_W = int(PLANT["grid"]["cap_w"])      # = config.json maximum_power_from_grid / _to_grid (3 x 25 A)




SOC_MIN, SOC_MAX = float(PLANT["battery"]["soc_min"]), float(PLANT["battery"]["soc_max"])   # = config.json battery min/max state of charge


# = config.json battery_charge_efficiency / battery_discharge_efficiency. The
# virtual pack integrates through these on every settled step (settle_slice,
# virtual_day, ab_walk), so like CAPACITY_KWH they must track what the LP plans
# on, or the planned and the settled lanes model a different pack. Until
# 2026-09-07 all three carried them as signature literals. They live here, not
# in deye.py, because batt_power_limits needs them and deye imports this module.
ETA_C = float(PLANT["battery"]["eta_charge"])
ETA_D = float(PLANT["battery"]["eta_discharge"])

# LOSS MAP V2 (an internal design note, adoption spec
# an internal design note). Behind the physics_v2
# knob, posted as runtime keys every solve (the associations table carries
# all of them; config.json is not touched). EMHASS topology: P_batt is the
# DC BUS. The battery block (these etas) holds the Deye DC-DC stage plus the
# cells; the bridge holds only the DC/AC conversion. Linear parts only (1 - a); the
# quadratic parts are the stress costs (Q_PORT, Q_BRIDGE).
ETA_DC_AC_V2 = float(PLANT["inverter"]["eta_v2"]["dc_ac"])          # bridge inverting, a 1,86 %
ETA_AC_DC_V2 = float(PLANT["inverter"]["eta_v2"]["ac_dc"])          # bridge rectifying (AC -> port 95,1 % at 11,2 kW minus DC-DC)
ETA_C_V2 = float(PLANT["inverter"]["eta_v2"]["charge"])              # DC-DC charge a 1,92 % + battery side 0,37 %
ETA_D_V2 = float(PLANT["inverter"]["eta_v2"]["discharge"])              # DC-DC discharge a 1,93 % + battery side 0,37 %
GRID_CHARGE_AC_MAX_W = float(PLANT["inverter"]["grid_charge_ac_max_w"])   # grid-only charge: cells 10,92 kW = 11.300 x 0,990 x 0,977
STANDBY_LOAD_W = float(PLANT["inverter"]["standby_load_w"])         # the Deye's own draw the UPS-load register does not see (~110 W - 22 W bias)

# The battery side, cells <-> port (cable 2,55 mOhm + R_eff/3 + hysteresis):
# loss_kW = A * P + B * P^2 with P the port power in kW.
BATT_SIDE_A = float(PLANT["battery"]["side_loss_a"])
BATT_SIDE_B_KW = float(PLANT["battery"]["side_loss_b_kw"])


def cells_to_port_w(p_cells_w: float) -> float:
    """The port power that moves `p_cells_w` through the cells (+ discharges).
    Charge: port - loss(port) = cells. Discharge: port + loss(port) = cells.
    Past the charge parabola's vertex the vertex itself is returned."""
    c = abs(float(p_cells_w)) / 1000.0
    if c == 0.0:
        return 0.0
    a, b = BATT_SIDE_A, BATT_SIDE_B_KW
    if p_cells_w < 0:
        disc = (1.0 - a) ** 2 - 4.0 * b * c
        port = ((1.0 - a) - max(disc, 0.0) ** 0.5) / (2.0 * b)
        return -round(port * 1000.0, 1)
    port = (-(1.0 + a) + ((1.0 + a) ** 2 + 4.0 * b * c) ** 0.5) / (2.0 * b)
    return round(port * 1000.0, 1)


def bus_to_port_w(p_batt_w: float, eta_c: float, eta_d: float) -> float:
    """The port power for a DC-bus P_batt: the cells take P_batt x eta_c on a
    charge and give P_batt / eta_d on a discharge (EMHASS's own SOC model), and
    the port is the cells through the battery side."""
    p = float(p_batt_w)
    return cells_to_port_w(p * eta_c if p < 0 else p / eta_d)


def batt_power_limits(port_w: float, eta_d: float = ETA_D, v2: bool = False) -> tuple[float, float]:
    """The knob's pack PORT power as EMHASS's (charge_max, discharge_max).

    THE KNOB IS WHAT THE PORT DOES (2026-09-29). The add-on bounds the
    bus discharge at eff_dis * discharge_max and the bus charge at charge_max
    itself (the 21:43 and 21:58 plans of 09-29).
    v1: P_batt is read as the port: discharge through eta_d, charge raw.
    v2: P_batt is the DC bus, the writer converts it to the port
    (bus_to_port_w): charge_max is the bus power whose cells take
    port(1 - A - B port); discharge_max is the cell power that gives the port."""
    w = float(port_w)
    if not v2:
        return round(w, 1), round(w / float(eta_d), 1)
    p = w / 1000.0
    cells_c = p * (1.0 - BATT_SIDE_A - BATT_SIDE_B_KW * p)
    cells_d = p * (1.0 + BATT_SIDE_A + BATT_SIDE_B_KW * p)
    return round(cells_c / ETA_C_V2 * 1000.0, 1), round(cells_d * 1000.0, 1)


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
# arbitrage quad LOSS_QUAD (an internal tool) crosses bridge +
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
# with casa.yaml's loss_quad 0,00483 -> 0,004 in step; the bridge did not
# change. Revisit on the 13 Sep full-window port figure.
Q_PORT = float(PLANT["inverter"]["q_port_kw_per_kw2"])     # kW lost per (kW battery power)^2; loss map v2 (2026-09-30): DC-DC b 0,0021 + battery side 0,0021


Q_BRIDGE = float(PLANT["inverter"]["q_bridge_kw_per_kw2"])   # kW lost per (kW AC power)^2; bridge b, inverting 0,0002, rectifying ~0,001


P_NOM_BATT_KW = float(PLANT["battery"]["p_nom_kw"])   # = config.json battery charge/discharge power max


P_NOM_INV_KW = float(PLANT["inverter"]["p_nom_kw"])    # = config.json inverter_ac_output_max


def stress_costs(buy, sell, p_nom_batt_kw: float = P_NOM_BATT_KW) -> tuple[float, float]:
    """(battery, inverter) stress unit costs, priced at the horizon's mean
    price level so the knobs follow the tariff regime and the day's prices.
    The add-on's battery nominal is max(charge_max, discharge_max) of the
    POSTED limits, so the caller passes that (12,5 was 4 % off)."""
    if not buy:
        return 0.0, 0.0
    pi = sum((b + s) / 2.0 for b, s in zip(buy, sell)) / len(buy)
    u_batt = max(round(float(p_nom_batt_kw) * Q_PORT * pi, 5), 0.0)
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
# penalty is at full strength; it goes off at REBALANCE_SURPLUS_OFF_DAY; the
# horizon end aims at full from REBALANCE_SOC_FINAL_DAY; from REBALANCE_PULL_DAY
# an active pull steps in (deficit threshold 1,0 with a small cost, so every
# kWh below full is charged per hour and the planner books a top-up in the
# cheapest slots). The clock (rebalance.py, 2026-10-03) is the age of the
# newest REBALANCE_BUDGET_H hours of counted time at the top (bank voltage at
# or above REBALANCE_TOP_V, stretches of REBALANCE_DWELL_H and longer), and
# nothing on record counts as overdue, not as relaxed. The low band gets the same dwell treatment
# statically: DEFICIT_BASE below 20% keeps an overnight cushion priced, not
# fenced (brief dips stay allowed).
SURPLUS_BASE = 0.005            # EUR/kWh/h above battery_soc_surplus_threshold


SURPLUS_THRESHOLD = 0.85        # config.json's value; a live knob since 2026-09-27


DEFICIT_BASE = (0.20, 0.01)     # (threshold, EUR/kWh/h) for the 10-20% band


# THE SCHEDULE IN WHOLE DAYS (2026-10-02, evening). Day 0 to 7: the surplus
# cost at full strength, the pack is not held at the top. From day 7: the
# surplus cost is off. From day 12: the horizon-end SOC is full. From day 14:
# the pull below 100 %, 0,1 ct a day (0,001 EUR/kWh/h on day 14, 0,002 on day
# 15, up to REBALANCE_PULL). Never a fraction of a step: the continuous ramps
# passed through 0,00001 EUR/kWh/h, which EMHASS turns into a 2,5e-9 per Wh
# coefficient beside a 48.200 Wh pack, and the 17:13 (surplus) and 17:58
# (deficit) solves of 2 October hit the 120 s limit, MIP and LP retry both.
REBALANCE_SURPLUS_OFF_DAY = 7.0
REBALANCE_SOC_FINAL_DAY = 12.0
REBALANCE_PULL_DAY = 14.0


REBALANCE_PULL = 0.007          # the pull's ceiling, day 20 on (0,003 until 2026-10-02, the site)


REBALANCE_PULL_PER_DAY = 0.001


REBALANCE_FULL_LEVEL = 0.995    # "at the top" for a pack known only by its SOC (the replay's virtual pack)


# WHAT COUNTS AS TIME AT THE TOP (2026-10-03). The PACE packs bleed their
# high cells only while the cells sit in the knee, so the live clock counts the
# BANK VOLTAGE, not the SOC: pack 3 reported 99,5 % on 3 days of a month while it
# sat at the same top voltage as packs 1/2 (its full-charge voltage is set
# higher), and the inverter SOC follows the mean. 56,0 V is 3,50 V a cell; the
# holds float at ~56,6 V, and 55,2 V (3,45 V) matched "every pack's max cell at
# or above 3,45 V" to 5 of 2.742 minutes over 09-02..10-02 (VM, 15 s).
REBALANCE_TOP_V = 56.0


REBALANCE_DWELL_H = 1.0         # a stretch at the top counts only from this long on (the hysteresis)


REBALANCE_BUDGET_H = 8.0        # the clock is the age of the newest BUDGET hours of counted top time


REBALANCE_LOOKBACK_D = 30.0     # older stretches are dropped; nothing inside it reads as overdue


# THE PULL LATCHES (2026-10-03). Once the clock reaches REBALANCE_PULL_DAY the
# pull stays on until the pack has had REBALANCE_RELEASE_H of counted top time
# inside the last REBALANCE_RELEASE_WINDOW_H, "mostly" a full balance. Without
# it the pull let go as soon as a short hold plus older stretches made up the
# budget, and the clock was back at day 12-14 a few days later.
REBALANCE_RELEASE_H = 6.0


REBALANCE_RELEASE_WINDOW_H = 48.0


def rebalance_clock_days(days_since_full) -> float:
    """The clock as the schedule reads it. None (no full charge on record)
    counts as OVERDUE with the pull at its ceiling, because a pack with no
    balance on record is assumed to need one (2026-09-15)."""
    if days_since_full is None:
        return REBALANCE_PULL_DAY + REBALANCE_PULL / REBALANCE_PULL_PER_DAY
    return float(days_since_full)


def rebalance_schedule(days_since_full, surplus_base: float = SURPLUS_BASE,
                       deficit_threshold: float = DEFICIT_BASE[0], deficit_cost: float = DEFICIT_BASE[1]):
    """Runtime SOC-knob overrides for the rebalancing dynamic (the whole-day
    schedule above). The bases are parameters since 2026-09-07 so the live
    knob layer can move them."""
    d = rebalance_clock_days(days_since_full)
    surplus = float(surplus_base) if d < REBALANCE_SURPLUS_OFF_DAY else 0.0
    if d >= REBALANCE_PULL_DAY:
        pull_days = math.floor(d - REBALANCE_PULL_DAY) + 1
        thr, cost = 1.0, round(min(REBALANCE_PULL, REBALANCE_PULL_PER_DAY * pull_days), 5)
    else:
        thr, cost = float(deficit_threshold), float(deficit_cost)
    return {"battery_soc_surplus_cost": surplus,
            "battery_soc_deficit_threshold": thr,
            "battery_soc_deficit_cost": cost}


# Overdue end-of-horizon target (2026-10-02): from REBALANCE_SOC_FINAL_DAY
# on, the plan's horizon-end SOC (soc_final, normally the 50 % knob) becomes
# full, capped by the soc_max knob. The horizon always ends at 24:00 tomorrow,
# so the target is at least a day away and stays reachable. Summer days rarely
# need it, because surplus fills the pack once the surplus cost is off; in
# winter, with flatter prices and little sun, it makes the planner buy the
# top-up, two days before the pull starts.
REBALANCE_SOC_FINAL = 1.0
# Off 2026-10-02 18:10, back on the same evening: the first live overdue solve hit the add-on's
# time limit. The end target was not the cause: the same payload at soc_final
# 0,5 fails too, and at 1,0 it solves in 1,4 s once the 1e-5 pull is gone (the
# whole-day schedule above). Kept as a switch.
REBALANCE_SOC_FINAL_ON = True


def rebalance_soc_final(days_since_full, soc_final: float, soc_max: float = 1.0) -> float:
    """Horizon-end SOC for the rebalancing dynamic: REBALANCE_SOC_FINAL (capped
    by soc_max) once the clock reaches REBALANCE_SOC_FINAL_DAY, else soc_final
    unchanged. None (no full on record) counts as overdue, like the schedule."""
    if not REBALANCE_SOC_FINAL_ON:
        return float(soc_final)
    if rebalance_clock_days(days_since_full) >= REBALANCE_SOC_FINAL_DAY:
        return round(min(REBALANCE_SOC_FINAL, float(soc_max)), 4)
    return float(soc_final)


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
# never fired on a plan lane and is retired; the site chose the ratio rule knowing
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
    # Absolute stress unit costs in ct/kWh AT NOMINAL POWER (2026-09-26).
    # Set, they replace the loss-map figures stress_costs() derives (0,61 and
    # 0,23 ct on a 16 ct day), because the LP hops between piecewise segments
    # on a flat midday when the marginal stress is smaller than the
    # quarter-to-quarter price step, and every hop is an intent change the
    # writer pays for in held steps and register writes. The EMHASS docs put
    # the inverter figure at 5 to 20 ct for "low and slow"; 1 ct is the
    # starting point. None or 0 keeps the derived figure; stress_scale
    # multiplies either.
    "battery_stress_ct": (None, "input_number.emhass_battery_stress_ct"),
    "inverter_stress_ct": (None, "input_number.emhass_inverter_stress_ct"),
    "surplus_base": (SURPLUS_BASE, "input_number.emhass_surplus_base"),
    "surplus_threshold": (SURPLUS_THRESHOLD, "input_number.emhass_surplus_threshold"),
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
    # Loss map v2 (2026-09-30): the measured plant on the runtime payload and
    # the writer's bus -> port conversion. 0 = the June model. Archived with
    # every plan, so the writer compiles each step with its own plan's physics.
    "physics_v2": (0.0, "input_boolean.emhass_physics_v2"),
    # Temperature-scheduled conservatism (spec H, 2026-09-30): above the
    # ramp start the hurdle and the battery stress scale rise per degree, up to the
    # ramp end. The input is sensor.emhass_pack_temp_used, the 1 h mean of the
    # Solarman pack temperature latched with a deadband (the wrapper holds the
    # latch), so a charge's ~5 °C swing does not re-shape every re-plan.
    "pack_temp_used": (None, "sensor.emhass_pack_temp_used"),
    "temp_ramp_start_c": (35.0, "input_number.emhass_temp_ramp_start_c"),
    "temp_ramp_end_c": (40.0, "input_number.emhass_temp_ramp_end_c"),
    "temp_hurdle_slope": (0.01, "input_number.emhass_temp_hurdle_slope"),     # EUR/kWh per °C
    "temp_stress_slope": (0.5, "input_number.emhass_temp_stress_slope"),     # battery stress_scale per °C (the inverter term keeps the base scale)
    "temp_deadband_c": (2.0, "input_number.emhass_temp_deadband_c"),
}


def knobs(inp_knobs: dict | None) -> dict:
    """Defaults overlaid with whatever the wrapper read; a None value keeps the default."""
    out = {k: d for k, (d, _e) in LIVE_KNOBS.items()}
    for k, v in (inp_knobs or {}).items():
        if k in out and v is not None:
            out[k] = v
    return out


def plan_etas(doc: dict | None) -> tuple[float, float]:
    """(eta_c, eta_d) a plan was solved on: the v2 payload posts them; an
    older plan used the June model."""
    pay = (doc or {}).get("payload") or {}
    return (float(pay.get("battery_charge_efficiency", ETA_C)),
            float(pay.get("battery_discharge_efficiency", ETA_D)))


def is_v2(kn: dict | None) -> bool:
    """A plan's physics: a missing or None flag (plans archived before
    2026-09-30) is the June model."""
    v = (kn or {}).get("physics_v2")
    return bool(v) and float(v) > 0.5


def temp_latch(prev: float | None, mean: float | None, band: float) -> float | None:
    """The latched pack temperature: it follows the mean only once the mean
    has moved `band` or more. A missing mean keeps the latch."""
    if mean is None:
        return prev
    if prev is None or abs(float(mean) - float(prev)) >= float(band) - 1e-9:   # 35,4 - 33,4 is 1,999... in floats
        return round(float(mean), 1)
    return prev


def temp_ramp(t_used: float | None, start: float, end: float) -> float:
    """Degrees above the ramp start, clipped to the ramp; 0 without a
    temperature or with end <= start."""
    if t_used is None or float(end) <= float(start):
        return 0.0
    return min(max(float(t_used) - float(start), 0.0), float(end) - float(start))


def conservatism(kn: dict) -> tuple[float, float, float]:
    """(weight_battery_discharge EUR/kWh, battery stress scale, inverter stress
    scale) with the temperature ramp on top of the base knobs. The hurdle and
    the BATTERY stress scale rise with the ramp (pack heat). The inverter
    stress scale is the base stress_scale only: that term also taxes PV
    export, which does not heat the pack (2026-09-30)."""
    d = temp_ramp(kn.get("pack_temp_used"), kn["temp_ramp_start_c"], kn["temp_ramp_end_c"])
    return (round(float(kn["weight_battery_discharge"]) + float(kn["temp_hurdle_slope"]) * d, 5),
            round(float(kn["stress_scale"]) + float(kn["temp_stress_slope"]) * d, 4),
            round(float(kn["stress_scale"]), 4))


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


# Spec F: +1 pp on the SOC target so the helper means "at least"; only the
# calibration error is left under v2.
SOC_TARGET_MARGIN_V2 = 0.01


# THE SOLVER'S OPTIMALITY GAP RIDES ON EVERY SOLVE (2026-09-26). The add-on's
# default lp_solver_mip_rel_gap is 0,01: on a ~30 EUR objective that is
# 0,30 EUR of slack, while the whole flattening gain the stress cost buys on
# a flat midday is ~0,04 EUR, so HiGHS returned whichever vertex it reached
# first and the 27th's plan hopped between 8,75 kW grid charges and
# solar-only steps differently on every solve. The key in config.json is not
# honoured by the add-on (the file said 1e-5, /get-config kept 0,01); as a
# runtime parameter it passes through the associations table. 0,2 % is
# ~0,06 EUR of slack (2026-09-26, after 1e-5 solved in 22 s against
# 3 s at 1 %). Solve time is the price: watch `seconds`.
LP_MIP_REL_GAP = 0.001


def soc_target_level(level: float, kn: dict) -> float:
    """The SOC target a plan posts for a helper value: v1 as is, v2 with the
    margin, capped at soc_max."""
    if is_v2(kn):
        return min(level + SOC_TARGET_MARGIN_V2, float(kn["soc_max"]))
    return level


def apply_knobs(payload: dict, kn: dict, t0: datetime, n: int, day: date) -> dict:
    """The runtime keys a knob set adds to a solve payload. Stress costs are
    scaled in place (they are price-scaled per solve upstream); the SOC target
    lands only when its time is ahead inside the horizon on `day`."""
    payload["lp_solver_mip_rel_gap"] = LP_MIP_REL_GAP
    v2 = is_v2(kn)
    weight, scale_b, scale_i = conservatism(kn)
    for key, knob, scale in (("battery_stress_cost", "battery_stress_ct", scale_b),
                             ("inverter_stress_cost", "inverter_stress_ct", scale_i)):
        ct = kn.get(knob)
        base = float(ct) / 100.0 if ct is not None and float(ct) > 0 else float(payload.get(key, 0.0))
        payload[key] = round(base * scale, 5)
    payload["soc_final"] = round(float(kn["soc_final"]), 4)
    payload["weight_battery_discharge"] = weight
    apply_plant(payload, kn)
    payload["battery_minimum_state_of_charge"] = float(kn["soc_min"])
    payload["battery_maximum_state_of_charge"] = float(kn["soc_max"])
    payload.pop("soc_target", None)
    payload.pop("soc_target_timestep", None)
    if float(kn["soc_target"]) > 0:
        k = soc_target_timestep(t0, n, day, kn["soc_target_at"])
        if k is not None:
            payload["soc_target"] = round(soc_target_level(float(kn["soc_target"]), kn), 4)
            payload["soc_target_timestep"] = k
    return payload


PLANT_KEYS = ("inverter_efficiency_dc_ac", "inverter_efficiency_ac_dc", "battery_charge_efficiency",
              "battery_discharge_efficiency", "inverter_ac_input_max")


def apply_plant(payload: dict, kn: dict) -> dict:
    """Post the physics layer of `kn` on a payload: the power limits from the
    knob, and under v2 the measured plant keys (the v1 payload carries none,
    so they are removed). One place for the live solve and the A/B tester."""
    v2 = is_v2(kn)
    payload["battery_charge_power_max"], payload["battery_discharge_power_max"] = \
        batt_power_limits(kn["batt_power_max_w"], v2=v2)
    for k in PLANT_KEYS:
        payload.pop(k, None)
    if v2:
        payload.update(inverter_efficiency_dc_ac=ETA_DC_AC_V2, inverter_efficiency_ac_dc=ETA_AC_DC_V2,
                       battery_charge_efficiency=ETA_C_V2, battery_discharge_efficiency=ETA_D_V2,
                       inverter_ac_input_max=GRID_CHARGE_AC_MAX_W)
    return payload


def cut_thresholds(kn: dict) -> dict:
    return {"on_ratio": float(kn["aux_cut_on_ratio"]), "on_min_kwh": float(kn["aux_cut_on_min_kwh"]),
            "off_ratio": float(kn["aux_cut_off_ratio"]), "off_min_kwh": float(kn["aux_cut_off_min_kwh"])}
