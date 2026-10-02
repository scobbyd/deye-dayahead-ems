"""The writer: the pure functions in emhasscore/writer.py, through the facade."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import emhass_core as core
from golden import fixtures as F

AMS = "Europe/Amsterdam"
Z = ZoneInfo(AMS)


def local(y, m, d, hh, mm, ss=0, fold=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=Z, fold=fold)


# (P_grid, P_batt) pairs that deye_command reads as each intent
INTENT_ROW = {"export": (-9000.0, 8000.0), "grid_charge": (2000.0, -6000.0), "self_supply": (0.0, 800.0),
              "self_balance": (0.0, -5000.0), "pv_export": (-3000.0, 0.0)}


def _doc(plan_ts, t0, n, intents, replay=False, status="Optimal", micro_cut=None, sell=0.08):
    """A minimal archived plan whose rows cycle through `intents`."""
    rows = []
    for k, t in enumerate(core.step_times(t0, n)):
        g, b = INTENT_ROW[intents[k % len(intents)]]
        rows.append({"timestamp": core.grid.stamp_z(t), "P_grid": g, "P_batt": b, "P_PV_curtailment": 0.0,
                     "unit_prod_price": sell})
    doc = {"plan_ts": plan_ts.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS, "soc_init": 0.5,
           "optim_status": status, "rows": rows}
    if replay:
        doc["replay"] = True
    if micro_cut is not None:
        doc["micro_cut"] = micro_cut
    return doc


# ---- step_in_force --------------------------------------------------------------

def test_step_in_force_picks_the_newest_organic_plan_covering_now(tmp_path):
    arch = str(tmp_path / "plans")
    t0 = local(2026, 9, 27, 10, 15)
    core.write_plan_archive(arch, local(2026, 9, 27, 9, 43),
                            _doc(local(2026, 9, 27, 9, 43), local(2026, 9, 27, 9, 45), 8, ["self_balance"]))
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 13),
                            _doc(local(2026, 9, 27, 10, 13), t0, 8, ["export", "export", "grid_charge"]))
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 14),
                            _doc(local(2026, 9, 27, 10, 14), t0, 8, ["pv_export"], replay=True))
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 14, 30),
                            _doc(local(2026, 9, 27, 10, 14, 30), t0, 8, ["pv_export"], status="Infeasible"))
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 43),
                            _doc(local(2026, 9, 27, 10, 43), local(2026, 9, 27, 10, 45), 8, ["self_supply"]))
    s = core.step_in_force(arch, local(2026, 9, 27, 10, 30, 20))
    assert s["plan_ts"] == local(2026, 9, 27, 10, 13).isoformat()       # not the replay, not the failed solve, not the future one
    assert s["index"] == 1 and s["n"] == 8
    assert s["row"]["P_batt"] == 8000.0 and s["next_row"]["P_grid"] == 2000.0
    assert s["stale"] is False and s["micro_cut"] is False and s["next_micro_cut"] is False


def test_step_in_force_indexes_by_floor_and_returns_none_outside_the_horizon(tmp_path):
    arch = str(tmp_path / "plans")
    t0 = local(2026, 9, 27, 10, 15)
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 13),
                            _doc(local(2026, 9, 27, 10, 13), t0, 2, ["export", "grid_charge"],
                                 micro_cut=[True, False]))
    assert core.step_in_force(arch, local(2026, 9, 27, 10, 14, 59)) is None          # before t0
    s = core.step_in_force(arch, local(2026, 9, 27, 10, 29, 59))
    assert s["index"] == 0 and s["next_row"]["P_grid"] == 2000.0
    assert s["micro_cut"] is True and s["next_micro_cut"] is False
    s = core.step_in_force(arch, local(2026, 9, 27, 10, 30, 0))
    assert s["index"] == 1 and s["next_row"] is None and s["next_micro_cut"] is False   # last step of the horizon
    assert core.step_in_force(arch, local(2026, 9, 27, 10, 45, 20)) is None          # past the horizon
    assert core.step_in_force(str(tmp_path / "empty"), local(2026, 9, 27, 10, 20)) is None


def test_step_in_force_flags_a_plan_older_than_60_minutes(tmp_path):
    arch = str(tmp_path / "plans")
    core.write_plan_archive(arch, local(2026, 9, 27, 9, 43),
                            _doc(local(2026, 9, 27, 9, 43), local(2026, 9, 27, 9, 45), 40, ["export"]))
    # 60, not 45: plan_ts is the solve START, so the :13 plan is 47 min 20 s
    # old at the :00:20 tick. 45 tolerated zero missed refreshes there and
    # one lost :43 solve would have taken a grid charge down for a step.
    assert core.step_in_force(arch, local(2026, 9, 27, 10, 42, 59))["stale"] is False
    assert core.step_in_force(arch, local(2026, 9, 27, 10, 43, 1))["stale"] is True
    assert core.WRITER_STALE_MIN == 60.0


def test_step_in_force_walks_the_dst_fold_through_utc():
    """The synthetic 10-24 23:05 record covers the autumn change-over. The two
    02:30 wall-clock ticks are four steps apart on the plan grid."""
    first = core.step_in_force(F.SYN_PLANS, local(2026, 10, 25, 2, 30, 20, fold=0))
    second = core.step_in_force(F.SYN_PLANS, local(2026, 10, 25, 2, 30, 20, fold=1))
    assert first is not None and second is not None
    assert first["plan_ts"] == second["plan_ts"]
    assert second["index"] - first["index"] == 4
    assert first["row"]["timestamp"] == "2026-10-25T00:30:00.000Z"
    assert second["row"]["timestamp"] == "2026-10-25T01:30:00.000Z"


# ---- compile, hold, diff, order --------------------------------------------------

STANDING_BASELINE = {f: core.DEYE_BASELINE[f] for f in core.WRITER_FIELDS}
CMD_EXPORT = core.deye_command(-9000.0, 8000.0, 51.2)                  # discharge clamp 156 A
CMD_GRID_CHARGE = core.deye_command(2000.0, -6000.0, 51.2)             # 117 A floor, clamp 240, program 6 SOC 100


def _standing_of(cmd):
    return {f: cmd[f] for f in core.WRITER_FIELDS}


def _step(cur, nxt, micro_cut=False, next_micro_cut=False, stale=False, sell=0.08):
    def row(intent):
        g, b = INTENT_ROW[intent]
        return {"timestamp": "2026-09-27T08:15:00.000Z", "P_grid": g, "P_batt": b, "P_PV_curtailment": 0.0,
                "unit_prod_price": sell}
    return {"plan_ts": "2026-09-27T10:13:00+02:00", "t0": "2026-09-27T10:15:00+02:00", "n": 8, "index": 0,
            "row": row(cur), "next_row": row(nxt) if nxt else None,
            "micro_cut": micro_cut, "next_micro_cut": next_micro_cut, "stale": stale}


def test_compile_step_v2_writes_the_port_power_for_the_planned_cells():
    st = _step("grid_charge", "grid_charge")
    v1, _ = core.compile_step(st, 54.0, margin=False)
    v2, _ = core.compile_step(dict(st, physics_v2=True), 54.0, margin=False)
    # INTENT_ROW grid_charge is P_batt -6.000 W on the bus: port 5.958,6 W (bus_to_port_w)
    port = core.bus_to_port_w(-6000.0, core.ETA_C_V2, core.ETA_D_V2)
    assert port == pytest.approx(-5958.6, abs=0.5)
    assert v2["battery_grid_charging_current"] < v1["battery_grid_charging_current"]
    assert v2["intent"] == v1["intent"]


def test_compile_step_v2_discharge_converts_the_port_power_and_keeps_the_intent():
    st = _step("export", "export")
    v1, _ = core.compile_step(st, 54.0, margin=False)
    v2, _ = core.compile_step(dict(st, physics_v2=True), 54.0, margin=False)
    port = core.bus_to_port_w(8000.0, core.ETA_C_V2, core.ETA_D_V2)
    assert port == pytest.approx(8023.5, abs=1.0) and port > 8000.0    # cells 8.188 W (bus / eta_d), then the battery-side model
    assert v2["intent"] == v1["intent"] == "export"
    assert v2["battery_max_discharging_current"] >= v1["battery_max_discharging_current"]


def test_writer_tick_records_the_physics_of_the_step():
    st = _step("export", "export")
    assert core.writer_tick("dry", STANDING_BASELINE, st, 51.2, NOW, {})["physics_v2"] is False
    assert core.writer_tick("dry", STANDING_BASELINE, dict(st, physics_v2=True), 51.2, NOW, {})["physics_v2"] is True


def test_step_in_force_reads_the_plans_own_physics(tmp_path):
    arch = str(tmp_path / "plans")
    t0 = local(2026, 9, 27, 10, 15)
    plan_ts = local(2026, 9, 27, 10, 13)
    doc = _doc(plan_ts, t0, 8, ["grid_charge"])
    core.write_plan_archive(arch, plan_ts, doc)
    now = local(2026, 9, 27, 10, 20)
    assert core.step_in_force(arch, now)["physics_v2"] is False      # archived before v2: no flag
    arch2 = str(tmp_path / "plans2")
    core.write_plan_archive(arch2, plan_ts, dict(doc, knobs={"physics_v2": 1.0}))
    assert core.step_in_force(arch2, now)["physics_v2"] is True


def test_writer_fields_are_the_baseline_minus_the_never_written_ones():
    assert set(core.WRITER_FIELDS) | set(core.WRITER_NEVER) == set(core.DEYE_BASELINE)
    assert not set(core.WRITER_FIELDS) & set(core.WRITER_NEVER)
    assert set(core.RESTORE_ORDER) == set(core.WRITER_FIELDS)
    assert core.WRITER_NEVER == ("energy_pattern", "zero_export_power", "export_surplus_power", "program_6_power")
    # entry: wasteful, costly, dangerous with the TOU command last; restore: the command first
    assert [core.DEYE_TIER[f] for f in core.WRITER_FIELDS] == ["wasteful"] * 4 + ["costly"] * 2 + ["dangerous"] * 3
    assert core.WRITER_FIELDS[-1] == "program_6_soc" and core.RESTORE_ORDER[:3] == (
        "program_6_soc", "battery_grid_charging", "battery_grid_charging_current")
    assert set(core.WRITER_ENTITY) == set(core.WRITER_FIELDS)


def test_writer_entity_map_agrees_with_the_go_live_harness():
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "golive" / "levers.py"
    spec = importlib.util.spec_from_file_location("levers", p)
    levers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(levers)
    for f, e in core.WRITER_ENTITY.items():
        assert levers.ALL[f] == e, f


def test_same_normalises_switch_strings_and_register_resolution():
    assert core.same("on", True) and core.same("off", False) and not core.same("off", True)
    assert core.same(True, True) and not core.same(False, True)
    assert core.same("240.0", 240.0) and core.same(239.6, 240.0) and not core.same(239.0, 240.0)
    assert core.same("Export First", "Export First") and not core.same("Zero Export To Load", "Export First")
    assert not core.same(None, 240.0) and not core.same("unavailable", 240.0)


def test_compile_step_reads_the_rows_and_carries_the_micro_cut():
    cur, nxt = core.compile_step(_step("export", "grid_charge", micro_cut=True), 51.2)
    assert cur["intent"] == "export" and cur["microinverter_export_cut_off"] is True
    assert cur["battery_max_discharging_current"] == pytest.approx(160.0)     # 156 A asked + the 4,3 A the register loses past the knee
    assert nxt["intent"] == "grid_charge" and nxt["microinverter_export_cut_off"] is False
    assert nxt["battery_grid_charging_current"] == pytest.approx(123.0) and nxt["battery_max_charging_current"] == 240.0   # 117 / 0,951
    cur, nxt = core.compile_step(_step("export", None), 51.2)
    assert nxt is None
    # no pack voltage: nominal
    cur, _ = core.compile_step(_step("export", "export"), None)
    assert cur["battery_max_discharging_current"] == core.calibrate_amps(core.deye_amps(8000.0, core.DEYE_PACK_V))
    # 0 A (hold the pack) and the 240 A nameplate are not calibrated
    pv, _ = core.compile_step(_step("pv_export", "pv_export"), 51.2)
    assert pv["battery_max_charging_current"] == 0.0 and pv["battery_max_discharging_current"] == 0.0


def test_compile_step_uses_the_writer_margin_on_a_curtailed_plateau():
    s = _step("self_balance", "self_balance", sell=-0.01)
    s["row"]["P_PV_curtailment"] = 800.0
    cur, _ = core.compile_step(s, 51.2)
    assert cur["export_surplus"] is False
    assert cur["battery_max_charging_current"] == pytest.approx(119.0)     # 5.000 W + 20 % -> 6.000 W, or + 20 A -> 6.024 W = 118 A, + 1 A charge-clamp shortfall


def test_held_record_falls_back_to_baseline_on_a_marginal_step():
    rec, held = core.held_record(CMD_EXPORT, CMD_GRID_CHARGE)
    assert held is True and rec["intent"] == "baseline"
    assert {f: rec[f] for f in core.WRITER_FIELDS} == STANDING_BASELINE
    rec, held = core.held_record(CMD_EXPORT, core.deye_command(-5000.0, 4000.0, 51.2))
    assert held is False and rec is CMD_EXPORT
    rec, held = core.held_record(CMD_EXPORT, None)
    assert held is True


def test_off_baseline_lists_only_the_fields_that_differ():
    assert core.off_baseline(dict(core.DEYE_BASELINE)) == {}
    assert core.off_baseline(CMD_EXPORT) == {"work_mode": "Export First", "grid_peak_shaving": False,
                                             "battery_max_discharging_current": 156.0}
    assert set(core.off_baseline(CMD_GRID_CHARGE)) == {"export_surplus",
                                                       "battery_grid_charging", "battery_grid_charging_current",
                                                       "program_6_soc"}


def test_writer_diff_is_empty_when_the_record_stands():
    assert core.writer_diff(STANDING_BASELINE, dict(core.DEYE_BASELINE)) == {}
    on_off = dict(STANDING_BASELINE, export_surplus="on", battery_grid_charging="off", grid_peak_shaving="on",
                  microinverter_export_cut_off="off", battery_max_charging_current="240.0")
    assert core.writer_diff(on_off, dict(core.DEYE_BASELINE)) == {}
    assert core.writer_diff(_standing_of(CMD_EXPORT), CMD_EXPORT) == {}


def test_writer_diff_keeps_the_charge_clamp_inside_the_deadband_and_lifts_it_on_a_grid_charge():
    st = dict(STANDING_BASELINE, battery_max_charging_current=230.0)
    assert core.writer_diff(st, dict(core.DEYE_BASELINE)) == {}                     # 10 A inside the deadband
    st = dict(STANDING_BASELINE, battery_max_charging_current=200.0)
    assert core.writer_diff(st, dict(core.DEYE_BASELINE)) == {"battery_max_charging_current": [200.0, 240.0]}
    st = dict(STANDING_BASELINE, battery_max_charging_current=230.0)
    d = core.writer_diff(st, CMD_GRID_CHARGE)
    assert d["battery_max_charging_current"] == [230.0, 240.0]                     # lifted although 10 A is inside the deadband
    assert d["program_6_soc"] == [5.0, 100.0] and d["battery_grid_charging"] == [False, True]
    assert d["battery_grid_charging_current"] == [0.0, 117.0] and d["export_surplus"] == [True, False]
    assert "battery_max_charging_current" not in core.writer_diff(STANDING_BASELINE, CMD_GRID_CHARGE)   # at nameplate: nothing to lift


def test_writer_diff_reports_an_unavailable_lever_as_a_write():
    st = dict(STANDING_BASELINE, work_mode=None)
    assert core.writer_diff(st, dict(core.DEYE_BASELINE)) == {"work_mode": [None, "Zero Export To Load"]}


def test_writer_diff_never_touches_the_fields_the_writer_does_not_own():
    grid = [(g, b, c, mc, s) for g in (-3000.0, 0.0, 2500.0) for b in (-6000.0, 0.0, 4000.0)
            for c in (0.0, 1500.0) for mc in (False, True) for s in (-0.01, 0.08)]
    for g, b, c, mc, s in grid:
        cmd = core.deye_command(g, b, 51.2, micro_cut=mc, pv_curtail_w=c, sell=s, margin=True)
        assert set(core.writer_diff(STANDING_BASELINE, cmd)) <= set(core.WRITER_FIELDS)
        for f in core.WRITER_NEVER:
            assert cmd[f] == core.DEYE_BASELINE[f], (f, g, b, c, mc, s)


def test_order_writes_restores_the_old_intent_dangerous_first_then_enters_the_new_one_dangerous_last():
    writes = core.order_writes(core.writer_diff(_standing_of(CMD_GRID_CHARGE), CMD_EXPORT))
    assert [f for f, _v in writes] == [
        "program_6_soc", "battery_grid_charging", "battery_grid_charging_current",     # back to baseline, dangerous first
        "export_surplus",                                                              # back to baseline, wasteful
        "battery_max_discharging_current",                                             # the new intent, wasteful
        "work_mode", "grid_peak_shaving"]                                              # the new intent, costly last
    assert dict(writes)["program_6_soc"] == 5.0 and dict(writes)["work_mode"] == "Export First"
    into = core.order_writes(core.writer_diff(STANDING_BASELINE, CMD_GRID_CHARGE))
    assert [f for f, _v in into] == ["export_surplus",
                                     "battery_grid_charging_current", "battery_grid_charging", "program_6_soc"]
    back = core.order_writes(core.writer_diff(_standing_of(CMD_EXPORT), dict(core.DEYE_BASELINE)))
    assert [f for f, _v in back] == ["work_mode", "grid_peak_shaving", "battery_max_discharging_current"]


# ---- the tick ---------------------------------------------------------------------

NOW = local(2026, 9, 27, 10, 30, 20)


def test_writer_tick_off_mode_wants_the_baseline_and_carries_no_intent():
    doc = core.writer_tick("off", _standing_of(CMD_EXPORT), None, 51.2, NOW, {})
    assert doc["status"] == "off" and doc["mode"] == "off"
    assert doc["intent"] is None and doc["next_intent"] is None and doc["held"] is False
    assert doc["record"] == {} and doc["plan_ts"] is None
    assert [f for f, _v in doc["writes"]] == ["work_mode", "grid_peak_shaving", "battery_max_discharging_current"]
    assert doc["written"] == [] and doc["failed"] == [] and doc["write_counts"] == {}
    assert doc["tick_ts"] == "2026-09-27T10:30:20+02:00"


def test_writer_tick_dry_mode_reports_the_diff_and_the_ordered_writes():
    doc = core.writer_tick("dry", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, {"work_mode": 2})
    assert doc["status"] == "dry" and doc["intent"] == "export" and doc["next_intent"] == "export"
    assert doc["held"] is False and doc["plan_ts"] == "2026-09-27T10:13:00+02:00"
    assert doc["record"] == {"work_mode": "Export First", "grid_peak_shaving": False,
                             "battery_max_discharging_current": 160.0}
    assert doc["diff"]["work_mode"] == ["Zero Export To Load", "Export First"]
    assert [f for f, _v in doc["writes"]] == ["battery_max_discharging_current", "work_mode", "grid_peak_shaving"]
    assert doc["written"] == [] and doc["write_counts"] == {"work_mode": 2}


def test_writer_tick_live_mode_is_ok_and_holds_a_marginal_step_at_the_baseline():
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW)
    assert doc["status"] == "ok" and doc["record"] != {}
    held = core.writer_tick("live", _standing_of(CMD_EXPORT), _step("export", "grid_charge"), 51.2, NOW)
    assert held["status"] == "ok" and held["held"] is True
    assert held["intent"] == "export" and held["next_intent"] == "grid_charge"
    assert held["record"] == {}
    assert [f for f, _v in held["writes"]] == ["work_mode", "grid_peak_shaving", "battery_max_discharging_current"]


def test_writer_tick_stale_or_missing_plan_wants_the_baseline():
    doc = core.writer_tick("live", _standing_of(CMD_GRID_CHARGE), _step("grid_charge", "grid_charge", stale=True), 51.2, NOW)
    assert doc["status"] == "stale" and doc["intent"] is None and doc["record"] == {}
    assert doc["plan_ts"] == "2026-09-27T10:13:00+02:00"
    assert [f for f, _v in doc["writes"]][:3] == ["program_6_soc", "battery_grid_charging", "battery_grid_charging_current"]
    none = core.writer_tick("live", STANDING_BASELINE, None, 51.2, NOW)
    assert none["status"] == "stale" and none["writes"] == [] and none["plan_ts"] is None


def test_writer_tick_unavailable_lever_is_degraded_and_wants_the_baseline():
    st = dict(_standing_of(CMD_EXPORT), grid_peak_shaving=None)
    doc = core.writer_tick("live", st, _step("export", "export"), 51.2, NOW)
    assert doc["status"] == "degraded" and doc["unavailable"] == ["grid_peak_shaving"]
    assert doc["record"] == {} and doc["intent"] == "export"
    assert ("grid_peak_shaving", True) in [tuple(w) for w in doc["writes"]]
    ok = core.writer_tick("live", _standing_of(CMD_EXPORT), _step("export", "export"), 51.2, NOW)
    assert ok["unavailable"] == [] and ok["status"] == "ok"


def test_writer_tick_without_a_pack_voltage_wants_the_baseline_and_is_degraded():
    """Spec section 4: an unavailable sensor.inverter_battery_voltage is a
    baseline attempt, not a record compiled at nominal (review finding 5)."""
    doc = core.writer_tick("live", _standing_of(CMD_EXPORT), _step("export", "export"), None, NOW)
    assert doc["status"] == "degraded" and doc["record"] == {}
    assert doc["intent"] == "export" and "pack_v" in doc["unavailable"]
    assert [f for f, _v in doc["writes"]] == ["work_mode", "grid_peak_shaving", "battery_max_discharging_current"]


def test_a_restore_that_failed_halfway_is_finished_by_the_next_off_tick():
    """Review finding 1 and 3 at the core level: the first off tick lands the
    dangerous fields and is cut off; the next off tick's writes are exactly
    the rest of the restore, in restore order."""
    standing = _standing_of(CMD_GRID_CHARGE)
    first = core.writer_tick("off", standing, None, 51.2, NOW)
    done = [w for w in first["writes"] if w[0] in ("program_6_soc", "battery_grid_charging", "battery_grid_charging_current")]
    first = core.fold_writes(first, [(f, v, 3.0) for f, v in done] + [("export_surplus", True, None)])
    for f, v, _lat in first["written"]:
        standing[f] = v
    assert first["status"] == "degraded" and first["failed"] == [["export_surplus", True, None]]
    second = core.writer_tick("off", standing, None, 51.2, NOW + timedelta(minutes=15), first["write_counts"])
    assert [f for f, _v in second["writes"]] == ["export_surplus"]
    assert core.wants_writes("off") is True


def test_writer_tick_carries_the_micro_cut_with_the_record():
    doc = core.writer_tick("live", STANDING_BASELINE, _step("self_balance", "self_balance", micro_cut=True), 51.2, NOW)
    assert doc["record"] == {"microinverter_export_cut_off": True}
    assert doc["writes"] == [["microinverter_export_cut_off", True]]


def test_fold_writes_counts_every_attempt_and_marks_a_timeout_degraded():
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, {"work_mode": 3})
    out = core.fold_writes(doc, [("battery_max_discharging_current", 156.0, 4.2), ("work_mode", "Export First", 6.5),
                                 ("grid_peak_shaving", False, None)])
    assert out["written"] == [["battery_max_discharging_current", 156.0, 4.2], ["work_mode", "Export First", 6.5]]
    assert out["failed"] == [["grid_peak_shaving", False, None]]
    assert out["write_counts"] == {"work_mode": 4, "battery_max_discharging_current": 1, "grid_peak_shaving": 1}
    assert out["status"] == "degraded"
    clean = core.fold_writes(core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW),
                             [("battery_max_discharging_current", 156.0, 4.2), ("work_mode", "Export First", 6.5),
                              ("grid_peak_shaving", False, 3.1)])
    assert clean["status"] == "ok" and clean["failed"] == []


def test_append_tick_and_last_counts_round_trip(tmp_path):
    d = str(tmp_path / "writer")
    assert core.last_counts(d, NOW) == {}
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, {"work_mode": 3})
    doc = core.fold_writes(doc, [("work_mode", "Export First", 5.0)])
    path = core.append_tick(d, NOW, doc)
    assert path.endswith("2026-09-27.jsonl")
    core.append_tick(d, NOW + timedelta(minutes=15), core.writer_tick("live", STANDING_BASELINE, None, 51.2,
                                                                       NOW + timedelta(minutes=15), doc["write_counts"]))
    lines = open(path).read().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["tick_ts"] == "2026-09-27T10:30:20+02:00"
    assert core.last_counts(d, NOW + timedelta(hours=1)) == {"work_mode": 4}
    assert core.last_counts(d, local(2026, 9, 28, 0, 5)) == {"work_mode": 4}      # yesterday's file, first tick of a day
    assert core.last_counts(d, local(2026, 9, 29, 0, 5)) == {}                    # two days: nothing to resume


# ---- a whole day over the real archive -------------------------------------------

def _walk_0905(mode="live"):
    """96 ticks over the 09-05 fixture archive, the standing state following the
    writes (a live-mode emulation with a perfect inverter). The archive of that
    day is hourly, so the staleness threshold is 75 min instead of 45."""
    standing = dict(STANDING_BASELINE)
    counts, out = {}, []
    for q in range(96):
        now = F.local(2026, 9, 5, 0, 0, 20) + timedelta(minutes=15 * q)
        # 09-05 was planned HOURLY at :05 (the 30-minute cadence came on 09-07),
        # so at 45 minutes every :00 tick would read stale; 75 covers one hour.
        step = core.step_in_force(F.PLANS, now, stale_min=75.0)
        doc = core.writer_tick(mode, standing, step, 51.2, now, counts)
        if mode == "live":
            doc = core.fold_writes(doc, [(f, v, 1.0) for f, v in doc["writes"]])
            for f, v in doc["writes"]:
                standing[f] = v
        counts = doc["write_counts"]
        out.append(doc)
    return out


def test_writer_day_0905_never_writes_a_dangerous_field_the_plan_did_not_import():
    """The dry-day acceptance rule (spec section 5) as a unit test: a held
    step writes the baseline, and a dangerous field stands only under a
    grid-charge intent the plan holds for two steps."""
    docs = _walk_0905()
    assert sum(1 for d in docs if d["status"] == "ok") >= 90
    for d in docs:
        if d["held"] or d["status"] != "ok":
            assert d["record"] == {}, d["tick_ts"]
        dangerous = [f for f in d["record"] if core.DEYE_TIER[f] == "dangerous"]
        if dangerous:
            assert d["intent"] == "grid_charge" and d["next_intent"] == "grid_charge", d["tick_ts"]
        for f in core.WRITER_NEVER:
            assert f not in d["diff"]
    assert any(d["record"] for d in docs)                       # the day is not all baseline
    total = sum(docs[-1]["write_counts"].values())
    assert 0 < total < 200, total                                # a whole day is well under the EEPROM worry


# ---- the YAML side pinned to the core -------------------------------------------

def test_every_automation_that_touches_the_inverter_is_gated_on_the_arming_switch():
    """Disarmed means hands off: an automation that writes a lever or runs the
    baseline script carries input_boolean.emhass_writer_armed as a condition,
    so a fresh install (no initial:, so off) never writes from YAML either."""
    import pathlib
    import yaml
    path = pathlib.Path(__file__).resolve().parents[1] / "ha" / "packages" / "emhass" / "emhass_writer.yaml"
    doc = yaml.safe_load(path.read_text())
    assert "initial" not in doc["input_boolean"]["emhass_writer_armed"]
    levers = set(core.WRITER_ENTITY.values())
    writing = []
    for a in doc["automation"]:
        acts = str(a["action"])
        if "script.emhass_deye_baseline" in acts or any(e in acts for e in levers):
            writing.append(a["id"])
            assert {"condition": "state", "entity_id": "input_boolean.emhass_writer_armed", "state": "on"} \
                in a["condition"], a["id"]
    assert sorted(writing) == ["emhass_writer_baseline_on_start", "emhass_writer_dead_man",
                               "emhass_writer_grid_charge_cap", "emhass_writer_heat_backstop",
                               "emhass_writer_soc_floor_backstop"]


def test_baseline_script_writes_the_registers_in_restore_order_with_baseline_values():
    import pathlib
    import yaml
    path = pathlib.Path(__file__).resolve().parents[1] / "ha" / "packages" / "emhass" / "emhass_writer.yaml"
    doc = yaml.safe_load(path.read_text())
    seq = doc["script"]["emhass_deye_baseline"]["sequence"]
    by_entity = {e: f for f, e in core.WRITER_ENTITY.items()}
    fields = []
    for s in seq:
        eid = s["target"]["entity_id"]
        f = by_entity[eid]
        fields.append(f)
        want = core.DEYE_BASELINE[f]
        if eid.startswith("switch."):
            assert s["action"] == ("switch.turn_on" if want else "switch.turn_off"), f
        elif eid.startswith("number."):
            assert s["action"] == "number.set_value" and float(s["data"]["value"]) == float(want), f
        else:
            assert s["action"] == "select.select_option" and s["data"]["option"] == want, f
        assert s.get("continue_on_error") is True, f
    assert fields == list(core.RESTORE_ORDER)
    assert doc["input_select"]["emhass_writer_mode"]["options"] == ["dry", "off", "live"]   # dry first: a fresh install writes nothing
    assert "initial" not in doc["input_select"]["emhass_writer_mode"]
    ids = [a["id"] for a in doc["automation"]]
    assert ids == ["emhass_writer_dead_man", "emhass_writer_grid_charge_cap", "emhass_writer_soc_floor_backstop",
                   "emhass_writer_heat_backstop",
                   "emhass_writer_baseline_on_start", "emhass_writer_growatt_cut_notify"]


# ---- review fix: a restore must be retried, and an off tick enforces the baseline ----

def test_wants_writes_enforces_in_live_and_off_and_retries_a_pending_restore_in_dry():
    """Leaving live is not a one-shot: a restore that failed on a readback or a
    raised service call is retried every tick until the diff is empty. An off
    tick always executes (the baseline is enforced, not merely published); a
    dry tick writes only a pending restore."""
    assert core.wants_writes("live") is True
    assert core.wants_writes("off") is True
    assert core.wants_writes("dry") is False
    assert core.wants_writes("dry", pending=True) is True
    assert core.wants_writes("dry", restore=True) is True


def test_a_dry_restore_writes_the_baseline_not_the_plan():
    """Bug 2026-09-30: leaving live for dry with a grid charge in the plan wrote
    the plan's 188 A at 00:54, and a pending restore wrote plan values again at
    01:15 and 02:45. A restore in dry diffs against the baseline like off; the
    plan's intent is still reported."""
    grid = _step("grid_charge", "grid_charge")
    st = _standing_of(CMD_GRID_CHARGE)
    doc = core.writer_tick("dry", st, grid, 51.2, NOW, restore=True)
    assert doc["status"] == "restore" and doc["intent"] == "grid_charge"
    assert doc["record"] == {}
    wanted = dict(doc["writes"])
    assert wanted["battery_grid_charging"] is False and wanted["program_6_soc"] == core.DEYE_BASELINE["program_6_soc"]
    assert all(core.same(core.DEYE_BASELINE[f], v) for f, v in doc["writes"])
    # from the baseline, a pending restore has nothing left to write
    done = core.writer_tick("dry", STANDING_BASELINE, grid, 51.2, NOW, restore=True)
    assert done["writes"] == []
    # without a restore, dry still reports the plan's diff
    plain = core.writer_tick("dry", STANDING_BASELINE, grid, 51.2, NOW)
    assert plain["status"] == "dry" and plain["writes"]


