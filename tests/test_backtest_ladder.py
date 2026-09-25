"""The knowledge ladder on the golden stub solver: lane specs, one settled day, resume."""
import os
from datetime import date
import numpy as np, pandas as pd, pytest
from backtest import frames, frame_schema, ladder
from golden import fixtures as F

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
CSV = os.path.join(DATA, "frames_2026_q15.csv")
# The stub solver does not know the bridge, so the P-lane invariants below need a day
# whose main-string potential stays under 12 kW on every quarter (the supplier's PV quarters
# carry read-timing jitter that puts single quarters of a sunny hour past it): 12 June
# 2026, potential at most 8,9 kW, 8 suspect quarters, nothing missing.
DAY = date(2026, 6, 12)
T0 = F.local(2026, 6, 12, 0, 0)


@pytest.fixture(scope="module")
def df():
    if not os.path.exists(CSV):
        pytest.skip(f"no reference frame at {CSV}")
    return frames.load(CSV)


def test_lane_specs():
    assert set(ladder.LANES) == {"P0", "P1", "P2", "F_da", "F_sol", "F_mix", "F_om0"}
    assert ladder.LANES["P0"]["days"] == 1 and ladder.LANES["P2"]["days"] == 3


def test_run_day_with_stub(df):
    day = DAY
    stub = F.StubSolver(T0)
    out = ladder.run_day(df, day, "P1", stub, ladder.LIVE_TARIFF)
    assert out["status"] == "ok", out
    assert out["lane"] == "P1" and out["day"] == DAY.isoformat()
    assert len(stub.calls) == 1 and stub.calls[0]["prediction_horizon"] == 192
    assert stub.calls[0]["soc_final"] == 0.5
    for k in ("eur", "cash_eur", "soc_term_eur", "loss_eur", "pv_kwh", "load_kwh",
              "cycles", "discharged_kwh", "nobatt_eur", "batt_value_eur"):
        assert isinstance(out[k], float), k
    assert out["cycles"] >= 0.0 and out["discharged_kwh"] >= 0.0
    assert 0.0 <= out["soc_above_90_pct"] <= 100.0 and 0.0 <= out["soc_below_15_pct"] <= 100.0
    assert out["eur_per_kwh"] is None or isinstance(out["eur_per_kwh"], float)


def test_run_day_capacity_is_a_parameter(df):
    """The settled pack's size scales the cycle count; the default is the plant's."""
    a = ladder.run_day(df, DAY, "P1", F.StubSolver(T0), ladder.LIVE_TARIFF)
    b = ladder.run_day(df, DAY, "P1", F.StubSolver(T0), ladder.LIVE_TARIFF, capacity_kwh=ladder.CAPACITY_KWH / 2)
    assert a["status"] == b["status"] == "ok"
    assert b["cycles"] == pytest.approx(2 * a["cycles"], rel=0.02)
    assert b["discharged_kwh"] == pytest.approx(a["discharged_kwh"])


def test_resume_skips_done(df, tmp_path):
    out = tmp_path / "ladder.csv"
    stub = F.StubSolver(F.local(2026, 6, 10, 0, 0))
    ladder.run(df, "2026-06-10", "2026-06-10", ["P0"], str(out), solver=stub)
    n1 = len(stub.calls)
    ladder.run(df, "2026-06-10", "2026-06-10", ["P0"], str(out), solver=stub)
    assert len(stub.calls) == n1
    assert len(pd.read_csv(out)) == 1


def test_resume_rejects_wrong_header(df, tmp_path):
    """An older CSV (here missing capped_steps) would otherwise resume with its
    rows misaligned under the current FIELDS; run() must refuse it."""
    import csv
    out = tmp_path / "ladder.csv"
    old_fields = [f for f in ladder.FIELDS if f != "capped_steps"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=old_fields)
        w.writeheader()
        w.writerow({k: "" for k in old_fields})
    stub = F.StubSolver(F.local(2026, 6, 10, 0, 0))
    with pytest.raises(ValueError, match="header"):
        ladder.run(df, "2026-06-10", "2026-06-10", ["P0"], str(out), solver=stub)


