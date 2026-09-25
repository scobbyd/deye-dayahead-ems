"""replay_ladder: the stand-in's virtual days as the ladder's actuals."""
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest import replay, replay_ladder as rl
from emhasscore.ladder import LADDER_COLUMNS, RUNGS, read_ladder, upsert_ladder
from emhasscore.objective import ETA_BRIDGE
from test_backtest_replay_inputs import synthetic_frame

TZ = "Europe/Amsterdam"
AMS = ZoneInfo(TZ)


def settled_days(first=date(2026, 7, 31), n=4):
    """A slices frame of n settled days: batt -1.000 W for the first 8 steps,
    +500 W for the last 8, grid the mirror, soc a ramp."""
    frames = []
    for k in range(n):
        d = first + timedelta(days=k)
        start = datetime.combine(d, datetime.min.time(), tzinfo=AMS)
        pb = [-1000.0] * 8 + [0.0] * 80 + [500.0] * 8
        day = {"date": d.isoformat(), "slice_start": start.isoformat(), "step_min": 15, "n": 96, "n_past": 96,
               "soc_start_pct": 40.0 + k, "soc_now_pct": 41.0 + k, "clamp_writes": 0, "dwell_95_h": 0.0,
               "p_batt_w": pb, "p_grid_w": [-x for x in pb], "soc_pct": [40.0 + k + i / 96 for i in range(96)],
               "p_pv_w": [0.0] * 96, "pv_curtail_w": [0.0] * 96, "p_load_w": [300.0] * 96,
               "pv_fc_w": [0.0] * 96, "load_fc_w": [300.0] * 96, "pv_meas_w": [0.0] * 96,
               "pv_micro_w": [0.0] * 96, "micro_cut_w": [0.0] * 96, "buy": [0.1] * 96, "sell": [0.08] * 96,
               "cost_eur": [0.0] * 96}
        frames.append(replay.slice_frame(day))
    return pd.concat(frames, ignore_index=True)


def test_batt_dc_inverts_replay_day():
    ac = [-1000.0, 0.0, 500.0]
    dc = rl.batt_dc_from_ac(ac)
    # replay_day: b_ac = b_dc x eta when discharging, b_dc / eta when charging
    back = [d * ETA_BRIDGE if d >= 0 else d / ETA_BRIDGE for d in dc]
    # batt_dc_from_ac rounds its DC output to 0,1 W (the settled slice's own
    # storage precision), so the round trip is exact to that resolution, not
    # to float precision: 500,0 W discharges to dc 505,6 and back to 500,04.
    assert back == pytest.approx(ac, abs=0.1)


def test_day_inputs_shape():
    sl, df = settled_days(), synthetic_frame(first=date(2026, 7, 24), days=14)
    inp = rl.day_inputs(sl, df, date(2026, 8, 1))
    act = inp["day"]
    assert set(act) >= {"pv_w", "load_w", "micro_w", "curtailed", "pv_peak_w", "soc_pct", "grid_w", "batt_dc_w"}
    assert len(act["grid_w"]) == 96 and act["grid_w"][0] == 1000.0
    assert act["batt_dc_w"][0] == pytest.approx(-1000.0 * ETA_BRIDGE)
    assert act["soc_pct"][0] == pytest.approx(41.0)              # the stand-in's, not the frame's
    # pv_w is the settled slice's p_pv_w (all zeros here), not the frame's
    # triangle (8.000 W at noon) - proves the rungs' sun is the stand-in's own
    day_sl = rl._day_slice(sl, date(2026, 8, 1))
    assert act["pv_w"] == [round(float(v), 1) for v in day_sl["p_pv_w"]]
    assert act["pv_w"][48] == 0.0
    assert act["pv_w"] == act["pv_peak_w"]
    assert act["curtailed"] == [0.0] * 96
    assert len(inp["win1"]["grid_w"]) == 192 and len(inp["win2"]["grid_w"]) == 288
    assert rl.day_inputs(sl, df, date(2026, 8, 3))["win1"] is None    # 08-04 not settled


def test_seed_row_chains_every_rung():
    row = rl.seed_row(settled_days(), date(2026, 7, 31))
    assert row["date"] == "2026-07-31"
    for r in RUNGS:
        assert row[f"{r}_status"] == "ok" and row[f"{r}_soc_end_pct"] == 41.0
    assert set(row) == set(LADDER_COLUMNS)


def test_add_rec_flag_is_idempotent(tmp_path):
    p = str(tmp_path / "l.csv")
    upsert_ladder(p, {"date": "2026-08-01", "flags": "omni2_reset"})
    upsert_ladder(p, {"date": "2026-08-02", "flags": ""})
    assert rl.add_rec_flag(p) == 2
    assert rl.add_rec_flag(p) == 0
    rows = {r["date"]: r["flags"] for r in read_ladder(p)}
    assert rows["2026-08-01"] == "omni2_reset;rec" and rows["2026-08-02"] == "rec"