def test_writer_review_flags_an_off_tick_that_left_a_diff_standing(tmp_path, capsys):
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "writer_review.py"
    spec = importlib.util.spec_from_file_location("writer_review", p)
    wr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wr)
    good = core.fold_writes(core.writer_tick("off", _standing_of(CMD_EXPORT), None, 51.2, NOW),
                            [(f, v, 2.0) for f, v in core.writer_tick("off", _standing_of(CMD_EXPORT), None, 51.2, NOW)["writes"]])
    bad = core.fold_writes(core.writer_tick("off", _standing_of(CMD_EXPORT), None, 51.2, NOW),
                           [("work_mode", "Zero Export To Load", None)])
    f = tmp_path / "day.jsonl"
    f.write_text(json.dumps(good) + "\n")
    assert wr.main(str(f)) == 0
    f.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    assert wr.main(str(f)) == 1
    out = capsys.readouterr().out
    assert "VIOLATION" in out and "failed write" in out


# ---- live mode: the settled chain anchors on the real pack ---------------------------

def test_virtual_day_soc_anchor_resets_the_pack_at_a_step_and_at_zero_equals_soc_start():
    """Spec 3.6: in live mode the virtual chain starts from the real pack. A
    mid-day anchor (the live switch) resets the SOC entering that step and
    leaves everything before it alone; an anchor at step 0 is soc_start_pct."""
    from datetime import date
    now = F.local(2026, 9, 6, 0, 0)
    base = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now)
    anchored = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now, soc_anchor=(40, 90.0))
    assert anchored["soc_pct"][:40] == base["soc_pct"][:40]
    assert anchored["soc_start_pct"] == base["soc_start_pct"]
    assert abs(anchored["soc_pct"][40] - 90.0) < 8.0                       # 90 % entering step 40, one step of flow after
    assert anchored["soc_pct"][40] != base["soc_pct"][40]
    assert anchored["soc_anchor"] == [40, 90.0] and base["soc_anchor"] is None
    at_zero = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now, soc_anchor=(0, 50.0))
    by_start = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now, soc_start_pct=50.0)
    assert at_zero["soc_pct"] == by_start["soc_pct"] and at_zero["soc_start_pct"] == 50.0


