"""The Deye as an actuator: a plan step compiled into the register vocabulary, the virtual inverter's
response, and the closed-loop settlement built as the delta between the two."""
from __future__ import annotations

from .grid import STEP_H
from .objective import CAPACITY_KWH, ETA_C, ETA_D, SOC_MAX, SOC_MIN  # noqa: F401  (ETA_* re-exported)
from .plant import PLANT


#
# The inverter takes NO battery power setpoint (see the reference_deye_native_ems
# note). Steering is indirect: work mode, grid charging, and the current clamps.
# So a plan step has to be COMPILED into a register record, and what the inverter
# then does has to be MODELLED rather than assumed. deye_command does the first,
# deye_response the second.
#
# Checked against prior art 2026-09-06: kellerza/sunsynk, the mature Deye/Sunsynk
# stack, steers exactly this way - its automations hold the pack back with a 1 A
# charge clamp and charge hard with 75 A, and it exposes the same three work
# modes. It documents no response model and no TOU slot semantics, so there was
# nothing to adopt; this is convergent, not copied.
#
# Nothing here writes to the inverter. The command record is the seam a future
# writer plugs into, which is why the safety tier travels ON the record.

DEYE_CURRENT_MAX_A = float(PLANT["battery"]["current_max_a"])      # ~ config.json battery power max 12.500 W at nominal V (244 A); the
                                # register's own ceiling, 212 W below the LP's, is the smaller of the two


DEYE_CURRENT_STEP_A = 1.0       # the register's own resolution, about 51 W at 51,2 V


DEYE_GRID_DEADBAND_W = 100.0    # below this the plan's grid figure is a consequence, not a goal


DEYE_BATT_DEADBAND_W = 100.0


DEYE_PACK_V = float(PLANT["battery"]["pack_voltage_v"])              # nominal; callers pass the measured voltage


# The insurance margin on a zero-export charge clamp (2026-09-08): need plus
# 20 % or plus 20 A, whichever is BIGGER, so the pack still fills when an oven,
# the AC or a cloud bank eats into the afternoon. On a surplus day charging a
# little fast costs nothing; ending it short costs evening sales.
DEYE_CLAMP_MARGIN_FRAC = 0.20


DEYE_CLAMP_MARGIN_A = 20.0


# The writer's deadband on the charge clamp: the register is rewritten only when
# the wanted value has moved by more than this from what is standing. 20 A is
# about 1 kW at pack voltage, the same size as the margin's floor, so a plateau
# that drifts by less than the insurance it already carries costs no write.
DEYE_CLAMP_DEADBAND_A = 20.0


# The writer's deadband on the DISCHARGE clamp (2026-09-26). The amps come
# from watts over the MEASURED pack voltage, which sags under load: the first
# live sale wrote 136 A at 12:00 and 141 A at 12:15 for the same 7,4 kW. 5 A
# is about 250 W, inside the meter noise the sun already makes, and absorbs
# that jitter and the offset creep between re-plans. A move to or from the
# 240 A baseline is always far outside it.
# 20 A since 2026-10-02 ("20 A everywhere seems prudent for now"), the
# same as the charge clamp and the grid charge: ~1 kW of tracking against a
# few writes a day (5 live days walked: 425 -> 411 writes, at most 0,25 EUR a
# day either way). A move to or from 0 A is always written (writer_diff), so
# a small self-supply still starts and stops.
DEYE_DISCHARGE_DEADBAND_A = 20.0


# The writer's deadband on the GRID CHARGING CURRENT (handoff 2026-09-26): a
# grid charge steps its current nearly every quarter hour for six hours while
# the plan shapes the import. A move to or from the 0 A baseline is always
# written whatever the value (the setpoint must land); see setpoint_write.
# 20 A (2026-09-30: "1 kW steps is perfectly fine"): at 0 A a 1 A drift
# of the loaded voltage cost a write (238 -> 237 A at 12:45 that day).
DEYE_GRID_CHARGE_DEADBAND_A = 20.0


# The writer's quantisation step on the three current registers (handoff
# 2026-09-26): a slowly ramping plan settles on one value per segment instead
# of a new one per quarter. 1 A is the register's own resolution (no change).
# UP for a charge that must land (the grid charge and the clamp that caps it),
# DOWN where the plan is a ceiling (the discharge clamp, the curtailed
# plateau's clamp, which carries the margin). The site picks the value.
DEYE_CURRENT_QUANT_A = 1.0


