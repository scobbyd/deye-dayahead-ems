"""replay: the walk's plumbing without the solver, and two real ticks with it."""
import gzip
import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest import replay
from backtest import replay_inputs as ri
from test_backtest_replay_inputs import constant_fraction, synthetic_frame

TZ = "Europe/Amsterdam"
AMS = ZoneInfo(TZ)


def test_plan_path_matches_write_plan_archive():
    tick = datetime(2026, 7, 24, 13, 0, 4, tzinfo=AMS)
    assert replay.plan_path("/x/plans", tick) == "/x/plans/20260724T130004.json.gz"


def test_slice_frame_long_format():
    day = {"date": "2026-07-23", "slice_start": "2026-07-23T00:00:00+02:00", "step_min": 15, "n": 4, "n_past": 4,
           "soc_start_pct": 40.0, "soc_now_pct": 41.5, "clamp_writes": 2, "dwell_95_h": 0.0,
           "p_batt_w": [-1000.0, -1000.0, 0.0, 500.0], "p_grid_w": [200.0, 100.0, 0.0, -300.0],
           "soc_pct": [40.5, 41.0, 41.0, 40.7], "p_pv_w": [0.0, 0.0, 100.0, 900.0], "pv_curtail_w": [0.0] * 4,
           "p_load_w": [300.0] * 4, "pv_fc_w": [0.0, 0.0, 80.0, 800.0], "load_fc_w": [310.0] * 4,
           "pv_meas_w": [0.0, 0.0, 100.0, 900.0], "pv_micro_w": [0.0] * 4, "micro_cut_w": [0.0] * 4,
           "buy": [0.1] * 4, "sell": [0.08] * 4, "cost_eur": [0.005, 0.0025, 0.0, -0.006]}
    df = replay.slice_frame(day)
    assert len(df) == 4
    assert list(df["ts_utc"])[0] == pd.Timestamp("2026-07-22 22:00:00+00:00")
    assert df["date"].iloc[0] == "2026-07-23" and df["soc_now_pct"].iloc[0] == 41.5
    assert df["p_batt_w"].tolist() == [-1000.0, -1000.0, 0.0, 500.0]


def test_tick_inputs_shape(tmp_path):
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=synthetic_frame(first=date(2026, 7, 14), days=16), frac=constant_fraction(0.4),
                    da=synthetic_da_prices())
    tick = datetime(2026, 7, 23, 10, 13, tzinfo=AMS)
    w.state["soc_end_pct"]["2026-07-22"] = 44.0
    inp = w.tick_inputs(tick, soc_pct=44.0, soc_source="settled")
    assert inp["now"] == tick.isoformat() and inp["tz"] == TZ and inp["base_url"] == "http://library"
    assert inp["archive_dir"] == os.path.join(str(tmp_path), "plans") and inp["dry_run"] is False
    assert len(inp["np_today"]) == 96 and inp["np_tomorrow"] is None          # before 13:00
    assert inp["epex"][0]["datetime"] == "2026-07-23T08:00:00Z"
    assert len(inp["solcast_today"]) == 48 and len(inp["solcast_ese_today"]) == 144
    assert inp["soc_pct"] == 44.0 and inp["soc_source"] == "settled"
    assert inp["tariff"] == ri.TARIFF and inp["knobs"]["growatt_share"] == 0.288
    assert len(inp["actual_pv_w"]) == 96 and inp["actual_load_w"][95] == inp["actual_load_w"][40]
    assert len(inp["prev_pv_w"]) == 96 and inp["prev_load_w"][95] == 395.0
    assert sorted(inp["load_days"]) == [date(2026, 7, 16) + timedelta(days=k) for k in range(7)]
    assert inp["load_now_w"] == 340.0
    later = w.tick_inputs(tick.replace(hour=13, minute=0, second=4), soc_pct=44.0, soc_source="settled")
    assert len(later["np_tomorrow"]) == 96
    w.close()