def test_anchor_file_round_trip_and_the_day_rule(tmp_path):
    """The writer drops the anchor when the mode goes live. On that day the
    chain re-anchors at the switch step; on any later day at midnight on the
    real midnight SOC; with neither, the archive's own anchor stands."""
    from datetime import date
    path = str(tmp_path / "anchor.json")
    assert core.read_anchor(path) is None
    core.write_anchor(path, local(2026, 9, 26, 11, 51, 15), 85.0)
    a = core.read_anchor(path)
    assert a == {"date": "2026-09-26", "step": 47, "soc_pct": 85.0, "ts": "2026-09-26T11:51:15+02:00"}
    assert core.soc_anchor_for_day(a, date(2026, 9, 26), 20.7) == (47, 85.0)
    assert core.soc_anchor_for_day(a, date(2026, 9, 27), 61.2) == (0, 61.2)
    assert core.soc_anchor_for_day(a, date(2026, 9, 27), None) is None
    assert core.soc_anchor_for_day(None, date(2026, 9, 26), 20.7) == (0, 20.7)
    assert core.soc_anchor_for_day(None, date(2026, 9, 26), None) is None
    # the DST fold: the step is counted from local midnight through UTC, so on the
    # long day the first 02:30 (CEST, 00:30Z) is step 10 and the second (CET, 01:30Z) step 14
    core.write_anchor(path, local(2026, 10, 25, 2, 30, fold=0), 40.0)
    assert core.read_anchor(path)["step"] == 10
    core.write_anchor(path, local(2026, 10, 25, 2, 30, fold=1), 40.0)
    assert core.read_anchor(path)["step"] == 14