def test_payload_is_main_pv_and_net_load(df):
    """The frame's PV columns are main-string already: the LP gets them as is,
    the microinverter leaves the load list only, and rows_to_gross puts it back."""
    day = date(2026, 6, 10)
    stub = F.StubSolver(F.local(2026, 6, 10, 0, 0))
    ladder.run_day(df, day, "P0", stub, ladder.LIVE_TARIFF)
    t0, n, win = ladder._horizon(df, day, 1)
    p = stub.calls[0]
    assert p["pv_power_forecast"] == [round(v, 1) for v in win["pv_pot_main_w"]]
    assert p["load_power_forecast"] == [round(l - round(m, 1), 1) for l, m in zip(win["load_w"], win["micro_w"])]


def test_day_ahead_lane_switches_at_midnight(df):
    day = date(2026, 6, 10)
    t0, n, win = ladder._horizon(df, day, 2)
    pv = ladder._pv_list(win, ladder.LANES["F_da"], day)
    steps = 96
    assert pv[:steps] == win["fc_pv_om24_main_w"].tolist()[:steps]
    assert pv[steps:] == win["fc_pv_om48_main_w"].tolist()[steps:]


def test_p_lane_settles_as_the_live_hindsight_lane(df):
    """For a P lane the true potential IS the plan's P_PV, so the local settle
    and scoring._hindsight_settle must agree to the cent."""
    from emhasscore import scoring
    day = DAY
    stub = F.StubSolver(T0)
    assert df.loc[ladder._horizon(df, day, 1)[2].index, "pv_pot_main_w"].max() < ladder.BRIDGE_W
    t0, n, mic, payload = ladder.plan_payload(df, day, "P1")
    rows = ladder.rows_to_gross(ladder.restamp(stub("x", payload)["rows"], t0, n), mic)
    act = ladder._day_actuals(df, day)
    doc = ladder._doc(t0, n, payload["soc_init"], ladder.LIVE_TARIFF)
    import copy
    s1, hs1, rep1 = ladder._settle(copy.deepcopy(rows), doc, day, mic, act)
    s2, hs2, rep2 = scoring._hindsight_settle(copy.deepcopy(rows), doc, day, ladder.TZ, mic, act,
                                              ladder.CAPACITY_KWH, ladder.LAMBDA_FRAC)
    assert s1 == s2 == "ok"
    assert abs(rep1["eur"] - rep2["eur"]) < 0.005, (rep1, rep2)
    assert hs1["p_batt_w"] == hs2["p_batt_w"]
    assert hs1["capped_steps"] == 0                # a P lane knows the sun: the bridge never caps it


def test_no_data_before_solcast(df):
    stub = F.StubSolver(F.local(2026, 3, 20, 0, 0))
    out = ladder.run_day(df, date(2026, 3, 20), "F_sol", stub, ladder.LIVE_TARIFF)
    assert out["status"] == "no_data" and not stub.calls


def test_no_battery_baseline_repair_rule():
    """G_nobatt = G_meas + B_meas_ac - repair; the repair only where the sell
    price is at or above zero, and never negative."""
    eta = ladder.ETA_BRIDGE
    act = {"grid_w": [-1000.0, -1000.0, 500.0, 0.0], "batt_dc_w": [2000.0, -2000.0, 0.0, 0.0],
           "pv_peak_w": [5000.0, 5000.0, 3000.0, 4000.0], "pv_w": [3000.0, 3000.0, 3500.0, 4000.0]}
    sell = [0.10, -0.01, 0.10, 0.0]
    g = ladder._grid_without_battery(act, sell)
    assert g[0] == pytest.approx(-1000.0 + 2000.0 * eta - 2000.0)     # curtailed, sell >= 0: repaired
    assert g[1] == pytest.approx(-1000.0 - 2000.0 / eta)              # curtailed, sell < 0: no repair
    assert g[2] == pytest.approx(500.0)                               # potential below measured: nothing
    assert g[3] == pytest.approx(0.0)