def synthetic_da_prices(first=date(2026, 7, 14), days=14):
    from backtest import pricefill as pf
    da, t = {}, datetime.combine(first, datetime.min.time(), tzinfo=AMS)
    end = t + timedelta(days=days)
    while t < end:
        h = t.hour
        da[t.astimezone(timezone.utc)] = 0.05 + (0.15 if 17 <= h < 21 else 0.0) - (0.06 if 11 <= h < 15 else 0.0)
        t += pf.STEP
    return da


def test_two_ticks_chain_the_virtual_pack(tmp_path):
    pytest.importorskip("emhass")
    from backtest.solver import LibrarySolver
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=synthetic_frame(first=date(2026, 7, 14), days=16), frac=constant_fraction(0.4),
                    da=synthetic_da_prices(), solver=LibrarySolver(data_dir=str(tmp_path / "emhass")))
    ticks = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))[:3]     # 23:43, 00:13, 00:43
    res = [w.run_tick(t) for t in ticks]
    assert res[0]["soc_source"] == "real" and res[0]["soc_pct"] == 48.0   # seeded from the frame (07-22 = 40 + 8)
    assert res[1]["ok"] and res[1]["soc_source"] == "settled"
    assert res[2]["ok"] and res[2]["soc_source"] == "settled"
    assert isinstance(res[2]["soc_pct"], float)
    assert len(os.listdir(w.archive)) == 3
    # the archived second plan starts from the settled SOC the walk reported
    import gzip
    doc = json.load(gzip.open(replay.plan_path(w.archive, ticks[1]), "rt"))
    assert doc["soc_init"] == pytest.approx(res[1]["soc_pct"] / 100.0, abs=1e-4)
    assert doc["tariff"] == ri.TARIFF and doc["knobs"]["growatt_share"] == 0.288
    # resume: an existing plan doc skips the solve
    again = w.run_tick(ticks[1])
    assert again["skipped"] is True
    w.close()


def test_settle_day_writes_parquet_and_state(tmp_path):
    pytest.importorskip("emhass")
    from backtest.solver import LibrarySolver
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=synthetic_frame(first=date(2026, 7, 14), days=16), frac=constant_fraction(0.4),
                    da=synthetic_da_prices(), solver=LibrarySolver(data_dir=str(tmp_path / "emhass")))
    for t in ri.ticks(date(2026, 7, 23), date(2026, 7, 23))[:2]:
        w.run_tick(t)
    day = w.settle_day(date(2026, 7, 23))
    assert day["n_past"] == 96 and day["soc_now_pct"] is not None
    assert os.path.exists(os.path.join(str(tmp_path), "slices", "2026-07-23.parquet"))
    assert json.load(open(os.path.join(str(tmp_path), "state.json")))["soc_end_pct"]["2026-07-23"] == day["soc_now_pct"]
    df = replay.load_slices(str(tmp_path))
    assert len(df) == 96 and df["date"].nunique() == 1
    w.close()


def test_resume_reuses_meta_knobs(tmp_path, monkeypatch):
    """A second Walk against the same run_dir must not re-read ri.live_knobs():
    the knobs frozen into meta.json at the run's first start stand, even when
    the live mirror would now give something else."""
    from emhasscore.objective import knobs as _knobs
    calls = []

    def fake_live_knobs(mirror=None):
        calls.append(1)
        return _knobs({"growatt_share": 0.111 if len(calls) == 1 else 0.999})

    monkeypatch.setattr(ri, "live_knobs", fake_live_knobs)
    frame, frac, da = synthetic_frame(first=date(2026, 7, 14), days=16), constant_fraction(0.4), synthetic_da_prices()
    w1 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                     frame=frame, frac=frac, da=da)
    w1._write_meta()
    assert w1.knobs["growatt_share"] == 0.111 and len(calls) == 1
    w1.close()

    w2 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                     frame=frame, frac=frac, da=da)
    assert w2.knobs["growatt_share"] == 0.111          # not 0.999: live_knobs was not called again
    assert len(calls) == 1
    w2._write_meta()
    meta = json.load(open(os.path.join(str(tmp_path), "meta.json")))
    assert meta["knobs"]["growatt_share"] == 0.111
    assert meta["resumed_utc"] and len(meta["resumed_utc"]) == 1
    w2.close()