# ---- the discharge clamp deadband and the current calibration (2026-09-26) -------

def test_calibrate_amps_inverts_the_measured_shortfall_per_field():
    """Refit 2026-09-29 on 121 binding intervals: the discharge clamp loses
    ~3 % up to a 150 A knee, then a flat ~4,3 A (221 -> 216,5, 240 -> 236,2);
    grid charging delivers 0,951 of the register; the charge clamp ~1 A under.
    The writer asks for the register value that delivers the plan's current;
    0 A and the 240 A nameplate are not currents to deliver and stay as they are."""
    D, G, C = "battery_max_discharging_current", "battery_grid_charging_current", "battery_max_charging_current"
    assert core.calibrate_amps(216.5, D) == 221.0                  # the 21:16 sale of 09-29, inverted
    assert core.calibrate_amps(97.2, D) == 100.0
    assert core.calibrate_amps(20.0, D) == 20.0                    # 3 % of 20 A less the intercept: 0,4 A
    assert core.calibrate_amps(87.2, G) == 92.0
    assert core.calibrate_amps(48.0, C) == 49.0
    for f in (D, G, C):
        assert core.calibrate_amps(0.0, f) == 0.0
        assert core.calibrate_amps(240.0, f) == 240.0
    for f, a in ((D, 236.0), (G, 230.0), (C, 239.5)):
        assert core.calibrate_amps(a, f) == 240.0                  # capped at nameplate
    for f in (D, G, C):
        for a in (10.0, 60.0, 145.0, 146.0, 150.0, 200.0, 228.0):   # the inverse lands within the register's 1 A
            reg = core.calibrate_amps(a, f)
            assert reg == 240.0 or abs(core.deye_delivered_a(f, reg) - a) <= 0.55, (f, a, reg)


def test_writer_diff_keeps_the_discharge_clamp_inside_a_20A_deadband():
    """The amps come from watts over the MEASURED pack voltage, which sags
    under load: the 12:00 tick wrote 136 A and the 12:15 tick 141 A for the
    same 7,4 kW. 5 A absorbed that; 20 A since 2026-10-02 (the same band as
    the charge clamp and the grid charge). A move of exactly the band, or to
    or from the baseline, is written."""
    assert core.DEYE_DISCHARGE_DEADBAND_A == 20.0
    rec = dict(CMD_EXPORT, battery_max_discharging_current=141.0)
    st = _standing_of(rec)
    assert core.writer_diff(dict(st, battery_max_discharging_current=122.0), rec) == {}
    assert core.writer_diff(dict(st, battery_max_discharging_current=160.0), rec) == {}
    assert core.writer_diff(dict(st, battery_max_discharging_current=121.0), rec) == {"battery_max_discharging_current": [121.0, 141.0]}
    assert core.writer_diff(dict(st, battery_max_discharging_current=161.0), rec) == {"battery_max_discharging_current": [161.0, 141.0]}
    assert core.writer_diff(dict(st, battery_max_discharging_current=240.0), rec) == {"battery_max_discharging_current": [240.0, 141.0]}
    back = core.writer_diff(dict(st, battery_max_discharging_current=141.0), dict(core.DEYE_BASELINE))
    assert back["battery_max_discharging_current"] == [141.0, 240.0]


# ---- live mode retires the virtual pack: the past is measured --------------------

def test_virtual_day_takes_the_measured_pack_meter_and_soc_over_the_past_in_live_mode():
    """With the writer live the real pack is the truth (2026-09-26: "retire
    the virtual pack"). Past steps with a measured pack power, meter power and
    SOC carry those instead of the settled simulation; a step missing one of
    them falls back to the settlement; the future still runs the plan from the
    last measured SOC, so the next solve and the charts start from reality."""
    from datetime import date
    now = F.local(2026, 9, 5, 12, 0)
    base = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now)
    n = base["n"]; past = base["n_past"]
    batt = [1000.0 + 10.0 * i for i in range(n)]
    grid = [-2000.0 + 5.0 * i for i in range(n)]
    soc = [60.0 + 0.1 * i for i in range(n)]
    batt[10] = None                                                     # one gap: settlement stands there
    live = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now,
                            actual_batt_w=batt, actual_grid_w=grid, actual_soc_pct=soc)
    assert live["soc_source"] == "measured" and base["soc_source"] == "virtual"
    for i in range(past):
        if i == 10 or base["p_batt_w"][i] is None:
            continue
        assert live["p_batt_w"][i] == batt[i] and live["p_grid_w"][i] == grid[i] and live["soc_pct"][i] == soc[i], i
        assert live["cost_eur"][i] == pytest.approx(core.step_cost(grid[i], live["buy"][i], live["sell"][i]), abs=1e-4)
    assert live["p_batt_w"][10] == base["p_batt_w"][10]                 # the gap
    assert live["soc_now_pct"] == soc[past - 1]
    assert live["pv_fc_w"][past:] == base["pv_fc_w"][past:]             # the future is still the plan's own rows
    assert abs(live["soc_pct"][past] - soc[past - 1]) < 8.0             # and walks on from the measured pack
    assert live["soc_pct"][past] != base["soc_pct"][past]
    # rolled_slices and rehydrate carry the same three lanes through to the today slice
    rs = core.rolled_slices(F.PLANS, now, F.TZ, actual_batt_w=batt, actual_grid_w=grid, actual_soc_pct=soc)
    assert rs["today"]["soc_source"] == "measured" and rs["today"]["p_batt_w"][0] == batt[0]


def test_virtual_day_charges_the_loss_back_only_where_no_meter_settled():
    """loss_eur (the quadratic loss the LP priced as money) is charged back on
    the steps the meter did not settle (2026-09-30): the metered past already
    paid it, so a live day's loss_eur is the steps ahead plus the gaps. The
    mask rides along as a '1'/'0' string."""
    from datetime import date
    now = F.local(2026, 9, 5, 12, 0)
    base = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now)
    n = base["n"]; past = base["n_past"]
    batt = [1000.0] * n; grid = [-2000.0] * n; soc = [60.0] * n
    batt[10] = None
    live = core.virtual_day(F.PLANS, date(2026, 9, 5), F.TZ, now,
                            actual_batt_w=batt, actual_grid_w=grid, actual_soc_pct=soc)
    assert base["measured"] == "0" * n
    m = live["measured"]
    assert len(m) == n and m[10] == "0" and set(m[past:]) == {"0"}
    assert m.count("1") == sum(1 for i in range(past) if i != 10 and base["p_batt_w"][i] is not None)
    assert 0 < live["loss_eur"] < base["loss_eur"]


# ---- the phantom guard (handoff 2026-09-26-writer-write-budget) --------------------
#
# The Solarman settings block (300 s poll) returned option zero for every
# register for one cycle at 10:25, 16:11 and 17:44 on 2026-09-26: work mode
# Export First, export surplus off, peak shaving off, program 6 SOC 0, with no
# service context. The writer "restored" values that already stood (six
# writes), and a phantom zero on a clamp the plan wants at zero would make it
# skip a needed write. A forced entity update does not re-poll the block
# (parser.is_scheduled: runtime % interval == 0), so the guard is pure: a
# standing value that differs from what the writer last saw, without a write
# of its own since, is held against the writer's memory for one tick and
# accepted when the next tick reads it again.

PHANTOM_RAW = dict(STANDING_BASELINE, work_mode="Export First", export_surplus=False, grid_peak_shaving=False,
                   program_6_soc=0.0)


def test_guard_standing_without_memory_takes_the_read_as_it_is():
    used, suspect = core.guard_standing(None, {}, PHANTOM_RAW)
    assert used == PHANTOM_RAW and suspect == []


def test_guard_standing_holds_an_unexplained_change_against_memory_for_one_tick():
    used, suspect = core.guard_standing(STANDING_BASELINE, {}, PHANTOM_RAW)
    assert used == STANDING_BASELINE
    assert sorted(f for f, _raw, _mem in suspect) == ["export_surplus", "grid_peak_shaving", "program_6_soc", "work_mode"]
    assert ["work_mode", "Export First", "Zero Export To Load"] in [list(s) for s in suspect]