def _stub_rows(df, day, lane, stub, edit=None):
    """Rows of one lane's plan on the stub, gross and restamped, after `edit`
    has had its way with the payload."""
    t0, n, mic, payload = ladder.plan_payload(df, day, lane)
    if edit:
        edit(payload)
    rows = ladder.rows_to_gross(ladder.restamp(stub("x", payload)["rows"], t0, n), mic)
    return t0, n, mic, payload, rows


def test_bridge_cap_binds_against_the_real_potential(df):
    """A plan that saw no sun commands a 12,5 kW discharge at noon; next to
    the real main-string potential that exceeds the 12 kW bridge, so the
    settled command is BRIDGE_W - pot, the grid absorbs the difference and
    the pack keeps the energy."""
    import copy
    day, k = DAY, 48                                                  # 12:00 local
    stub = F.StubSolver(T0)

    def no_sun_big_noon_load(p):
        p["pv_power_forecast"] = [0.0] * len(p["pv_power_forecast"])
        p["load_power_forecast"] = [400.0] * len(p["load_power_forecast"])
        p["load_power_forecast"][k] = 12500.0
        p["load_cost_forecast"][k] = max(p["load_cost_forecast"]) + 1.0   # the stub discharges at or above the median
    t0, n, mic, payload, rows = _stub_rows(df, day, "F_da", stub, no_sun_big_noon_load)
    act = ladder._day_actuals(df, day)
    doc = ladder._doc(t0, n, payload["soc_init"], ladder.LIVE_TARIFF)
    pot = act["pot_main_w"][k]                                       # main string: the bridge's own PV
    assert pot > 3000.0, pot
    s0, hs0, rep0 = ladder._settle(copy.deepcopy(rows), doc, day, mic, act, cap=False)
    s1, hs1, rep1 = ladder._settle(copy.deepcopy(rows), doc, day, mic, act, cap=True)
    assert s0 == s1 == "ok"
    assert hs0["capped_steps"] == 0 and hs1["capped_steps"] >= 1
    assert abs(hs0["p_batt_w"][k] - 12500.0) < 1.0                      # the uncapped command
    assert abs(hs1["p_batt_w"][k] - (ladder.BRIDGE_W - pot)) < 0.2       # the capped one
    d = hs0["p_batt_w"][k] - hs1["p_batt_w"][k]
    assert abs((hs1["p_grid_w"][k] - hs0["p_grid_w"][k]) - d) < 0.2       # the grid absorbed it
    kept = d * 0.25 / 1000.0 / ladder.ETA_D / ladder.CAPACITY_KWH * 100.0
    assert abs((hs1["soc_pct"][-1] - hs0["soc_pct"][-1]) - kept) < 0.05  # the pack kept it
    assert rep1["eur"] != rep0["eur"]
    # the derived command replay_day reads off the balance is the capped one
    b = hs1["p_load_w"][k] - (hs1["p_pv_w"][k] - hs1["pv_curtail_w"][k]) - hs1["p_grid_w"][k]
    assert abs(b - hs1["p_batt_w"][k]) < 0.2


def test_bridge_cap_follows_the_inverter_of_a_sweep(df):
    """hardware.apply moves objective.P_NOM_INV_KW; the cap reads it at call
    time, so a 17 kW hybrid caps later than the shipped 12 kW one."""
    import copy
    from backtest import hardware
    day, k = DAY, 48
    stub = F.StubSolver(T0)

    def no_sun_big_noon_load(p):
        p["pv_power_forecast"] = [0.0] * len(p["pv_power_forecast"])
        p["load_power_forecast"] = [400.0] * len(p["load_power_forecast"])
        p["load_power_forecast"][k] = 12500.0
        p["load_cost_forecast"][k] = max(p["load_cost_forecast"]) + 1.0
    t0, n, mic, payload, rows = _stub_rows(df, day, "F_da", stub, no_sun_big_noon_load)
    act = ladder._day_actuals(df, day)
    doc = ladder._doc(t0, n, payload["soc_init"], ladder.LIVE_TARIFF)
    pot = act["pot_main_w"][k]
    try:
        hardware.apply({"capacity_kwh": ladder.CAPACITY_KWH, "inverter_kw": 17.0})
        s, hs, _ = ladder._settle(copy.deepcopy(rows), doc, day, mic, act, cap=True)
    finally:
        hardware.apply(hardware.DEFAULT)
    assert s == "ok"
    assert abs(hs["p_batt_w"][k] - min(12500.0, 17000.0 - pot)) < 0.2