def test_settle_day_raises_when_the_previous_link_is_missing(tmp_path):
    """Past the walk's first day, a missing soc_end_pct link for the day
    before must abort settle_day, not fall back to virtual_day's own
    archive-midnight anchor."""
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 24), run_dir=str(tmp_path),
                    frame=synthetic_frame(first=date(2026, 7, 14), days=16), frac=constant_fraction(0.4),
                    da=synthetic_da_prices())
    with pytest.raises(RuntimeError, match="no settled SOC"):
        w.settle_day(date(2026, 7, 24))          # 2026-07-23 was never settled
    w.close()


def test_seed_keyed_on_state_not_archive_listing(tmp_path):
    """A failed first solve leaves the archive empty; the next tick must not
    look "unseeded" again and re-seed from the frame at a different instant -
    it must abort on a broken chain instead."""
    def stub_solver(base_url, payload, timeout=180):
        return {"ok": False, "message": "stub"}

    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=synthetic_frame(first=date(2026, 7, 14), days=16), frac=constant_fraction(0.4),
                    da=synthetic_da_prices(), solver=stub_solver)
    ticks = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))
    res0 = w.run_tick(ticks[0])
    assert res0["ok"] is False and res0["soc_source"] == "real"
    assert os.listdir(w.archive) == []               # the stub never reaches write_plan_archive
    assert w.state["seed_tick"] == ticks[0].isoformat()
    with pytest.raises(RuntimeError, match="virtual chain broken"):
        w.run_tick(ticks[1])                         # must NOT re-seed from the frame at ticks[1]
    w.close()


def test_knowledge_walk_inputs(tmp_path):
    frame = synthetic_frame(first=date(2026, 7, 14), days=16)
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=frame, frac=constant_fraction(0.4), da=synthetic_da_prices(), knowledge="hindsight")
    tick = datetime(2026, 7, 23, 10, 13, tzinfo=AMS)
    inp = w.tick_inputs(tick, soc_pct=44.0, soc_source="settled")
    assert sorted(inp["load_days"]) == [date(2026, 7, 22), date(2026, 7, 23)]     # today's shape twice: the median is the day
    assert inp["load_days"][date(2026, 7, 23)][40] == 340.0 == inp["load_days"][date(2026, 7, 22)][40]
    from emhasscore.series import load_series
    vals, info = load_series(tick.replace(minute=15), 8, TZ, inp["load_days"])
    assert vals is not None and info["ref_days"] == 2 and vals[1] == 342.0        # 10:30 local = slot 42
    d = ri.day_frame(frame, date(2026, 7, 23))
    assert inp["solcast_today"][24]["pv_estimate"] * 1000 == pytest.approx(float((d["pv_pot_main_w"] + d["micro_w"]).iloc[48:50].mean()), abs=0.5)
    assert "load_exact_w" not in inp                                             # hindsight: today only, via the profile
    w.close()
    w2 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), frame=frame, frac=constant_fraction(0.4),
                     da=synthetic_da_prices(), knowledge="omni2", run_dir=None)
    assert w2.run_dir.endswith("foresight_omni2")
    inp2 = w2.tick_inputs(tick, soc_pct=44.0, soc_source="settled")
    from emhasscore.planning import load_exact_key
    assert len(inp2["load_exact_w"]) == 3 * 96                                   # omni2: exact load over the box
    d2 = ri.day_frame(frame, date(2026, 7, 24))
    assert inp2["load_exact_w"][load_exact_key(d2.index[10].to_pydatetime())] == round(float(d2["load_w"].iloc[10]), 1)
    w2.close()
    with pytest.raises(ValueError):
        replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path), frame=frame,
                    frac=constant_fraction(0.4), da=synthetic_da_prices(), knowledge="magic")