def test_guard_standing_accepts_a_change_seen_on_two_consecutive_ticks():
    seen_last = dict(PHANTOM_RAW)                                     # the previous tick read the same values
    used, suspect = core.guard_standing(STANDING_BASELINE, seen_last, PHANTOM_RAW)
    assert used == PHANTOM_RAW and suspect == []
    # a value the previous tick read differently is still suspect
    seen_last["work_mode"] = "Zero Export To Load"
    used, suspect = core.guard_standing(STANDING_BASELINE, seen_last, PHANTOM_RAW)
    assert used["work_mode"] == "Zero Export To Load" and [f for f, _r, _m in suspect] == ["work_mode"]
    assert used["export_surplus"] is False


def test_guard_standing_passes_an_unavailable_lever_through_and_reads_switch_strings():
    raw = dict(STANDING_BASELINE, work_mode=None, export_surplus="on", grid_peak_shaving="on")
    used, suspect = core.guard_standing(STANDING_BASELINE, {}, raw)
    assert used["work_mode"] is None and suspect == []                # None is the degraded path, not a phantom
    assert used["export_surplus"] == "on"


def test_guard_closes_the_skip_case_a_phantom_zero_on_a_clamp_the_plan_wants_at_zero():
    memory = dict(STANDING_BASELINE)                                  # the clamp stands at 240
    raw = dict(STANDING_BASELINE, battery_max_charging_current=0.0)   # the poll says 0
    used, suspect = core.guard_standing(memory, {}, raw)
    doc = core.writer_tick("live", used, _step("pv_export", "pv_export"), 51.2, NOW, suspect=suspect)
    assert ["battery_max_charging_current", 0.0] in doc["writes"]     # the needed write is not skipped
    assert doc["suspect"] == [["battery_max_charging_current", 0.0, 240.0]]
    assert doc["standing"] == used
    plain = core.writer_tick("live", raw, _step("pv_export", "pv_export"), 51.2, NOW)
    assert ["battery_max_charging_current", 0.0] not in plain["writes"] and plain["suspect"] == []   # without the guard the write is skipped


def _archive_ticks():
    import pathlib
    p = pathlib.Path(__file__).resolve().parent / "golden" / "writer" / "2026-09-26.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _raw_standing(doc, before):
    """What the wrapper read at that tick, rebuilt from a document that predates
    the `standing` key: the diff carries the standing value of every field that
    had to move; a field that produced no diff stood where the previous tick
    left it when that produces no diff either (a current inside the deadband),
    else at what the record (or the baseline) wanted."""
    record = dict(core.DEYE_BASELINE, **(doc.get("record") or {}))
    quiet = set(core.WRITER_FIELDS) - set(core.writer_diff(before, record)) if before else set()
    return {f: (doc["diff"][f][0] if f in doc["diff"] else before[f] if f in quiet else record[f])
            for f in core.WRITER_FIELDS}


def test_guard_against_the_archive_of_2026_09_26_saves_exactly_the_six_phantom_writes():
    """The archived ticks of the first live day replayed through the guard:
    16:15 (four writes) and 17:45 (two) are the phantoms; on every other tick
    the guard changes nothing (the 12:08 and 12:15 ticks predate the 5 A
    discharge deadband, so the archived diff is not the yardstick there)."""
    memory, seen_last, saved, flagged = None, {}, 0, []
    for doc in _archive_ticks():
        raw = _raw_standing(doc, memory)
        used, suspect = core.guard_standing(memory, seen_last, raw)
        record = dict(core.DEYE_BASELINE, **(doc.get("record") or {}))
        diff = core.writer_diff(used, record)
        if suspect:
            flagged.append((doc["tick_ts"][11:16], sorted(f for f, _r, _m in suspect)))
            saved += len(doc["written"]) - len(diff)
        else:
            assert used == raw and diff == core.writer_diff(raw, record), doc["tick_ts"]
        seen_last = dict(raw)
        memory = dict(used)
        for f, v, _lat in doc.get("written") or []:
            memory[f] = v
            seen_last[f] = v
    assert flagged == [("16:15", ["export_surplus", "grid_peak_shaving", "program_6_soc", "work_mode"]),
                       ("17:45", ["export_surplus", "program_6_soc"])]
    assert saved == 6


# 2026-10-01 21:30: the charge clamp write 240 -> 0 landed, but a Solarman poll
# already in flight put 240 back 0.5 s later and the block did not re-poll
# inside the 150 s readback. The memory kept 240, so at 21:45 the guard held
# the real 0 as a phantom and the inverter sat at 0 against a baseline of 240
# until 22:00. A failed write leaves the field unknown to the memory instead.

def test_guard_memory_takes_a_written_value_and_forgets_a_failed_one():
    raw = dict(STANDING_BASELINE, work_mode="Export First")
    standing = dict(raw)
    believed, seen = core.guard_memory(raw, standing,
                                       written=[["work_mode", "Zero Export To Load", 3]],
                                       failed=[["battery_max_charging_current", 0.0, None]])
    assert believed["work_mode"] == seen["work_mode"] == "Zero Export To Load"
    assert believed["battery_max_charging_current"] is None
    assert seen["battery_max_charging_current"] == 240.0          # the raw read stands as what was seen
    assert {f: v for f, v in believed.items() if f not in ("work_mode", "battery_max_charging_current")} == \
        {f: v for f, v in standing.items() if f not in ("work_mode", "battery_max_charging_current")}
    assert raw == dict(STANDING_BASELINE, work_mode="Export First")   # the inputs are not mutated


def test_guard_memory_trusts_the_next_read_after_a_late_landing_write():
    """The 2026-10-01 21:30 -> 21:45 ticks replayed: the late write is read at
    21:45 as it stands and the held baseline is written back at once."""
    raw_2130 = dict(STANDING_BASELINE, battery_max_discharging_current=99.0, work_mode="Export First",
                    grid_peak_shaving=False)
    believed, seen = core.guard_memory(raw_2130, dict(raw_2130),
                                       written=[["work_mode", "Zero Export To Load", 3],
                                                ["grid_peak_shaving", True, 3],
                                                ["battery_max_discharging_current", 0.0, 3]],
                                       failed=[["battery_max_charging_current", 0.0, None]])
    raw_2145 = dict(STANDING_BASELINE, battery_max_charging_current=0.0, battery_max_discharging_current=0.0)
    used, suspect = core.guard_standing(believed, seen, raw_2145)
    assert suspect == [] and used == raw_2145
    held = core.writer_tick("live", used, _step("pv_export", "export"), 51.2, NOW, suspect=suspect)
    assert held["held"] and ["battery_max_charging_current", 240.0] in held["writes"]


# ---- the write budget knobs: deadbands and quantisation (handoff 2026-09-26) -------
#
# Three quarters of a day's writes are the three current registers following
# the plan's shaped power a quarter hour at a time. The knobs below are the
# levers the site picks values for; the defaults are today's behaviour, so nothing
# moves live until a value is chosen.

def test_the_discharge_clamp_always_moves_to_and_from_its_zero_rest():
    """0 A is where an idle step holds the pack. Inside a 20 A band a night
    self-supply of 6-12 A would otherwise never start from it, and never
    stop back on it at the next idle step (the grid charge's rule)."""
    rec = dict(CMD_EXPORT, battery_max_discharging_current=8.0)
    st = _standing_of(rec)
    assert core.writer_diff(dict(st, battery_max_discharging_current=0.0), rec) == {"battery_max_discharging_current": [0.0, 8.0]}
    rest = dict(rec, battery_max_discharging_current=0.0)
    assert core.writer_diff(dict(st, battery_max_discharging_current=8.0), rest) == {"battery_max_discharging_current": [8.0, 0.0]}
    assert core.writer_diff(dict(st, battery_max_discharging_current=8.0), dict(rec, battery_max_discharging_current=20.0)) == {}
    assert core.writer_diff(dict(st, battery_max_discharging_current=0.0), rest) == {}


def test_the_charge_clamp_always_moves_to_and_from_its_zero_rest():
    """pv_export holds the pack with a zero charge clamp; a small solar charge
    (7 A on 2026-10-02 11:15) inside the 20 A band must still start from it
    and stop back on it."""
    rec = dict(CMD_EXPORT, battery_max_charging_current=7.0)
    st = _standing_of(rec)
    assert core.writer_diff(dict(st, battery_max_charging_current=0.0), rec)["battery_max_charging_current"] == [0.0, 7.0]
    rest = dict(rec, battery_max_charging_current=0.0)
    assert core.writer_diff(dict(st, battery_max_charging_current=7.0), rest)["battery_max_charging_current"] == [7.0, 0.0]
    assert "battery_max_charging_current" not in core.writer_diff(dict(st, battery_max_charging_current=7.0),
                                                                  dict(rec, battery_max_charging_current=20.0))


def test_writer_knobs_default_to_todays_behaviour():
    assert core.WRITER_KNOBS == {"charge_deadband_a": 20.0, "discharge_deadband_a": 20.0,
                                 "grid_deadband_a": 20.0, "quant_a": 1.0, "segment_mean": False}
    assert core.DEYE_GRID_CHARGE_DEADBAND_A == 20.0 and core.DEYE_CURRENT_QUANT_A == 1.0


def test_quantise_amps_rounds_up_or_down_to_the_step_and_passes_the_ends_through():
    assert core.quantise_amps(117.0, 20.0, up=True) == 120.0
    assert core.quantise_amps(120.0, 20.0, up=True) == 120.0
    assert core.quantise_amps(159.0, 20.0, up=False) == 140.0
    assert core.quantise_amps(159.0, 15.0, up=False) == 150.0
    assert core.quantise_amps(159.0, 15.0, up=True) == 165.0
    assert core.quantise_amps(7.0, 20.0, up=False) == 20.0                 # never quantised down to a hold
    assert core.quantise_amps(235.0, 20.0, up=True) == 240.0               # never above nameplate
    assert core.quantise_amps(0.0, 20.0, up=True) == 0.0 and core.quantise_amps(240.0, 20.0, up=False) == 240.0
    assert core.quantise_amps(117.0, 1.0, up=True) == 117.0                # the register's own step: no change


def test_setpoint_write_always_moves_to_and_from_the_baseline():
    """The grid charging current is a setpoint that must land: 0 A to 15 A is
    inside a 20 A deadband but starves the charge the plan pays for."""
    assert core.setpoint_write(0.0, 15.0, 20.0, baseline=0.0) == (15.0, True)
    assert core.setpoint_write(15.0, 0.0, 20.0, baseline=0.0) == (0.0, True)
    assert core.setpoint_write(100.0, 110.0, 20.0, baseline=0.0) == (100.0, False)
    assert core.setpoint_write(100.0, 120.0, 20.0, baseline=0.0) == (120.0, True)
    assert core.setpoint_write(None, 10.0, 20.0, baseline=0.0) == (10.0, True)
    assert core.setpoint_write(0.0, 0.0, 20.0, baseline=0.0) == (0.0, False)


