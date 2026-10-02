"""The writer: the plan step in force compiled into the register record and
diffed against what stands on the real Deye. Pure functions only; the pyscript
wrapper (pyscript/emhass_writer.py) reads entities, calls these, and writes.

Spec: an internal design note. Measured
semantics: an internal design note."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .grid import _parse_ts, STEP_MIN
from .deye import (clamp_write, deye_command, DEYE_BASELINE, DEYE_CLAMP_DEADBAND_A, DEYE_CURRENT_MAX_A,
                   DEYE_CHARGE_SHORT_A, DEYE_CURRENT_QUANT_A, DEYE_DISCHARGE_DEADBAND_A,
                   DEYE_DISCHARGE_SHORT_ICPT_A, DEYE_DISCHARGE_SHORT_KNEE_A, DEYE_DISCHARGE_SHORT_SLOPE,
                   DEYE_GRID_CHARGE_DEADBAND_A, DEYE_GRID_CHARGE_GAIN, DEYE_PACK_R_OHM, DEYE_PACK_V, DEYE_TIER,
                   loaded_voltage, quantise_amps, setpoint_write)
from .archive import ARCHIVE_REACH, iter_organic_plans
from .objective import bus_to_port_w, is_v2, ETA_C_V2, ETA_D_V2
from .plant import PLANT


# The plan refreshes at :13 and :43 and a tick runs at :00, :15, :30 and :45
# (+20 s). plan_ts is the solve START, so the :13 plan is 47 min 20 s old at
# the :00:20 tick when the :43 solve is lost (Infeasible, add-on timeout):
# 60 minutes tolerates exactly that one missed refresh (review 2026-09-26;
# 45 did not), and older than that the writer holds the baseline.
WRITER_STALE_MIN = 60.0


def step_in_force(archive_dir: str, now: datetime, stale_min: float = WRITER_STALE_MIN) -> dict | None:
    """The row at `now` and the row after it, from the newest organic plan whose
    horizon covers `now`, or None when no plan does. Replays and failed solves
    are not the chain (iter_organic_plans), and a plan solved after `now` is
    not in force yet. Index arithmetic goes through UTC (the DST fold)."""
    now_utc = now.astimezone(timezone.utc)
    for doc in iter_organic_plans(archive_dir, since=now - ARCHIVE_REACH, until=now):
        t0 = _parse_ts(doc["t0"]).astimezone(timezone.utc)
        n = int(doc.get("n") or 0)
        i = int((now_utc - t0).total_seconds() // (STEP_MIN * 60))
        if i < 0 or i >= n:
            continue
        rows = doc["rows"]
        cuts = doc.get("micro_cut") or []
        age = now_utc - _parse_ts(doc["plan_ts"]).astimezone(timezone.utc)
        return {"plan_ts": doc["plan_ts"], "t0": doc["t0"], "n": n, "index": i,
                "row": rows[i], "next_row": rows[i + 1] if i + 1 < n else None,
                "micro_cut": bool(cuts[i]) if i < len(cuts) else False,
                "next_micro_cut": bool(cuts[i + 1]) if i + 1 < len(cuts) else False,
                "rows": rows, "cuts": [bool(cuts[j]) if j < len(cuts) else False for j in range(n)],
                "physics_v2": is_v2(doc.get("knobs")),
                "stale": age > timedelta(minutes=float(stale_min))}
    return None


# The registers the writer never touches. They stay at the baseline so the
# discharge clamp is the only pack lever on a sale (a sell setpoint or a TOU
# power below nameplate would silently cap it, measured 2026-09-25).
WRITER_NEVER = ("energy_pattern", "zero_export_power", "export_surplus_power", "program_6_power")


# ENTRY ORDER: wasteful, then costly, then dangerous; within the dangerous
# tier the cap, the permission, and the TOU command last, so a grid charge
# starts only when everything else already stands. A dead controller that
# stops mid-sequence has set nothing that spends money.
WRITER_FIELDS = ("export_surplus", "battery_max_charging_current", "battery_max_discharging_current",
                 "microinverter_export_cut_off", "work_mode", "grid_peak_shaving",
                 "battery_grid_charging_current", "battery_grid_charging", "program_6_soc")


# RESTORE ORDER: the TOU command and the permission first (either one alone
# stops the import), then the cap; then the sale (work mode before peak
# shaving); then the wasteful fields. An interrupted restore has stopped the
# spending before it stopped the waste.
RESTORE_ORDER = ("program_6_soc", "battery_grid_charging", "battery_grid_charging_current",
                 "work_mode", "grid_peak_shaving",
                 "export_surplus", "battery_max_charging_current", "battery_max_discharging_current",
                 "microinverter_export_cut_off")


# record field -> the Solarman entity (deye_p3 profile). The go-live harness
# keeps the same map in an internal tool; a test pins them equal.
WRITER_ENTITY = {f: PLANT["writer_entities"][f] for f in WRITER_FIELDS}


def _as_bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("on", "true", "1")


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def same(standing, wanted) -> bool:
    """Whether what stands is what the record wants. Switches read back as
    "on"/"off", numbers as strings; a current register resolves to 1 A, so
    anything within half an amp is the same value. None (unavailable) is
    never the same, so it is always a write."""
    if standing is None:
        return False
    if isinstance(wanted, bool):
        return _as_bool(standing) is wanted
    if isinstance(wanted, (int, float)):
        s = _as_float(standing)
        return s is not None and abs(s - float(wanted)) < 0.5
    return str(standing) == str(wanted)


CURRENT_FIELDS = ("battery_max_charging_current", "battery_max_discharging_current",
                  "battery_grid_charging_current")


def calibrate_amps(a: float, field: str = "battery_max_discharging_current") -> float:
    """The register value on `field` that DELIVERS `a` (the inverse of
    deye_delivered_a), rounded to the register's 1 A. 0 A (hold the pack) and
    the nameplate are not currents to deliver and pass through; the result
    never exceeds nameplate."""
    a = float(a)
    if a <= 0.0 or a >= DEYE_CURRENT_MAX_A:
        return a
    if field == "battery_max_discharging_current":
        knee_short = DEYE_DISCHARGE_SHORT_SLOPE * DEYE_DISCHARGE_SHORT_KNEE_A - DEYE_DISCHARGE_SHORT_ICPT_A
        if a >= DEYE_DISCHARGE_SHORT_KNEE_A - knee_short:
            reg = a + knee_short
        else:
            reg = max(a, (a - DEYE_DISCHARGE_SHORT_ICPT_A) / (1.0 - DEYE_DISCHARGE_SHORT_SLOPE))
    elif field == "battery_grid_charging_current":
        reg = a / DEYE_GRID_CHARGE_GAIN
    else:
        reg = a + DEYE_CHARGE_SHORT_A
    return min(float(round(reg)), DEYE_CURRENT_MAX_A)


# The write budget knobs (handoff 2026-09-26). The defaults ARE today's
# behaviour; an internal tool sweeps candidates over the plan and
# the site picks the values, which then move into the deye.py constants.
WRITER_KNOBS = {"charge_deadband_a": DEYE_CLAMP_DEADBAND_A, "discharge_deadband_a": DEYE_DISCHARGE_DEADBAND_A,
                "grid_deadband_a": DEYE_GRID_CHARGE_DEADBAND_A, "quant_a": DEYE_CURRENT_QUANT_A,
                "segment_mean": False}


def _knobs(knobs: dict | None) -> dict:
    return dict(WRITER_KNOBS, **(knobs or {}))


def quantise_record(cmd: dict, step_a: float) -> dict:
    """The record's currents on the quantisation grid: UP for the grid charge
    and the clamp that caps it (a charge that must land), DOWN for the
    discharge clamp and the plateau clamp (ceilings)."""
    if float(step_a) <= 1.0:
        return cmd
    grid = bool(cmd.get("battery_grid_charging"))
    cmd["battery_grid_charging_current"] = quantise_amps(cmd["battery_grid_charging_current"], step_a, up=True)
    cmd["battery_max_charging_current"] = quantise_amps(cmd["battery_max_charging_current"], step_a, up=grid)
    cmd["battery_max_discharging_current"] = quantise_amps(cmd["battery_max_discharging_current"], step_a, up=False)
    return cmd


# The intents whose currents the segment mean replaces: a sale's discharge
# clamp, a grid charge's current and cap. A self-balance plateau keeps its own
# per-step clamp (the margin and the charge deadband already cover it), and
# pv_export has no current to average.
SEGMENT_MEAN_INTENTS = {"export": ("battery_max_discharging_current",),
                        "self_supply": ("battery_max_discharging_current",),
                        "grid_charge": ("battery_grid_charging_current", "battery_max_charging_current")}


def segment_of(intents: list, i: int) -> tuple[int, int]:
    """[start, end) of the run of the same intent around index i."""
    a = i
    while a > 0 and intents[a - 1] == intents[i]:
        a -= 1
    b = i + 1
    while b < len(intents) and intents[b] == intents[i]:
        b += 1
    return a, b


def compile_step(step: dict, pack_v: float | None, margin: bool = True,
                 knobs: dict | None = None, pack_i: float | None = None) -> tuple[dict, dict | None]:
    """The step in force and the step after it, each through deye_command with
    the writer's margin, the current fields calibrated for the register's
    offset and put on the knobs' quantisation grid. pack_v None (sensor
    unavailable) compiles at nominal.

    With the segment_mean knob (lever 5 of the write budget, 2026-09-26) a run
    of steps with one intent is written ONCE, at the mean of the run's
    currents: the plan's power is a sawtooth quarter by quarter (the LP
    follows the 15-minute price shape), so a deadband saves little, while the
    energy of the run at its mean is the same energy. The run is anchored at
    its start through the plan's own rows (`rows`, `index`), so every tick in
    it computes the same value; a refresh that moves the mean goes through the
    deadbands like any other change.

    With the measured pack current `pack_i` (plan sign, + discharges) each row
    compiles at the port voltage its own power will pull (loaded_voltage from
    the open-circuit voltage pack_v + R * pack_i), so the pack delivers the
    plan's watts rather than the watts at whatever the voltage read at the
    tick. Without it (offline callers, tests) pack_v is used as is."""
    v = float(pack_v) if pack_v else DEYE_PACK_V
    v_oc = v + DEYE_PACK_R_OHM * float(pack_i) if (pack_v and pack_i is not None) else None
    k = _knobs(knobs)

    def raw(row, cut):
        sell = row.get("unit_prod_price")
        soc = row.get("SOC_opt")          # the plan's SOC after the step, a fraction; drives the export rule
        p_batt = float(row["P_batt"])
        if step.get("physics_v2"):
            # Loss map v2: P_batt is the DC bus; the pack moves the port power
            # that puts the PLANNED cell power through the cells, so the SOC
            # follows the plan and the quadratic loss lands on the grid, where
            # the stress cost already prices it.
            p_batt = bus_to_port_w(p_batt, ETA_C_V2, ETA_D_V2)
        v_row = loaded_voltage(v_oc, p_batt) if v_oc is not None else v
        return deye_command(float(row["P_grid"]), p_batt, v_row, micro_cut=bool(cut),
                            pv_curtail_w=float(row.get("P_PV_curtailment") or 0.0),
                            sell=float(sell) if sell is not None else None, margin=margin,
                            soc_pct=float(soc) * 100.0 if soc is not None else None)

    def finish(cmd):
        for f in CURRENT_FIELDS:
            cmd[f] = calibrate_amps(cmd[f], f)
        return quantise_record(cmd, k["quant_a"])

    cur = raw(step["row"], step.get("micro_cut"))
    nxt = raw(step["next_row"], step.get("next_micro_cut")) if step.get("next_row") else None
    rows = step.get("rows")
    if k.get("segment_mean") and rows and step.get("index") is not None:
        cuts = step.get("cuts") or [False] * len(rows)
        cmds = [raw(r, cuts[j] if j < len(cuts) else False) for j, r in enumerate(rows)]
        intents = [c["intent"] for c in cmds]
        i = int(step["index"])
        a, b = segment_of(intents, i)
        fields = SEGMENT_MEAN_INTENTS.get(intents[i], ())
        if b - a > 1 and fields:
            for f in fields:
                mean = round(sum(float(cmds[j][f]) for j in range(a, b)) / (b - a))
                cur[f] = float(mean)
                if nxt is not None and i + 1 < b:
                    nxt[f] = float(mean)
    return finish(cur), (finish(nxt) if nxt is not None else None)


def baseline_record() -> dict:
    rec = dict(DEYE_BASELINE)
    rec["intent"] = "baseline"
    rec["tier"] = DEYE_TIER
    return rec


def held_record(cmd: dict, next_cmd: dict | None) -> tuple[dict, bool]:
    """THE HOLD RULE: a register is written only for an intent the plan holds
    for two consecutive steps. The LP's marginal step (one step of export
    between two of self-balance, the last step of the horizon) is the
    baseline. (record, held)."""
    if next_cmd is None or next_cmd.get("intent") != cmd.get("intent"):
        return baseline_record(), True
    return cmd, False


def off_baseline(record: dict) -> dict:
    """The writer fields on which the record differs from the baseline."""
    return {f: record[f] for f in WRITER_FIELDS if not same(DEYE_BASELINE[f], record[f])}


def writer_diff(standing: dict, record: dict, deadband_a: float = DEYE_CLAMP_DEADBAND_A,
                knobs: dict | None = None) -> dict:
    """field -> [standing, wanted] for every writer field that has to move.
    The charge clamp goes through clamp_write (the charge deadband, and the
    lift on a grid charge), the discharge clamp through its own deadband, the
    grid charging current through setpoint_write (its deadband, a move to or
    from 0 A always written); every other field moves on any difference.
    `deadband_a` is the charge deadband (kept for its callers); the knobs
    carry the other two."""
    k = _knobs(knobs)
    out = {}
    for f in WRITER_FIELDS:
        s, w = standing.get(f), record[f]
        if f == "battery_max_charging_current":
            # the charge clamp rests at 0 A too (pv_export banks nothing), so
            # a small solar charge (7 A seen 2026-10-02 11:15) starts and stops
            sa = _as_float(s)
            if sa is not None and (abs(sa) < 0.5) != (abs(float(w)) < 0.5):
                v, wrote = float(w), True
            else:
                v, wrote = clamp_write(sa, float(w), deadband_a, lift=bool(record.get("battery_grid_charging")))
            if wrote:
                out[f] = [s, v]
        elif f == "battery_max_discharging_current":
            # 0 A is the discharge clamp's rest (an idle step holds the pack
            # there), so a move to or from it is always written: a 20 A band
            # would otherwise keep a small self-supply (6-12 A at night) from
            # ever starting, or from ever stopping on the next idle step.
            v, wrote = setpoint_write(_as_float(s), float(w), k["discharge_deadband_a"], baseline=0.0)
            if wrote:
                out[f] = [s, v]
        elif f == "battery_grid_charging_current":
            v, wrote = setpoint_write(_as_float(s), float(w), k["grid_deadband_a"],
                                      baseline=float(DEYE_BASELINE[f]))
            if wrote:
                out[f] = [s, v]
        elif not same(s, w):
            out[f] = [s, w]
    return out


def order_writes(diff: dict) -> list[tuple[str, object]]:
    """The diff as an ordered write list: first the fields going BACK to the
    baseline in restore order (the old intent is taken down, dangerous
    first), then the fields leaving it in entry order (the new intent is set
    up, dangerous last)."""
    back = [f for f in RESTORE_ORDER if f in diff and same(DEYE_BASELINE[f], diff[f][1])]
    fwd = [f for f in WRITER_FIELDS if f in diff and f not in back]
    return [(f, diff[f][1]) for f in back] + [(f, diff[f][1]) for f in fwd]


# ---- the phantom guard (2026-09-26) -----------------------------------------------
#
# The Solarman settings block (the 300 s poll of the deye_p3 profile) returned
# option zero for every register in it for one cycle at 10:25, 16:11 and 17:44
# on the first live day: work mode Export First, export surplus off, peak
# shaving off, program 6 SOC 0, with no service context, and the registers the
# writer never touches recovered by themselves at the next poll. The writer
# "restored" values that already stood (six of the day's 24 writes), and a
# phantom zero on a clamp the plan wants at zero would make it skip a needed
# write. A forced entity update does not re-poll the block (the integration
# schedules a group only when its counter hits the interval), so the guard is
# pure: a standing value that differs from what the writer last saw, with no
# write of its own since, is held against the writer's memory for one tick and
# accepted when the next tick reads the same value again. A real change from
# outside (the kill switch, a hand on the inverter) is therefore acted on one
# tick late; a phantom costs nothing.

def guard_standing(memory: dict | None, seen_last: dict, standing: dict) -> tuple[dict, list]:
    """(the standing record to diff against, the suspect fields as [field,
    read, memory]). `memory` is what the writer believes stands (its last used
    record, updated by its own writes; None on the first tick after a reload),
    `seen_last` is the raw read of the previous tick. An unavailable lever
    (None) is never suspect: that is the degraded path."""
    if memory is None:
        return dict(standing), []
    used, suspect = {}, []
    for f, raw in standing.items():
        mem = memory.get(f)
        if raw is None or mem is None or same(raw, mem):
            used[f] = raw
        elif f in seen_last and seen_last[f] is not None and same(seen_last[f], raw):
            used[f] = raw                      # the second sighting: it is real
        else:
            used[f] = mem
            suspect.append([f, raw, mem])
    return used, suspect


def guard_memory(raw: dict, standing: dict, written: list, failed: list) -> tuple[dict, dict]:
    """(believed, seen) for the guard memory after a tick. A write that read
    back is what stands now. A write that did not read back may still have
    landed late (2026-10-01 21:30: a poll in flight put the old value back and
    the block did not re-poll inside the readback), so its field is forgotten
    (None) and the next tick takes its read as it is instead of holding it."""
    seen, believed = dict(raw), dict(standing)
    for f, v, _lat in written:
        seen[f] = v
        believed[f] = v
    for f, _v, _lat in failed:
        believed[f] = None
    return believed, seen


# ---- the ceilings (2026-09-27) ----------------------------------------------------
#
# A ceiling is a safety cap above the plan: the writer takes the lower of the
# record's value and the ceiling on a current field, in EVERY mode and on
# every fallback record (off, stale, degraded, held), because the baseline
# stands the clamps at nameplate. A standing value above a ceiling is always
# written, through any deadband, and the wrapper executes those writes
# (`safety_writes`) even in dry mode. The first ceiling is the SOC-floor kill:
# the real SOC under the planner's floor holds the discharge clamp at 0 A
# (2026-09-27 02:00: the pack sat at 6 % against a 10 % floor, discharging to
# the house, after an FCC miscount and a 30-minute drift).

SOC_FLOOR_RELEASE_PTS = 2.0


def soc_floor_tripped(prev: bool | None, soc_pct: float | None, floor_pct: float | None,
                      release_pts: float = SOC_FLOOR_RELEASE_PTS) -> bool:
    """The SOC-floor latch. It trips when the SOC falls below the floor and
    releases at floor + `release_pts`. `prev` None (the first tick after a
    reload) decides without hysteresis. An unavailable SOC or floor keeps the
    previous state; a floor of 0 never trips."""
    if floor_pct is not None and float(floor_pct) <= 0:
        return False
    if soc_pct is None or floor_pct is None:
        return bool(prev)
    soc, floor = float(soc_pct), float(floor_pct)
    if soc < floor:
        return True
    if prev and soc < floor + float(release_pts):
        return True
    return False


# The second ceiling is the HEAT BACKSTOP (2026-09-30): at a pack
# temperature at or above the cut (the live 1 h mean, not the conservatism
# ramp's latched value) every battery current field is capped at the cut
# power, charge and discharge, released 1 °C under the cut. A last resort
# behind the temperature ramp (objective.conservatism); both the cut and the
# power are helpers. A power of 0 or less disables it, so a helper that was
# never seeded (it sits at its minimum) cannot stop the pack.
HEAT_CUT_RELEASE_C = 1.0
HEAT_CUT_FALLBACK_V = 56.0        # no pack voltage: a high V gives fewer amps, so the cap errs low
HEAT_CUT_STEP_A = 5.0             # rounded DOWN to 5 A: the pack voltage's drift does not cost a write a tick
HEAT_CUT_FIELDS = ("battery_max_charging_current", "battery_grid_charging_current",
                   "battery_max_discharging_current")


def heat_cut_tripped(prev: bool | None, temp_c: float | None, cut_c: float | None,
                     release_c: float = HEAT_CUT_RELEASE_C) -> bool:
    """The heat-backstop latch. It trips at a temperature at or above the cut
    and releases under cut - `release_c`. `prev` None (the first tick after a
    reload) decides without hysteresis. An unavailable temperature or cut
    keeps the previous state."""
    if temp_c is None or cut_c is None:
        return bool(prev)
    t, cut = float(temp_c), float(cut_c)
    if t >= cut:
        return True
    if prev and t > cut - float(release_c):
        return True
    return False


def heat_cut_amps(power_kw: float | None, pack_v: float | None) -> dict:
    """field -> register ceiling in A that holds the pack PORT at `power_kw`
    or just under it (through calibrate_amps, so the delivered current is the
    cap, then down to a HEAT_CUT_STEP_A step). {} when the power is missing or
    0 (disabled)."""
    if power_kw is None or float(power_kw) <= 0.0:
        return {}
    v = float(pack_v) if pack_v else HEAT_CUT_FALLBACK_V
    a = float(power_kw) * 1000.0 / v
    return {f: float(int(calibrate_amps(a, f) // HEAT_CUT_STEP_A) * HEAT_CUT_STEP_A) for f in HEAT_CUT_FIELDS}


def writer_ceilings(soc_floor_tripped: bool = False, heat_cut_a: dict | None = None) -> dict:
    """field -> ceiling in A, from the latched safety conditions. Where two
    ceilings hold the same field the lower one wins."""
    out = {f: float(a) for f, a in (heat_cut_a or {}).items()}
    if soc_floor_tripped:
        out["battery_max_discharging_current"] = 0.0
    return out


def apply_ceilings(record: dict, ceilings: dict | None) -> dict:
    """The record with every ceilinged field at most its ceiling."""
    rec = dict(record)
    for f, cap in (ceilings or {}).items():
        rec[f] = min(float(rec[f]), float(cap))
    return rec


def ceiling_writes(standing: dict, ceilings: dict | None) -> list:
    """[field, ceiling] for every ceilinged field that stands above its
    ceiling or is unknown: the writes a tick executes in any mode."""
    out = []
    for f, cap in (ceilings or {}).items():
        s = _as_float(standing.get(f))
        if s is None or s > float(cap) + 0.5:
            out.append([f, float(cap)])
    return out


def writer_tick(mode: str, standing: dict, step: dict | None, pack_v: float | None, now: datetime,
                counts: dict | None = None, deadband_a: float = DEYE_CLAMP_DEADBAND_A,
                suspect: list | None = None, knobs: dict | None = None, ceilings: dict | None = None,
                pack_i: float | None = None, restore: bool = False) -> dict:
    """One tick, everything but the reads and the writes. `standing` is the
    writer fields as read from HA (None = unavailable), `step` is
    step_in_force's answer. The document is what the wrapper publishes and
    archives; `writes` is the ordered list it executes in live mode (and on a
    restore), `written`/`failed` are filled by fold_writes afterwards.
    `standing` is what the tick diffed against (after guard_standing) and
    `suspect` what the guard held back, so the archive can be replayed.
    `ceilings` (field -> A) cap the record whatever the mode or the fallback;
    `safety_writes` are the writes that bring a field down to its ceiling,
    which the wrapper executes in every mode.
    `restore` (dry only: leaving live, or a restore still pending) diffs
    against the baseline like off. The plan's intent is still reported, but
    a restore never writes the plan's values (bug 2026-09-30: 188 A grid
    charge written at 00:54 after a live -> dry switch)."""
    doc = {"tick_ts": now.isoformat(timespec="seconds"), "mode": mode, "plan_ts": None,
           "intent": None, "next_intent": None, "held": False, "record": {}, "diff": {}, "writes": [],
           "written": [], "failed": [], "write_counts": dict(counts or {}), "status": "ok",
           "physics_v2": False, "unavailable": [f for f in WRITER_FIELDS if standing.get(f) is None],
           "standing": {f: standing.get(f) for f in WRITER_FIELDS}, "suspect": [list(x) for x in (suspect or [])]}
    if mode != "off" and (pack_v is None or not float(pack_v)):
        doc["unavailable"].append("pack_v")      # spec section 4: an unavailable entity is a baseline attempt
    record, status = baseline_record(), "ok"
    if mode == "off":
        status = "off"
    elif step is None or step.get("stale"):
        status = "stale"
        doc["plan_ts"] = step["plan_ts"] if step else None
    else:
        cmd, nxt = compile_step(step, pack_v, margin=True, knobs=knobs, pack_i=pack_i)
        record, held = held_record(cmd, nxt)
        doc.update(plan_ts=step["plan_ts"], intent=cmd["intent"], next_intent=nxt["intent"] if nxt else None,
                   held=held, physics_v2=bool(step.get("physics_v2")))
    if doc["unavailable"] and mode != "off":
        record, status = baseline_record(), "degraded"
    if mode == "dry" and restore:
        record = baseline_record()
        status = "restore" if status == "ok" else status
    if mode == "dry" and status == "ok":
        status = "dry"
    ceilings = dict(ceilings or {})
    record = apply_ceilings(record, ceilings)
    doc["ceilings"] = ceilings
    doc["record"] = off_baseline(record)
    diff = writer_diff(standing, record, deadband_a, knobs=knobs)
    safety = ceiling_writes(standing, ceilings)
    for f, cap in safety:                      # through any deadband: a ceiling is never left exceeded
        diff[f] = [standing.get(f), cap]
    doc["diff"] = diff
    doc["writes"] = [[f, v] for f, v in order_writes(diff)]
    doc["safety_writes"] = [[f, v] for f, v in doc["writes"] if f in dict(safety)]
    doc["status"] = status
    return doc


def fold_writes(doc: dict, results: list) -> dict:
    """Fold the wrapper's write results [(field, value, latency_s | None)] into
    the tick document. Every attempt counts (the EEPROM question is about
    writes sent); a None latency is a readback timeout and degrades the tick."""
    counts = dict(doc.get("write_counts") or {})
    written, failed = [], []
    for f, v, lat in results:
        counts[f] = int(counts.get(f, 0)) + 1
        (written if lat is not None else failed).append([f, v, lat])
    doc.update(written=written, failed=failed, write_counts=counts)
    if failed:
        doc["status"] = "degraded"
    return doc


def _tick_path(writer_dir: str, day) -> str:
    return os.path.join(writer_dir, day.strftime("%Y-%m-%d") + ".jsonl")


def append_tick(writer_dir: str, now_local: datetime, doc: dict) -> str:
    """One line per tick, one file per local day."""
    os.makedirs(writer_dir, exist_ok=True)
    path = _tick_path(writer_dir, now_local.date())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(doc, separators=(",", ":"), default=str) + "\n")
    return path


def last_counts(writer_dir: str, now_local: datetime) -> dict:
    """The write counters of the last archived tick, from today's file or
    yesterday's (the first tick of a day), so a pyscript reload resumes the
    count instead of restarting it."""
    for back in (0, 1):
        path = _tick_path(writer_dir, (now_local - timedelta(days=back)).date())
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            continue
        if lines:
            return dict(json.loads(lines[-1]).get("write_counts") or {})
    return {}


def day_was_live(writer_dir: str, day) -> bool:
    """Whether the writer drove the inverter on `day`: any archived tick of
    that day in live mode. The ledger and the yesterday slice key the real
    pack on this, not on the mode NOW (2026-09-27: a switch to dry at 02:01
    turned the first live day's lanes back into the virtual pack)."""
    try:
        with open(_tick_path(writer_dir, day), encoding="utf-8") as f:
            for ln in f:
                if ln.strip() and json.loads(ln).get("mode") == "live":
                    return True
    except (OSError, ValueError):
        pass
    return False


def wants_writes(mode: str, restore: bool = False, pending: bool = False) -> bool:
    """Whether a tick executes its ordered writes. Live and off always do: in
    off the baseline is ENFORCED every tick, not merely published, so a
    restore that failed on the way out of live is retried until the diff is
    empty (review 2026-09-26: a one-shot restore left a sale standing with a
    fresh heartbeat, so the dead man could not see it). Dry writes only a
    restore that is still pending after leaving live, or an explicit one."""
    return mode in ("live", "off") or bool(restore) or bool(pending)


# ---- the live anchor of the settled chain (spec 3.6) -----------------------------
#
# The scoreboard's virtual pack and the real pack part company the moment the
# writer goes live mid-day (2026-09-26 11:51: virtual 19 %, real 85 %, and the
# settled lane booked the evening sale away at the 10 % floor). The writer
# drops this file on the live switch; the settled chain re-anchors at that
# step on that day, and at midnight on the real midnight SOC on every later
# live day.

def day_step(now: datetime) -> int:
    """15-minute step of `now` counted from its LOCAL midnight through UTC (the
    DST fold: the long day has 100 steps, the second 02:30 is step 14)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    return int((now.astimezone(timezone.utc) - midnight.astimezone(timezone.utc)).total_seconds() // (STEP_MIN * 60))


def write_anchor(path: str, now: datetime, soc_pct: float) -> dict:
    doc = {"date": now.date().isoformat(), "step": day_step(now), "soc_pct": float(soc_pct),
           "ts": now.isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    return doc


def read_anchor(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def soc_anchor_for_day(anchor: dict | None, day, midnight_soc_pct: float | None) -> tuple | None:
    """(step, soc_pct) the settled chain anchors on for `day` in live mode: the
    switch step on the switch day, else midnight on the real midnight SOC,
    else None (the archive's own anchor stands)."""
    if anchor and anchor.get("date") == day.isoformat() and anchor.get("soc_pct") is not None:
        return int(anchor.get("step") or 0), float(anchor["soc_pct"])
    if midnight_soc_pct is not None:
        return 0, float(midnight_soc_pct)
    return None