def test_tariff_override_is_frozen_in_meta_and_priced_into_ticks(tmp_path):
    """--energy-tax prices every tick of the run and survives a resume: the
    second Walk on the run_dir takes the tariff from meta.json, not ri.TARIFF."""
    frame, frac, da = synthetic_frame(first=date(2026, 7, 14), days=16), constant_fraction(0.4), synthetic_da_prices()
    tf = dict(ri.TARIFF, energy_tax=0.09161)
    w1 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                     frame=frame, frac=frac, da=da, tariff=tf)
    w1._write_meta()
    tick = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))[1]
    inp = w1.tick_inputs(tick, 50.0, "seed")
    assert inp["tariff"]["energy_tax"] == 0.09161 and inp["tariff"]["supplier_fee"] == ri.TARIFF["supplier_fee"]
    w1.close()
    w2 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                     frame=frame, frac=frac, da=da)
    assert w2.tariff["energy_tax"] == 0.09161
    assert json.load(open(os.path.join(str(tmp_path), "meta.json")))["tariff"]["energy_tax"] == 0.09161
    w2.close()
    assert ri.TARIFF["energy_tax"] == 0.0


def test_pv_scale_zero_walks_a_day_without_a_plant(tmp_path):
    """--pv-scale 0 empties every PV column (measured, potential, Growatt and
    the forecast lanes), the walk still solves and settles, the plant's size is
    frozen in meta.json and a resumed Walk takes it from there."""
    pytest.importorskip("emhass")
    from backtest.solver import LibrarySolver
    frame, frac, da = synthetic_frame(first=date(2026, 7, 14), days=16), constant_fraction(0.4), synthetic_da_prices()
    scaled = ri.scale_pv(frame, 0.0)
    assert all(float(scaled[c].abs().sum()) == 0.0 for c in ri.PV_COLS if c in scaled.columns)
    assert float(frame["pv_main_w"].abs().sum()) > 0                      # the input frame is untouched
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=frame, frac=frac, da=da, solver=LibrarySolver(data_dir=str(tmp_path / "emhass")), pv_scale=0.0)
    w._write_meta()
    tick = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))[1]
    inp = w.tick_inputs(tick, 50.0, "seed")
    assert max(inp["actual_pv_w"]) == 0.0 and max(inp["micro_w"]) == 0.0
    res = w.run_tick(tick)
    assert res.get("ok"), res
    plans = sorted(os.listdir(os.path.join(str(tmp_path), "plans")))
    doc = json.load(gzip.open(os.path.join(str(tmp_path), "plans", plans[-1])))
    assert max(float(r["P_PV"]) for r in doc["rows"]) == 0.0 and doc["optim_status"] == "Optimal"
    w.close()
    assert json.load(open(os.path.join(str(tmp_path), "meta.json")))["pv_scale"] == 0.0
    w2 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path), frame=frame, frac=frac, da=da)
    assert w2.pv_scale == 0.0 and float(w2.df["pv_main_w"].abs().sum()) == 0.0
    w2.close()