def test_compile_step_quantises_the_currents_up_for_a_grid_charge_and_down_for_a_ceiling():
    k = dict(core.WRITER_KNOBS, quant_a=20.0)
    cur, nxt = core.compile_step(_step("export", "grid_charge"), 51.2, knobs=k)
    assert cur["battery_max_discharging_current"] == 160.0                 # 160 asked, the sale is a ceiling: down, already on the step
    assert nxt["battery_grid_charging_current"] == 140.0                   # 123 asked (117 / 0,951): up
    assert nxt["battery_max_charging_current"] == 240.0                    # the clamp stays open: the grid current is the floor
    g, _ = core.compile_step(_step("grid_charge", "grid_charge"), 50.0, knobs=k)   # 6.000 W / 50 V = 120 A / 0,951 = 126
    assert g["battery_grid_charging_current"] == 140.0 and g["battery_max_charging_current"] == 240.0   # a charge that must land: up
    s = _step("self_balance", "self_balance", sell=-0.01)
    s["row"]["P_PV_curtailment"] = 800.0
    plateau, _ = core.compile_step(s, 51.2, knobs=k)
    assert plateau["battery_max_charging_current"] == 100.0               # 119 asked, a ceiling with the margin: down
    small = _step("self_supply", "self_supply")                            # 800 W = 16 A, 16,3 asked: never under one step
    assert core.compile_step(small, 51.2, knobs=k)[0]["battery_max_discharging_current"] == 20.0
    pv, _ = core.compile_step(_step("pv_export", "pv_export"), 51.2, knobs=k)
    assert pv["battery_max_charging_current"] == 0.0 and pv["battery_max_discharging_current"] == 0.0
    same_as_before, _ = core.compile_step(_step("export", "export"), 51.2)
    assert same_as_before["battery_max_discharging_current"] == 160.0      # no knobs: unchanged


def test_writer_diff_takes_the_grid_and_discharge_deadbands_from_the_knobs():
    k = dict(core.WRITER_KNOBS, grid_deadband_a=20.0, discharge_deadband_a=15.0)
    gc = dict(CMD_GRID_CHARGE, battery_grid_charging_current=110.0, battery_max_charging_current=117.0)
    st = _standing_of(gc)
    assert core.writer_diff(dict(st, battery_grid_charging_current=100.0), gc, knobs=k) == {}
    assert core.writer_diff(dict(st, battery_grid_charging_current=100.0), gc) == {}          # the default is 20 A now
    assert core.writer_diff(dict(st, battery_grid_charging_current=100.0), gc, knobs=dict(k, grid_deadband_a=0.0)) == {
        "battery_grid_charging_current": [100.0, 110.0]}
    assert core.writer_diff(dict(st, battery_grid_charging_current=90.0), gc, knobs=k) == {"battery_grid_charging_current": [90.0, 110.0]}
    assert core.writer_diff(dict(st, battery_grid_charging_current=0.0), dict(gc, battery_grid_charging_current=15.0), knobs=k) == {
        "battery_grid_charging_current": [0.0, 15.0]}
    back = core.writer_diff(st, dict(core.DEYE_BASELINE), knobs=k)
    assert back["battery_grid_charging_current"] == [110.0, 0.0]
    rec = dict(CMD_EXPORT, battery_max_discharging_current=141.0)
    es = _standing_of(rec)
    assert core.writer_diff(dict(es, battery_max_discharging_current=150.0), rec, knobs=k) == {}
    assert core.writer_diff(dict(es, battery_max_discharging_current=156.0), rec, knobs=k) == {"battery_max_discharging_current": [156.0, 141.0]}
    assert core.writer_diff(dict(es, battery_max_discharging_current=150.0), rec) == {}                # the default is 20 A now


def test_writer_tick_carries_the_knobs_into_the_record_and_the_diff():
    k = dict(core.WRITER_KNOBS, quant_a=20.0, discharge_deadband_a=15.0)
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, knobs=k)
    assert doc["record"]["battery_max_discharging_current"] == 160.0
    st = dict(STANDING_BASELINE, battery_max_discharging_current=150.0, work_mode="Export First", grid_peak_shaving=False)
    assert core.writer_tick("live", st, _step("export", "export"), 51.2, NOW, knobs=k)["writes"] == []
    plain = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW)
    assert plain["record"]["battery_max_discharging_current"] == 160.0


# ---- the walk tool's tracking account (an internal tool) ------------------

def _walk_mod():
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "writer_walk.py"
    spec = importlib.util.spec_from_file_location("writer_walk", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_walk_tracking_prices_the_gap_between_the_standing_register_and_the_plan():
    """Per tick: the power the standing register delivers (the +3 A offset
    taken off) against the plan's P_batt, in kWh over the quarter and EUR at
    the step's price. A sale counts both ways (the clamp is the setpoint), a
    self-supply or a plateau only its shortfall, a grid charge both ways at
    the buy price through the smaller of the two currents; a held or baseline
    record tracks nothing."""
    wm = _walk_mod()
    row = {"P_grid": -9000.0, "P_batt": 8000.0, "unit_prod_price": 0.10, "unit_load_cost": 0.25}
    rec = dict(CMD_EXPORT, battery_max_discharging_current=160.0)
    st = _standing_of(dict(rec, battery_max_discharging_current=140.0))                    # 136 A delivered = 6.963 W
    kwh, eur, _net = wm.track(rec, st, row, 51.2)
    assert kwh == pytest.approx((8000.0 - 136.0 * 51.2) * 0.25 / 1000.0, abs=1e-6)
    assert eur == pytest.approx(kwh * 0.10, abs=1e-6)
    assert wm.track(rec, _standing_of(rec), row, 51.2) == (0.0, 0.0, 0.0)                  # 155,7 A delivered: 7.972 W, under 1 A
    ss = dict(core.deye_command(0.0, 800.0, 51.2), battery_max_discharging_current=19.0)   # self_supply: 16 A + 3
    over = _standing_of(dict(ss, battery_max_discharging_current=40.0))
    assert wm.track(ss, over, dict(row, P_grid=0.0, P_batt=800.0), 51.2) == (0.0, 0.0, 0.0)   # above the plan: no shortfall
    under = _standing_of(dict(ss, battery_max_discharging_current=13.0))                   # 12,81 A = 655,9 W
    kwh, eur, _net = wm.track(ss, under, dict(row, P_grid=0.0, P_batt=800.0), 51.2)
    assert kwh == pytest.approx((800.0 - 12.81 * 51.2) * 0.25 / 1000.0, abs=1e-6) and eur == pytest.approx(kwh * 0.25, abs=1e-6)
    gc = dict(CMD_GRID_CHARGE, battery_grid_charging_current=120.0, battery_max_charging_current=120.0)
    capped = _standing_of(dict(gc, battery_grid_charging_current=140.0, battery_max_charging_current=100.0))   # 99 A binds (grid: 133 A)
    kwh, eur, _net = wm.track(gc, capped, dict(row, P_grid=2000.0, P_batt=-6000.0), 51.2)
    assert kwh == pytest.approx((6000.0 - 99.0 * 51.2) * 0.25 / 1000.0, abs=1e-6) and eur == pytest.approx(kwh * 0.25, abs=1e-6)
    assert wm.track(core.baseline_record(), STANDING_BASELINE, row, 51.2) == (0.0, 0.0, 0.0)


# ---- lever 5: one write per intent segment at the segment's mean power ---------------
#
# The plan's power is a sawtooth, not a ramp (2026-09-26 evening: 237, 192,
# 237, 198, 121, 169, 101 A quarter by quarter), so a deadband or a
# quantisation step on the currents saves little. The structural lever writes
# a segment of one intent once, at the mean of the segment's currents. Behind
# a knob that defaults off; the walk quantifies it.

def test_step_in_force_carries_the_rows_ahead_for_the_segment_mean(tmp_path):
    arch = str(tmp_path / "plans")
    t0 = local(2026, 9, 27, 10, 15)
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 13),
                            _doc(local(2026, 9, 27, 10, 13), t0, 6, ["export", "export", "export", "grid_charge", "grid_charge", "pv_export"],
                                 micro_cut=[False, True, False, False, False, False]))
    s = core.step_in_force(arch, local(2026, 9, 27, 10, 30, 20))
    assert s["index"] == 1 and len(s["rows"]) == 6 and s["rows"][1] is s["row"]
    assert s["cuts"] == [False, True, False, False, False, False]
    last = core.step_in_force(arch, local(2026, 9, 27, 11, 30, 20))
    assert last["index"] == 5 and len(last["rows"]) == 6 and last["cuts"][5] is False
    bare = core.step_in_force(F.SYN_PLANS, local(2026, 10, 25, 2, 30, 20, fold=0))
    assert len(bare["cuts"]) == len(bare["rows"])                       # padded when the document has no micro_cut


def test_compile_step_segment_mean_writes_the_segment_once_at_its_mean_current():
    def row(intent, batt):
        g, _b = INTENT_ROW[intent]
        return {"timestamp": "x", "P_grid": g, "P_batt": batt, "P_PV_curtailment": 0.0, "unit_prod_price": 0.08}
    ahead = [row("export", 8000.0), row("export", 6000.0), row("export", 4000.0), row("grid_charge", -6000.0), row("grid_charge", -4000.0)]
    step = {"row": ahead[0], "next_row": ahead[1], "micro_cut": False, "next_micro_cut": False, "stale": False,
            "rows": ahead, "cuts": [False] * 5, "index": 0}
    k = dict(core.WRITER_KNOBS, segment_mean=True)
    cur, nxt = core.compile_step(step, 51.2, knobs=k)
    assert cur["intent"] == "export" and nxt["intent"] == "export"
    # 8.000, 6.000, 4.000 W at 51,2 V: 156 + 117 + 78 A -> 117 A mean, + 3 A
    assert cur["battery_max_discharging_current"] == 120.0 and nxt["battery_max_discharging_current"] == 120.0
    second = dict(step, row=ahead[1], next_row=ahead[2], index=1)
    cur2, _ = core.compile_step(second, 51.2, knobs=k)
    assert cur2["battery_max_discharging_current"] == 120.0                # the same value: no write in the segment
    fourth = dict(step, row=ahead[3], next_row=ahead[4], index=3)
    gc, _ = core.compile_step(fourth, 51.2, knobs=k)
    assert gc["intent"] == "grid_charge"
    assert gc["battery_grid_charging_current"] == 103.0 and gc["battery_max_charging_current"] == 240.0   # (117 + 78) / 2 = 97,5 -> 98 / 0,951
    plain, _ = core.compile_step(step, 51.2)
    assert plain["battery_max_discharging_current"] == 160.0              # knob off: the step's own power
    without_rows, _ = core.compile_step({k2: v for k2, v in step.items() if k2 not in ("rows", "cuts", "index")}, 51.2, knobs=k)
    assert without_rows["battery_max_discharging_current"] == 160.0       # no rows ahead: the step's own power


