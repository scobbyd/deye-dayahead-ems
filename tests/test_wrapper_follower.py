"""The heating follower wrapper (pyscript/heating_follower.py) through pyscript_harness:
the plan sensor and the two setpoint sensors from a thermal plan in a temp archive."""
from datetime import datetime

from pyscript_harness import Harness
from test_thermal import _thermal_doc, local

import emhass_core as core


def _archive(tmp_path, doc):
    arch = tmp_path / "config" / "emhass" / "plans"
    arch.mkdir(parents=True)
    core.write_plan_archive(str(arch), datetime.fromisoformat(doc["plan_ts"]), doc)
    return arch


def _load(h, arch):
    h.set_state("input_select.heating_planner", "emhass")
    mod = h.load("heating_follower.py")
    mod.ARCHIVE = str(arch)
    return mod


def _sets(h):
    return {e["entity_id"]: e for e in h.effects if e["kind"] == "state.set"}


def _notifies(h):
    return [e for e in h.effects if e["kind"] == "service.call" and e["domain"] == "notify"]


def test_tick_publishes_plan_and_targets(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    d = _thermal_doc(t0, 8, hp=[1200] * 8, t_liv=[19.0, 19.3, 19.6, 19.9, 20.2, 20.5, 20.8, 21.0])
    for r in d["rows"]:
        r["P_deferrable1"] = 900.0                  # the multi runs: the upstairs target is the predicted temperature
    arch = _archive(tmp_path, d)
    h = Harness(now=local(2026, 10, 10, 10, 20, 40))
    h.set_state("input_number.heating_daikin_target_correction", "1.0")
    h.set_state("input_boolean.heating_pellet_available", "off")
    h.set_state("input_boolean.heating_upstairs_enabled", "on")
    mod = _load(h, arch)
    mod.heating_follower_tick()
    sets = _sets(h)
    assert sets["sensor.heating_emhass_plan"]["state"] == "AC"
    assert sets["sensor.heating_emhass_plan"]["attributes"]["p_hp_living_w"] == 1200
    assert sets["sensor.heating_emhass_target_living"]["state"] == 22.0          # end of block 21,0 + 1,0
    assert sets["sensor.heating_emhass_target_upstairs"]["state"] == core.daikin_grid(19.5, 0.0) == 19.5
    assert sets["sensor.heating_follower_heartbeat"]["state"] == "2026-10-10T10:20:40+02:00"
    assert not _notifies(h)


def test_tick_without_plan_is_off_and_notifies_once_in_emhass_mode(tmp_path):
    arch = tmp_path / "config" / "emhass" / "plans"
    arch.mkdir(parents=True)
    h = Harness(now=local(2026, 10, 10, 10, 20, 40))
    mod = _load(h, arch)
    mod.heating_follower_tick()
    mod.heating_follower_tick()
    plan = [e for e in h.effects if e["kind"] == "state.set" and e["entity_id"] == "sensor.heating_emhass_plan"]
    assert plan[-1]["state"] == "off" and plan[-1]["attributes"]["status"] == "no plan"
    tgt = [e for e in h.effects if e["kind"] == "state.set" and e["entity_id"] == "sensor.heating_emhass_target_living"]
    assert tgt[-1]["state"] == "unknown"
    assert len(_notifies(h)) == 1
    msg = _notifies(h)[0]["data"]["message"]
    assert "legacy planner takes the living" in msg and "upstairs units park" in msg


def test_off_does_not_notify_in_legacy_mode_or_without_the_helper(tmp_path):
    arch = tmp_path / "config" / "emhass" / "plans"
    arch.mkdir(parents=True)
    h = Harness(now=local(2026, 10, 10, 10, 20, 40))
    mod = _load(h, arch)
    h.set_state("input_select.heating_planner", "legacy")
    mod.heating_follower_tick()
    h2 = Harness(now=local(2026, 10, 10, 10, 20, 40))
    mod2 = h2.load("heating_follower.py")                  # the helper does not exist
    mod2.ARCHIVE = str(arch)
    mod2.heating_follower_tick()
    assert not _notifies(h) and not _notifies(h2)


def test_recovery_rearms_the_notification(tmp_path):
    arch = tmp_path / "config" / "emhass" / "plans"
    arch.mkdir(parents=True)
    h = Harness(now=local(2026, 10, 10, 10, 20, 40))
    mod = _load(h, arch)
    mod.heating_follower_tick()
    t0 = local(2026, 10, 10, 10, 0)
    core.write_plan_archive(str(arch), t0, _thermal_doc(t0, 8, hp=[1200] * 8))
    mod.heating_follower_tick()
    assert [e for e in h.effects if e["kind"] == "state.set" and e["entity_id"] == "sensor.heating_emhass_plan"][-1]["state"] == "AC"
    for p in arch.iterdir():
        p.unlink()
    mod.heating_follower_tick()
    assert len(_notifies(h)) == 2


def test_a_surprise_in_the_core_is_logged_and_published_as_off(tmp_path, monkeypatch):
    arch = tmp_path / "config" / "emhass" / "plans"
    arch.mkdir(parents=True)
    h = Harness(now=local(2026, 10, 10, 10, 20, 40))
    mod = _load(h, arch)

    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(mod.core, "thermal_step_in_force", boom)
    mod.heating_follower_tick()
    assert _sets(h)["sensor.heating_emhass_plan"]["state"] == "off"
    assert [e for e in h.effects if e["kind"] == "log" and e["level"] == "warning"]