def test_hardware_override_reaches_the_lp_the_pack_and_the_actuator(tmp_path):
    """--capacity-kwh / --inverter-kw: the LP's plant params, the payload's power
    knob, the settlement's capacity and the register ceiling all move together,
    the set is frozen in meta.json, and the default restores the shipped values."""
    pytest.importorskip("emhass")
    from backtest import hardware as hwm
    from backtest.solver import LibrarySolver
    from emhasscore import deye, objective
    frame, frac, da = synthetic_frame(first=date(2026, 7, 14), days=16), constant_fraction(0.4), synthetic_da_prices()
    hw = {"capacity_kwh": 96.4, "inverter_kw": 17.0}
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=frame, frac=frac, da=da, solver=LibrarySolver(data_dir=str(tmp_path / "emhass")), hardware=hw)
    assert w.knobs["batt_power_max_w"] == 17500.0
    assert deye.DEYE_CURRENT_MAX_A == 336.0 and objective.P_NOM_INV_KW == 17.0
    tick = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))[1]
    res = w.run_tick(tick)               # run() is what patches the solver; run_tick alone must be patched too
    assert res.get("ok"), res
    hwm.patch_solver(w.solver, w.hw)
    pc = w.solver.params["plant_conf"]
    assert pc["battery_nominal_energy_capacity"] == 96400 and pc["inverter_ac_output_max"] == 17000
    assert pc["battery_charge_power_max"] == 17500.0
    w._write_meta()
    w.close()
    meta = json.load(open(os.path.join(str(tmp_path), "meta.json")))
    assert meta["hardware"] == {"capacity_kwh": 96.4, "inverter_kw": 17.0}
    w2 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path), frame=frame, frac=frac, da=da)
    assert w2.hw["capacity_kwh"] == 96.4 and w2.knobs["batt_power_max_w"] == 17500.0
    w2.close()
    hwm.apply(hwm.DEFAULT)
    assert deye.DEYE_CURRENT_MAX_A == 240.0 and objective.P_NOM_INV_KW == 12.0
    assert hwm.label(hw) == "_c96.4_i17" and hwm.label(hwm.DEFAULT) == ""


def test_walk_keeps_the_settled_rebalance_clock(tmp_path):
    """The walk starts with nothing on record (overdue: the pull is on in the
    first plan), folds every settled slice into state.json's rebalance clock,
    and hands the state to run_plan on the next tick."""
    pytest.importorskip("emhass")
    from backtest.solver import LibrarySolver
    frame, frac, da = synthetic_frame(first=date(2026, 7, 14), days=16), constant_fraction(0.4), synthetic_da_prices()
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=frame, frac=frac, da=da, solver=LibrarySolver(data_dir=str(tmp_path / "emhass")))
    ticks = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))
    res = w.run_tick(ticks[0])                                                # the seed tick, 23:43 the day before
    assert res.get("ok"), res
    doc = json.load(gzip.open(os.path.join(str(tmp_path), "plans", sorted(os.listdir(os.path.join(str(tmp_path), "plans")))[-1])))
    assert doc["payload"]["battery_soc_deficit_threshold"] == 1.0            # overdue: nothing on record
    assert res["days_since_full"] is None
    w.run_tick(ticks[1])
    w.run_tick(ticks[2])
    st = w.state.get("rebalance")
    assert st is not None and st["seen"] is not None                          # the settled slice was folded
    w.settle_day(date(2026, 7, 23))
    saved = json.load(open(os.path.join(str(tmp_path), "state.json")))["rebalance"]
    assert saved["seen"].startswith("2026-07-24T00:00")                       # the day's last quarter, folded at settlement
    w.close()


def test_frame_file_is_frozen_in_meta_and_resumed(tmp_path):
    """--frame names another frame file (a pretend world); the name is frozen
    in meta.json and a resume on the run_dir reads the same name. A bare name
    resolves into the data dir, the default to data/frame.csv."""
    frame, frac, da = synthetic_frame(first=date(2026, 7, 14), days=16), constant_fraction(0.4), synthetic_da_prices()
    w1 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                     frame=frame, frac=frac, da=da, frame_csv="frame_winter.csv")
    w1._write_meta()
    w1.close()
    assert json.load(open(os.path.join(str(tmp_path), "meta.json")))["frame"] == "frame_winter.csv"
    w2 = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                     frame=frame, frac=frac, da=da)
    assert w2.frame_csv == "frame_winter.csv"
    w2.close()
    assert ri.frame_path(None) == ri.FRAME_CSV
    assert ri.frame_path("frame_winter.csv") == os.path.join(ri.BT_DATA, "frame_winter.csv")
    assert ri.frame_path("/x/y.csv") == "/x/y.csv"
