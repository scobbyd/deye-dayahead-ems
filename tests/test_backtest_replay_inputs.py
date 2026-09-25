"""replay_inputs: the frame becomes the wrapper's HA reads."""
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest import replay_inputs as ri

TZ = "Europe/Amsterdam"
AMS = ZoneInfo(TZ)
UTC = timezone.utc


def synthetic_frame(first=date(2026, 7, 20), days=10):
    """A quarter-hour frame in load_frame's shape. pv is a triangle over the
    local day (0 at midnight, 8.000 W at noon), load = 300 + local slot,
    micro = pv / 10, potential = pv x 1,5 on the two suspect quarters at 12:00
    and 12:15 of every day, soc = 40 + day index."""
    rows = []
    t = datetime.combine(first, datetime.min.time(), tzinfo=AMS)
    end = t + timedelta(days=days)
    while t < end:
        slot = t.hour * 4 + t.minute // 15
        pv = 8000.0 * (1 - abs(slot - 48) / 48)
        d = (t.date() - first).days
        suspect = 1 if slot in (48, 49) else 0
        rows.append({"ts_utc": t.astimezone(UTC), "pv_main_w": pv, "micro_w": pv / 10, "load_w": 300.0 + slot,
                     "pv_pot_main_w": pv * (1.5 if suspect else 1.0), "soc_pct": 40.0 + d, "suspect": suspect,
                     "grid_w": -1000.0, "batt_dc_w": 0.0, "da_eur_kwh": 0.10, "net_result_eur": 0.01,
                     "fc_pv_om24_main_w": pv * 0.9, "fc_pv_om48_main_w": pv * 0.8})
        t += timedelta(minutes=15)
    return pd.DataFrame(rows).set_index("ts_utc")


def test_day_frame_is_the_local_day():
    df = synthetic_frame()
    d = ri.day_frame(df, date(2026, 7, 22))
    assert len(d) == 96
    assert d.index[0] == datetime(2026, 7, 22, 0, tzinfo=AMS).astimezone(UTC)
    assert d.index[-1] == datetime(2026, 7, 22, 23, 45, tzinfo=AMS).astimezone(UTC)


def test_day_frame_rejects_a_short_day():
    df = synthetic_frame(days=2)
    with pytest.raises(ValueError):
        ri.day_frame(df, date(2026, 7, 25))


def test_actuals_full_day_shape_and_values():
    df = synthetic_frame()
    a = ri.actuals(df, date(2026, 7, 22))
    assert set(a) == {"pv_w", "load_w", "micro_w", "curtailed", "pv_peak_w", "soc_pct"}
    assert all(len(v) == 96 for v in a.values())
    assert a["load_w"][10] == 310.0
    assert a["pv_w"][48] == pytest.approx(8000.0 + 800.0)          # main + micro
    assert a["micro_w"][48] == pytest.approx(800.0)
    assert a["curtailed"][48] == 1.0 and a["curtailed"][47] == 0.0
    assert a["pv_peak_w"][48] == pytest.approx(8000.0 * 1.5 + 800.0)   # potential + micro
    assert a["pv_peak_w"][47] == a["pv_w"][47]


def test_actuals_with_gap_upto_holds_the_last_value():
    df = synthetic_frame()
    a = ri.actuals(df, date(2026, 7, 22), gap_upto=40)          # the wrapper's _actuals_15min(today, slot)
    assert len(a["load_w"]) == 96
    assert a["load_w"][40] == 340.0                             # the current quarter is known
    assert a["load_w"][41] == 340.0 and a["load_w"][95] == 340.0   # future steps hold


def test_load_days_seven_complete_days_before_today():
    df = synthetic_frame()
    ld = ri.load_days(df, date(2026, 7, 29))
    assert sorted(ld) == [date(2026, 7, 22) + timedelta(days=k) for k in range(7)]
    assert all(len(v) == 96 for v in ld.values())
    assert ld[date(2026, 7, 22)][5] == 305.0


def test_load_now_and_soc_at_the_tick():
    df = synthetic_frame()
    tick = datetime(2026, 7, 22, 10, 13, tzinfo=AMS)
    assert ri.load_now_w(df, tick) == 340.0                     # slot 40 = 10:00 local
    assert ri.soc_at(df, tick) == 42.0


def constant_fraction(value=0.4):
    idx = pd.date_range("2026-07-19", "2026-08-01", freq="h", tz="UTC")
    return {"_previous_day1": pd.Series(value, index=idx), "_previous_day2": pd.Series(value, index=idx)}


