"""The Deye as an actuator: a plan step compiled into the register vocabulary, the virtual inverter's
response, and the closed-loop settlement built as the delta between the two."""
from __future__ import annotations

from .grid import STEP_H
from .objective import CAPACITY_KWH, SOC_MAX, SOC_MIN
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
# = config.json battery_charge_efficiency / battery_discharge_efficiency. The
# virtual pack integrates through these on every settled step (settle_slice,
# virtual_day, ab_walk), so like CAPACITY_KWH they must track what the LP plans
# on, or the planned and the settled lanes model a different pack. Until
# 2026-09-07 all three carried them as signature literals.
ETA_C = float(PLANT["battery"]["eta_charge"])
ETA_D = float(PLANT["battery"]["eta_discharge"])


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
}


# The state that must survive a dead controller.
DEYE_BASELINE = {
    "work_mode": "Zero Export To CT",
    "energy_pattern": "Load First",
    "zero_export_power": 25.0,
    "export_surplus": True,
    "export_surplus_power": 14500.0,
    "battery_max_charging_current": DEYE_CURRENT_MAX_A,
    "battery_max_discharging_current": DEYE_CURRENT_MAX_A,
    "battery_grid_charging": False,
    "battery_grid_charging_current": 0.0,
    "microinverter_export_cut_off": False,
}


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


def deye_command(p_grid_w: float, p_batt_w: float, pack_v: float = DEYE_PACK_V,
                 micro_cut: bool = False, target_soc: float | None = None,
                 pv_curtail_w: float = 0.0, sell: float | None = None,
                 margin: bool = False) -> dict:
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
      self_supply   discharging into the load -> zero export, discharge clamp at the
                                                 plan; error on the battery up to
                                                 the clamp, then on the grid
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

    `target_soc` is what a real writer would put in the TOU slot; settlement
    (settle_step) never passes one, so in every settled lane the two discharge
    intents run through deye_response's Export First branch (pack uncommanded,
    the delta is zero) and its zero-export branch (the clamp above). The TOU
    branch of deye_response is exercised only by the unit tests today.

    Two ORTHOGONAL axes, which is the thing an earlier version got wrong by
    treating Export First as a battery command:

      PV ROUTING     work_mode, export_surplus. Export First sends PV to the grid
                     ahead of the pack, which is how a morning of falling prices
                     is sold off while the pack is held empty for the cheap block.
      BATTERY        grid charging with its current, or a TOU target SOC with a
                     power cap, or nothing at all. Only this moves the pack.

    They compose; the intents below are combinations, not alternatives.
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
        export_pays = (sell is None or float(sell) > 0.0) and float(pv_curtail_w) <= 0.0
        if export_pays and -p_grid_w > -p_batt_w:
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
        cmd.update(export_surplus=False, battery_grid_charging=True,
                   battery_grid_charging_current=deye_amps(-p_batt_w, pack_v),
                   battery_max_charging_current=deye_amps(-p_batt_w, pack_v))
    elif intent in ("export", "self_supply"):
        # A DISCHARGE IS NOT A WORK MODE (2026-09-06). Export First governs where
        # PV is allowed to go; it does not command the pack. Reading it as
        # "discharge at the clamp" put 12 to 17 kW on the meter where the 09-05
        # replay measured a few hundred watts. The pack is commanded by a TOU
        # slot: drive toward target_soc at up to power, which discharges above
        # the target and charges below it.
        cmd.update(battery_max_discharging_current=deye_amps(p_batt_w, pack_v),
                   tou_target_soc=target_soc, tou_power_w=abs(float(p_batt_w)))
        # UNVERIFIED, and the only lever here that is: discharging INTO the grid
        # needs export permitted, and under Zero Export To CT the CT rule holds
        # it to load. Export First is the candidate; bench-check it before the
        # writer exists. self_supply needs no such permission.
        cmd["work_mode" if intent == "export" else "export_surplus"] = (
            "Export First" if intent == "export" else False)
    elif intent == "pv_export":
        # Sell PV now, bank nothing: hold the pack with a ZERO CHARGE CLAMP and
        # let the surplus leave. Deliberately NOT Export First (2026-09-06):
        # a zero clamp is wasteful-tier, so a dead controller that leaves it
        # standing merely fails to charge, while Export First is costly-tier and
        # would sell into a negative price. It also keeps a register whose exact
        # semantics we have not bench-verified off the common path.
        cmd.update(battery_max_charging_current=0.0, export_surplus=True)
    # A STEP THAT DECLINES SUN IS A ZERO-EXPORT STEP (2026-09-07). The intent
    # table above reads an idle battery as "sell the sun" and lifts the CT rule,
    # so on a full-pack negative-price afternoon the base run exported what the
    # plan had actually curtailed, and a real sun dip then settled as a phantom
    # import (2,31 kWh on the 09-05 rolling re-solve with stress off). The LP
    # never curtails and exports main PV in the same step - curtailment only
    # pays when export does not - so any planned curtailment means the strings
    # are to be held to the sink, whatever the battery does. The gen port still
    # spills past the CT rule, exactly as deye_response models it.
    if float(pv_curtail_w) > 0:
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
    pv_main_w, curtail_main_w, curtail_micro_w.

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

    if cmd.get("battery_grid_charging"):
        charge = min(float(cmd["battery_grid_charging_current"]) * pack_v, charge_cap, headroom)
        discharge, pv_main = 0.0, main_pot
    elif cmd.get("tou_target_soc") is not None:
        # Drive toward the target at up to the slot's power, in whichever
        # direction the pack happens to be on. The cap applies to both.
        want = (float(soc) - float(cmd["tou_target_soc"])) * step_w
        # `or` would turn a deliberate 0 W - hold the pack where it is, which is
        # what the sell-the-morning intent asks for - into the full clamp.
        tp = cmd.get("tou_power_w")
        cap = min(float(tp) if tp is not None else disch_cap, disch_cap)
        discharge = min(cap, floor, max(0.0, want))
        charge = min(cap, charge_cap, headroom, max(0.0, -want))
        pv_main = main_pot
    elif cmd.get("work_mode") == "Export First":
        charge = discharge = 0.0            # PV may export; the pack is uncommanded
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
    """Hours a settled SOC lane spends at or above `threshold_pct` (the site rule,
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
