"""The ladder's hourly cash sidecar and the earned series it feeds to the recorder."""
import csv
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from emhasscore import ladder, scoring

TZ = "Europe/Amsterdam"


def _iso(y, m, d, hh):
    return datetime(y, m, d, hh, 0, tzinfo=ZoneInfo(TZ)).isoformat()


def test_hour_sums_normal_day_buckets_four_steps_per_hour():
    steps = [0.01] * 96
    rows = ladder.hour_sums(steps, date(2026, 9, 10), TZ)
    assert len(rows) == 24
    assert rows[0][0] == _iso(2026, 9, 10, 0)
    assert rows[-1][0] == _iso(2026, 9, 10, 23)
    assert all(abs(v - 0.04) < 1e-9 for _, v in rows)


@pytest.mark.parametrize("day,n,hours", [(date(2026, 10, 25), 100, 25), (date(2026, 3, 29), 92, 23)])
def test_hour_sums_follows_dst_days(day, n, hours):
    rows = ladder.hour_sums([1.0] * n, day, TZ)
    assert len(rows) == hours
    assert len({k for k, _ in rows}) == hours          # no two buckets share a start
    assert abs(sum(v for _, v in rows) - n) < 1e-9     # nothing lost at the seam


def test_hour_sums_rejects_the_wrong_step_count():
    with pytest.raises(ValueError):
        ladder.hour_sums([1.0] * 95, date(2026, 9, 10), TZ)


def test_hours_sidecar_round_trip_replaces_one_day(tmp_path):
    p = str(tmp_path / "ladder.csv")
    hp = ladder.hours_path(p)
    assert hp.endswith("ladder_hours.csv")
    d1 = ladder.hour_sums([0.01] * 96, date(2026, 9, 10), TZ)
    d2 = ladder.hour_sums([0.02] * 96, date(2026, 9, 11), TZ)
    ladder.upsert_ladder_hours(hp, "2026-09-10", d1)
    ladder.upsert_ladder_hours(hp, "2026-09-11", d2)
    assert len(ladder.read_ladder_hours(hp)) == 48
    ladder.upsert_ladder_hours(hp, "2026-09-10", ladder.hour_sums([0.03] * 96, date(2026, 9, 10), TZ))
    rows = ladder.read_ladder_hours(hp)
    assert len(rows) == 48
    assert rows[0][0] == _iso(2026, 9, 10, 0) and abs(rows[0][1] - 0.12) < 1e-9
    assert rows[24][0] == _iso(2026, 9, 11, 0) and abs(rows[24][1] - 0.08) < 1e-9
    assert rows == sorted(rows)


def _ladder_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ladder.LADDER_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ladder.LADDER_COLUMNS})


def test_earned_series_merges_days_and_hours_in_the_earned_sign(tmp_path):
    p = str(tmp_path / "ladder.csv")
    _ladder_csv(p, [
        {"date": "2026-09-01", "actual_eur": -1.0, "actual_status": "ok", "flags": "seed"},
        {"date": "2026-09-02", "actual_eur": -2.5, "actual_status": "ok"},
        {"date": "2026-09-03", "actual_eur": "", "actual_status": "no_actuals"},
        {"date": "2026-09-04", "actual_eur": -0.96, "actual_status": "ok"},
    ])
    ladder.upsert_ladder_hours(ladder.hours_path(p), "2026-09-04",
                               ladder.hour_sums([-0.01] * 96, date(2026, 9, 4), TZ))
    s = ladder.ladder_earned_series(p)
    assert s[0] == (_iso(2026, 9, 2, 0), 2.5)          # the daily row, negated once
    assert len(s) == 1 + 24                             # seed and no_actuals skipped
    assert s[1] == (_iso(2026, 9, 4, 0), 0.04)          # the hourly rows win for their day
    assert [k for k, _ in s] == sorted(k for k, _ in s)


def test_replay_day_returns_the_per_step_lane():
    n = 4
    compact = {"n": n, "buy": [0.2] * n, "sell": [0.1] * n, "p_load_w": [1000.0] * n, "p_pv_w": [0.0] * n,
               "p_grid_w": [1000.0] * n, "p_batt_w": [0.0] * n, "settled": True,
               "soc_start_pct": 50.0, "soc_pct": [50.0] * n}
    rep = scoring.replay_day(compact, [1000.0] * n, [0.0] * n, [0.0] * n, 49.9, 0.05)
    assert rep["status"] == "ok"
    assert len(rep["step_eur"]) == n
    assert abs(sum(rep["step_eur"]) - (rep["cash"] + rep["loss"])) < 1e-6


def test_earned_series_reconciles_hours_to_the_days_value(tmp_path):
    p = str(tmp_path / "ladder.csv")
    _ladder_csv(p, [{"date": "2026-09-06", "actual_eur": -1.2, "actual_status": "ok"}])
    steps = [-0.02] * 48 + [0.01] * 48                  # replayed day sums to -0.48; the row says -1.20
    ladder.upsert_ladder_hours(ladder.hours_path(p), "2026-09-06", ladder.hour_sums(steps, date(2026, 9, 6), TZ))
    s = ladder.ladder_earned_series(p)
    assert len(s) == 24
    assert abs(sum(v for _, v in s) - 1.2) < 1e-6          # the period total is the ladder's number
    assert all(v > 0 for _, v in s[:12]) and all(v < 0 for _, v in s[12:])   # residual spread by weight keeps the shape