# THE REGISTERS DELIVER LESS THAN THEY ASK, AND NOT BY A FIXED 3 A (refit
# 2026-09-29 on 121 binding intervals, 09-26 12:00 to 09-29 22:00; the first
# trials had read a flat 3 A off four points). Per field:
#   discharge clamp: the shortfall grows ~0,030 A per A up to ~150 A, then
#                    holds at ~4,3 A (43 -> 42,3, 100 -> 97,2, 141 -> 136,9,
#                    221 -> 216,5, 240 -> 236,2); night RMS 0,19 A. Sun adds
#                    ~0,55 A per kW of PV, not modelled.
#   grid charging:   proportional, 0,951 (92 -> 87,2, 240 -> 226,7); RMS 0,68 A.
#   charge clamp:    ~1 A on three clean points; kept a constant until more exist.
# The writer asks for the register value that delivers the plan's current
# (writer.py calibrate_amps inverts deye_delivered_a); the twin keeps modelling
# asked = delivered, because the settlement has to reproduce the plan on the
# plan's own inputs.
DEYE_DISCHARGE_SHORT_SLOPE = float(PLANT["registers"]["discharge_short_slope"])
DEYE_DISCHARGE_SHORT_KNEE_A = float(PLANT["registers"]["discharge_short_knee_a"])
DEYE_DISCHARGE_SHORT_ICPT_A = float(PLANT["registers"]["discharge_short_icpt_a"])
DEYE_GRID_CHARGE_GAIN = float(PLANT["registers"]["grid_charge_gain"])
DEYE_CHARGE_SHORT_A = float(PLANT["registers"]["charge_short_a"])


def deye_delivered_a(field: str, reg_a: float) -> float:
    """The current the pack actually moves for register value `reg_a` on `field`."""
    reg = float(reg_a)
    if reg <= 0.0:
        return 0.0
    if field == "battery_max_discharging_current":
        short = DEYE_DISCHARGE_SHORT_SLOPE * min(reg, DEYE_DISCHARGE_SHORT_KNEE_A) - DEYE_DISCHARGE_SHORT_ICPT_A
        return reg - max(0.0, short)
    if field == "battery_grid_charging_current":
        return reg * DEYE_GRID_CHARGE_GAIN
    return max(0.0, reg - DEYE_CHARGE_SHORT_A)


# THE PORT VOLTAGE SAGS UNDER LOAD (measured 2026-09-29): 53,46 -> 52,44 V
# stepping 1 -> 235 A, 51,63 -> 52,67 V stepping 236 -> 14 A, so pack plus
# cable read ~4,6 mOhm. The writer ticks with the pack often at rest (a hold,
# a flip of intent), and amps from the resting voltage undershoot the plan by
# ~2 % at full power: 11.484 W asked at 52,7 V gave 218 A, which the loaded
# 51,6 V turns into 11,25 kW. loaded_voltage solves V = V_oc -/+ R * I for the
# step's own power. Carried separately from the internal R instrument
# (battery_calc), which tracks temperature at ~3 %/degC; a constant is ~1 A.
DEYE_PACK_R_OHM = float(PLANT["battery"]["r_ohm"])


def loaded_voltage(v_oc: float, p_batt_w: float, r_ohm: float = DEYE_PACK_R_OHM) -> float:
    """Port voltage while the pack moves `p_batt_w` (plan sign: + discharges)
    from open-circuit `v_oc`. Discharge: V = V_oc - R*I with P = V*I; charge:
    V = V_oc + R*I."""
    v, p, r = float(v_oc), float(p_batt_w), float(r_ohm)
    if r <= 0.0 or p == 0.0 or v <= 0.0:
        return v
    if p > 0.0:
        disc = v * v - 4.0 * r * p
        if disc <= 0.0:
            return v / 2.0                      # past the maximum-power point; never reached at 12 kW
        i = (v - disc ** 0.5) / (2.0 * r)
        return v - r * i
    i = (-v + (v * v + 4.0 * r * -p) ** 0.5) / (2.0 * r)
    return v + r * i


# THE SETTLEMENT EMULATES THE WRITER (2026-09-12). Every settled lane
# (rolled_slices, the ladder's actual rung, the closed replay) runs the
# actual step under the writer's insurance margin, so surplus sun above an
# LP-curtailed plan fills the pack instead of being declined. Without it the
# pack only ever gets the plan's plateau, every half-hourly re-solve starts
# from a pack that is behind, and the plateau climbs through the morning;
# with it the pack runs ahead, the plateau falls, and the clamp tapers down
# in deadband steps (the 2 August 2026 replay probe). settle_step keeps the
# base run without the margin, so the plan is still reproduced on its own
# inputs. The A/B tester can still override it per lane.
SETTLE_MARGIN = True


def clamp_write(standing_a: float | None, wanted_a: float,
                deadband_a: float = DEYE_CLAMP_DEADBAND_A, lift: bool = False) -> tuple[float, bool]:
    """(value now standing, whether a write happened). Nothing standing yet is
    always a write; otherwise the standing value is kept inside the deadband
    (a move of exactly the deadband is written). `lift` is the grid-charge
    rule: a standing clamp below the wanted grid-charge current is always
    raised, because the register caps the grid charge as well and a stale
    clamp would starve a charge the plan pays for."""
    if standing_a is None or abs(float(wanted_a) - float(standing_a)) >= float(deadband_a):
        return float(wanted_a), True
    if lift and float(standing_a) < float(wanted_a):
        return float(wanted_a), True
    return float(standing_a), False