def test_walk_tracking_reports_the_signed_value_at_the_step_price():
    """Third figure: the energy's value at the step's own price, signed (a
    sale above the plan reads as a gain, a dearer import as a loss). Energy
    shifts between steps, so this is the intra-segment shaping a mean loses,
    not a settlement."""
    wm = _walk_mod()
    row = {"P_grid": -9000.0, "P_batt": 8000.0, "unit_prod_price": 0.10, "unit_load_cost": 0.25}
    rec = dict(CMD_EXPORT, battery_max_discharging_current=159.0)
    under = _standing_of(dict(rec, battery_max_discharging_current=140.0))
    kwh, eur, net = wm.track(rec, under, row, 51.2)
    assert net == pytest.approx(-eur, abs=1e-6) and net < 0
    over = _standing_of(dict(rec, battery_max_discharging_current=180.0))
    kwh, eur, net = wm.track(rec, over, row, 51.2)
    assert net == pytest.approx(eur, abs=1e-6) and net > 0
    gc = dict(CMD_GRID_CHARGE, battery_grid_charging_current=120.0, battery_max_charging_current=120.0)
    more = _standing_of(dict(gc, battery_grid_charging_current=140.0, battery_max_charging_current=140.0))
    kwh, eur, net = wm.track(gc, more, dict(row, P_grid=2000.0, P_batt=-6000.0), 51.2)
    assert net == pytest.approx(-eur, abs=1e-6) and net < 0                # a dearer import


# ---- yesterday's slice keeps the real pack across midnight (2026-09-27) --------------

def test_rolled_slices_and_rehydrate_carry_yesterdays_anchor_and_measured_lanes():
    """Found on the first live day's morning after: at the 00:13 rollover the
    yesterday slice was rebuilt by virtual_day WITHOUT the live anchor and the
    measured lanes, so 2026-09-26 went back to the virtual pack (10 % at the
    floor all evening where the real pack sold from 96 % to 18 %). Yesterday
    takes its own anchor and its own measured pack, meter and SOC lanes,
    exactly as today does; without them the behaviour is unchanged."""
    from datetime import date
    now = F.local(2026, 9, 6, 0, 30)
    base = core.rolled_slices(F.PLANS, now, F.TZ)
    n = base["yesterday"]["n"]
    batt = [500.0 + 10.0 * i for i in range(n)]
    grid = [-1000.0 + 5.0 * i for i in range(n)]
    soc = [60.0 + 0.1 * i for i in range(n)]
    rs = core.rolled_slices(F.PLANS, now, F.TZ, prev_soc_anchor=(40, 90.0),
                            prev_batt_w=batt, prev_grid_w=grid, prev_soc_pct=soc)
    y = rs["yesterday"]
    assert y["date"] == "2026-09-05" and y["soc_source"] == "measured" and y["soc_anchor"] == [40, 90.0]
    assert y["p_batt_w"][0] == batt[0] and y["p_grid_w"][50] == grid[50] and y["soc_pct"][95] == soc[95]
    assert base["yesterday"]["soc_source"] == "virtual" and base["yesterday"]["soc_anchor"] is None
    assert rs["today"] == base["today"]                                    # today is untouched by yesterday's lanes
    rh = core.rehydrate(F.PLANS, F.TZ, now.isoformat(), 26.0, prev_soc_anchor=(40, 90.0),
                        prev_batt_w=batt, prev_grid_w=grid, prev_soc_pct=soc)
    assert rh["yesterday"]["soc_source"] == "measured" and rh["yesterday"]["soc_anchor"] == [40, 90.0]
    assert rh["yesterday"]["p_batt_w"][0] == batt[0]


# ---- the ledger's actual rung keeps the real pack on a live day (2026-09-27) ---------

def test_day_was_live_reads_the_writer_archive(tmp_path):
    d = str(tmp_path / "writer")
    assert core.day_was_live(d, local(2026, 9, 26, 12, 0).date()) is False           # no file
    core.append_tick(d, local(2026, 9, 26, 11, 30, 20), core.writer_tick("dry", STANDING_BASELINE, None, 51.2, local(2026, 9, 26, 11, 30, 20)))
    assert core.day_was_live(d, local(2026, 9, 26, 12, 0).date()) is False           # dry only
    core.append_tick(d, local(2026, 9, 26, 11, 51, 15), core.writer_tick("live", STANDING_BASELINE, None, 51.2, local(2026, 9, 26, 11, 51, 15)))
    assert core.day_was_live(d, local(2026, 9, 26, 12, 0).date()) is True
    assert core.day_was_live(d, local(2026, 9, 27, 12, 0).date()) is False


def _ladder_act(n, soc0=90.0):
    """A synthetic measured day: a steady 1 kW discharge, no sun, 500 W load."""
    return {"pv_w": [0.0] * n, "load_w": [500.0] * n, "grid_w": [-500.0] * n, "batt_dc_w": [1000.0] * n,
            "micro_w": [0.0] * n, "curtailed": [0.0] * n, "pv_peak_w": [0.0] * n,
            "soc_pct": [round(soc0 - 0.5 * i, 2) for i in range(n)]}


def test_ladder_actual_rung_takes_the_anchor_and_the_measured_lanes_on_a_live_day():
    """The nightly ladder settled the first live day through the virtual pack
    (start 24 %, end 10 %, minus 5,51 EUR) where the real pack ran 85 % to
    15 %, and its row overwrote the measured hours in the sidecar. A day the
    writer was live on carries `anchor` and `measured` in its ladder inputs;
    the actual rung then walks the real pack, meter and SOC, and the next day
    chains from the real end."""
    from datetime import date
    n = core.expected_steps(date(2026, 9, 5), F.TZ)
    act = _ladder_act(n)
    plain = core.ladder_day(F.PLANS, "http://none", "2026-09-05", F.TZ, None, {"day": act}, None, rungs=("actual",))
    live = core.ladder_day(F.PLANS, "http://none", "2026-09-05", F.TZ, None,
                           {"day": act, "anchor": (40, 90.0), "measured": True}, None, rungs=("actual",))
    assert plain["actual_status"] == "ok" and live["actual_status"] == "ok"
    assert live["actual_soc_end_pct"] == act["soc_pct"][-1]                       # the real end of the day
    assert plain["actual_soc_end_pct"] != live["actual_soc_end_pct"]
    lanes = dict(live["_lanes"])
    first = next(iter(lanes.values()))
    assert first["discharge_kw"] == pytest.approx(1.0, abs=0.01)                    # the measured pack, not the plan's
    assert live["actual_eur"] is not None and "actual_reset" in live["flags"]
    # the wrapper's inputs builder marks a live day through the writer archive
    chained = core.ladder_day(F.PLANS, "http://none", "2026-09-06", F.TZ, None, {"day": _ladder_act(n, 40.0)}, live,
                              rungs=("actual",))
    assert chained["soc_start_pct"] == pytest.approx(act["soc_pct"][-1], abs=0.01)   # chained from the real end


# ---- the ceilings: the SOC-floor kill (2026-09-27) ------------------------------------

def test_soc_floor_trips_below_the_floor_and_releases_two_points_above_it():
    assert core.soc_floor_tripped(False, 9.9, 10.0) is True
    assert core.soc_floor_tripped(False, 10.0, 10.0) is False
    assert core.soc_floor_tripped(True, 11.0, 10.0) is True          # inside the hysteresis: holds
    assert core.soc_floor_tripped(True, 12.0, 10.0) is False         # floor + 2 points releases
    assert core.soc_floor_tripped(False, 11.0, 10.0) is False


def test_soc_floor_first_tick_after_a_reload_has_no_hysteresis():
    assert core.soc_floor_tripped(None, 6.0, 10.0) is True
    assert core.soc_floor_tripped(None, 11.0, 10.0) is False


def test_soc_floor_keeps_its_state_when_the_soc_or_the_floor_is_unavailable():
    assert core.soc_floor_tripped(True, None, 10.0) is True
    assert core.soc_floor_tripped(False, None, 10.0) is False
    assert core.soc_floor_tripped(None, None, 10.0) is False
    assert core.soc_floor_tripped(True, 6.0, None) is True


def test_soc_floor_of_zero_never_trips():
    assert core.soc_floor_tripped(None, 0.0, 0.0) is False
    assert core.soc_floor_tripped(True, 3.0, 0.0) is False


# ---- the heat backstop (2026-09-30): 45 °C -> 6 kW, both helpers ----

def test_heat_cut_trips_at_the_cut_and_releases_one_degree_under_it():
    assert core.heat_cut_tripped(False, 45.0, 45.0) is True
    assert core.heat_cut_tripped(False, 44.9, 45.0) is False
    assert core.heat_cut_tripped(True, 44.5, 45.0) is True           # inside the hysteresis: holds
    assert core.heat_cut_tripped(True, 44.0, 45.0) is False          # cut - 1 °C releases
    assert core.heat_cut_tripped(None, 44.5, 45.0) is False          # first tick after a reload: no hysteresis


def test_heat_cut_keeps_its_state_when_the_temperature_or_the_cut_is_unavailable():
    assert core.heat_cut_tripped(True, None, 45.0) is True
    assert core.heat_cut_tripped(False, None, 45.0) is False
    assert core.heat_cut_tripped(None, None, 45.0) is False
    assert core.heat_cut_tripped(True, 46.0, None) is True


def test_heat_cut_amps_hold_the_port_power_on_every_battery_current_field():
    caps = core.heat_cut_amps(6.0, 53.0)
    assert set(caps) == set(core.HEAT_CUT_FIELDS)
    a = 6000.0 / 53.0
    for f, reg in caps.items():
        assert core.calibrate_amps(a, f) - 5.0 < reg <= core.calibrate_amps(a, f), f
        assert reg % 5.0 == 0.0, f
        assert reg < core.DEYE_CURRENT_MAX_A
    assert caps["battery_grid_charging_current"] > a                  # the grid register delivers ~0,951 of itself


def test_heat_cut_amps_do_not_move_with_a_small_voltage_drift():
    assert core.heat_cut_amps(6.0, 53.0) == core.heat_cut_amps(6.0, 53.2)


def test_heat_cut_power_zero_or_missing_disables_it():
    assert core.heat_cut_amps(0.0, 53.0) == {}                       # an unseeded helper sits at its minimum
    assert core.heat_cut_amps(None, 53.0) == {}


def test_heat_cut_without_a_pack_voltage_errs_low():
    assert core.heat_cut_amps(6.0, None) == core.heat_cut_amps(6.0, core.HEAT_CUT_FALLBACK_V)
    assert core.HEAT_CUT_FALLBACK_V >= 56.0


def test_writer_ceilings_merge_the_heat_cut_with_the_soc_floor_lower_wins():
    heat = core.heat_cut_amps(6.0, 53.0)
    ceil = core.writer_ceilings(soc_floor_tripped=True, heat_cut_a=heat)
    assert ceil["battery_max_discharging_current"] == 0.0
    assert ceil["battery_max_charging_current"] == heat["battery_max_charging_current"]
    assert core.writer_ceilings() == {}


def test_heat_cut_caps_a_full_power_grid_charge_in_live_mode():
    heat = core.heat_cut_amps(6.0, 53.0)
    doc = core.writer_tick("live", STANDING_BASELINE, _step("grid_charge", "grid_charge"), 53.0, NOW,
                           ceilings=core.writer_ceilings(heat_cut_a=heat))
    for f in core.HEAT_CUT_FIELDS:
        assert float(doc["record"].get(f, STANDING_BASELINE[f])) <= heat[f], f


