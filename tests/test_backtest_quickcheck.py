"""quickcheck: the tables from ladder rows, the lane skip, the install hint."""
import os
import pandas as pd, pytest

from backtest import quickcheck as qc
from backtest import ladder


def _rows():
    def r(day, lane, eur, nobatt, dis, cycles):
        return {"day": day, "lane": lane, "status": "ok", "eur": eur, "nobatt_eur": nobatt, "discharged_kwh": dis,
                "cycles": cycles, "pv_kwh": 30.0, "curtailed_kwh": 0.0, "load_kwh": 12.0}
    return pd.DataFrame([r("2026-06-10", "P1", -8.0, -4.0, 40.0, 0.9), r("2026-06-11", "P1", -6.0, -3.0, 30.0, 0.7),
                         r("2026-07-01", "P1", -9.0, -5.0, 50.0, 1.0),
                         r("2026-06-10", "F_da", -7.0, -4.0, 38.0, 0.8),
                         {"day": "2026-06-11", "lane": "F_da", "status": "no_data"}])


def test_eu_formatting():
    assert qc._eu(1096.851) == "1.096,85" and qc._eu(-47.178) == "-47,18" and qc._eu(0.15, 3) == "0,150"
    assert qc._eu(float("nan")) == "" and qc._eu(None) == ""


def test_yearly_and_monthly_tables_flip_the_sign_and_divide():
    side = qc.ladder_to_sidecar(_rows())
    assert len(side) == 4 and set(side["lane"]) == {"P1", "F_da"}
    y = qc.yearly_table(side, 48.2)
    assert y.loc["P1", "days"] == 3 and y.loc["P1", "earned_eur"] == pytest.approx(23.0)
    assert y.loc["P1", "nobatt_earned_eur"] == pytest.approx(12.0)
    assert y.loc["P1", "batt_gain_eur"] == pytest.approx(11.0)          # nobatt - eur, positive = the pack lowered money out
    assert y.loc["P1", "batt_out_kwh"] == 120.0 and y.loc["P1", "eur_per_kwh"] == pytest.approx(11.0 / 120.0)
    assert y.loc["F_da", "days"] == 1 and y.loc["P1", "capacity_kwh"] == 48.2
    m = qc.monthly_table(side, 48.2)
    p1 = m[m["lane"] == "P1"].set_index("month")
    assert list(p1.index) == ["2026-06", "2026-07"] and p1.loc["2026-06", "batt_gain_eur"] == pytest.approx(7.0)
    txt = qc.format_table(y, qc.YEAR_COLS)
    assert "23,00" in txt and "cash batt EUR" in txt and "0,092" in txt


def test_main_skips_missing_lanes_and_refuses_a_bad_frame(tmp_path, capsys, monkeypatch):
    pytest.importorskip("emhass")
    from test_backtest_frame_schema import synthetic_frame
    p = tmp_path / "frame.csv"
    synthetic_frame(days=2).to_csv(p)
    # two days: a two-day lane has no day with D+1 inside the frame, and F_da has no columns; both are said out loud
    rc = qc.main(["--frame", str(p), "--lanes", "P2,F_da", "--out", str(tmp_path / "run")])
    out = capsys.readouterr()
    assert rc == 2 and "skip F_da" in out.out and "fc_pv_om24_main_w" in out.out
    assert "too few" in out.err
    bad = tmp_path / "bad.csv"
    synthetic_frame(days=2).drop(columns=["soc_pct"]).to_csv(bad)
    assert qc.main(["--frame", str(bad), "--out", str(tmp_path / "run")]) == 2
    assert "soc_pct" in capsys.readouterr().err
    assert qc.main(["--frame", str(p), "--lanes", "P9", "--out", str(tmp_path / "run")]) == 2


def test_main_runs_the_ladder_on_a_stub_solver(tmp_path, capsys, monkeypatch):
    """The sweep loop end to end with the golden stub in place of the library:
    two capacities, one lane, one settled day each, both tables printed."""
    pytest.importorskip("emhass")
    from golden import fixtures as F
    from test_backtest_frame_schema import synthetic_frame
    from backtest import hardware
    df = synthetic_frame(days=12)
    p = tmp_path / "frame.csv"
    df.to_csv(p)

    class Stub(F.StubSolver):
        params = {"plant_conf": {}}

        def __init__(self, data_dir=None):
            super().__init__(F.local(2026, 6, 17, 0, 0))

    monkeypatch.setattr("backtest.solver.LibrarySolver", Stub)
    rc = qc.main(["--frame", str(p), "--lanes", "P0", "--capacity", "24.1,48.2", "--start", "2026-06-17",
                  "--end", "2026-06-17", "--out", str(tmp_path / "run")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "### 24,1 kWh pack" in out.replace("24.1", "24,1") and "## yearly" in out and "## monthly" in out
    assert os.path.exists(tmp_path / "run" / "ladder_c24.1_i12.csv") and os.path.exists(tmp_path / "run" / "ladder_plant.csv")
    y = pd.read_csv(tmp_path / "run" / "yearly.csv")
    assert sorted(y["capacity_kwh"]) == [24.1, 48.2] and (y["days"] == 1).all()
    assert hardware.is_default({"capacity_kwh": ladder.CAPACITY_KWH, "inverter_kw": ladder.P_NOM_INV_KW})