def test_pv_rows_are_half_hour_means_in_kw():
    df = synthetic_frame()
    total, ese = ri.pv_rows(df, constant_fraction(0.0), date(2026, 7, 22), 0, share=0.288)
    assert len(total) == 48 and len(ese) == 48
    assert total[0]["period_start"] == datetime(2026, 7, 22, 0, tzinfo=AMS).astimezone(UTC).isoformat()
    d = ri.day_frame(df, date(2026, 7, 22))
    want = float(d["fc_pv_om24_main_w"].iloc[48:50].mean()) / 1000.0     # 12:00 and 12:15 local
    assert total[24]["pv_estimate"] == pytest.approx(want, abs=1e-4)      # fraction 0: no Growatt, total = main
    assert total[24]["pv_estimate10"] == total[24]["pv_estimate"]
    assert ese[24]["pv_estimate"] == 0.0


def test_pv_rows_lane_by_horizon_offset():
    df = synthetic_frame()
    t0, _ = ri.pv_rows(df, constant_fraction(0.0), date(2026, 7, 23), 0, share=0.288)
    t2, _ = ri.pv_rows(df, constant_fraction(0.0), date(2026, 7, 23), 2, share=0.288)
    assert t2[24]["pv_estimate"] == pytest.approx(t0[24]["pv_estimate"] * 0.8 / 0.9, rel=1e-3)   # om48 vs om24


def test_pv_rows_split_invariant():
    """pv_split(total, ese, share) must give micro = ese_string x share/(1-share)
    and main = the frame lane, so main + micro = total."""
    from emhasscore.series import pv_split
    df = synthetic_frame()
    share = 0.288
    total, ese = ri.pv_rows(df, constant_fraction(0.4), date(2026, 7, 22), 0, share=share)
    tot_w = [r["pv_estimate"] * 1000 for r in total]
    ese_w = [r["pv_estimate"] * 1000 for r in ese]
    main, micro = pv_split(tot_w, ese_w, share)
    d = ri.day_frame(df, date(2026, 7, 22))
    lane = float(d["fc_pv_om24_main_w"].iloc[48:50].mean())
    assert main[24] == pytest.approx(lane, abs=0.2)
    assert micro[24] == pytest.approx(lane * 0.4 * share / (1 - share), abs=0.2)


def test_solcast_inputs_keys_and_ese_span():
    df = synthetic_frame()
    s = ri.solcast_inputs(df, constant_fraction(0.4), date(2026, 7, 22), share=0.288)
    assert set(s) == {"solcast_today", "solcast_tomorrow", "solcast_day3", "solcast_ese_today"}
    assert len(s["solcast_today"]) == 48 and len(s["solcast_day3"]) == 48
    assert len(s["solcast_ese_today"]) == 144                     # D, D+1, D+2 in one list
    assert s["solcast_ese_today"][48]["period_start"] == s["solcast_tomorrow"][0]["period_start"]


def test_ticks_schedule():
    t = ri.ticks(date(2026, 7, 23), date(2026, 7, 23))
    assert t[0] == datetime(2026, 7, 22, 23, 43, tzinfo=AMS)
    assert t[-1] == datetime(2026, 7, 24, 0, 13, tzinfo=AMS)
    at13 = [x for x in t if x.hour == 13 and x.minute == 0]
    assert at13 == [datetime(2026, 7, 23, 13, 0, 4, tzinfo=AMS)]
    assert len(t) == 1 + 48 + 1 + 1                              # 23:43, 48 half-hours on the 23rd, 13:00:04, 00:13


def test_live_knobs_falls_back_to_defaults(tmp_path):
    from emhasscore.objective import knobs
    assert ri.live_knobs(str(tmp_path)) == knobs({})


def test_ese_fraction_from_files():
    pytest.importorskip("pvlib")
    if not os.path.exists(ri.OM_PREV_CSV):
        pytest.skip(f"no Open-Meteo cache at {ri.OM_PREV_CSV}")
    frac = ri.ese_fraction()
    s = frac["_previous_day1"]
    noon = s[pd.Timestamp("2026-07-23 11:00:00+00:00")]
    assert 0.2 < noon < 0.8
    assert s[pd.Timestamp("2026-07-23 01:00:00+00:00")] == 0.5      # night: no array output, neutral share