def test_merge_flags_restores_and_is_idempotent(tmp_path):
    p = str(tmp_path / "l.csv")
    upsert_ladder(p, {"date": "2026-08-01", "flags": "omni2_caps;rec"})
    before = {r["date"]: r["flags"] for r in read_ladder(p)}
    upsert_ladder(p, {"date": "2026-08-01", "flags": ""})    # the core's rewrite on a chained day
    assert rl.merge_flags(p, before) == 1
    assert read_ladder(p)[0]["flags"] == "omni2_caps;rec"
    assert rl.merge_flags(p, before) == 0


def test_supplier_join_adds_two_columns(tmp_path):
    p = str(tmp_path / "l.csv")
    upsert_ladder(p, {"date": "2026-07-31", "flags": "rec;seed"})
    upsert_ladder(p, {"date": "2026-08-01", "actual_eur": -1.0, "flags": "rec"})
    sl, df = settled_days(), synthetic_frame(first=date(2026, 7, 24), days=14)
    out = rl.supplier_join(p, sl, df, str(tmp_path / "j.csv"))
    assert list(out.columns)[-2:] == ["supplier_eur", "supplier_da_eur"]
    seed = out[out["date"] == "2026-07-31"].iloc[0]
    assert pd.isna(seed["supplier_eur"]) and pd.isna(seed["supplier_da_eur"])
    r = out[out["date"] == "2026-08-01"].iloc[0]
    assert r["supplier_eur"] == pytest.approx(-96 * 0.01)                       # -sum(net_result_eur), ladder sign
    # frame grid -1.000 W all day x sell 0,08 x 0,25 h -> -0,02 EUR per step
    assert r["supplier_da_eur"] == pytest.approx(-96 * 0.02)
    assert os.path.exists(str(tmp_path / "j.csv"))
    # without the supplier's column the settlement column is empty and the day-ahead one stays
    out2 = rl.supplier_join(p, sl, df.drop(columns=["net_result_eur"]), str(tmp_path / "j2.csv"))
    r2 = out2[out2["date"] == "2026-08-01"].iloc[0]
    assert pd.isna(r2["supplier_eur"]) and r2["supplier_da_eur"] == pytest.approx(-96 * 0.02)


def test_np_rows_for_covers_days_minus_one_to_plus_three():
    from test_backtest_replay import synthetic_da_prices
    rows = rl.np_rows_for(["2026-08-01", "2026-08-02"], synthetic_da_prices(first=date(2026, 7, 25), days=14), TZ)
    starts = sorted(r["start"] for r in rows)
    assert starts[0] == datetime(2026, 7, 31, 0, tzinfo=AMS).astimezone(timezone.utc).isoformat()
    assert len(rows) == 96 * 6                                               # 07-31 .. 08-05 for days 08-01 and 08-02


def test_actual_rung_reproduces_the_stand_in(tmp_path):
    """On the smoke run's archive the actual rung's replay_day, fed the
    stand-in's own flows (pv_w on the stand-in's own p_pv_w basis, curtailed
    all zero), equals the stand-in's cash exactly - even with the synthetic
    day's two suspect noon quarters, since the rungs no longer see the
    frame's curtailment mask at all."""
    pytest.importorskip("emhass")
    from backtest import replay_inputs as ri
    from backtest.solver import LibrarySolver
    from test_backtest_replay import synthetic_da_prices
    from test_backtest_replay_inputs import constant_fraction
    frame = synthetic_frame(first=date(2026, 7, 14), days=14)
    w = replay.Walk("foresight", date(2026, 7, 23), date(2026, 7, 23), run_dir=str(tmp_path),
                    frame=frame, frac=constant_fraction(0.4), da=synthetic_da_prices(),
                    solver=LibrarySolver(data_dir=str(tmp_path / "emhass")))
    for t in ri.ticks(date(2026, 7, 23), date(2026, 7, 23))[:2]:      # 23:43 and 00:13: the day is covered
        w.run_tick(t)
    w.settle_day(date(2026, 7, 23))
    sl = replay.load_slices(str(tmp_path))
    inp = rl.day_inputs(sl, frame, date(2026, 7, 23))
    from emhasscore.ladder import _run_actual
    from emhasscore.scoring import lambda_for
    lam = lambda_for(sl["sell"].tolist(), 0.9)
    res = _run_actual(w.archive, date(2026, 7, 23), TZ, inp["day"], 48.2, lam,
                      soc0=float(sl["soc_start_pct"].iloc[0]))
    assert res["status"] == "ok"
    # scoring.replay_day rounds cash to 4 dp; the identity is exact below that
    assert res["cash"] == pytest.approx(float(sl["cost_eur"].sum()), abs=1e-4)
    w.close()