def _fake_ex(n=96):
    # charge 2 kW all day, import 1 kW, SOC climbing 0..96, PV 3 kW with 1 kW declined, load 0,5 kW
    return {"p_batt_w": [-2000.0] * n, "p_grid_w": [1000.0] * n, "soc_pct": [float(i) for i in range(n)],
            "p_pv_w": [3000.0] * n, "pv_curtail_w": [1000.0] * n, "p_load_w": [500.0] * n,
            "buy": [0.25] * n, "sell": [0.10] * n}


def test_hour_lanes_are_hour_means_of_the_replay():
    lanes = ladder.hour_lanes(_fake_ex(), date(2026, 9, 10), TZ)
    assert len(lanes) == 24
    k, v = lanes[0]
    assert k == _iso(2026, 9, 10, 0)
    assert v["charge_kw"] == 2.0 and v["discharge_kw"] == 0.0
    assert v["import_kw"] == 1.0 and v["export_kw"] == 0.0
    assert v["pv_kw"] == 2.0 and v["load_kw"] == 0.5
    assert abs(v["soc_pct"] - 1.5) < 1e-9                    # steps 0,1,2,3
    assert v["buy_ct"] == 25.0 and v["sell_ct"] == 10.0


def test_hours_sidecar_carries_the_lanes_and_reads_back(tmp_path):
    p = str(tmp_path / "ladder.csv")
    hp = ladder.hours_path(p)
    ladder.upsert_ladder_hours(hp, "2026-09-10", ladder.hour_sums([0.01] * 96, date(2026, 9, 10), TZ),
                               ladder.hour_lanes(_fake_ex(), date(2026, 9, 10), TZ))
    rows = ladder.read_ladder_hour_rows(hp)
    assert len(rows) == 24 and rows[0]["charge_kw"] == 2.0 and abs(rows[0]["eur"] - 0.04) < 1e-9
    assert ladder.read_ladder_hours(hp)[0] == (_iso(2026, 9, 10, 0), 0.04)   # the tuple reader still works
    # a day written without lanes reads back with None lanes, not a crash
    ladder.upsert_ladder_hours(hp, "2026-09-11", ladder.hour_sums([0.02] * 96, date(2026, 9, 11), TZ))
    rows = ladder.read_ladder_hour_rows(hp)
    assert len(rows) == 48 and rows[-1]["charge_kw"] is None


def test_shadow_series_merges_the_tariff_sidecar_under_the_ladder_rows(tmp_path):
    p = str(tmp_path / "ladder.csv")
    ladder.upsert_ladder_hours(ladder.hours_path(p), "2026-09-10", ladder.hour_sums([0.0] * 96, date(2026, 9, 10), TZ),
                               ladder.hour_lanes(_fake_ex(), date(2026, 9, 10), TZ))
    tp = ladder.tariff_path(p)
    ladder.upsert_tariff_hours(tp, "2026-09-09", [(_iso(2026, 9, 9, h), 30.0, 12.0) for h in range(24)])
    ladder.upsert_tariff_hours(tp, "2026-09-10", [(_iso(2026, 9, 10, h), 99.0, 99.0) for h in range(24)])
    s = ladder.ladder_shadow_series(p)
    assert len(s["charge_kw"]) == 24 and s["charge_kw"][0] == (_iso(2026, 9, 10, 0), 2.0)
    assert len(s["buy_ct"]) == 48
    assert s["buy_ct"][0] == (_iso(2026, 9, 9, 0), 30.0)        # the tariff sidecar fills the deep past
    assert s["buy_ct"][24] == (_iso(2026, 9, 10, 0), 25.0)      # the ladder's own row wins on its day


def test_today_hours_stop_at_the_settled_steps():
    ex = _fake_ex()
    ex["cost_eur"] = [-0.01] * 96
    ex["n_past"] = 10                                     # 00:00 .. 02:30 settled
    hours, lanes = ladder.today_hours(ex, date(2026, 9, 10), TZ)
    assert [k for k, _ in hours] == [_iso(2026, 9, 10, h) for h in (0, 1, 2)]
    assert abs(hours[0][1] - (-0.04)) < 1e-9 and abs(hours[2][1] - (-0.02)) < 1e-9   # the running hour is partial
    assert [k for k, _ in lanes] == [k for k, _ in hours]
    assert abs(lanes[2][1]["soc_pct"] - 8.5) < 1e-9        # steps 8 and 9 only


def test_earned_series_carries_a_day_the_ladder_has_not_settled(tmp_path):
    p = str(tmp_path / "ladder.csv")
    _ladder_csv(p, [{"date": "2026-09-09", "actual_eur": -1.0, "actual_status": "ok"}])
    ladder.upsert_ladder_hours(ladder.hours_path(p), "2026-09-10", [(_iso(2026, 9, 10, 0), -0.5), (_iso(2026, 9, 10, 1), 0.25)])
    s = ladder.ladder_earned_series(p)
    assert s == [(_iso(2026, 9, 9, 0), 1.0), (_iso(2026, 9, 10, 0), 0.5), (_iso(2026, 9, 10, 1), -0.25)]
