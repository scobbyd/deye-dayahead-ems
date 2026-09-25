"""metrics: one row per lane per day from the settled slices."""
import pandas as pd

from backtest import metrics as mx


def _day():
    rows = []
    for k in range(96):
        buy, sell = (0.10, 0.05) if k < 48 else (0.30, 0.25)
        pv = 4000.0 if 36 <= k < 60 else 0.0
        load = 1000.0
        if 8 <= k < 16:            # night: charge 4 kW from the grid
            batt, grid = -4000.0, 5000.0
        elif 72 <= k < 80:         # evening: discharge 4 kW, 1 kW to the load, 3 kW exported
            batt, grid = 4000.0, -3000.0
        else:
            batt, grid = 0.0, load - pv
        soc = 50.0 + (k - 8) * 2 if 8 <= k < 16 else (66.0 if 16 <= k < 72 else (66.0 - (k - 71) * 2 if 72 <= k < 80 else (50.0 if k < 8 else 50.0)))
        rows.append({"date": "2026-03-10", "ts_utc": pd.Timestamp("2026-03-09 23:00Z") + pd.Timedelta(minutes=15 * k),
                     "p_batt_w": batt, "p_grid_w": grid, "soc_pct": soc, "p_pv_w": pv, "pv_curtail_w": 0.0,
                     "p_load_w": load, "buy": buy, "sell": sell, "soc_start_pct": 50.0, "clamp_writes": 3})
    return pd.DataFrame(rows)


def test_day_metrics_splits_charge_and_discharge_by_where_the_energy_went():
    lad = {"actual_cash": -2.0, "actual_loss": 0.1, "actual_eur": -1.9, "actual_status": "ok",
           "hindsight_eur": -2.1, "omni2_eur": -2.2}
    bal = {"supplier_net_eur": 3.0, "da_eur": -1.0}
    r = mx.day_metrics(_day(), lad, bal, (47, 1), "foresight")
    assert r["batt_in_kwh"] == 8.0 and r["batt_out_kwh"] == 8.0
    assert r["grid_to_batt_kwh"] == 8.0                    # all of the charge came off the meter
    assert r["charge_cost_per_kwh"] == 0.10                # night buy price
    # 2 kWh fed the load at 0,30, 6 kWh left at 0,25
    assert abs(r["discharge_value_eur"] - (2 * 0.30 + 6 * 0.25)) < 1e-6
    assert abs(r["captured_spread"] - (r["discharge_value_per_kwh"] - 0.10)) < 1e-9
    assert r["supplier_net_eur"] == -3.0 and r["supplier_da_eur"] == -1.0
    assert r["solves_ok"] == 47 and r["solves_failed"] == 1
    assert r["rung_omni2_eur"] == -2.2 and r["batt_gain_eur"] == round(r["nobatt_cash_eur"] + 2.0, 4)
    assert r["h_above_90"] == 0.0 and r["h_below_15"] == 0.0 and r["clamp_writes"] == 3


def test_monthly_weights_per_kwh_by_throughput():
    lad = {"actual_cash": -2.0, "actual_loss": 0.1, "actual_eur": -1.9, "actual_status": "ok"}
    a = mx.day_metrics(_day(), lad, None, None, "foresight")
    b = dict(a, date="2026-03-11")
    m = mx.monthly(pd.DataFrame([a, b], columns=mx.COLUMNS))
    row = m.loc[("foresight", "2026-03")]
    assert row["days"] == 2 and row["batt_in_kwh"] == 16.0
    assert row["charge_cost_per_kwh"] == 0.10


def test_solves_by_day_counts_a_retried_tick_once(tmp_path):
    log = ("2026-09-13T13:50:49+00:00 2026-03-26T02:13:00+01:00 src=settled ok=False \n"
           "2026-09-13T13:50:50+00:00 2026-03-26T02:43:00+01:00 src=settled ok=True \n"
           "2026-09-13T22:35:21+00:00 2026-03-26T02:13:00+01:00 src=settled ok=False \n")
    (tmp_path / "walk.log").write_text(log)
    assert mx.solves_by_day(str(tmp_path)) == {"2026-03-26": (1, 1)}


def test_without_a_supplier_the_columns_are_empty(tmp_path):
    lad = {"actual_cash": -2.0, "actual_loss": 0.1, "actual_eur": -1.9, "actual_status": "ok"}
    r = mx.day_metrics(_day(), lad, None, None, "foresight")
    assert r["supplier_net_eur"] is None and r["supplier_da_eur"] is None
    assert mx.supplier_rows(None) == {} and mx.supplier_rows(str(tmp_path / "missing.csv")) == {}
    p = tmp_path / "daily.csv"
    p.write_text("day,net_eur,da_eur\n2026-03-10,3.0,-1.0\n2026-03-11,2.5,\n")
    rows = mx.supplier_rows(str(p))
    assert rows["2026-03-10"] == {"supplier_net_eur": 3.0, "da_eur": -1.0}
    assert rows["2026-03-11"] == {"supplier_net_eur": 2.5, "da_eur": None}