def test_ticks_skip_the_missing_dst_hour_and_never_repeat_an_instant():
    """29 March 2026: the clock jumps 02:00 -> 03:00. No tick at 02:13 or 02:43,
    every UTC instant once, 13:00:04 still present, 92 quarters of a day."""
    t = ri.ticks(date(2026, 3, 29), date(2026, 3, 29))
    walls = [(x.hour, x.minute) for x in t if x.date() == date(2026, 3, 29)]
    assert (2, 13) not in walls and (2, 43) not in walls
    assert (1, 43) in walls and (3, 13) in walls and (13, 0) in walls
    instants = [x.astimezone(UTC) for x in t]
    assert len(instants) == len(set(instants))
    assert len(t) == 1 + 46 + 1 + 1                              # 23:43, 46 half-hours on the short day, 13:00:04, 00:13
    assert all(x.utcoffset().total_seconds() == (3600 if x.hour < 2 and x.date() == date(2026, 3, 29) or x.date() < date(2026, 3, 29) else 7200) for x in t)


def test_load_frame_reads_a_pv_hole_as_dark(tmp_path):
    """A quarter with no PV record at all is read as dark rather than aborting
    a walk that crosses it (the reference frame has a 16-hour hole on
    2026-02-15, 64 quarters); the load is present."""
    df = synthetic_frame(days=3)
    df.loc[df.index[:64], ["pv_main_w", "pv_pot_main_w"]] = float("nan")
    p = tmp_path / "frame.csv"
    df.to_csv(p)
    got = ri.load_frame(str(p))
    assert got.attrs.get("pv_hole_quarters") == 64
    d = ri.day_frame(got, date(2026, 7, 20))                     # no longer raises
    assert d["pv_main_w"].iloc[:64].sum() == 0.0 and d["pv_main_w"].notna().all() and d["load_w"].notna().all()
    assert "net_result_eur" in got.columns                        # the optional supplier column rides along when present


def test_load_frame_without_the_supplier_column(tmp_path):
    df = synthetic_frame(days=2).drop(columns=["net_result_eur"])
    p = tmp_path / "frame.csv"
    df.to_csv(p)
    got = ri.load_frame(str(p))
    assert "net_result_eur" not in got.columns and list(got.columns[:7]) == ri.FRAME_COLS


def test_soc_at_refuses_a_missing_seed():
    df = synthetic_frame()
    df.loc[df.index[:8], "soc_pct"] = float("nan")
    with pytest.raises(ValueError):
        ri.soc_at(df, datetime(2026, 7, 20, 0, 13, tzinfo=AMS))


def test_pv_rows_exact_take_the_potential_and_the_real_growatt():
    from emhasscore.series import pv_split
    df = synthetic_frame()
    share = 0.288
    total, ese = ri.pv_rows(df, constant_fraction(0.4), date(2026, 7, 22), 0, share=share, exact=True)
    d = ri.day_frame(df, date(2026, 7, 22))
    pot = float(d["pv_pot_main_w"].iloc[48:50].mean()); mic = float(d["micro_w"].iloc[48:50].mean())
    assert total[24]["pv_estimate"] * 1000 == pytest.approx(pot + mic, abs=0.5)
    main, micro = pv_split([r["pv_estimate"] * 1000 for r in total], [r["pv_estimate"] * 1000 for r in ese], share)
    assert main[24] == pytest.approx(pot, abs=0.5) and micro[24] == pytest.approx(mic, abs=0.5)
    s = ri.solcast_inputs(df, constant_fraction(0.4), date(2026, 7, 22), share, exact_days=1)
    lane = float(d["fc_pv_om24_main_w"].iloc[48:50].mean())
    assert s["solcast_today"][24]["pv_estimate"] * 1000 == pytest.approx(pot + mic, abs=0.5)      # exact today
    assert s["solcast_tomorrow"][24]["pv_estimate"] * 1000 != pytest.approx(pot + mic, abs=0.5)   # forecast tomorrow
    assert set(ri.KNOWLEDGE) == {"actual", "hindsight", "hindsight_pv", "hindsight_load", "omni2"}


def test_load_exact_w_keys_the_box_by_utc_instant():
    from emhasscore.planning import load_exact_key
    df = synthetic_frame()
    ex = ri.load_exact_w(df, date(2026, 7, 22), 3)
    assert len(ex) == 3 * 96
    d = ri.day_frame(df, date(2026, 7, 23))
    t = d.index[42].to_pydatetime()
    assert ex[load_exact_key(t)] == round(float(d["load_w"].iloc[42]), 1)
    assert load_exact_key(t) == "2026-07-23T08:30Z"                   # 10:30 local
    assert ri.KNOWLEDGE["omni2"]["load_exact_days"] == 3 and ri.KNOWLEDGE["hindsight"]["load_exact_days"] == 0