def test_run_day_reports_capped_steps(df):
    stub = F.StubSolver(T0)
    out = ladder.run_day(df, DAY, "P1", stub, ladder.LIVE_TARIFF)
    assert out["status"] == "ok" and isinstance(out["capped_steps"], int)
    assert out["capped_steps"] == 0                # the invariant: a P lane is never capped
    assert "capped_steps" in ladder.FIELDS


def test_run_day_not_optimal(df):
    stub = F.StubSolver(F.local(2026, 6, 10, 0, 0), status="Infeasible")
    out = ladder.run_day(df, date(2026, 6, 10), "P0", stub, ladder.LIVE_TARIFF)
    assert out["status"] == "not_optimal" and "Infeasible" in out["message"]
    assert "eur" not in out and len(stub.calls) == 1


def test_run_day_solve_failed(df):
    calls = []

    def failing(base_url, payload, timeout=180):
        calls.append(payload)
        return {"ok": False, "message": "last-run: infeasible (Infeasible)", "seconds": 0.1}
    out = ladder.run_day(df, date(2026, 6, 10), "P0", failing, ladder.LIVE_TARIFF)
    assert out["status"] == "solve_failed" and out["message"] == "last-run: infeasible (Infeasible)"
    assert "eur" not in out and len(calls) == 1


def _ladder_csv(path, rows):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ladder.FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ladder.FIELDS})


def _ok(day, lane, eur, batt_value, discharged, cycles=0.5, above=10.0, below=5.0):
    return {"day": day, "lane": lane, "status": "ok", "eur": eur, "cash_eur": eur, "loss_eur": 0.0,
            "batt_value_eur": batt_value, "discharged_kwh": discharged, "cycles": cycles,
            "soc_above_90_pct": above, "soc_below_15_pct": below}


def test_summarise_shared_days_and_zero_guard(tmp_path):
    """Only days every lane settled count; a lane with no discharge gets no
    eur_per_kwh; vs_P1_eur is P1-relative."""
    path = tmp_path / "l.csv"
    _ladder_csv(path, [
        _ok("2026-06-10", "P1", -8.0, -4.0, 40.0), _ok("2026-06-10", "F_da", -7.0, -3.0, 30.0),
        _ok("2026-06-11", "P1", -6.0, -3.0, 0.0), _ok("2026-06-11", "F_da", -5.0, -2.0, 0.0),
        _ok("2026-06-12", "P1", -9.0, -5.0, 50.0),                          # F_da missing: not a shared day
        {"day": "2026-06-13", "lane": "P1", "status": "no_data"}, {"day": "2026-06-13", "lane": "F_da", "status": "no_data"},
    ])
    s = ladder.summarise(str(path))
    assert set(s.index) == {"P1", "F_da"}
    assert s.loc["P1", "days"] == 2 and s.loc["F_da", "days"] == 2
    assert s.loc["P1", "total_eur"] == pytest.approx(-14.0) and s.loc["F_da", "total_eur"] == pytest.approx(-12.0)
    assert s.loc["F_da", "vs_P1_eur"] == pytest.approx(2.0) and s.loc["P1", "vs_P1_eur"] == 0.0
    assert s.loc["P1", "eur_per_kwh"] == pytest.approx(-7.0 / 40.0, abs=1e-3)
    # a lane that never discharged on the shared days: no value per kWh, no division by zero
    path2 = tmp_path / "z.csv"
    _ladder_csv(path2, [_ok("2026-06-10", "P1", -8.0, -4.0, 0.0), _ok("2026-06-10", "F_da", -7.0, -3.0, 0.0)])
    z = ladder.summarise(str(path2))
    assert z["eur_per_kwh"].isna().all()


def test_summarise_without_p1(tmp_path):
    path = tmp_path / "l.csv"
    _ladder_csv(path, [_ok("2026-06-10", "F_da", -7.0, -3.0, 30.0), _ok("2026-06-10", "F_sol", -7.5, -3.5, 31.0)])
    s = ladder.summarise(str(path))
    assert "vs_P1_eur" not in s.columns and set(s.index) == {"F_da", "F_sol"}