def test_ceilings_cap_the_plan_record_and_force_the_write_through_the_deadband():
    ceil = {"battery_max_discharging_current": 0.0}
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, ceilings=ceil)
    assert doc["record"]["battery_max_discharging_current"] == 0.0
    assert doc["diff"]["battery_max_discharging_current"] == [240.0, 0.0]
    assert doc["ceilings"] == ceil
    # a standing 10 A is inside the discharge deadband of a plan move, but above the ceiling
    st = dict(STANDING_BASELINE, battery_max_discharging_current=10.0)
    doc = core.writer_tick("off", st, None, 51.2, NOW, ceilings=ceil)
    assert doc["diff"]["battery_max_discharging_current"] == [10.0, 0.0]
    assert ["battery_max_discharging_current", 0.0] in doc["safety_writes"]


def test_ceilings_hold_in_every_mode_and_on_every_fallback_record():
    ceil = {"battery_max_discharging_current": 0.0}
    cases = [("off", STANDING_BASELINE, None, 51.2),
             ("live", STANDING_BASELINE, None, 51.2),                                   # stale
             ("live", STANDING_BASELINE, _step("export", "export"), None),              # degraded
             ("live", STANDING_BASELINE, _step("export", "grid_charge"), 51.2),         # held at baseline
             ("dry", STANDING_BASELINE, _step("export", "export"), 51.2)]
    for mode, st, step, v in cases:
        doc = core.writer_tick(mode, st, step, v, NOW, ceilings=ceil)
        assert doc["safety_writes"] == [["battery_max_discharging_current", 0.0]], (mode, doc["status"])


def test_a_ceiling_already_standing_needs_no_safety_write_and_never_raises_a_lower_value():
    ceil = {"battery_max_discharging_current": 50.0}
    st = dict(STANDING_BASELINE, battery_max_discharging_current=50.0)
    doc = core.writer_tick("dry", st, None, 51.2, NOW, ceilings=ceil)
    assert doc["safety_writes"] == []
    assert "battery_max_discharging_current" not in doc["diff"]
    # the plan wants less than the ceiling: the plan's value stands, nothing is raised to the ceiling
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, ceilings=ceil)
    assert doc["record"]["battery_max_discharging_current"] == 50.0
    small = {"battery_max_discharging_current": 200.0}
    doc = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, ceilings=small)
    assert doc["record"]["battery_max_discharging_current"] == 160.0


def test_no_ceilings_leaves_the_tick_exactly_as_before():
    a = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW)
    b = core.writer_tick("live", STANDING_BASELINE, _step("export", "export"), 51.2, NOW, ceilings={})
    for k in ("record", "diff", "writes", "status"):
        assert a[k] == b[k]
    assert a["safety_writes"] == [] and b["ceilings"] == {}


def test_ceilings_of_the_soc_floor():
    assert core.writer_ceilings(soc_floor_tripped=True) == {"battery_max_discharging_current": 0.0}
    assert core.writer_ceilings(soc_floor_tripped=False) == {}


def test_the_soc_floor_backstop_runs_after_the_dead_man_and_only_lowers_the_discharge_clamp():
    import pathlib
    import yaml
    path = pathlib.Path(__file__).resolve().parents[1] / "ha" / "packages" / "emhass" / "emhass_writer.yaml"
    autos = {a["id"]: a for a in yaml.safe_load(path.read_text())["automation"]}
    dead, back = autos["emhass_writer_dead_man"], autos["emhass_writer_soc_floor_backstop"]
    assert dead["trigger"][0]["minutes"] == back["trigger"][0]["minutes"] == "/5"
    assert "seconds" not in dead["trigger"][0] and back["trigger"][0]["seconds"] == 30
    conds = " ".join(c.get("value_template", "") for c in back["condition"])
    assert "emhass_writer_heartbeat" in conds and "input_number.emhass_soc_min" in conds
    writes = [a for a in back["action"] if a.get("action") == "number.set_value"]
    assert writes == [{"action": "number.set_value",
                       "target": {"entity_id": core.WRITER_ENTITY["battery_max_discharging_current"]},
                       "data": {"value": 0}}]


# ---- export shuts only at a negative sell price with the pack above 90 % (2026-09-27) --

GC, SB = INTENT_ROW["grid_charge"], INTENT_ROW["self_balance"]


@pytest.mark.parametrize("sell, soc, export", [
    (0.08, 50.0, True),       # paying: the sun the pack cannot take is sold during a grid charge
    (0.08, 97.0, True),       # paying and nearly full: the tapering pack must not throttle the sun
    (-0.02, 80.0, True),      # negative but room: export stays on, the pack absorbs first
    (-0.02, 95.0, False),     # negative and above 90 %: export shuts
])
def test_grid_charge_keeps_export_unless_the_price_is_negative_and_the_pack_above_90(sell, soc, export):
    cmd = core.deye_command(*GC, 51.2, sell=sell, soc_pct=soc, margin=True)
    assert cmd["battery_grid_charging"] is True and cmd["program_6_soc"] == 100.0
    assert cmd["export_surplus"] is export


@pytest.mark.parametrize("sell, soc, curtail, export", [
    (-0.02, 80.0, 0.0, True),
    (-0.02, 80.0, 1500.0, True),     # the LP declined sun, but the rule keeps export: clamp opens instead
    (-0.02, 95.0, 0.0, False),
    (0.0, 95.0, 0.0, True),          # zero is not negative
])
def test_self_balance_shuts_export_only_negative_and_above_90(sell, soc, curtail, export):
    cmd = core.deye_command(*SB, 51.2, pv_curtail_w=curtail, sell=sell, soc_pct=soc, margin=True)
    assert cmd["export_surplus"] is export
    if export and sell < 0:
        assert cmd["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A    # take all the sun first


def test_without_a_soc_deye_command_keeps_the_old_export_rule():
    assert core.deye_command(*GC, 51.2, sell=0.08)["export_surplus"] is False
    assert core.deye_command(*SB, 51.2, sell=-0.02)["export_surplus"] is False
    assert core.deye_command(*SB, 51.2, sell=0.08)["export_surplus"] is True


def test_compile_step_reads_the_plan_soc_for_the_export_rule():
    step = _step("grid_charge", "grid_charge", sell=0.08)
    step["row"]["SOC_opt"] = step["next_row"]["SOC_opt"] = 0.5
    cur, nxt = core.compile_step(step, 51.2)
    assert cur["export_surplus"] is True and nxt["export_surplus"] is True
    step["row"]["unit_prod_price"] = step["next_row"]["unit_prod_price"] = -0.02
    step["row"]["SOC_opt"] = step["next_row"]["SOC_opt"] = 0.95
    cur, _ = core.compile_step(step, 51.2)
    assert cur["export_surplus"] is False


# ---- a full-power step takes the nameplate (2026-10-02) ---------------------

def test_step_in_force_carries_the_plans_port_power_knob(tmp_path):
    arch = str(tmp_path / "plans")
    doc = _doc(local(2026, 9, 27, 10, 13), local(2026, 9, 27, 10, 15), 4, ["export"])
    doc["knobs"] = {"batt_power_max_w": 12000.0, "physics_v2": 1.0}
    core.write_plan_archive(arch, local(2026, 9, 27, 10, 13), doc)
    assert core.step_in_force(arch, local(2026, 9, 27, 10, 15, 20))["p_max_w"] == 12000.0
    old = str(tmp_path / "old")
    core.write_plan_archive(old, local(2026, 9, 27, 10, 13),
                            _doc(local(2026, 9, 27, 10, 13), local(2026, 9, 27, 10, 15), 4, ["export"]))
    assert core.step_in_force(old, local(2026, 9, 27, 10, 15, 20))["p_max_w"] is None


def test_compile_step_puts_a_full_power_step_on_the_nameplate():
    """The 2 October 2026 evening sale: the plan sold at the 12 kW knob from
    18:30 to 19:30, the amps at the measured voltage came to 233-235 A, and
    the 20 A deadband held the 216 A of an earlier step for the whole run
    (~0,9 kW short at the day's best price). A step at the knob is not a
    current to track: it asks the register for everything it has."""
    D, G = "battery_max_discharging_current", "battery_grid_charging_current"
    full = _step("export", "export")
    full["p_max_w"] = 12000.0
    full["row"]["P_batt"] = full["next_row"]["P_batt"] = 11992.4       # within the solver's slack of the bound
    cur, nxt = core.compile_step(full, 53.5)
    assert cur[D] == 240.0 and nxt[D] == 240.0
    part = dict(full, row=dict(full["row"], P_batt=11000.0))
    assert core.compile_step(part, 53.5)[0][D] < 220.0                 # under the knob: the plan's own current
    assert core.compile_step(dict(full, p_max_w=None), 53.5)[0][D] < 240.0   # an old plan without the knob: unchanged
    gc = _step("grid_charge", "grid_charge")
    gc["p_max_w"] = 6000.0                                             # INTENT_ROW grid charge is 6 kW
    assert core.compile_step(gc, 54.0)[0][G] == 240.0
    ss = _step("self_supply", "self_supply")
    ss["p_max_w"] = 800.0
    assert core.compile_step(ss, 51.2)[0][D] == 240.0


def test_compile_step_full_power_stays_under_a_heat_ceiling():
    full = _step("export", "export")
    full["p_max_w"] = 12000.0
    full["row"]["P_batt"] = full["next_row"]["P_batt"] = 12000.0
    doc = core.writer_tick("live", STANDING_BASELINE, full, 53.5, NOW,
                           ceilings=core.writer_ceilings(heat_cut_a={"battery_max_discharging_current": 115.0}))
    assert doc["record"]["battery_max_discharging_current"] == 115.0


def test_writer_diff_always_moves_a_setpoint_to_and_from_the_nameplate():
    """The nameplate is a rest like 0 A: a sale at 225 A standing in front of
    a full-power step moves to 240 A through the deadband, and a 240 A left
    by a full step or the baseline does not keep selling over a 225 A step.
    The charge clamp is a ceiling and keeps its deadband either way."""
    D, G, C = "battery_max_discharging_current", "battery_grid_charging_current", "battery_max_charging_current"
    rec = dict(CMD_EXPORT, battery_max_discharging_current=240.0)
    st = _standing_of(rec)
    assert core.writer_diff(dict(st, **{D: 225.0}), rec) == {D: [225.0, 240.0]}
    assert core.writer_diff(dict(st, **{D: 240.0}), dict(rec, **{D: 225.0})) == {D: [240.0, 225.0]}
    assert core.writer_diff(dict(st, **{D: 230.0}), dict(rec, **{D: 225.0})) == {}   # off the nameplate: the deadband
    gc = dict(CMD_GRID_CHARGE, battery_grid_charging_current=240.0)
    gs = _standing_of(gc)
    assert core.writer_diff(dict(gs, **{G: 229.0}), gc) == {G: [229.0, 240.0]}
    assert core.writer_diff(dict(gs, **{G: 240.0}), dict(gc, **{G: 229.0})) == {G: [240.0, 229.0]}
    sb = dict(core.deye_command(0.0, -5000.0, 51.2), battery_max_charging_current=240.0)
    ss = _standing_of(sb)
    assert core.writer_diff(dict(ss, **{C: 225.0}), sb) == {}
    assert core.writer_diff(dict(ss, **{C: 240.0}), dict(sb, **{C: 225.0})) == {}