def setpoint_write(standing_a: float | None, wanted_a: float, deadband_a: float,
                   baseline: float = 0.0, top: float | None = None) -> tuple[float, bool]:
    """clamp_write for a register that is a SETPOINT rather than a ceiling: a
    move to or from the baseline is always written (0 A to 15 A is inside a
    20 A deadband but starves a grid charge the plan pays for), and so is a
    move TO `top` (the nameplate a full-power step asks for, 2026-10-02:
    225 A standing under a 240 A sale is ~0,8 kW short at the best price);
    any other move, a move down from `top` too, goes through the deadband
    (2026-10-03: a standing nameplate errs toward selling at full power;
    the forced move down cost one write a week and tracked no better)."""
    if standing_a is None:
        return float(wanted_a), True
    s, w = float(standing_a), float(wanted_a)
    if abs(s - w) < 0.5:
        return s, False                          # the register resolves to 1 A: nothing to write
    if (abs(s - float(baseline)) < 0.5) != (abs(w - float(baseline)) < 0.5):
        return w, True
    if top is not None and abs(w - float(top)) < 0.5:
        return w, True
    return clamp_write(s, w, deadband_a)


def quantise_amps(a: float, step_a: float, up: bool) -> float:
    """`a` on the writer's quantisation grid: UP (ceil) or DOWN (floor) to a
    multiple of `step_a`. 0 A (hold the pack) and the nameplate pass through;
    a value never goes below one step (down would turn a small sale into a
    hold) nor above the nameplate."""
    a, step = float(a), float(step_a)
    if step <= DEYE_CURRENT_STEP_A or a <= 0.0 or a >= DEYE_CURRENT_MAX_A:
        return a
    q = -(-a // step) * step if up else (a // step) * step
    return min(max(q, step), DEYE_CURRENT_MAX_A)


# What a field costs if a dead controller leaves it standing. The registers latch
# and there is no comms-loss timeout inverter-side, so this is not decoration: it
# is the revert policy, and it is per-field because one blanket watchdog is the
# wrong shape when only one field can actually spend money.
DEYE_TIER = {
    "battery_grid_charging": "dangerous",           # imports indefinitely
    "battery_grid_charging_current": "dangerous",
    "work_mode": "costly",                          # Export First sells into negative prices
    "export_surplus": "wasteful",                   # throws energy away, never spends
    "export_surplus_power": "wasteful",
    "battery_max_charging_current": "wasteful",
    "battery_max_discharging_current": "wasteful",
    "microinverter_export_cut_off": "wasteful",
    "zero_export_power": "wasteful",
    "energy_pattern": "wasteful",
    "grid_peak_shaving": "costly",                  # off + Export First is the sell recipe
    "program_6_soc": "dangerous",                   # 100 keeps a grid charge alive
    "program_6_power": "wasteful",                  # below nameplate it caps every sell
}


# The state that must survive a dead controller. Zero Export To Load since the
# go-live (2026-09-25): the external meter left with the supplier and the internal
# CTs at the grid port match the fiscal meter to ~20 W. The three fields after
# the cutoff are the registers the supplier used that the record did not carry:
# peak shaving ON blocks every battery sale, program 6 SOC 100 is what makes
# a grid charge happen, program 6 power below nameplate caps every sale.
DEYE_BASELINE = dict(PLANT["baseline"], battery_max_charging_current=DEYE_CURRENT_MAX_A,
                     battery_max_discharging_current=DEYE_CURRENT_MAX_A)


def deye_amps(w: float, pack_v: float = DEYE_PACK_V) -> float:
    """Watts to a quantised current clamp, rounded to nearest.

    The EEPROM budget depends on how often the value CHANGES, not on how coarse
    it is - a flat charge block is one write at any step size - so there is no
    reason to quantise harder than the register does. Flooring, which an earlier
    version did, put a systematic under-bias of up to a full step on every
    charge step."""
    v = float(pack_v) or DEYE_PACK_V
    a = max(0.0, float(w)) / v
    a = round(a / DEYE_CURRENT_STEP_A) * DEYE_CURRENT_STEP_A
    return min(a, DEYE_CURRENT_MAX_A)


EXPORT_SHUT_SOC_PCT = 90.0      # export shuts only above this SOC, and only at a negative sell price


def export_shuts(sell: float | None, soc_pct: float) -> bool:
    """The site's export rule (2026-09-27): PV export goes off only when the all-in
    sell price is negative AND the pack is above EXPORT_SHUT_SOC_PCT. An unknown
    price is read as paying."""
    return sell is not None and float(sell) < 0.0 and float(soc_pct) > EXPORT_SHUT_SOC_PCT


def deye_command(p_grid_w: float, p_batt_w: float, pack_v: float = DEYE_PACK_V,
                 micro_cut: bool = False, target_soc: float | None = None,
                 pv_curtail_w: float = 0.0, sell: float | None = None,
                 margin: bool = False, soc_pct: float | None = None) -> dict:
    """One plan step compiled into the register vocabulary. p_batt_w follows the
    plan's sign: negative charges, positive discharges.

    The intent is read off the plan rather than configured, and it decides where
    a forecast error lands:

      self_balance  grid is a consequence     -> grid charging OFF; while selling pays
                                                 export stays ON and the error lands on
                                                 whichever flow the plan has bigger (the
                                                 pack, clamp at nameplate; or the meter,
                                                 clamp at the plan); at a non-positive
                                                 sell price zero export with the margin
                                                 clamp, error on the battery up to it
      grid_charge   import is the goal        -> grid charging ON, error on the grid
      export        discharging to sell       -> Export First, error on the grid
      self_supply   discharging into the load -> zero export, discharge clamp open
                                                 (240 A): the pack follows the
                                                 house, the error lands on the pack
      pv_export     battery idle              -> no freedom; the grid takes it

    `sell` is the step's sell price; None (unknown) is read as paying, so a
    caller that does not carry a tariff gets the no-curtailment behaviour.

    `margin` adds the insurance margin (DEYE_CLAMP_MARGIN_*) to a zero-export
    charge clamp. It is the WRITER's policy: the real pack should fill even if
    the afternoon goes worse than forecast. Settlement leaves it off, because
    the base run of the delta method has to reproduce the plan on the plan's
    own inputs, and a clamp above the plan's charge does not on a step the LP
    curtailed (it would book a deviation the world never made). The virtual
    pack needs no insurance: the next half-hourly solve starts from its
    settled SOC.

    `target_soc` is accepted for signature compatibility and IGNORED since the
    go-live trials (2026-09-26): the pack is commanded by the discharge clamp
    on a discharge, and by program 6 SOC 100 on a grid charge.

    Two axes, both measured on the real inverter:

      PV ROUTING     work_mode, export_surplus, grid_peak_shaving. Export First
                     sends PV to the grid ahead of the pack; with peak shaving
                     OFF it also lets the pack sell.
      BATTERY        grid charging with its current AND program 6 SOC 100, or
                     the discharge clamp, or a zero charge clamp. Only these
                     move the pack.
    """
    charging = p_batt_w < -DEYE_BATT_DEADBAND_W
    discharging = p_batt_w > DEYE_BATT_DEADBAND_W
    if charging and p_grid_w > DEYE_GRID_DEADBAND_W:
        intent = "grid_charge"
    elif charging:
        intent = "self_balance"
    elif discharging and p_grid_w < -DEYE_GRID_DEADBAND_W:
        intent = "export"
    elif discharging:
        intent = "self_supply"
    else:
        intent = "pv_export"

    cmd = dict(DEYE_BASELINE)
    cmd["microinverter_export_cut_off"] = bool(micro_cut)
    if intent == "self_balance":
        # NO CURTAILMENT ON RESIDUALS WHILE SELLING PAYS (2026-09-08). This
        # branch used to shut export and pin the clamp to the plan's P_batt on
        # every charging step, so on 09-07 sun above forecast was thrown away
        # at 0,04 to 0,19 EUR/kWh with the pack at 14 to 31 %: 2,3 kWh over
        # eight steps, none of which any plan had declined. While export pays
        # the windfall follows the plan's bigger flow. A step that mostly banks
        # takes it into the pack (clamp at nameplate, overflow exports once the
        # pack is full); a step that mostly sells keeps the plan's clamp so the
        # extra goes out. Either way the CT rule stays lifted and nothing is
        # declined. Only a non-positive price shuts export.
        #
        # A NON-CURTAILED STEP IS TAKE-ALL-THE-SUN AT ANY PRICE (2026-09-12).
        # The negative-price leg used to pin the clamp to the plan's P_batt as
        # well, so on 09-12 at 11:30 to 12:00 (sell just under zero, plan
        # charging 5,4 kW at grid zero, nothing declined, and the SAME plan
        # importing at 0,023 from 12:15 to fill the pack) 770 W of sun above
        # Solcast was thrown away: worth at least the import it displaced. On
        # a step the LP did not curtail, P_batt is the forecast surplus, a
        # consequence exactly like P_grid, never a target. So export shuts and
        # the clamp stays at nameplate. Only the LP's own decline is a
        # target: the headroom it leaves is a decision, the clamp holds the
        # plan's charge and carries the insurance margin.
        #
        # EXPORT SHUTS ONLY AT A NEGATIVE ALL-IN SELL PRICE WITH THE PACK ABOVE
        # 90 % (2026-09-27). With the step's SOC known (the writer passes
        # the plan's SOC_opt) that is the whole rule: below 90 % or at a price
        # that is not negative, export stays on and the clamp opens, so the pack
        # takes the sun first and only what it cannot take is sold. Without a
        # SOC (settlement, replay) the rule above stands unchanged.
        if soc_pct is None:
            export_pays = (sell is None or float(sell) > 0.0) and float(pv_curtail_w) <= 0.0
        else:
            export_pays = not export_shuts(sell, soc_pct)
        if export_pays and float(pv_curtail_w) <= 0.0 and -p_grid_w > -p_batt_w:
            cmd.update(export_surplus=True,
                       battery_max_charging_current=deye_amps(-p_batt_w, pack_v))
        elif export_pays:
            cmd.update(export_surplus=True,
                       battery_max_charging_current=DEYE_CURRENT_MAX_A)
        elif float(pv_curtail_w) <= 0.0:
            cmd.update(export_surplus=False,
                       battery_max_charging_current=DEYE_CURRENT_MAX_A)
        else:
            need = -p_batt_w
            if margin:
                need = max(need * (1.0 + DEYE_CLAMP_MARGIN_FRAC), need + DEYE_CLAMP_MARGIN_A * float(pack_v))
            cmd.update(export_surplus=False,
                       battery_max_charging_current=deye_amps(need, pack_v))
    elif intent == "grid_charge":
        # THE TOU TARGET IS THE COMMAND (verified 2026-09-25 and 26): the switch
        # and the current alone import nothing at 90 % SOC; with program 6 SOC
        # at 100 the meter matched the twin to 2 W. Switch and current are the
        # permission and the cap.
        #
        # THE GRID CURRENT IS A FLOOR, NOT A CEILING (measured 2026-09-27 under
        # sun, 240 A clamp): at 60 A with ~125 A of surplus the pack took the
        # whole surplus and the meter read +15 W; at 160 A with ~130 A of
        # surplus the grid imported only the shortfall. So the charge clamp
        # stays at nameplate: the plan's charge is the minimum, sun above
        # the forecast is banked, not throttled ("7 kW is the minimum,
        # more is always welcome").
        #
        # A GRID CHARGE KEEPS EXPORT ON (2026-09-27): the charge is bought
        # for a later, higher price, while selling the sun the pack cannot take
        # (a tapering pack near full) still pays now. Export shuts only under
        # the negative-price rule. Without a SOC the old export-off stands.
        cmd.update(export_surplus=(soc_pct is not None and not export_shuts(sell, soc_pct)),
                   battery_grid_charging=True,
                   battery_grid_charging_current=deye_amps(-p_batt_w, pack_v),
                   battery_max_charging_current=DEYE_CURRENT_MAX_A,
                   program_6_soc=100.0)
    elif intent in ("export", "self_supply"):
        # THE PACK IS COMMANDED BY THE DISCHARGE CLAMP (verified on the real
        # inverter 2026-09-25 and 26). Under Export First with peak shaving OFF
        # three levers all move the pack and the smallest wins: the sell
        # setpoint (export_surplus_power, a GRID setpoint), the discharge clamp
        # (a PACK setpoint, 97 A of 100 delivered) and the TOU slot power (a
        # PACK cap). The clamp is the one the record uses: it is what the plan's
        # P_batt means, and it puts the load error on the meter, where the
        # settlement's delta already books it. The other two stay wide open.
        # With peak shaving ON the inverter sells NOTHING from the pack, so the
        # export intent turns it off and the baseline turns it back on.
        if intent == "export":
            cmd.update(battery_max_discharging_current=deye_amps(p_batt_w, pack_v),
                       work_mode="Export First", grid_peak_shaving=False)
        else:
            # LIVE FROM BATTERY IS THE OPEN CLAMP (2026-10-03): the pack
            # follows the house's actual draw, so the load error lands on the
            # pack, not the meter. Under Zero Export To Load with peak shaving on
            # the pack can only feed the house, and the plan never covered just
            # part of the load (0 of 179 self-supply quarters in the 10 ct
            # spread-tariff backtest of 09-26..10-02), so the clamp at the plan
            # enforced nothing. The SOC-floor kill still stops it at the floor.
            cmd.update(battery_max_discharging_current=DEYE_CURRENT_MAX_A)
            # LIVE FROM BATTERY KEEPS EXPORT SURPLUS ON (2026-10-03), under
            # the same rule as every other intent. Turning it off dates from
            # before the go-live trials, when it was read as the permission that
            # lets the pack discharge into the grid; peak shaving (on here) is
            # what blocks a pack sale. Off it only kept PV from being sold, and
            # cost flips (~9 writes a day in the spread-tariff backtest). Without
            # a SOC (settlement, replay) the old export-off stands.
            cmd["export_surplus"] = soc_pct is not None and not export_shuts(sell, soc_pct)
    elif intent == "pv_export":
        # Sell PV now, bank nothing: hold the pack with a ZERO CHARGE CLAMP and
        # let the surplus leave. Deliberately NOT Export First (2026-09-06):
        # a zero clamp is wasteful-tier, so a dead controller that leaves it
        # standing merely fails to charge, while Export First is costly-tier and
        # would sell into a negative price. It also keeps a register whose exact
        # semantics we have not bench-verified off the common path.
        #
        # AN IDLE STEP RESTS THE PACK (2026-09-27). The zero charge clamp
        # alone left the discharge clamp at 240 A, so at night Zero Export To
        # Load ran the house (and a little export) off the pack through steps
        # the plan held idle: 1,6 kW mean on 09-27 21:10-21:50, the round trip
        # the LP had declined. A zero discharge clamp holds it (-10 W measured
        # 09-26); a dead writer leaving it standing merely imports (wasteful).
        cmd.update(battery_max_charging_current=0.0, battery_max_discharging_current=0.0,
                   export_surplus=True)
    # A STEP THAT DECLINES SUN IS A ZERO-EXPORT STEP (2026-09-07). The intent
    # table above reads an idle battery as "sell the sun" and lifts the CT rule,
    # so on a full-pack negative-price afternoon the base run exported what the
    # plan had actually curtailed, and a real sun dip then settled as a phantom
    # import (2,31 kWh on the 09-05 rolling re-solve with stress off). The LP
    # never curtails and exports main PV in the same step - curtailment only
    # pays when export does not - so any planned curtailment means the strings
    # are to be held to the sink, whatever the battery does. The gen port still
    # spills past the CT rule, exactly as deye_response models it.
    # With the step's SOC known, the site's export rule (2026-09-27) overrides this:
    # the strings are held to the sink only at a negative price above 90 %.
    if float(pv_curtail_w) > 0 and (soc_pct is None or export_shuts(sell, soc_pct)):
        cmd["export_surplus"] = False
    cmd["intent"] = intent
    cmd["tier"] = DEYE_TIER
    return cmd


def wanted_clamp(p_grid_w: float, p_batt_w: float, pack_v: float = DEYE_PACK_V, micro_cut: bool = False,
                 pv_curtail_w: float = 0.0, sell: float | None = None, margin: bool = False) -> tuple[float, bool]:
    """(amps, grid_charge): the charge clamp a plan step asks for, what the
    writer would want in battery_max_charging_current before the deadband,
    and whether the step is a grid charge (clamp_write's lift rule)."""
    cmd = deye_command(p_grid_w, p_batt_w, pack_v, micro_cut=micro_cut, pv_curtail_w=pv_curtail_w,
                       sell=sell, margin=margin)
    return float(cmd["battery_max_charging_current"]), bool(cmd.get("battery_grid_charging"))


def wanted_clamp_a(p_grid_w: float, p_batt_w: float, pack_v: float = DEYE_PACK_V, micro_cut: bool = False,
                   pv_curtail_w: float = 0.0, sell: float | None = None, margin: bool = False) -> float:
    """The amps of wanted_clamp."""
    return wanted_clamp(p_grid_w, p_batt_w, pack_v, micro_cut=micro_cut, pv_curtail_w=pv_curtail_w,
                        sell=sell, margin=margin)[0]


def deye_response(cmd: dict, main_pot_w: float, micro_w: float, load_w: float, soc: float,
                  capacity_kwh: float = CAPACITY_KWH, pack_v: float = DEYE_PACK_V) -> dict:
    """The virtual Deye: what the inverter does with that command, given the sun,
    the load and the pack. Returns batt_w (plan sign), grid_w (+ = import),
    pv_main_w, curtail_main_w, curtail_micro_w. Every branch was measured on
    the real inverter on 2026-09-25 and 26 (an internal design note); the zero-export branch models both Zero Export To CT
    and Zero Export To Load, which differ only in where the CT sits.

    This is the settlement. The lane it replaces computed the grid as the plan's
    own P_grid plus the forecast errors, which books every error to the meter;
    here the error lands where the hardware puts it, and under zero export that
    is the battery.

    The must-take half is on the GEN PORT, so the CT rule cannot reach it: with
    the pack full and the strings at zero it still pushes past the meter, which
    is the only state where the cutoff is worth anything.
    """
    micro = 0.0 if cmd.get("microinverter_export_cut_off") else max(0.0, float(micro_w))
    main_pot = max(0.0, float(main_pot_w))
    load = float(load_w)
    step_w = capacity_kwh * 1000.0 / STEP_H                 # a full pack swing in one step
    headroom = max(0.0, SOC_MAX - float(soc)) * step_w
    floor = max(0.0, float(soc) - SOC_MIN) * step_w
    charge_cap = float(cmd["battery_max_charging_current"]) * pack_v
    disch_cap = float(cmd["battery_max_discharging_current"]) * pack_v

    gc_target = float(cmd.get("program_6_soc", 5.0)) / 100.0
    if cmd.get("battery_grid_charging") and gc_target > float(soc):
        # The switch and the current are permission and cap; the TOU target
        # decides whether the pack wants charge at all (2026-09-25: 40 A with
        # the target at 5 % imported nothing at 90 % SOC).
        # The grid current is a FLOOR (2026-09-27, under sun): the pack takes
        # the larger of it and the PV surplus, up to the charge clamp. With
        # export off the CT rule throttles the strings to what the pack and
        # the house take.
        surplus = micro + main_pot - load
        charge = min(charge_cap, headroom,
                     max(float(cmd["battery_grid_charging_current"]) * pack_v, surplus))
        discharge = 0.0
        if cmd.get("export_surplus"):
            pv_main = main_pot
        else:
            pv_main = min(main_pot, max(0.0, load + charge - micro))
    elif cmd.get("work_mode") == "Export First":
        # MEASURED 2026-09-25/26. The pack follows the smallest of three levers:
        # the sell setpoint (grid), the discharge clamp (pack) and the TOU slot
        # power (pack). With peak shaving ON the pack only covers the house.
        # PV beyond load + setpoint charges the pack.
        pv_ac = main_pot + micro
        setp = float(cmd.get("export_surplus_power", 14500.0))
        tou_cap = float(cmd.get("program_6_power", 12000.0))
        want = max(0.0, (load - pv_ac) if cmd.get("grid_peak_shaving", True) else (setp + load - pv_ac))
        discharge = min(disch_cap, tou_cap, floor, want)
        charge = min(charge_cap, headroom, max(0.0, pv_ac - load - setp))
        pv_main = main_pot
    else:
        surplus = micro + main_pot - load
        charge = min(charge_cap, headroom, max(0.0, surplus))
        discharge = min(disch_cap, floor, max(0.0, -surplus))
        if cmd.get("export_surplus"):
            pv_main = main_pot                              # the CT rule is lifted
        else:
            pv_main = min(main_pot, max(0.0, load + charge - discharge - micro))
    grid = load + charge - discharge - pv_main - micro
    return {"batt_w": round(discharge - charge, 1), "grid_w": round(grid, 1),
            "pv_main_w": round(pv_main, 1),
            "curtail_main_w": round(max(0.0, main_pot - pv_main), 1),
            "curtail_micro_w": round(max(0.0, float(micro_w)) - micro, 1)}


def settle_step(plan_grid_w: float, plan_batt_w: float, plan_pv_w: float, plan_load_w: float,
                plan_curtail_w: float, pv_w: float, load_w: float,
                micro_plan_w: float, micro_w: float, soc: float,
                capacity_kwh: float = CAPACITY_KWH, micro_cut: bool = False,
                sell: float | None = None, margin: bool = False,
                eta_c: float = ETA_C, eta_d: float = ETA_D,
                clamp_a: float | None = None) -> tuple[float, float, float]:
    """One step's closed-loop settlement. Returns (grid_w, batt_w, curtail_w).

    The plan's own figures moved by the DELTA the virtual inverter produces
    between the plan's inputs and the real ones - never a from-scratch balance,
    because the site does not balance and the plan's P_grid carries EMHASS's
    conversion loss that an absolute sum would discard.

    All three are deltas, curtailment included. Taking the model's absolute
    curtailment lets a saturation both runs share leak into the answer: with the
    pack near full each run declines enormous amounts while the difference
    between them, the only part that is about the forecast error, is small.

    `margin` replays the writer's insurance clamp: the base run keeps the plan's
    own command (it has to reproduce the plan on the plan's inputs), the actual
    run carries the margin, so the delta holds the forecast error AND the extra
    charge the margin buys on a curtailed step.

    THE PACK'S OWN LIMITS CLOSE THE BOOKS (2026-09-08). The delta keeps
    the plan's P_batt wherever base and actual run agree, and they agree at
    zero charge on a step where the virtual pack has no headroom, so the plan's
    charge into a full pack survived the settlement and integrate_soc clamped
    the energy away: 4,5 kWh over six steps on the 09-06 margin replay. The
    settled flows now respect the pack through the same etas the SOC walk
    uses: charge beyond headroom goes to the meter when export pays and to the
    strings when it does not, discharge below the floor comes back from the
    meter, and the walk lands exactly on the limit instead of clamping."""
    cmd = deye_command(plan_grid_w, plan_batt_w, micro_cut=micro_cut, pv_curtail_w=plan_curtail_w, sell=sell)
    base = deye_response(cmd, max(0.0, plan_pv_w - micro_plan_w), micro_plan_w,
                         plan_load_w, soc, capacity_kwh)
    if margin:
        cmd = deye_command(plan_grid_w, plan_batt_w, micro_cut=micro_cut, pv_curtail_w=plan_curtail_w,
                           sell=sell, margin=True)
    if clamp_a is not None:
        # THE STANDING REGISTER (2026-09-08). The writer rewrites the charge
        # clamp only when the wanted value has moved by more than the deadband,
        # so the actual run is driven by what is standing, not by what this
        # step's plan asks for. The base run keeps the plan's own command.
        cmd = dict(cmd, battery_max_charging_current=float(clamp_a))
    act = deye_response(cmd, max(0.0, pv_w - micro_w), micro_w, load_w, soc, capacity_kwh)
    # On a cut step the plan's curtailment already holds the FORECAST Growatt; the
    # micro term moves it by the Growatt's own forecast error, nothing else.
    d_curt = ((act["curtail_main_w"] + act["curtail_micro_w"])
              - (base["curtail_main_w"] + base["curtail_micro_w"]))
    grid = plan_grid_w + (act["grid_w"] - base["grid_w"])
    batt = plan_batt_w + (act["batt_w"] - base["batt_w"])
    curt = max(0.0, plan_curtail_w + d_curt)
    step_w = float(capacity_kwh) * 1000.0 / STEP_H
    head_w = max(0.0, SOC_MAX - float(soc)) * step_w / eta_c      # charge that still fits this step
    floor_w = max(0.0, float(soc) - SOC_MIN) * step_w * eta_d     # discharge the pack can still give
    if batt < -head_w:
        excess = -batt - head_w
        batt = -head_w
        if cmd.get("export_surplus"):
            grid -= excess
        else:
            curt += excess
    elif batt > floor_w:
        grid += batt - floor_w
        batt = floor_w
    return grid, batt, curt


def soc_dwell_h(soc_pct, threshold_pct: float = 95.0) -> float:
    """Hours a settled SOC lane spends at or above `threshold_pct` (
    2026-09-08): the cost side of front-loading, counted wherever a settled
    lane exists. None steps do not count."""
    return round(sum(1 for v in soc_pct if v is not None and float(v) >= threshold_pct) * STEP_H, 2)


def integrate_soc(soc: float, batt_w: float, eta_c: float, eta_d: float,
                  capacity_kwh: float = CAPACITY_KWH) -> tuple[float, bool]:
    """One step of the virtual pack: charge through eta_c, discharge through
    eta_d, then clamp into [SOC_MIN, SOC_MAX] like the real pack's BMS would.
    Returns (soc, clamped): the clamp is the only place the virtual pack is
    allowed to deviate from the commanded flow, so callers count it."""
    if batt_w < 0:
        soc += (-batt_w / 1000.0) * STEP_H * eta_c / capacity_kwh
    elif batt_w > 0:
        soc -= (batt_w / 1000.0) * STEP_H / eta_d / capacity_kwh
    clamped = soc < SOC_MIN or soc > SOC_MAX
    return min(max(soc, SOC_MIN), SOC_MAX), clamped


def settle_slice(compact: dict, pv_w, load_w, micro_w=None,
                 capacity_kwh: float = CAPACITY_KWH,
                 eta_c: float = ETA_C, eta_d: float = ETA_D, margin: bool = SETTLE_MARGIN,
                 deadband_a: float = DEYE_CLAMP_DEADBAND_A) -> dict | None:
    """A compact day slice re-settled against what actually happened. Same shape
    in, same shape out, so every consumer is unchanged.

    This is what puts the SCOREBOARD on the closed-loop basis. plan_for_day
    stays the scoring selector - the plan of record still has to be frozen
    before the day starts - and only the settlement moves: where the plan is
    self-balancing the pack absorbs the forecast error instead of the meter
    being billed for it."""
    n = int(compact.get("n") or 0)
    if not n or any(x is None or len(x) != n for x in (pv_w, load_w)):
        return None
    mic = list(micro_w) if micro_w is not None and len(micro_w) == n else [0.0] * n
    mic_plan = compact.get("pv_micro_w") or [0.0] * n
    if len(mic_plan) != n:
        mic_plan = [0.0] * n
    cut_w = compact.get("micro_cut_w") or [0.0] * n
    if len(cut_w) != n:
        cut_w = [0.0] * n
    out = dict(compact)
    soc = float(compact["soc_start_pct"]) / 100.0
    pc_in = compact.get("pv_curtail_w") or [0.0] * n
    sell = compact.get("sell") or []
    grid, batt, curt, socs, clamps = [], [], [], [], []
    standing, writes = None, 0
    for i in range(n):
        s_i = float(sell[i]) if i < len(sell) and sell[i] is not None else None
        cut = float(cut_w[i] or 0.0) > 0
        wanted, lift = wanted_clamp(float(compact["p_grid_w"][i]), float(compact["p_batt_w"][i]), micro_cut=cut,
                                    pv_curtail_w=float(pc_in[i]), sell=s_i, margin=margin)
        standing, wrote = clamp_write(standing, wanted, deadband_a, lift=lift)
        writes += wrote
        g, b, c = settle_step(float(compact["p_grid_w"][i]), float(compact["p_batt_w"][i]),
                              float(compact["p_pv_w"][i]), float(compact["p_load_w"][i]),
                              float(pc_in[i]), float(pv_w[i]), float(load_w[i]),
                              float(mic_plan[i]), float(mic[i]), soc, capacity_kwh,
                              micro_cut=cut, sell=s_i, margin=margin, eta_c=eta_c, eta_d=eta_d,
                              clamp_a=standing)
        soc, _clamped = integrate_soc(soc, b, eta_c, eta_d, capacity_kwh)
        grid.append(round(g, 1)); batt.append(round(b, 1))
        curt.append(round(c, 1)); socs.append(round(soc * 100, 2)); clamps.append(standing)
    out.update(p_grid_w=grid, p_batt_w=batt, pv_curtail_w=curt, soc_pct=socs, settled=True,
               clamp_a=clamps, clamp_writes=writes)
    return out