def test_run_resume_false_truncates(df, tmp_path):
    out = tmp_path / "ladder.csv"
    stub = F.StubSolver(F.local(2026, 6, 10, 0, 0))
    ladder.run(df, "2026-06-10", "2026-06-10", ["P0"], str(out), solver=stub)
    ladder.run(df, "2026-06-10", "2026-06-10", ["P0"], str(out), solver=stub, resume=False)
    t = pd.read_csv(out)
    assert len(t) == 1 and len(stub.calls) == 2 and list(t.columns) == ladder.FIELDS


def test_run_writes_error_row_and_goes_on(df, tmp_path):
    out = tmp_path / "ladder.csv"
    stub = F.StubSolver(F.local(2026, 6, 10, 0, 0))
    calls = []

    def flaky(base_url, payload, timeout=180):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return stub(base_url, payload, timeout)
    ladder.run(df, "2026-06-10", "2026-06-10", ["P0", "P1"], str(out), solver=flaky)
    t = pd.read_csv(out)
    assert list(t.status) == ["error", "ok"] and t.message.iloc[0] == "RuntimeError: boom"


def test_main_rejects_unknown_lane(tmp_path, capsys):
    with pytest.raises(SystemExit):
        ladder.main(["--lanes", "P1,F_nope", "--summary", "--out", str(tmp_path / "x.csv")])
    assert "F_nope" in capsys.readouterr().err


def test_summarise_names_lanes_without_an_ok_day(tmp_path, capsys):
    path = tmp_path / "l.csv"
    _ladder_csv(path, [_ok("2026-06-10", "P1", -8.0, -4.0, 40.0), _ok("2026-06-10", "F_da", -7.0, -3.0, 30.0),
                       {"day": "2026-06-10", "lane": "F_sol", "status": "no_data"}])
    s = ladder.summarise(str(path))
    assert set(s.index) == {"P1", "F_da"} and s.loc["P1", "days"] == 1
    assert "F_sol" in capsys.readouterr().out


# ---- ruling 2026-09-11: the tariffs, the optional supplier payout, the plant blocks

def test_tariff_presets():
    from emhasscore.plant import PLANT
    assert ladder.TARIFFS["plant"] is ladder.PLANT_TARIFF and ladder.LIVE_TARIFF is ladder.PLANT_TARIFF
    assert ladder.PLANT_TARIFF == {k: float(PLANT["tariff"][k]) for k in ("energy_tax", "supplier_fee", "btw_pct", "feedin_fee")}
    assert ladder.TARIFFS["2027"] is ladder.TARIFF_2027 and ladder.TARIFF_2027["energy_tax"] > 0 and ladder.TARIFF_2027["feedin_fee"] == 0.0
    # the incumbent EMS's tariff: a fee both ways puts buy and sell 2 x fee x (1 + BTW) apart
    assert ladder.TARIFFS["supplier"] is ladder.TARIFF_SUPPLIER
    assert ladder.TARIFF_SUPPLIER == {"energy_tax": 0.0, "supplier_fee": 0.02, "btw_pct": 21.0, "feedin_fee": 0.02}
    buy, sell = ladder.tariff_frame([0.10], **ladder.TARIFF_SUPPLIER)
    assert buy[0] - sell[0] == pytest.approx(2 * 0.02 * 1.21, abs=1e-5)


def test_supplier_payout_loader(tmp_path):
    p = tmp_path / "daily.csv"
    p.write_text("day,payout_eur\n2026-06-10,1.5\n2026-06-11,\n2026-06-12,-0.25\n")
    assert ladder.supplier_payout(str(p)) == {"2026-06-10": 1.5, "2026-06-12": -0.25}
    q = tmp_path / "daily_date.csv"
    q.write_text("date,status,payout_eur\n2026-06-10,complete,1.5\n")
    assert ladder.supplier_payout(str(q)) == {"2026-06-10": 1.5}
    assert ladder.supplier_payout(str(tmp_path / "missing.csv")) == {} and ladder.supplier_payout(None) == {}
    assert "supplier_payout_eur" in ladder.FIELDS


def test_run_joins_the_payout_per_day(df, tmp_path):
    out = tmp_path / "ladder.csv"
    stub = F.StubSolver(T0)
    ladder.run(df, DAY.isoformat(), DAY.isoformat(), ["P0", "P1"], str(out), solver=stub,
               payout={DAY.isoformat(): 2.5})
    t = pd.read_csv(out)
    assert t["supplier_payout_eur"].tolist() == [2.5, 2.5]
    out2 = tmp_path / "ladder2.csv"
    ladder.run(df, DAY.isoformat(), DAY.isoformat(), ["P0"], str(out2), solver=stub)     # off by default
    assert pd.read_csv(out2)["supplier_payout_eur"].isna().all()


def test_summarise_blocks_and_payout(tmp_path):
    """A range picks its own shared days; the payout over those days sits in
    attrs; two blocks never mix a modelled day into the measured total."""
    path = tmp_path / "l.csv"
    rows = [
        dict(_ok("2026-01-20", "P1", -3.0, -1.0, 10.0), supplier_payout_eur=0.5),
        dict(_ok("2026-01-20", "F_da", -2.5, -0.5, 9.0), supplier_payout_eur=0.5),
        dict(_ok("2026-02-10", "P1", -4.0, -1.0, 10.0), supplier_payout_eur=1.0),          # in neither block
        dict(_ok("2026-02-10", "F_da", -3.0, -1.0, 10.0), supplier_payout_eur=1.0),
        dict(_ok("2026-06-10", "P1", -8.0, -4.0, 40.0), supplier_payout_eur=2.0),
        dict(_ok("2026-06-10", "F_da", -7.0, -3.0, 30.0), supplier_payout_eur=2.0),
        dict(_ok("2026-06-11", "P1", -6.0, -3.0, 20.0)),                                  # no payout that day
        dict(_ok("2026-06-11", "F_da", -5.0, -2.0, 20.0)),
    ]
    _ladder_csv(path, rows)
    whole = ladder.summarise(str(path))
    assert whole.loc["P1", "days"] == 4 and whole.attrs["supplier_payout_eur"] == pytest.approx(3.5)
    blocks = {"modelled plant": (None, date(2026, 2, 7)), "measured plant": (date(2026, 2, 15), None)}
    modelled = ladder.summarise(str(path), *blocks["modelled plant"])
    assert modelled.loc["P1", "days"] == 1 and modelled.loc["P1", "total_eur"] == pytest.approx(-3.0)
    assert modelled.attrs == {"days": 1, "days_with_payout": 1, "supplier_payout_eur": 0.5,
                              "first_day": "2026-01-20", "last_day": "2026-01-20"}
    measured = ladder.summarise(str(path), *blocks["measured plant"])
    assert measured.loc["P1", "days"] == 2 and measured.loc["P1", "total_eur"] == pytest.approx(-14.0)
    assert measured.attrs["supplier_payout_eur"] == pytest.approx(2.0) and measured.attrs["days_with_payout"] == 1
    # an older CSV without the column still summarises, with no payout
    import csv
    old_csv = tmp_path / "old.csv"
    fields = [f for f in ladder.FIELDS if f != "supplier_payout_eur"]
    with open(old_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow({k: _ok("2026-06-10", "P1", -8.0, -4.0, 40.0).get(k, "") for k in fields})
    old = ladder.summarise(str(old_csv))
    assert old.attrs["supplier_payout_eur"] is None and old.loc["P1", "days"] == 1


def test_print_blocks_prints_both(tmp_path, capsys):
    path = tmp_path / "l.csv"
    _ladder_csv(path, [dict(_ok("2026-01-20", "P1", -3.0, -1.0, 10.0), supplier_payout_eur=0.5),
                       dict(_ok("2026-06-10", "P1", -8.0, -4.0, 40.0), supplier_payout_eur=2.0)])
    blocks = {"modelled plant": (None, date(2026, 2, 7)), "measured plant": (date(2026, 2, 15), None)}
    ladder.print_blocks(str(path), blocks)
    out = capsys.readouterr().out
    assert "== modelled plant: 1 shared days" in out and "what-if" in out
    assert "== measured plant: 1 shared days" in out
    assert "supplier payout over 1 of these days: 0,50 EUR in" in out and "2,00 EUR in" in out
    assert ladder._eu(1096.851) == "1.096,85" and ladder._eu(-47.178) == "-47,18"
    # a run on the measured plant only: the modelled block says so instead of a table
    _ladder_csv(path, [dict(_ok("2026-06-10", "P1", -8.0, -4.0, 40.0), supplier_payout_eur=2.0)])
    ladder.print_blocks(str(path), blocks)
    out = capsys.readouterr().out
    assert "== modelled plant: no rows" in out and "== measured plant: 1 shared days" in out
    # without a blocks table: one block, everything measured, no payout line
    _ladder_csv(path, [_ok("2026-06-10", "P1", -8.0, -4.0, 40.0)])
    ladder.print_blocks(str(path))
    out = capsys.readouterr().out
    assert "== measured plant: 1 shared days" in out and "modelled" not in out and "payout" not in out


def _two_plant_frame():
    """Ten local days: the first four modelled, six measured, every required column present."""
    from test_backtest_frame_schema import synthetic_frame
    df = frame_schema.validate(synthetic_frame(days=10, start="2026-02-04"))
    df.loc[df.index < pd.Timestamp("2026-02-08", tz="Europe/Amsterdam"), "plant"] = "modelled"
    return df


def test_blocks_and_default_range_from_the_frame():
    df = _two_plant_frame()
    days = ladder.plant_days(df)
    assert [d.isoformat() for d in days["modelled"]] == ["2026-02-04", "2026-02-05", "2026-02-06", "2026-02-07"]
    assert days["measured"][0].isoformat() == "2026-02-08" and len(days["measured"]) == 6
    blocks = ladder.blocks_for(df)
    assert blocks == {"modelled plant": (None, date(2026, 2, 7)), "measured plant": (date(2026, 2, 15), None)}
    # the reference rule: the measured block starts a week after the plant's first full day (2026-02-15)
    assert ladder.MEASURED_LEAD_DAYS == 7
    # a frame with measured rows only: one open block
    assert ladder.blocks_for(df[df["plant"] == "measured"]) == {"measured plant": (None, None)}
    assert ladder.blocks_for(None) == {"measured plant": (None, None)}
    # the default range: too few measured days for a two-day lane after the lead week, so the lead is dropped
    assert ladder.default_range(df, ["P1"]) == ("2026-02-08", "2026-02-12")
    assert ladder.default_range(df, ["P0"]) == ("2026-02-08", "2026-02-13")
    with pytest.raises(ValueError, match="no full local day"):
        ladder.default_range(df[df["plant"] == "modelled"])


def test_default_range_on_the_reference_frame(df):
    """On the reference frame the rule lands on the documented range: 15
    February (the new plant's first full day plus a week) to 6 September (P2
    needs D+2 inside the frame, whose last full day is 8 September)."""
    assert ladder.default_range(df) == ("2026-02-15", "2026-09-06")
    assert ladder.blocks_for(df) == {"modelled plant": (None, date(2026, 2, 7)), "measured plant": (date(2026, 2, 15), None)}


def test_main_derives_the_range_from_the_frame(tmp_path, monkeypatch):
    """main() runs the lanes on the measured plant only, on the range the
    frame allows, unless told otherwise."""
    import backtest.frames as fr
    seen = {}
    monkeypatch.setattr(ladder, "run", lambda df, start, end, *a, **k: seen.update(start=start, end=end, payout=k.get("payout")))
    monkeypatch.setattr(fr, "load", lambda path: _two_plant_frame())
    ladder.main(["--lanes", "P1", "--frames", "x.csv", "--out", str(tmp_path / "o.csv")])
    assert seen == {"start": "2026-02-08", "end": "2026-02-12", "payout": {}}
    ladder.main(["--lanes", "P1", "--frames", "x.csv", "--out", str(tmp_path / "o.csv"), "--start", "2026-02-09"])
    assert seen["start"] == "2026-02-09" and seen["end"] == "2026-02-12"
