"""Native EMHASS core: pure functions first, then the HTTP/archive/scoring
layers against an in-process stub of the add-on."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import emhass_core as core

AMS = "Europe/Amsterdam"
Z = ZoneInfo(AMS)


def local(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=Z)


# ---- horizon -----------------------------------------------------------------

def test_horizon_normal_day_from_14_00_04():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    assert t0 == local(2026, 9, 1, 14, 15)
    assert n == 39 + 96          # 14:15..24:00 today is 39 steps, tomorrow 96


def test_horizon_on_a_boundary_keeps_the_boundary():
    t0, n = core.horizon(local(2026, 9, 1, 13, 0, 0), AMS)
    assert t0 == local(2026, 9, 1, 13, 0)
    assert n == 44 + 96


def test_horizon_late_evening_rolls_into_tomorrow():
    t0, n = core.horizon(local(2026, 9, 1, 23, 50), AMS)
    assert t0 == local(2026, 9, 2, 0, 0)
    assert n == 96


def test_horizon_before_autumn_change_tomorrow_has_100_steps():
    # 2026-10-25 is the EU autumn change (25 h day)
    _, n = core.horizon(local(2026, 10, 24, 14, 0, 4), AMS)
    assert n == 39 + 100


def test_horizon_before_spring_change_tomorrow_has_92_steps():
    # 2026-03-29 is the EU spring change (23 h day)
    _, n = core.horizon(local(2026, 3, 28, 14, 0, 4), AMS)
    assert n == 39 + 92


def test_horizon_days_ahead_two_adds_the_day_after():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS, days_ahead=2)
    assert t0 == local(2026, 9, 1, 14, 15)
    assert n == 39 + 96 + 96


def test_run_plan_extends_to_day3_when_solcast_covers_it(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS, days_ahead=2)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url)
    inp["solcast_day3"] = solcast(datetime.combine(now.date() + timedelta(days=2),
                                                   datetime.min.time(), tzinfo=Z), 3.0)
    res = core.run_plan(inp)
    assert res["ok"], res
    assert res["n"] == n and res["pv_gap_steps"] == 0
    assert res["n_predicted_steps"] == 96          # the extra day has no Nord Pool rows
    assert stub.posts[0][1]["prediction_horizon"] == n


def test_expected_steps():
    assert core.expected_steps(date(2026, 9, 1), AMS) == 96
    assert core.expected_steps(date(2026, 10, 25), AMS) == 100
    assert core.expected_steps(date(2026, 3, 29), AMS) == 92


def test_step_times_are_absolute_15_minutes_apart_across_dst():
    t0, n = core.horizon(local(2026, 10, 24, 14, 0, 4), AMS)
    ts = core.step_times(t0, n)
    assert len(ts) == n
    # timestamps, not (b - a): two aware datetimes sharing one ZoneInfo subtract
    # as wall-clock time and would show a -45 min "gap" across the autumn fold
    gaps = {round(b.timestamp() - a.timestamp()) for a, b in zip(ts, ts[1:])}
    assert gaps == {900}
    assert ts[-1] == local(2026, 10, 25, 23, 45)


# ---- prices ------------------------------------------------------------------

def np_rows(day_local, price=100.0):
    """96 Nord Pool rows for a normal local day: {start UTC ISO, end, price EUR/MWh}."""
    rows, start = [], day_local.astimezone(timezone.utc)
    for k in range(96):
        s = start + timedelta(minutes=15 * k)
        rows.append({"start": s.isoformat(), "end": (s + timedelta(minutes=15)).isoformat(),
                     "price": price + k})
    return rows


def test_price_series_full_nordpool_no_prediction():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    da, npred = core.price_series(t0, n, np_rows(local(2026, 9, 1, 0, 0)),
                                  np_rows(local(2026, 9, 2, 0, 0), 200.0), [])
    assert len(da) == n and npred == 0
    assert da[0] == pytest.approx((100 + 57) / 1000)     # 14:15 is step 57 of today
    assert da[39] == pytest.approx(200 / 1000)            # first step of tomorrow


def test_price_series_missing_tomorrow_uses_epex_and_counts():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    epex = [{"datetime": t.astimezone(timezone.utc).isoformat(), "value": 0.05}
            for t in core.step_times(t0, n)]
    da, npred = core.price_series(t0, n, np_rows(local(2026, 9, 1, 0, 0)), [], epex)
    assert npred == 96
    assert da[0] == pytest.approx(0.157)
    assert da[39] == pytest.approx(0.05)


def test_price_series_without_nordpool_is_fully_predicted():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    epex = [{"datetime": t.astimezone(timezone.utc).isoformat(), "value": 0.07}
            for t in core.step_times(t0, n)]
    da, npred = core.price_series(t0, n, [], [], epex)
    assert npred == n and set(da) == {0.07}


def test_price_series_holds_last_value_over_a_late_gap_but_not_the_first_step():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    epex = [{"datetime": t.astimezone(timezone.utc).isoformat(), "value": 0.05}
            for t in core.step_times(t0, n)][:-4]          # last hour missing everywhere
    da, npred = core.price_series(t0, n, np_rows(local(2026, 9, 1, 0, 0)), [], epex)
    assert da[-4:] == [0.05] * 4 and npred == 96
    with pytest.raises(ValueError):
        core.price_series(t0, n, [], [], [])


def test_tariff_2027_wedge():
    buy, sell = core.tariff([0.10, -0.01], 0.11, 0.02, 21, 0.0)
    assert buy == [pytest.approx(0.2783), pytest.approx(0.1452)]
    assert sell == [pytest.approx(0.121), pytest.approx(-0.0121)]


# ---- pv ----------------------------------------------------------------------

def solcast(day_local, kw=1.0):
    """48 half-hourly Solcast rows, period_start local ISO, pv_estimate kW."""
    return [{"period_start": (day_local + timedelta(minutes=30 * k)).isoformat(),
             "pv_estimate": kw + k * 0.01} for k in range(48)]


def test_pv_series_step_hold_and_gap_count():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    today, tomorrow = solcast(local(2026, 9, 1, 0, 0)), solcast(local(2026, 9, 2, 0, 0), 2.0)
    del tomorrow[10]                                   # 05:00-05:30 tomorrow missing
    pv, gaps = core.pv_series(t0, n, today, tomorrow)
    assert len(pv) == n and gaps == 2
    assert pv[0] == pytest.approx((1.0 + 28 * 0.01) * 1000)   # 14:15 lies in half-hour 28
    assert pv[1] == pv[2]                                      # 14:30 and 14:45 share half-hour 29
    assert pv[39 + 20] == 0.0 and pv[39 + 21] == 0.0          # the two missing steps


# ---- PV split: the Growatt is must-take ----------------------------------------

def test_pv_split_carves_the_growatt_out_of_the_ese_site():
    total = [10000.0, 6000.0, 0.0]
    ese = [6000.0, 4000.0, 0.0]
    main, micro = core.pv_split(total, ese, 0.288)
    assert micro == [1728.0, 1152.0, 0.0]
    assert main == [8272.0, 4848.0, 0.0]
    assert [a + b for a, b in zip(main, micro)] == pytest.approx(total)


def test_pv_split_never_exceeds_the_total_or_goes_negative():
    # ESE series larger than the combined one (stale site fetch): the Growatt
    # can never be more than the whole array, and main never goes negative.
    main, micro = core.pv_split([500.0], [9000.0], 0.288)
    assert micro == [500.0] and main == [0.0]


def test_pv_split_share_is_clamped_into_the_unit_interval():
    assert core.pv_split([1000.0], [1000.0], -1.0) == ([1000.0], [0.0])
    assert core.pv_split([1000.0], [1000.0], 5.0) == ([0.0], [1000.0])


def test_pv_split_length_mismatch_raises():
    with pytest.raises(ValueError):
        core.pv_split([1.0, 2.0], [1.0], 0.288)


def test_build_payload_shapes_and_length_check():
    t0, n = core.horizon(local(2026, 9, 1, 14, 0, 4), AMS)
    p = core.build_payload(t0, n, 0.8, 0.8, [0.0] * n, [0.3] * n, [0.1] * n)
    assert p["prediction_horizon"] == n and p["optimization_time_step"] == 15
    assert p["soc_init"] == 0.8 and p["soc_final"] == 0.8
    assert len(p["pv_power_forecast"]) == len(p["load_cost_forecast"]) == len(p["prod_price_forecast"]) == n
    with pytest.raises(ValueError):
        core.build_payload(t0, n, 0.8, 0.8, [0.0] * (n - 1), [0.3] * n, [0.1] * n)


def test_step_cost_sign_conventions():
    assert core.step_cost(1000.0, 0.30, 0.10) == pytest.approx(0.075)    # import 1 kW for 15 min at 0,30
    assert core.step_cost(-1000.0, 0.30, 0.10) == pytest.approx(-0.025)  # export earns
    assert core.step_cost(-1000.0, 0.30, -0.02) == pytest.approx(0.005)  # export at a negative price costs


# ---- the Growatt cut -------------------------------------------------------------

def test_aux_cut_decision_enters_at_1_5x_and_2_kwh():
    assert core.aux_cut_decision(10.39, 6.49, False)["active"] is True        # 09-05 at 11:00, ratio 1,60
    assert core.aux_cut_decision(10.39, 7.82, False)["active"] is False       # 10:00, ratio 1,33
    assert core.aux_cut_decision(1.9, 1.0, False)["active"] is False          # ratio 1,9 but under 2 kWh
    assert core.aux_cut_decision(2.0, 1.3, False)["active"] is True           # exactly at both thresholds
    d = core.aux_cut_decision(5.0, 0.0, False)
    assert d["active"] is False and d["ratio"] is None                         # nothing left to cut


def test_aux_cut_decision_hysteresis_leaves_at_1_2x_or_1_kwh_whichever_first():
    assert core.aux_cut_decision(1.3, 1.0, True)["active"] is True            # ratio 1,3: stays cut
    assert core.aux_cut_decision(1.19, 1.0, True)["active"] is False          # ratio under 1,2: re-enabled
    assert core.aux_cut_decision(0.99, 0.5, True)["active"] is False          # ratio 1,98 but under 1 kWh: re-enabled
    assert core.aux_cut_decision(1.3, 1.0, False)["active"] is False          # not cut: 1,3 does not enter
    d = core.aux_cut_decision(3.0, 2.0, True)
    assert d["was_active"] is True and d["curtail_kwh"] == 3.0 and d["aux_kwh"] == 2.0 and d["ratio"] == 1.5
    assert (d["on_ratio"], d["on_min_kwh"], d["off_ratio"], d["off_min_kwh"]) == (1.5, 2.0, 1.2, 1.0)


def test_aux_cut_state_reads_todays_newest_plan_and_resets_at_midnight(tmp_path):
    arch = str(tmp_path / "plans")
    def put(made, active, status="Optimal", replay=False):
        core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "optim_status": status,
                                             "replay": replay, "soc_init": 0.5, "rows": [],
                                             "aux_cut": {"active": active}})
    now = local(2026, 9, 2, 14, 0)
    assert core.aux_cut_state(arch, now, AMS) is False                        # empty archive
    put(local(2026, 9, 1, 23, 5), True)                                        # yesterday's cut does not carry over
    assert core.aux_cut_state(arch, now, AMS) is False
    put(local(2026, 9, 2, 12, 5), True)
    assert core.aux_cut_state(arch, now, AMS) is True
    put(local(2026, 9, 2, 13, 5), False)
    assert core.aux_cut_state(arch, now, AMS) is False                         # the newest plan decides
    put(local(2026, 9, 2, 13, 35), True, replay=True)                          # a replay is not the chain
    put(local(2026, 9, 2, 13, 40), True, status="Infeasible")
    assert core.aux_cut_state(arch, now, AMS) is False
    put(local(2026, 9, 2, 15, 5), True)                                        # made after `now`: not yet
    assert core.aux_cut_state(arch, now, AMS) is False

# ---- the Deye as an actuator ---------------------------------------------------

def test_deye_command_self_balance_puts_the_error_on_the_battery():
    """Plan wants grid at zero while charging: grid charging OFF, so the pack
    absorbs a forecast error instead of the meter. Selling pays here, so the
    clamp sits at nameplate and export stays on (2026-09-08); at a non-paying
    price the CT rule holds, and the clamp is the plan's charge only on a step
    the LP itself curtailed (2026-09-12)."""
    c = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2)
    assert c["work_mode"] == "Zero Export To CT"
    assert c["battery_grid_charging"] is False
    assert c["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A
    assert c["export_surplus"] is True
    assert c["intent"] == "self_balance"
    z = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=-0.01, pv_curtail_w=800.0)
    assert z["battery_max_charging_current"] == pytest.approx(98.0)      # 5000/51,2 = 97,7
    assert z["export_surplus"] is False
    assert c["tier"]["battery_grid_charging"] == "dangerous"


def test_deye_command_deliberate_grid_charge_turns_the_dangerous_field_on():
    c = core.deye_command(p_grid_w=2000.0, p_batt_w=-6000.0, pack_v=51.2)
    assert c["battery_grid_charging"] is True
    assert c["battery_grid_charging_current"] == pytest.approx(117.0)    # 6000/51,2 = 117,2
    assert c["intent"] == "grid_charge"


def test_deye_command_export_discharge_is_a_tou_target_not_a_work_mode():
    """Export First only lets PV reach the grid. The pack is commanded by a TOU
    target SOC with a power cap, which is what the 09-05 acceptance replay proved
    when the work-mode reading put 17 kW on a meter that measured 4,6."""
    c = core.deye_command(p_grid_w=-9000.0, p_batt_w=8000.0, pack_v=51.2, target_soc=0.3)
    assert c["work_mode"] == "Export First"
    assert c["battery_max_discharging_current"] == pytest.approx(156.0)  # 8000/51,2 = 156,3
    assert c["tou_target_soc"] == 0.3 and c["tou_power_w"] == pytest.approx(8000.0)
    assert c["intent"] == "export"


def test_deye_command_pv_export_holds_the_pack_with_a_zero_charge_clamp():
    """Falling prices before a cheap block: push PV out while it still pays and
    bank nothing, so the headroom survives for the cheap hours.

    A zero clamp rather than Export First. It is wasteful-tier, so a dead
    controller that leaves it standing merely fails to charge, where Export First
    is costly-tier and would sell into a negative price. It also keeps a register
    whose exact semantics are not bench-verified off the common path."""
    c = core.deye_command(p_grid_w=-3000.0, p_batt_w=0.0, pack_v=51.2)
    assert c["intent"] == "pv_export"
    assert c["battery_max_charging_current"] == 0.0
    assert c["export_surplus"] is True
    assert c["work_mode"] == "Zero Export To CT"                  # untouched
    assert core.DEYE_TIER["battery_max_charging_current"] == "wasteful"
    # bank nothing is not do nothing: the pack may still cover the house
    assert c["battery_max_discharging_current"] == core.DEYE_CURRENT_MAX_A


def test_deye_response_pv_export_holds_the_pack_and_sells_the_sun():
    """The 09-06 morning shape: PV leaves, the pack does not move, and nothing is
    curtailed because the surplus has somewhere to go."""
    cmd = core.deye_command(p_grid_w=-5000.0, p_batt_w=0.0, pack_v=51.2)
    r = core.deye_response(cmd, main_pot_w=5600.0, micro_w=0.0, load_w=200.0, soc=0.10)
    assert r["batt_w"] == pytest.approx(0.0, abs=1.0)
    assert r["grid_w"] == pytest.approx(-5400.0, abs=1.0)
    assert r["curtail_main_w"] == 0.0


def test_deye_command_baseline_is_the_conservative_state():
    b = core.DEYE_BASELINE
    assert b["battery_grid_charging"] is False
    assert b["work_mode"] == "Zero Export To CT"
    assert b["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A
    assert b["microinverter_export_cut_off"] is False


def test_deye_response_self_balance_absorbs_a_pv_shortfall_on_the_battery():
    """The whole point of zero-export. Plan wanted 5 kW of charge from 6 kW of
    sun; only 4 kW shows up, so the battery gets 3,5 kW and the METER STAYS AT
    ZERO. The old settlement booked the 1 kW shortfall to the grid."""
    cmd = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2)
    r = core.deye_response(cmd, main_pot_w=4000.0, micro_w=0.0, load_w=500.0, soc=0.5)
    assert r["grid_w"] == pytest.approx(0.0, abs=1.0)
    assert r["batt_w"] == pytest.approx(-3500.0, abs=1.0)
    assert r["curtail_main_w"] == 0.0


def test_deye_response_self_balance_curtails_the_strings_when_the_pack_is_full():
    """At a non-paying price. When selling pays a full pack exports instead (below)."""
    cmd = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=-0.01)
    r = core.deye_response(cmd, main_pot_w=6000.0, micro_w=0.0, load_w=500.0, soc=1.0)
    assert r["batt_w"] == pytest.approx(0.0, abs=1.0)          # no headroom
    assert r["grid_w"] == pytest.approx(0.0, abs=1.0)          # zero export holds
    assert r["curtail_main_w"] == pytest.approx(5500.0, abs=1.0)


def test_deye_response_self_balance_exports_the_sun_when_the_pack_is_full_and_selling_pays():
    cmd = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=0.07)
    r = core.deye_response(cmd, main_pot_w=6000.0, micro_w=0.0, load_w=500.0, soc=1.0)
    assert r["batt_w"] == pytest.approx(0.0, abs=1.0)          # no headroom
    assert r["grid_w"] == pytest.approx(-5500.0, abs=1.0)      # the surplus leaves
    assert r["curtail_main_w"] == 0.0


def test_deye_response_the_must_take_half_spills_past_zero_export():
    """The Growatt is on the gen port and the CT rule cannot reach it: with the
    pack full and the strings at zero it still pushes past the meter."""
    cmd = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=-0.01)
    r = core.deye_response(cmd, main_pot_w=6000.0, micro_w=1700.0, load_w=500.0, soc=1.0)
    assert r["curtail_main_w"] == pytest.approx(6000.0, abs=1.0)   # strings fully off
    assert r["grid_w"] == pytest.approx(-1200.0, abs=1.0)          # 1700 - 500 exported anyway
    assert r["curtail_micro_w"] == 0.0


def test_deye_response_cutting_the_micro_stops_the_spill():
    cmd = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, micro_cut=True, sell=-0.01)
    r = core.deye_response(cmd, main_pot_w=6000.0, micro_w=1700.0, load_w=500.0, soc=1.0)
    assert r["curtail_micro_w"] == pytest.approx(1700.0)
    assert r["grid_w"] == pytest.approx(0.0, abs=1.0)              # strings cover the load
    assert r["curtail_main_w"] == pytest.approx(5500.0, abs=1.0)


def test_deye_response_grid_charge_draws_the_shortfall_from_the_meter():
    cmd = core.deye_command(p_grid_w=2000.0, p_batt_w=-6000.0, pack_v=51.2)
    r = core.deye_response(cmd, main_pot_w=4000.0, micro_w=0.0, load_w=500.0, soc=0.5)
    # 6.000 W floors to a 115 A clamp = 5.888 W: the quantisation is visible in the
    # response, which is the point of doing it at the record rather than at the write.
    assert r["batt_w"] == pytest.approx(-5990.4, abs=1.0)          # 117 A x 51,2 V
    assert r["grid_w"] == pytest.approx(2490.4, abs=1.0)           # 5990,4 + 500 - 4000
    assert r["curtail_main_w"] == 0.0


def test_deye_response_export_discharges_into_the_meter():
    cmd = core.deye_command(p_grid_w=-9000.0, p_batt_w=8000.0, pack_v=51.2, target_soc=0.3)
    r = core.deye_response(cmd, main_pot_w=1000.0, micro_w=0.0, load_w=500.0, soc=0.8)
    assert r["batt_w"] == pytest.approx(7936.0, abs=64.0)          # 155 A x 51,2 V
    assert r["grid_w"] < -8000.0


def test_deye_response_never_discharges_below_the_floor():
    cmd = core.deye_command(p_grid_w=-9000.0, p_batt_w=8000.0, pack_v=51.2, target_soc=0.05)
    r = core.deye_response(cmd, main_pot_w=0.0, micro_w=0.0, load_w=500.0, soc=core.SOC_MIN)
    assert r["batt_w"] == pytest.approx(0.0, abs=1.0)


def test_deye_response_never_charges_past_full():
    cmd = core.deye_command(p_grid_w=2000.0, p_batt_w=-6000.0, pack_v=51.2)
    r = core.deye_response(cmd, main_pot_w=0.0, micro_w=0.0, load_w=0.0, soc=SOC_ALMOST_FULL)
    # one step of headroom only: 0,2 % of 48,2 kWh over 15 min
    assert -r["batt_w"] < 6000.0 and r["batt_w"] < 0


SOC_ALMOST_FULL = 1.0 - 0.002


# ---- stub EMHASS ---------------------------------------------------------------
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_rows(t0, n, buy=None, sell=None, pv=None, status="Optimal", soc0=0.5):
    """A plan the way GET /api/v1/plan returns it: even steps import 1 kW, odd
    steps export 0,5 kW, SOC drifts down 0,1 % per step."""
    rows = []
    for k, t in enumerate(core.step_times(t0, n)):
        pg = 1000.0 if k % 2 == 0 else -500.0
        b = (buy or [0.28] * n)[k]
        s = (sell or [0.10] * n)[k]
        rows.append({"timestamp": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                     "P_PV": (pv or [0.0] * n)[k], "P_Load": 400.0, "P_grid_pos": max(pg, 0.0),
                     "P_grid_neg": min(pg, 0.0), "P_grid": pg, "P_batt": -600.0 if k % 2 else 600.0,
                     "SOC_opt": round(soc0 - 0.001 * k, 4), "P_PV_curtailment": 0.0,
                     "P_hybrid_inverter": 0.0, "unit_load_cost": b, "unit_prod_price": s,
                     "cost_profit": -core.step_cost(pg, b, s), "optim_status": status})
    return rows


class StubEmhass:
    """Records every POST; answers last-run, plan (from plan_fn), healthz, get-config."""

    def __init__(self):
        self.posts, self.plan_fn, self.config, self.healthz = [], lambda posts: [], {}, {"status": "ok"}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.posts.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"EMHASS >> ok")

            def do_GET(self):
                if self.path.startswith("/api/v1/last-run"):
                    doc = {"status": "ok", "action": (outer.posts[-1][0].rsplit("/", 1)[-1] if outer.posts else "none"),
                           "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "duration_total_seconds": 0.4}
                elif self.path.startswith("/api/v1/plan"):
                    doc = {"status": "ok", "plan": outer.plan_fn(outer.posts)}
                elif self.path.startswith("/healthz"):
                    doc = outer.healthz
                elif self.path.startswith("/get-config"):
                    doc = outer.config
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(doc).encode()
                self.send_response(503 if doc.get("status") == "degraded" else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def stub():
    s = StubEmhass()
    yield s
    s.close()


def plan_inputs(now, tmp_path, base_url, dry_run=False):
    t0, n = core.horizon(now, AMS)
    today = now.astimezone(Z).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"now": now.isoformat(), "tz": AMS, "base_url": base_url, "archive_dir": str(tmp_path / "plans"),
            "dry_run": dry_run, "np_today": np_rows(today), "np_tomorrow": np_rows(today + timedelta(days=1), 200.0),
            "epex": [], "solcast_today": solcast(today), "solcast_tomorrow": solcast(today + timedelta(days=1)),
            "soc_pct": 80.0, "tariff": {"energy_tax": 0.11, "supplier_fee": 0.02, "btw_pct": 21, "feedin_fee": 0.0}}


def test_run_plan_happy_path_archives_and_rolls(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url))
    assert res["ok"], res
    assert res["n"] == n and res["n_predicted_steps"] == 0 and res["pv_gap_steps"] == 0
    assert res["optim_status"] == "Optimal"
    path, body = stub.posts[0]
    assert path == "/action/naive-mpc-optim"
    assert body["prediction_horizon"] == n and body["soc_init"] == 0.8 and body["soc_final"] == 0.5
    # 14:15 is Nord Pool step 57 of today: (0,157 + 0,11 + 0,02) x 1,21
    assert len(body["pv_power_forecast"]) == n and body["load_cost_forecast"][0] == pytest.approx(0.34727, abs=1e-5)
    assert res["archive_path"].endswith(".json.gz") and (tmp_path / "plans").exists()
    doc = core.load_plan(res["archive_path"])
    assert doc["n"] == n and len(doc["rows"]) == n and doc["payload"]["prediction_horizon"] == n
    assert res["today"] is None                                   # no plan was made before 00:00 today
    nd = res["next_day"]
    assert nd["date"] == (now.date() + timedelta(days=1)).isoformat()
    assert nd["n"] == core.expected_steps(now.date() + timedelta(days=1), AMS)
    assert len(nd["p_grid_w"]) == nd["n"] == len(nd["cost_eur"]) == len(nd["soc_pct"])
    assert nd["soc_start_pct"] == pytest.approx(100 * (0.5 - 0.001 * (39 - 1)))   # SOC after today's last step
    assert nd["p_grid_w"][0] == -500.0                          # global step 39 is odd: an export step
    assert nd["cost_eur"][0] == pytest.approx(core.step_cost(-500.0, 0.28, 0.10))


def _scale_solcast(rows, f):
    return [{**r, "pv_estimate": r["pv_estimate"] * f} for r in rows]


def _split_inputs(now, tmp_path, url, ese_fraction=0.6, share=0.5):
    """plan_inputs plus a load profile and an ESE site series, so run_plan has
    everything the split needs. The Growatt then works out to
    ese_fraction x share of the combined forecast."""
    inp = plan_inputs(now, tmp_path, url)
    inp["load_days"] = load_days(now.date() - timedelta(days=1))
    inp["solcast_ese_today"] = _scale_solcast(inp["solcast_today"], ese_fraction)
    inp["solcast_ese_tomorrow"] = _scale_solcast(inp["solcast_tomorrow"], ese_fraction)
    inp["knobs"] = {"growatt_share": share}
    return inp


def test_run_plan_splits_the_growatt_out_of_pv_and_into_the_load(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = _split_inputs(now, tmp_path, stub.url)
    combined, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    load_only, _ = core.load_series(t0, n, AMS, inp["load_days"], inp.get("load_now_w"))
    res = core.run_plan(inp)
    assert res["ok"], res
    body = stub.posts[0][1]
    micro = [round(c * 0.3, 1) for c in combined]                 # 0,6 of the array x 0,5 share
    assert body["pv_power_forecast"] == pytest.approx([round(c - m, 1) for c, m in zip(combined, micro)])
    assert body["load_power_forecast"] == pytest.approx([round(l - m, 1) for l, m in zip(load_only, micro)])
    # the must-take half is gone from PV entirely, and the two halves still sum
    assert sum(body["pv_power_forecast"]) + sum(micro) == pytest.approx(sum(combined))
    assert res["pv_split"] is True
    assert res["pv_micro_kwh"] == pytest.approx(sum(micro) * core.STEP_H / 1000.0, abs=1e-3)
    doc = core.load_plan(res["archive_path"])
    assert doc["growatt_share"] == 0.5
    assert doc["pv_micro_w"] == pytest.approx(micro)


def test_run_plan_archives_the_rows_in_gross_terms(stub, tmp_path):
    """The split is a payload detail, not a change of units. What EMHASS solved
    on is net of the must-take half; what gets archived puts it back into both
    P_PV and P_Load, so the plan's own balance is untouched and every consumer
    downstream of the archive needs no migration for pre-split plans."""
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n, pv=[1234.0] * n)
    inp = _split_inputs(now, tmp_path, stub.url)
    res = core.run_plan(inp)
    assert res["ok"], res
    doc = core.load_plan(res["archive_path"])
    micro = doc["pv_micro_w"]
    assert any(m > 0 for m in micro)
    for r, m in zip(doc["rows"], micro):
        assert float(r["P_PV"]) == pytest.approx(1234.0 + m)
        assert float(r["P_Load"]) == pytest.approx(400.0 + m)
        assert float(r["P_PV_curtailment"]) == 0.0        # main-only, left alone
    # the balance the slice reads is unchanged by the round trip
    for r, m in zip(doc["rows"], micro):
        assert float(r["P_PV"]) - float(r["P_Load"]) == pytest.approx(1234.0 - 400.0)


def _cut_inputs(now, tmp_path, url, curtail_w):
    """Split inputs plus a stub whose (uncut) plan declines `curtail_w` on every step
    of today; the second (cut) post echoes zero curtailment."""
    inp = _split_inputs(now, tmp_path, url, ese_fraction=1.0, share=0.5)
    t0, n = core.horizon(now, AMS)
    today = now.date()
    def plan_fn(posts):
        rows = make_rows(t0, n)
        if len(posts) == 1:                                   # the uncut solve
            for r in rows:
                if core._parse_ts(r["timestamp"]).astimezone(Z).date() == today:
                    r["P_PV_curtailment"] = curtail_w
        return rows
    return inp, plan_fn, t0, n


def _today_micro_kwh(inp, t0, n, now):
    p50, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    ese, _ = core.pv_series(t0, n, inp["solcast_ese_today"], inp["solcast_ese_tomorrow"])
    _, micro = core.pv_split(p50, ese, inp["knobs"]["growatt_share"])
    idx = [k for k, t in enumerate(core.step_times(t0, n)) if t.date() == now.date()]
    return sum(micro[k] for k in idx) * core.STEP_H / 1000.0, micro, idx


def test_run_plan_cuts_the_growatt_when_curtailment_outweighs_it(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    inp, plan_fn, t0, n = _cut_inputs(now, tmp_path, stub.url, curtail_w=3000.0)
    stub.plan_fn = plan_fn
    aux_kwh, micro, idx = _today_micro_kwh(inp, t0, n, now)
    load_w, _ = core.load_series(t0, n, AMS, inp["load_days"], inp.get("load_now_w"))
    res = core.run_plan(inp)
    assert res["ok"], res
    assert res["aux_cut_active"] is True and res["aux_cut_steps"] == len(idx)
    assert res["aux_cut_curtail_kwh"] == pytest.approx(3.0 * len(idx) * core.STEP_H, abs=1e-3)
    assert res["aux_cut_aux_kwh"] == pytest.approx(aux_kwh, abs=1e-3)
    assert len(stub.posts) == 2                                              # uncut to decide, cut to plan
    l1, l2 = stub.posts[0][1]["load_power_forecast"], stub.posts[1][1]["load_power_forecast"]
    for k in range(n):
        if k in idx:
            assert l2[k] == pytest.approx(load_w[k], abs=0.06)              # the Growatt left today's load
            assert l1[k] == pytest.approx(load_w[k] - micro[k], abs=0.06)
        else:
            assert l2[k] == pytest.approx(load_w[k] - micro[k], abs=0.06)   # tomorrow keeps it
    doc = core.load_plan(res["archive_path"])
    assert doc["micro_cut"] == [k in set(idx) for k in range(n)]
    assert doc["aux_cut"]["active"] is True and doc["aux_cut"]["second_solve"] == "ok"
    assert doc["aux_cut"]["cut_from"] == core.step_times(t0, n)[idx[0]].isoformat()
    assert doc["payload"]["load_power_forecast"] == l2
    # gross restore: a cut step puts the Growatt into available AND declined, not into the load
    k_cut, k_keep = idx[0], idx[-1] + 1
    assert doc["rows"][k_cut]["P_PV"] == pytest.approx(0.0 + micro[k_cut], abs=0.06)
    assert doc["rows"][k_cut]["P_PV_curtailment"] == pytest.approx(0.0 + micro[k_cut], abs=0.06)   # cut solve echoed 0
    assert doc["rows"][k_cut]["P_Load"] == pytest.approx(400.0)
    assert doc["rows"][k_keep]["P_Load"] == pytest.approx(400.0 + micro[k_keep], abs=0.06)
    assert doc["rows"][k_keep]["P_PV_curtailment"] == 0.0
    # the slice exposes the cut as watts of Growatt
    nd = res["next_day"]
    assert nd["micro_cut_w"] == [0.0] * nd["n"]                              # tomorrow is not cut


def test_run_plan_leaves_the_growatt_when_curtailment_is_small(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    inp, plan_fn, t0, n = _cut_inputs(now, tmp_path, stub.url, curtail_w=100.0)   # ~1 kWh over the afternoon
    stub.plan_fn = plan_fn
    res = core.run_plan(inp)
    assert res["ok"] and res["aux_cut_active"] is False and len(stub.posts) == 1
    doc = core.load_plan(res["archive_path"])
    assert doc["micro_cut"] == [False] * n and doc["aux_cut"]["second_solve"] is None
    assert doc["rows"][0]["P_Load"] > 400.0                                  # the Growatt stayed in the load


def test_run_plan_growatt_cut_hysteresis_carries_from_todays_plan(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    probe, _fn, t0, n = _cut_inputs(now, tmp_path / "probe", stub.url, curtail_w=0.0)
    aux_kwh, micro, idx = _today_micro_kwh(probe, t0, n, now)
    def scenario(name, standing, ratio):
        """Own archive per case: run_plan archives at `now`, and a second solve at the
        same instant would read its own predecessor as today's newest plan."""
        inp, plan_fn, _t0, _n = _cut_inputs(now, tmp_path / name, stub.url,
                                            curtail_w=ratio * aux_kwh / (len(idx) * core.STEP_H) * 1000.0)
        made = now.replace(hour=13, minute=5)
        core.write_plan_archive(inp["archive_dir"], made, {"plan_ts": made.isoformat(), "optim_status": "Optimal",
                                                           "soc_init": 0.5, "rows": [], "aux_cut": {"active": standing}})
        stub.posts.clear(); stub.plan_fn = plan_fn
        res = core.run_plan(inp)
        assert res["ok"], res
        return res["aux_cut_active"], len(stub.posts)
    assert scenario("enter_no", False, 1.3) == (False, 1)                     # 1,3 does not enter
    assert scenario("stay", True, 1.3) == (True, 2)                           # 1,3 with a cut standing: stays cut
    assert scenario("leave", True, 1.1) == (False, 1)                         # under 1,2: re-enabled
    assert scenario("enter_yes", False, 1.6) == (True, 2)                     # 1,6 enters


def test_run_plan_keeps_the_uncut_plan_when_the_cut_solve_fails(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    inp, plan_fn, t0, n = _cut_inputs(now, tmp_path, stub.url, curtail_w=3000.0)
    def flaky(posts):
        rows = plan_fn(posts)
        if len(posts) == 2:
            for r in rows:
                r["optim_status"] = "Infeasible"
        return rows
    stub.plan_fn = flaky
    res = core.run_plan(inp)
    assert res["ok"] and res["aux_cut_active"] is False and len(stub.posts) == 2
    doc = core.load_plan(res["archive_path"])
    assert doc["micro_cut"] == [False] * n and doc["aux_cut"]["second_solve"] == "not Optimal"
    assert doc["rows"][0]["optim_status"] == "Optimal"                       # the uncut rows were archived


def test_run_plan_growatt_can_drive_the_load_negative(stub, tmp_path):
    """The whole point: at midday the must-take half exceeds the house load, so
    the load series must be allowed below zero. EMHASS accepts that (probed on
    the live add-on 2026-09-06); clamping it here would hide the export."""
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = _split_inputs(now, tmp_path, stub.url, ese_fraction=1.0, share=1.0)
    res = core.run_plan(inp)
    assert res["ok"], res
    body = stub.posts[0][1]
    assert min(body["load_power_forecast"]) < 0
    assert body["pv_power_forecast"] == [0.0] * n          # all of it is must-take now


def test_run_plan_without_an_ese_series_sends_the_combined_forecast(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = _split_inputs(now, tmp_path, stub.url)
    inp["solcast_ese_today"] = inp["solcast_ese_tomorrow"] = []
    combined, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    res = core.run_plan(inp)
    assert res["ok"], res
    assert res["pv_split"] is False
    assert stub.posts[0][1]["pv_power_forecast"] == pytest.approx(combined)


def test_run_plan_load_exact_w_overrides_the_profile_step_by_step(stub, tmp_path):
    """The replay's omniscient walk hands exact load on named UTC instants; it
    replaces the profile only on those steps and the Growatt split still nets
    the micro off it. Live never passes the key, so nothing else moves."""
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = _split_inputs(now, tmp_path, stub.url)
    combined, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    load_only, _ = core.load_series(t0, n, AMS, inp["load_days"], inp.get("load_now_w"))
    times = core.step_times(t0, n)
    exact = {core.load_exact_key(times[3]): 4321.0, core.load_exact_key(times[5]): -50.0,
             core.load_exact_key(times[0] - timedelta(days=30)): 999.0}     # off the horizon: ignored
    inp["load_exact_w"] = exact
    res = core.run_plan(inp)
    assert res["ok"], res
    assert res["load_exact_steps"] == 2
    assert res["pv_split"] is True
    micro = [round(c * 0.3, 1) for c in combined]
    want = list(load_only)
    want[3], want[5] = 4321.0, 0.0                                           # clamped at zero like the profile
    assert stub.posts[0][1]["load_power_forecast"] == pytest.approx([round(l - m, 1) for l, m in zip(want, micro)])


def test_run_plan_does_not_split_without_its_own_load_forecast(stub, tmp_path):
    """No load list means EMHASS runs its own load method, and there is then
    nowhere to put the must-take half. Sending main-only PV without it would
    silently delete the Growatt from the plan, so the split stands down."""
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = _split_inputs(now, tmp_path, stub.url)
    inp.pop("load_days")
    combined, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    res = core.run_plan(inp)
    assert res["ok"], res
    assert res["pv_split"] is False
    body = stub.posts[0][1]
    assert body["pv_power_forecast"] == pytest.approx(combined)
    assert "load_power_forecast" not in body


def test_stress_costs_scale_with_the_price_level_and_floor_at_zero():
    u_batt, u_inv = core.stress_costs([0.2] * 4, [0.1] * 4)
    assert u_batt == pytest.approx(12.5 * core.Q_PORT * 0.15, abs=1e-5)
    assert u_inv == pytest.approx(12.0 * core.Q_BRIDGE * 0.15, abs=1e-5)
    assert core.stress_costs([-0.1] * 4, [-0.2] * 4) == (0.0, 0.0)
    assert core.stress_costs([], []) == (0.0, 0.0)


def test_rebalance_schedule_fades_then_pulls():
    fresh = core.rebalance_schedule(0)
    assert fresh == {"battery_soc_surplus_cost": 0.005,
                     "battery_soc_deficit_threshold": 0.20,
                     "battery_soc_deficit_cost": 0.01}
    half = core.rebalance_schedule(3.5)
    assert half["battery_soc_surplus_cost"] == pytest.approx(0.0025)
    assert half["battery_soc_deficit_threshold"] == 0.20
    at_target = core.rebalance_schedule(7)
    assert at_target["battery_soc_surplus_cost"] == 0.0
    assert at_target["battery_soc_deficit_threshold"] == 0.20
    overdue = core.rebalance_schedule(10.5)
    assert overdue["battery_soc_surplus_cost"] == 0.0
    assert overdue["battery_soc_deficit_threshold"] == 1.0
    assert overdue["battery_soc_deficit_cost"] == pytest.approx(0.0015)
    assert core.rebalance_schedule(21)["battery_soc_deficit_cost"] == pytest.approx(0.003)
    # nothing on record is overdue, not relaxed (2026-09-15): the pull at full strength
    assert core.rebalance_schedule(None) == core.rebalance_schedule(14)
    assert core.rebalance_schedule(None)["battery_soc_deficit_threshold"] == 1.0


def test_days_since_full_walks_the_plan_of_record(tmp_path):
    tz = AMS
    now = local(2026, 9, 10, 14, 0)
    arch = str(tmp_path / "plans")
    # a plan made 2026-09-07 12:00 covering 09-08 fully, topping at 100% mid-day
    t0, n = core.horizon(local(2026, 9, 7, 12, 0, 4), tz)
    rows = make_rows(t0, n)
    for r in rows:
        ts = core._parse_ts(r["timestamp"]).astimezone(Z)
        r["SOC_opt"] = 0.996 if (ts.date().isoformat() == "2026-09-08" and ts.hour == 16) else 0.5
    core.write_plan_archive(arch, local(2026, 9, 7, 12, 0), {
        "plan_ts": local(2026, 9, 7, 12, 0).isoformat(), "optim_status": "Optimal",
        "soc_init": 0.5, "rows": rows})
    assert core.days_since_full(arch, now, tz) == 2          # 09-08 is two days back
    assert core.days_since_full(arch, now, tz, level=0.999) is None


def test_run_plan_passes_the_price_scaled_stress_override(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url))
    assert res["ok"], res
    _, body = stub.posts[0]
    buy, sell = body["load_cost_forecast"], body["prod_price_forecast"]
    pi = sum((b + s) / 2.0 for b, s in zip(buy, sell)) / len(buy)
    assert body["battery_stress_cost"] == pytest.approx(round(12.5 * core.Q_PORT * pi, 5), abs=1e-6)
    assert body["inverter_stress_cost"] == pytest.approx(round(12.0 * core.Q_BRIDGE * pi, 5), abs=1e-6)
    # empty archive -> days_since_full None -> OVERDUE (2026-09-15): no surplus
    # penalty, the pull at full strength; a pack with no balance on record needs one
    assert body["battery_soc_surplus_cost"] == 0.0
    assert body["battery_soc_deficit_threshold"] == 1.0
    assert body["battery_soc_deficit_cost"] == core.REBALANCE_PULL
    assert res["days_since_full"] is None


def test_run_plan_dry_run_solves_but_writes_nothing(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url, dry_run=True))
    assert res["ok"] and "archive_path" not in res and not (tmp_path / "plans").exists()
    assert [p for p, _ in stub.posts] == ["/action/naive-mpc-optim"]


def test_run_plan_rejects_a_misaligned_horizon(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    shifted = core.step_times(t0, 2)[1]
    stub.plan_fn = lambda posts: make_rows(shifted, n)
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url))
    assert not res["ok"] and "misaligned" in res["message"]
    assert not (tmp_path / "plans").exists()


def test_run_plan_archives_a_non_optimal_plan_but_does_not_roll(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n, status="Infeasible")
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url))
    assert not res["ok"] and res["optim_status"] == "Infeasible"
    assert len(core.list_plans(str(tmp_path / "plans"))) == 1
    assert "today" not in res and "next_day" not in res


def test_run_plan_aborts_on_too_many_pv_gaps_before_posting(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    inp = plan_inputs(now, tmp_path, stub.url)
    inp["solcast_tomorrow"] = []
    res = core.run_plan(inp)
    assert not res["ok"] and "solcast" in res["message"] and stub.posts == []


def test_run_plan_clamps_soc_into_the_band(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url, dry_run=True)
    inp["soc_pct"] = 104.0
    assert core.run_plan(inp)["ok"]
    assert stub.posts[0][1]["soc_init"] == 1.0
    inp = plan_inputs(now, tmp_path, stub.url, dry_run=True)
    inp["soc_pct"] = 4.0
    assert core.run_plan(inp)["ok"]
    assert stub.posts[1][1]["soc_init"] == 0.1


def test_publish_posts_the_eleven_custom_ids(stub):
    res = core.publish(stub.url)
    assert res["ok"]
    path, body = stub.posts[-1]
    assert path == "/action/publish-data"
    ids = sorted(v["entity_id"] for v in body.values())
    assert len(ids) == 11 and all(i.startswith("sensor.emhass_da_") for i in ids)
    assert body["custom_batt_soc_forecast_id"]["entity_id"] == "sensor.emhass_da_soc"


def test_health_reports_ok_and_drift(stub, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"a": 1, "data_path": "/x", "heat_topology": {}}))
    stub.config = {"a": 1}
    stub.healthz = {"status": "ok", "last_run_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    h = core.health(stub.url, str(cfg))
    assert h["ok"] and h["drift"] == [] and h["last_run_age_h"] == pytest.approx(0.0, abs=0.1)
    stub.config = {"a": 2}
    h = core.health(stub.url, str(cfg))
    assert not h["ok"] and h["drift"] == ["a"]
    stub.config = {"a": 1}
    stub.healthz = {"status": "degraded", "last_run_ts": None}
    assert not core.health(stub.url, str(cfg))["ok"]


def test_plan_for_day_does_not_let_a_replay_reach_into_the_next_day(tmp_path):
    """A replay doc keeps the source plan's pre-midnight plan_ts under a much
    later filename, and its horizon covers the day AFTER the one it replays.
    In filename order it was reached first and handed that day too: live on
    2026-09-05, 09-04 scored against a plan solved 25 h before the day began."""
    arch = str(tmp_path / "plans")
    def put(made, plan_ts, t0, n, marker, replay=False):
        core.write_plan_archive(arch, made, {"plan_ts": plan_ts.isoformat(), "t0": t0.isoformat(), "n": n,
                                             "tz": AMS, "soc_init": 0.5, "soc_final": 0.5,
                                             "optim_status": "Optimal", "n_predicted_steps": marker,
                                             "pv_gap_steps": 0, "replay": replay,
                                             "rows": make_rows(t0, n)})
    # 195 steps from 23:15 cover the next two whole days
    put(local(2026, 9, 1, 23, 5), local(2026, 9, 1, 23, 5), local(2026, 9, 1, 23, 15), 195, 1)
    put(local(2026, 9, 2, 23, 5), local(2026, 9, 2, 23, 5), local(2026, 9, 2, 23, 15), 195, 2)
    # the replay of 09-02, run on 09-03: newest FILE, oldest plan_ts, and it
    # carries all 96 steps of 09-03 with it
    put(local(2026, 9, 3, 19, 2), local(2026, 9, 1, 23, 5), local(2026, 9, 1, 23, 15), 195, 3, replay=True)
    # its own day is still the replay's, which is what the harness is for
    assert core.plan_for_day(arch, date(2026, 9, 2), AMS)[0]["n_predicted_steps"] == 3
    # but the day after belongs to the genuine day-ahead plan, not to the replay
    assert core.plan_for_day(arch, date(2026, 9, 3), AMS)[0]["n_predicted_steps"] == 2
    assert core.newest_plan_for_day(arch, date(2026, 9, 3), AMS)[0]["n_predicted_steps"] == 2
    heads = core.plan_heads(arch)
    assert [h["plan_ts"] for _p, h in heads] == sorted((h["plan_ts"] for _p, h in heads), reverse=True)


def test_plan_for_day_picks_the_newest_optimal_plan_made_before_midnight(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    prev = local(2026, 9, 1, 13, 15)
    for hh, status in ((13, "Optimal"), (15, "Optimal"), (16, "Infeasible")):
        made = local(2026, 9, 1, hh, 0, 30)
        t0, n = core.horizon(made, AMS)
        core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS,
                                             "soc_init": 0.5, "soc_final": 0.5, "optim_status": status,
                                             "n_predicted_steps": hh, "pv_gap_steps": 0, "rows": make_rows(t0, n, status=status)})
    made = local(2026, 9, 2, 13, 0, 30)                       # made ON the day: must not be used for that day
    t0, n = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS,
                                         "soc_init": 0.5, "soc_final": 0.5, "optim_status": "Optimal",
                                         "n_predicted_steps": 99, "pv_gap_steps": 0, "rows": make_rows(t0, n)})
    doc, sl = core.plan_for_day(arch, day, AMS)
    assert doc["n_predicted_steps"] == 15                       # the 15:00 plan beats 13:00, the 16:00 one is not Optimal
    assert sl["n"] == 96 and sl["slice_start"].startswith("2026-09-02T00:00")
    assert core.plan_for_day(arch, date(2026, 9, 4), AMS) is None
    # Scoring stays frozen on the pre-midnight plan (asserted above), but the
    # DISPLAY slices deliberately do not: today is spliced by virtual_day from
    # whichever plan was in force at each step, so the 13:00 solve made ON the
    # day now carries the later steps. Asserting 15 here was asserting the bug
    # that froze the chart on last night's plan for a whole day.
    rolled = core.rolled_slices(arch, local(2026, 9, 2, 14, 0), AMS)
    assert rolled["today"]["n_predicted_steps"] == 99            # the newest plan in force, not the frozen one
    assert rolled["today"]["sources"] == sorted(rolled["today"]["sources"], reverse=True)
    assert len(rolled["today"]["sources"]) == 2                  # spliced: last night's plan, then the 13:00 one
    assert rolled["next_day"]["n_predicted_steps"] == 99


# ---- scoring -----------------------------------------------------------------

def flat_plan(n=96, p_grid=1000.0, buy=0.30, sell=0.10, soc_start=50.0, soc_end=40.0, **meta):
    """A compact slice by hand: first half imports p_grid, second half exports it."""
    pg = [p_grid] * (n // 2) + [-p_grid] * (n - n // 2)
    soc = [round(soc_start + (soc_end - soc_start) * (k + 1) / n, 3) for k in range(n)]
    d = {"date": "2026-09-02", "slice_start": "2026-09-02T00:00:00+02:00", "step_min": 15, "n": n, "full_day": True,
         "plan_ts": "2026-09-01T13:15:04+02:00", "t0": "2026-09-01T13:15:00+02:00", "n_predicted_steps": 0,
         "pv_gap_steps": 0, "optim_status": "Optimal", "soc_start_pct": soc_start,
         "p_batt_w": [0.0] * n, "p_grid_w": pg, "soc_pct": soc, "p_pv_w": [2000.0] * n, "p_load_w": [500.0] * n,
         "buy": [buy] * n, "sell": [sell] * n}
    d["cost_eur"] = [round(core.step_cost(pg[k], buy, sell), 5) for k in range(n)]
    d.update(meta)
    return d


def flat_actuals(plan, g_meas=0.0, b_dc=0.0):
    """The measured day the replay lane is laid over: a constant grid trace and a
    constant battery DC power, against flat_plan's 2 kW of PV."""
    n = plan["n"]
    return {"pv_w": [2000.0] * n, "grid_w": [float(g_meas)] * n, "batt_dc_w": [float(b_dc)] * n}


DAY_ACTUALS = {"pv_w": [0.0] * 96, "grid_w": [0.0] * 96, "batt_dc_w": [0.0] * 96}


def test_planned_cost_and_soc_term():
    plan = flat_plan()
    lam = core.lambda_for(plan["sell"], 0.9)
    assert lam == pytest.approx(0.09)
    pc = core.planned_cost(plan, 48.0, lam)
    assert pc["cash"] == pytest.approx(48 * 0.075 - 48 * 0.025)        # 2,40 EUR
    assert pc["soc_term"] == pytest.approx(0.10 * 48 * 0.09)           # 10 % drained, 0,432 EUR
    assert pc["total"] == pytest.approx(2.832)
    assert core.soc_term(40.0, 50.0, 48.0, 0.09) == pytest.approx(-0.432)   # a charged pack is a credit


def test_settle_slice_moves_the_error_onto_the_pack_not_the_meter():
    """A self-balancing plan step that gets less sun than forecast: the meter
    holds and the pack charges less. Open-loop this became an import."""
    n = 4
    compact = {"n": n, "soc_start_pct": 50.0,
               "p_grid_w": [0.0] * n, "p_batt_w": [-5000.0] * n,
               "p_pv_w": [5500.0] * n, "p_load_w": [500.0] * n,
               "pv_curtail_w": [0.0] * n, "soc_pct": [50.0] * n,
               "buy": [0.3] * n, "sell": [0.1] * n}
    out = core.settle_slice(compact, [4500.0] * n, [500.0] * n)
    assert out["settled"] is True
    assert out["p_grid_w"][0] == pytest.approx(0.0, abs=1.0)     # meter unmoved
    assert out["p_batt_w"][0] == pytest.approx(-4000.0, abs=1.0)  # 1000 W less charge
    assert out["soc_pct"][-1] < 50.0 + 4 * 5000 * core.STEP_H / 1000 / core.CAPACITY_KWH * 100


def test_settle_slice_leaves_a_deliberate_grid_trade_on_the_meter():
    """Where the plan imports on purpose the error still belongs to the grid."""
    n = 2
    compact = {"n": n, "soc_start_pct": 50.0,
               "p_grid_w": [2000.0] * n, "p_batt_w": [-6000.0] * n,
               "p_pv_w": [4500.0] * n, "p_load_w": [500.0] * n,
               "pv_curtail_w": [0.0] * n, "soc_pct": [50.0] * n,
               "buy": [0.3] * n, "sell": [0.1] * n}
    out = core.settle_slice(compact, [3500.0] * n, [500.0] * n)
    assert out["p_grid_w"][0] == pytest.approx(3000.0, abs=1.0)   # 1 kW less sun, 1 kW more import
    assert out["p_batt_w"][0] == pytest.approx(-6000.0, abs=1.0)  # charge held


def test_deye_command_a_declining_step_is_zero_export_whatever_the_battery_does():
    """R2 (2026-09-07). An idle battery used to map to the export intent and lift
    the CT rule even when the plan curtailed at grid zero; the base run then
    exported what the plan declined and a real dip settled as phantom import."""
    idle = core.deye_command(p_grid_w=0.0, p_batt_w=0.0, pack_v=51.2, pv_curtail_w=5500.0)
    assert idle["export_surplus"] is False and idle["battery_max_charging_current"] == 0.0
    # the gen-port spill case: grid slightly negative from the must-take half, strings declined
    spill = core.deye_command(p_grid_w=-1000.0, p_batt_w=0.0, pack_v=51.2, pv_curtail_w=4000.0)
    assert spill["export_surplus"] is False
    # no curtailment: the export intent still sells the sun
    sell = core.deye_command(p_grid_w=-3000.0, p_batt_w=0.0, pack_v=51.2)
    assert sell["export_surplus"] is True


def test_settle_step_no_phantom_import_when_a_full_pack_declines_sun_and_the_sun_dips():
    # plan: pack full, strings declined 5.500 of 6.000 W, load 500, grid 0
    g, b, c = core.settle_step(0.0, 0.0, 6000.0, 500.0, 5500.0, 1800.0, 500.0, 0.0, 0.0, 1.0)
    assert g == pytest.approx(0.0) and b == pytest.approx(0.0)
    assert c == pytest.approx(1300.0)                              # the dip eats the declined sun, nothing else moves


def test_settle_slice_hands_the_plan_its_own_must_take_share():
    """A full pack under zero export with the Growatt pushing 1.500 W past a
    500 W load spills 1.000 W, and the plan knew it (grid -1000). With the plan
    side given a zero share the base run held the meter at zero, the actual run
    spilled, and the difference doubled the spill."""
    n = 2
    compact = {"n": n, "soc_start_pct": 100.0, "p_grid_w": [-1000.0] * n, "p_batt_w": [0.0] * n,
               "p_pv_w": [7000.0] * n, "p_load_w": [500.0] * n, "pv_curtail_w": [5500.0] * n,
               "pv_micro_w": [1500.0] * n, "soc_pct": [100.0] * n}
    out = core.settle_slice(compact, [7000.0] * n, [500.0] * n, [1500.0] * n)
    assert out["p_grid_w"] == [pytest.approx(-1000.0)] * n         # the spill once, as the plan had it
    assert out["p_batt_w"] == [pytest.approx(0.0)] * n
    legacy = dict(compact)
    del legacy["pv_micro_w"]                                       # a pre-split slice still settles
    assert core.settle_slice(legacy, [7000.0] * n, [500.0] * n, [1500.0] * n) is not None


def test_settle_step_a_cut_growatt_neither_spills_nor_charges():
    """Pack full, load 500, Growatt 1.500 forecast and 1.700 real, strings declined.
    Cut: no spill at the meter, and the declined lane grows by the Growatt's own
    forecast error (200 W). Not cut: the spill lands on the meter as the plan had it."""
    cut = core.settle_step(0.0, 0.0, 7000.0, 500.0, 7000.0, 7200.0, 500.0, 1500.0, 1700.0, 1.0, micro_cut=True)
    assert cut[0] == pytest.approx(0.0) and cut[1] == pytest.approx(0.0) and cut[2] == pytest.approx(7200.0)
    kept = core.settle_step(-1000.0, 0.0, 7000.0, 500.0, 5500.0, 7200.0, 500.0, 1500.0, 1700.0, 1.0)
    assert kept[0] == pytest.approx(-1200.0)                                 # the real spill, 200 W more than planned


def test_virtual_day_and_settle_slice_honour_the_cut(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    i_cut = core._slot(local(2026, 9, 2, 12, 0))
    cuts = [False] * 96
    cuts[i_cut] = True
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=1.0, pv=[7000.0] * 96, load=[500.0] * 96,
                        p_batt=[0.0] * 96, grid=[-1000.0] * 96, pv_micro_w=[1500.0] * 96, micro_cut=cuts)
    doc = core.load_plan(core.list_plans(arch)[-1])
    # on the cut step the plan's own curtailment must hold the Growatt (as run_plan writes it)
    for k, r in enumerate(doc["rows"]):
        r["P_PV_curtailment"] = 7000.0 if cuts[k] else 5500.0
        if cuts[k]:
            r["P_grid"] = 0.0
    import gzip as _gzip, json as _json
    with _gzip.open(core.list_plans(arch)[-1], "wt") as f:
        _json.dump(doc, f)
    sl = core.compact_slice(core.day_slice(doc["rows"], day, AMS, 1.0), doc)
    assert sl["micro_cut_w"][i_cut] == 1500.0 and sum(sl["micro_cut_w"]) == 1500.0
    vd = core.virtual_day(arch, day, AMS, local(2026, 9, 3, 1, 0), [7200.0] * 96, [500.0] * 96, [0.0] * 96, None, [1700.0] * 96)
    assert vd["micro_cut_w"][i_cut] == 1500.0 and vd["micro_cut_w"][i_cut + 1] == 0.0
    assert vd["p_grid_w"][i_cut] == pytest.approx(0.0)                        # cut: no spill
    assert vd["p_grid_w"][i_cut + 1] == pytest.approx(-1200.0)                # not cut: the real spill
    settled = core.settle_slice(sl, [7200.0] * 96, [500.0] * 96, [1700.0] * 96)
    assert settled["p_grid_w"][i_cut] == pytest.approx(0.0) and settled["p_grid_w"][i_cut + 1] == pytest.approx(-1200.0)


def test_compact_slice_carries_the_must_take_share(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, pv_micro_w=[300.0] * 96)
    doc = core.load_plan(core.list_plans(arch)[-1])
    sl = core.compact_slice(core.day_slice(doc["rows"], day, AMS, 0.5), doc)
    assert sl["pv_micro_w"] == [300.0] * 96
    write_full_day_plan(arch, local(2026, 9, 1, 23, 30), day)      # no share archived: zeros
    doc = core.load_plan(core.list_plans(arch)[-1])
    assert core.compact_slice(core.day_slice(doc["rows"], day, AMS, 0.5), doc)["pv_micro_w"] == [0.0] * 96


def test_settle_slice_needs_matching_series():
    assert core.settle_slice({"n": 4}, [1.0], [1.0]) is None


def test_score_row_gap_is_replayed_minus_hindsight():
    plan = flat_plan()
    act = flat_actuals(plan, g_meas=2000.0)          # 2 kW imported all day on the real meter
    realised = {"cash_eur": 7.2, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": 40.0, "load_kwh": 10.0}
    hs = {"status": "ok", "eur": -5.0}               # the 20/20 ceiling
    row = core.score_row(date(2026, 9, 2), plan, realised, 48.0, 0.9, hindsight=hs, actuals=act)
    assert row["planned_eur"] == pytest.approx(2.832) and row["replay_status"] == "ok"
    # realised is STILL recorded (drift alarm) - re-settled in the plan's frame -
    # but it is no longer a verdict lane, so the gap does not touch it.
    assert row["realised_cash_eur"] == pytest.approx(14.4) and row["realised_cash_meter_eur"] == 7.2
    assert row["realised_eur"] == pytest.approx(14.4)
    assert row["hindsight_eur"] == -5.0
    assert row["gap_eur"] == pytest.approx(row["replayed_eur"] - (-5.0))        # actual - max
    assert row["forecast_gap_eur"] == pytest.approx(row["replayed_eur"] - row["planned_eur"])
    assert "meter_drift" in row["flags"]             # the meter said 7,20 where the frame says 14,40
    assert row["pv_planned_kwh"] == pytest.approx(48.0) and row["load_planned_kwh"] == pytest.approx(12.0)
    assert row["lambda_eur_kwh"] == pytest.approx(0.09)
    assert set(row) == set(core.SCORE_COLUMNS)


def test_score_row_no_gap_without_the_hindsight_ceiling():
    plan = flat_plan()
    act = flat_actuals(plan, g_meas=2000.0)
    realised = {"cash_eur": 7.2, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": 40.0, "load_kwh": 10.0}
    row = core.score_row(date(2026, 9, 2), plan, realised, 48.0, 0.9,
                         hindsight={"status": "no_actuals", "eur": None}, actuals=act)
    assert row["gap_eur"] is None and "no_hindsight" in row["flags"]
    assert row["replayed_eur"] is not None and row["realised_eur"] is not None    # both still recorded


def test_replay_is_a_difference_so_an_unmetered_residual_cancels():
    """The site does not balance; ~4,2 kWh/day of standby and conversion loss sits
    between (load - PV - battery) and the meter. Written as a difference, that
    residual moves both lanes by the same money and cannot open a gap."""
    plan = flat_plan()
    lam = core.lambda_for(plan["sell"], 0.9)
    a = flat_actuals(plan, g_meas=0.0, b_dc=1000.0)
    b = flat_actuals(plan, g_meas=400.0, b_dc=1000.0)          # 400 W neither meter explains
    ra = core.replay_day(plan, a["grid_w"], a["batt_dc_w"], a["pv_w"], 48.0, lam)
    rb = core.replay_day(plan, b["grid_w"], b["batt_dc_w"], b["pv_w"], 48.0, lam)
    real_a = core.cash_in_frame(a["grid_w"], plan["buy"], plan["sell"])
    real_b = core.cash_in_frame(b["grid_w"], plan["buy"], plan["sell"])
    assert rb["cash"] - ra["cash"] == pytest.approx(real_b - real_a, abs=1e-4)
    assert (real_b - rb["cash"]) == pytest.approx(real_a - ra["cash"], abs=1e-4)


def test_replay_takes_the_plans_battery_off_the_harvest_not_the_ceiling():
    """B_plan is read off the balance, not P_batt, so the 12 kW bridge-cap steps
    stay honest - but the balance has to use the sun the plan actually HARVESTED.
    p_pv_w is the ceiling, and on a curtailed step the difference overstates the
    commanded charge by exactly the declined watts."""
    n = 96
    plan = flat_plan(n=n, p_grid=0.0)
    lam = core.lambda_for(plan["sell"], 0.9)
    act = flat_actuals(plan, g_meas=0.0, b_dc=0.0)
    base = core.replay_day(plan, act["grid_w"], act["batt_dc_w"], act["pv_w"], 48.0, lam)
    curt = dict(plan, pv_curtail_w=[800.0] * n)
    rep = core.replay_day(curt, act["grid_w"], act["batt_dc_w"], act["pv_w"], 48.0, lam)
    # B_plan = load - (pv - curtail) - grid, so declining 800 W makes the
    # commanded charge 800 W SMALLER, and the replayed grid follows it down
    assert base["peak_import_kw"] == pytest.approx(1.5)        # 500 - 2000 - 0 = -1500
    assert rep["peak_import_kw"] == pytest.approx(0.7)         # 500 - 1200 - 0 = -700
    # a smaller commanded charge is less power to buy, so the lane gets CHEAPER:
    # the old form was charging the shadow EMS for sun it had already declined
    assert rep["cash"] < base["cash"]


def test_replay_of_a_settled_slice_takes_the_settled_battery():
    """settle_slice rewrites the battery, the grid and the SOC but keeps the
    FORECAST PV and load in p_pv_w / p_load_w, so the row balance no longer
    describes the settled trajectory. A house that executes the settled plan
    to the watt must replay to exactly the settled grid's cash (found on
    2026-09-04: 11:15 self-balanced to +165 W on the real deficit, the balance
    still said -3.658 W, and the lane was billed 3,7 kW of import at 0,114)."""
    n = 8
    plan = flat_plan(n=n, p_grid=0.0, soc_start=50.0, soc_end=50.0)
    plan.update(p_batt_w=[-1500.0] * n, pv_curtail_w=[0.0] * n)     # self-balance: 2.000 PV - 500 load
    out = core.settle_slice(plan, [1000.0] * n, [1500.0] * n)         # the real day: a 500 W deficit
    assert out["p_batt_w"][0] == pytest.approx(500.0, abs=1.0) and out["p_grid_w"][0] == pytest.approx(0.0, abs=1.0)
    lam = core.lambda_for(plan["sell"], 0.9)
    b_dc = [b / core.ETA_BRIDGE if b >= 0 else b * core.ETA_BRIDGE for b in out["p_batt_w"]]
    rep = core.replay_day(out, out["p_grid_w"], b_dc, [1000.0] * n, 48.0, lam)
    assert rep["cash"] == pytest.approx(core.cash_in_frame(out["p_grid_w"], out["buy"], out["sell"]), abs=1e-3)
    assert rep["peak_import_kw"] == pytest.approx(0.0, abs=0.01)


def test_replay_crosses_the_bridge_asymmetrically():
    """1 kW leaving the pack lands as 989 W on the AC bus; 1 kW entering it costs
    1011 W off the bus. Only that one term survives the difference."""
    plan = flat_plan()
    lam = core.lambda_for(plan["sell"], 0.9)
    n = plan["n"]
    dis = core.replay_day(plan, [0.0] * n, [1000.0] * n, [2000.0] * n, 48.0, lam)
    chg = core.replay_day(plan, [0.0] * n, [-1000.0] * n, [2000.0] * n, 48.0, lam)
    # B_plan is -2500 W in the importing half, -500 W in the exporting half
    assert dis["peak_import_kw"] == pytest.approx(3.49)        # 2500 + 989
    assert chg["peak_import_kw"] == pytest.approx(1.49)        # 2500 - 1011
    assert chg["peak_export_kw"] == pytest.approx(0.51)        # 500 - 1011
    assert dis["status"] == "ok" and dis["soc_term"] == pytest.approx(0.432)


def test_replay_flags_a_trace_that_asks_too_much_of_the_connection():
    plan = flat_plan()
    lam = core.lambda_for(plan["sell"], 0.9)
    n = plan["n"]
    rep = core.replay_day(plan, [15000.0] * n, [0.0] * n, [2000.0] * n, 48.0, lam)
    assert rep["status"] == "caps" and rep["cap_steps"] == 48   # 17.500 W in the importing half only
    assert rep["eur"] is not None                               # scored anyway, but flagged
    assert core.replay_day(plan, None, None, None, 48.0, lam)["status"] == "no_actuals"
    short = core.replay_day(plan, [0.0] * (n - 1), [0.0] * n, [2000.0] * n, 48.0, lam)
    assert short["status"] == "no_actuals" and short["eur"] is None


def test_score_row_flags():
    plan = flat_plan()
    act = flat_actuals(plan, g_meas=1000.0)          # frame cash 7,20, near enough the 3,00 meter? no
    realised = {"cash_eur": 7.2, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": None, "load_kwh": None}
    row = core.score_row(date(2026, 9, 2), flat_plan(n_predicted_steps=12, pv_gap_steps=2), realised, 48.0, 0.9,
                         actuals=act)
    assert row["flags"] == "predicted_prices;pv_gaps"
    row = core.score_row(date(2026, 9, 2), None, realised, 48.0, 0.9, actuals=act)
    assert row["flags"] == "no_plan" and row["gap_eur"] is None and row["realised_cash_eur"] == 7.2
    row = core.score_row(date(2026, 9, 2), plan, dict(realised, cash_eur=None), 48.0, 0.9)
    assert "no_meter" in row["flags"] and row["gap_eur"] is None
    row = core.score_row(date(2026, 9, 2), plan, realised, 48.0, 0.9)          # no measured series
    assert "no_replay" in row["flags"] and row["gap_eur"] is None
    assert row["replay_status"] == "no_actuals" and row["realised_eur"] == pytest.approx(7.2)
    row = core.score_row(date(2026, 9, 2), plan, dict(realised, soc_end_pct=None), 48.0, 0.9, actuals=act)
    assert "no_soc" in row["flags"] and row["gap_eur"] is None


def test_csv_upsert_replaces_a_day_and_keeps_order(tmp_path):
    p = str(tmp_path / "scores.csv")
    plan = flat_plan()
    act = flat_actuals(plan)
    realised = {"cash_eur": 3.0, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": 1.0, "load_kwh": 2.0}
    hs = {"status": "ok", "eur": -1.0}
    core.upsert_score(p, core.score_row(date(2026, 9, 3), flat_plan(date="2026-09-03"), realised, 48.0, 0.9, hs, act))
    core.upsert_score(p, core.score_row(date(2026, 9, 2), plan, realised, 48.0, 0.9, hs, act))
    rows = core.upsert_score(p, core.score_row(date(2026, 9, 3), flat_plan(date="2026-09-03"),
                                               dict(realised, cash_eur=4.0), 48.0, 0.9, hs, act))
    assert [r["date"] for r in rows] == ["2026-09-02", "2026-09-03"]
    assert rows[1]["realised_cash_meter_eur"] == 4.0
    assert rows[1]["gap_eur"] == pytest.approx(rows[1]["replayed_eur"] - (-1.0))
    back = core.read_scores(p)
    assert back == rows and back[0]["n_predicted_steps"] == 0 and back[0]["replay_status"] == "ok"


def test_rolling_window_and_compact_lists():
    rows = []
    for k in range(35):
        d = (date(2026, 9, 1) + timedelta(days=k)).isoformat()
        rows.append({c: None for c in core.SCORE_COLUMNS} | {"date": d, "planned_eur": 2.0, "replayed_eur": 2.5,
                                                             "hindsight_eur": 2.0, "realised_eur": 3.0, "gap_eur": 0.5,
                                                             "flags": "pv_gaps" if k == 34 else ""})
    r = core.rolling(rows)
    assert r["gap_30d"] == 15.0 and r["n_days"] == 30 and r["planned_30d"] == 60.0
    assert r["replayed_30d"] == 75.0 and r["hindsight_30d"] == 60.0
    assert r["n_flagged"] == 1 and len(r["recent"]) == 7 and len(r["days"]) == 30
    # the table is actual, max, gap: replayed, hindsight, replayed-minus-hindsight
    assert r["recent"][-1] == ["2026-10-05", 2.5, 2.0, 0.5, "pv_gaps"] and r["days"][0] == ["2026-09-06", 2.5, 2.0, 0.5]
    short = core.rolling(rows[:3])
    assert short["gap_30d"] == 1.5 and short["n_days"] == 3 and len(short["recent"]) == 3
    assert core.rolling([])["n_days"] == 0
    # a row missing a verdict lane (no hindsight ceiling, or too old for actuals)
    # is not comparable and drops out until the day is re-scored
    stale = [dict(rows[0], hindsight_eur=None)]
    assert core.rolling(stale)["n_days"] == 0 and core.rolling(stale)["gap_30d"] == 0


def test_score_day_reads_the_archive_and_rescoring_keeps_realised(tmp_path):
    arch, csvp = str(tmp_path / "plans"), str(tmp_path / "scores.csv")
    made = local(2026, 9, 1, 13, 0, 30)
    t0, n = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS,
                                         "soc_init": 0.5, "soc_final": 0.5, "optim_status": "Optimal",
                                         "n_predicted_steps": 0, "pv_gap_steps": 0, "rows": make_rows(t0, n)})
    realised = {"cash_eur": 3.0, "soc_start_pct": 50.0, "soc_end_pct": 45.0, "pv_kwh": 40.0, "load_kwh": 10.0}
    hs = {"status": "ok", "eur": -1.0}
    res = core.score_day(arch, csvp, "2026-09-02", AMS, realised, 48.0, 0.9, hs, DAY_ACTUALS)
    row = res["row"]
    # the 3,00 meter figure against a zero measured grid trace: the drift alarm
    assert row["flags"] == "meter_drift" and row["plan_ts"] == made.isoformat()
    assert res["rolling"]["n_days"] == 1
    assert row["replay_status"] == "ok" and row["replayed_eur"] is not None
    assert row["gap_eur"] == pytest.approx(row["replayed_eur"] - (-1.0))
    # 13:00:30 ceils to 13:15, so 1 Sep has 43 steps (indices 0..42); the day
    # starts at the SOC after index 42
    assert row["soc_start_plan_pct"] == pytest.approx(100 * (0.5 - 0.001 * 42))
    empty = {"cash_eur": None, "soc_start_pct": None, "soc_end_pct": None, "pv_kwh": None, "load_kwh": None}
    # a rescore with no fresh hindsight keeps the stored ceiling, so the gap holds
    again = core.score_day(arch, csvp, "2026-09-02", AMS, empty, 48.0, 0.9, None, DAY_ACTUALS)["row"]
    assert again["realised_cash_meter_eur"] == 3.0 and again["gap_eur"] == row["gap_eur"]
    missing = core.score_day(arch, csvp, "2026-09-05", AMS, realised, 48.0, 0.9, hs, DAY_ACTUALS)["row"]
    assert missing["flags"] == "no_plan"


def test_rehydrate_rebuilds_from_files(tmp_path):
    arch, csvp = str(tmp_path / "plans"), str(tmp_path / "scores.csv")
    made = local(2026, 9, 1, 13, 0, 30)
    t0, n = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS,
                                         "soc_init": 0.5, "soc_final": 0.5, "optim_status": "Optimal",
                                         "n_predicted_steps": 0, "pv_gap_steps": 0, "rows": make_rows(t0, n)})
    realised = {"cash_eur": 3.0, "soc_start_pct": 50.0, "soc_end_pct": 45.0, "pv_kwh": 40.0, "load_kwh": 10.0}
    core.score_day(arch, csvp, "2026-09-02", AMS, realised, 48.0, 0.9, {"status": "ok", "eur": -1.0}, DAY_ACTUALS)
    r = core.rehydrate(arch, csvp, AMS, local(2026, 9, 2, 9, 0).isoformat(), 26)
    assert r["today"]["date"] == "2026-09-02" and r["next_day"] is None
    assert r["rolling"]["n_days"] == 1 and r["last_row"]["date"] == "2026-09-02" and r["plan_fresh"] is True
    assert core.rehydrate(arch, csvp, AMS, local(2026, 9, 3, 9, 0).isoformat(), 26)["plan_fresh"] is False
    assert core.rehydrate(str(tmp_path / "none"), str(tmp_path / "none.csv"), AMS, local(2026, 9, 3, 9, 0).isoformat(), 26)["rolling"] is None


# ---- virtual SOC chaining ------------------------------------------------------

def write_plan(arch, made, status="Optimal", soc0=0.5):
    t0, n = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n,
                                         "tz": AMS, "soc_init": soc0, "soc_final": soc0,
                                         "optim_status": status, "n_predicted_steps": 0, "pv_gap_steps": 0,
                                         "rows": make_rows(t0, n, status=status, soc0=soc0)})
    return t0, n


def test_virtual_soc_at_start_mid_and_uncovered(tmp_path):
    arch = str(tmp_path / "plans")
    made = local(2026, 9, 2, 13, 2, 9)
    t0, n = write_plan(arch, made)
    soc, src = core.virtual_soc_at(arch, t0)                       # t0 is the plan's first step
    assert soc == pytest.approx(0.5) and src == made.isoformat()
    later = core.step_times(t0, 5)[4]                              # 4 whole steps in: SOC after step 3
    soc, _ = core.virtual_soc_at(arch, later)
    assert soc == pytest.approx(0.5 - 0.001 * 3)
    beyond = core.step_times(t0, n + 4)[n + 3]                     # past the horizon: uncovered
    assert core.virtual_soc_at(arch, beyond) == (None, None)
    assert core.virtual_soc_at(str(tmp_path / "none"), t0) == (None, None)


def test_archive_is_gzipped_on_write_and_legacy_json_still_loads(tmp_path):
    import gzip as _gzip, json as _json, os as _os
    arch = str(tmp_path / "plans")
    made = local(2026, 9, 2, 13, 0, 30)
    path = core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "optim_status": "Optimal", "rows": []})
    assert path.endswith("20260902T130030.json.gz")
    with _gzip.open(path, "rt") as f:                              # a real gzip member, not a renamed file
        assert _json.load(f)["plan_ts"] == made.isoformat()
    assert core.load_plan(path)["optim_status"] == "Optimal"
    legacy = _os.path.join(arch, "20260902T120000.json")            # a pre-gzip document
    with open(legacy, "w") as f:
        _json.dump({"plan_ts": local(2026, 9, 2, 12, 0).isoformat(), "optim_status": "Optimal", "rows": []}, f)
    later = core.write_plan_archive(arch, local(2026, 9, 2, 14, 0), {"plan_ts": local(2026, 9, 2, 14, 0).isoformat(),
                                                                       "optim_status": "Optimal", "rows": []})
    # ordered by stem across both suffixes, and both readable
    assert [core._plan_stem(_os.path.basename(p)) for p in core.list_plans(arch)] == [
        "20260902T120000", "20260902T130030", "20260902T140000"]
    assert [d["plan_ts"][11:13] for d in core.organic_plans(arch)] == ["14", "13", "12"]
    assert core.load_plan(legacy)["plan_ts"].startswith("2026-09-02T12")
    assert _os.path.getsize(path) < 200 and not _os.path.exists(path + ".tmp")


def test_organic_plans_since_stops_at_the_bound_without_opening_older_docs(tmp_path, monkeypatch):
    arch = str(tmp_path / "plans")
    for h in (9, 11, 13, 15):
        write_plan(arch, local(2026, 9, 2, h, 0, 30))
    core.plan_heads(arch)                                          # warm the head cache: one read per new file, once
    opened = []
    real = core.load_plan
    monkeypatch.setattr(core.archive, "load_plan", lambda path: opened.append(path) or real(path))
    got = core.organic_plans(arch, since=local(2026, 9, 2, 12, 0))
    assert [d["plan_ts"] for d in got] == [local(2026, 9, 2, 15, 0, 30).isoformat(),
                                          local(2026, 9, 2, 13, 0, 30).isoformat()]
    assert len(opened) == 2                                        # the two older docs were never read
    assert len(core.organic_plans(arch)) == 4                      # unbounded still returns everything
    opened.clear()
    later = list(core.iter_organic_plans(arch, until=local(2026, 9, 2, 12, 0)))
    assert [d["plan_ts"][11:13] for d in later] == ["11", "09"] and len(opened) == 2


def test_virtual_day_opens_only_the_plans_that_can_be_in_force(tmp_path, monkeypatch):
    arch = str(tmp_path / "plans")
    # three days of hourly plans; the 09-03 reconstruction needs the 09-03 plans up
    # to `now` plus the single newest 09-02 plan, and nothing from 09-01 or 09-04
    for d in (1, 2, 3, 4):
        for h in range(0, 24, 6):
            write_plan(arch, local(2026, 9, d, h, 5, 0))
    core.plan_heads(arch)                                          # warm the head cache: one read per new file, once
    opened = []
    real = core.load_plan
    monkeypatch.setattr(core.archive, "load_plan", lambda path: opened.append(path) or real(path))
    now = local(2026, 9, 3, 14, 0)
    vd = core.virtual_day(arch, date(2026, 9, 3), AMS, now)
    assert vd is not None and vd["n"] == 96 and all(v is not None for v in vd["soc_pct"])
    stamps = sorted(core.load_plan(p)["plan_ts"][:13] for p in set(opened))
    assert stamps == ["2026-09-02T18", "2026-09-03T00", "2026-09-03T06", "2026-09-03T12"]
    # the chain: 00:00 from the 09-02 18:05 plan, 06:00 onward from the day's own plans
    assert vd["sources"] == [local(2026, 9, 3, 12, 5).isoformat(), local(2026, 9, 3, 6, 5).isoformat(),
                             local(2026, 9, 3, 0, 5).isoformat(), local(2026, 9, 2, 18, 5).isoformat()]


def test_days_since_full_opens_documents_lazily(tmp_path, monkeypatch):
    arch = str(tmp_path / "plans")
    for d in range(1, 8):
        for h in (0, 12):
            write_plan(arch, local(2026, 9, d, h, 5, 0))
    core.plan_heads(arch)                                          # warm the head cache: one read per new file, once
    opened = []
    real = core.load_plan
    monkeypatch.setattr(core.archive, "load_plan", lambda path: opened.append(path) or real(path))
    assert core.days_since_full(arch, local(2026, 9, 7, 14, 0), AMS) is None      # make_rows never reaches 99,5 %
    # one document per day decided (today plus each of the six days back), not the fourteen archived
    assert len(set(opened)) <= 8


def test_plan_selectors_stop_at_the_reach_bound(tmp_path, monkeypatch):
    arch = str(tmp_path / "plans")
    for d in range(1, 9):
        write_plan(arch, local(2026, 9, d, 23, 5, 0))                  # one pre-midnight plan a day
    core.plan_heads(arch)                                          # warm the head cache: one read per new file, once
    opened = []
    real = core.load_plan
    monkeypatch.setattr(core.archive, "load_plan", lambda path: opened.append(path) or real(path))
    # a day nothing covers: the walk opens only the plans within ARCHIVE_REACH of it, not the whole archive
    assert core.plan_for_day(arch, date(2026, 9, 20), AMS) is None
    assert opened == []                                            # every head is older than the reach
    assert core.newest_plan_for_day(arch, date(2026, 9, 20), AMS) is None and opened == []
    assert core.plan_for_day(arch, date(2026, 9, 10), AMS) is None
    # 09-10 midnight less three days is 09-07 00:00: the 09-08 and 09-07 evening plans
    # are candidates (neither covers 09-10), the six older ones are never opened
    assert {core.load_plan(p)["plan_ts"][:10] for p in opened} == {"2026-09-08", "2026-09-07"}
    opened.clear()
    # a covered day still resolves, from the plan made the evening before
    assert core.plan_for_day(arch, date(2026, 9, 5), AMS)[0]["plan_ts"].startswith("2026-09-04T23")
    assert core.newest_plan_for_day(arch, date(2026, 9, 5), AMS)[0]["plan_ts"].startswith("2026-09-04T23")


def test_virtual_soc_at_skips_non_optimal_and_prefers_the_newest_covering_plan(tmp_path):
    arch = str(tmp_path / "plans")
    write_plan(arch, local(2026, 9, 2, 13, 2, 9), soc0=0.5)
    write_plan(arch, local(2026, 9, 2, 15, 0, 30), soc0=0.8)       # newer Optimal
    t_bad, _ = write_plan(arch, local(2026, 9, 2, 16, 0, 30), status="Infeasible", soc0=0.2)
    probe = local(2026, 9, 2, 18, 0)
    soc, src = core.virtual_soc_at(arch, probe)
    assert src == local(2026, 9, 2, 15, 0, 30).isoformat()         # newest Optimal wins, Infeasible skipped
    steps = round((probe.astimezone(timezone.utc)
                   - local(2026, 9, 2, 15, 15).astimezone(timezone.utc)).total_seconds() / 900)
    assert soc == pytest.approx(0.8 - 0.001 * (steps - 1))


def test_run_plan_records_soc_source_and_chains_end_to_end(stub, tmp_path):
    now1 = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0_1, n1 = core.horizon(now1, AMS)
    stub.plan_fn = lambda posts: make_rows(t0_1, n1, soc0=0.8)
    inp = plan_inputs(now1, tmp_path, stub.url)
    res1 = core.run_plan(inp)
    assert res1["ok"] and res1["soc_source"] == "real"
    assert core.load_plan(res1["archive_path"])["soc_source"] == "real"
    now2 = now1.replace(hour=15)                                    # one hour later, chained
    t0_2, n2 = core.horizon(now2, AMS)
    v_soc, v_src = core.virtual_soc_at(str(tmp_path / "plans"), t0_2)
    assert v_src == now1.isoformat()
    assert v_soc == pytest.approx(0.8 - 0.001 * 3)                  # 15:00 step ends at t0_2 = 15:15
    stub.plan_fn = lambda posts: make_rows(t0_2, n2, soc0=v_soc)
    inp2 = plan_inputs(now2, tmp_path, stub.url)
    inp2["soc_pct"] = v_soc * 100
    inp2["soc_source"] = "virtual"
    res2 = core.run_plan(inp2)
    assert res2["ok"] and res2["soc_source"] == "virtual"
    assert stub.posts[-1][1]["soc_init"] == pytest.approx(round(v_soc, 4))
    assert res2["next_day"]["soc_source"] == "virtual"


# ---- virtual_day and newest_plan_for_day ---------------------------------------

def write_full_day_plan(arch, made, day, soc0=0.5, p_batt=None, pv=None, load=None,
                        buy=None, sell=None, status="Optimal", grid=None, pv_micro_w=None, micro_cut=None):
    """A plan archived at `made` whose rows cover exactly one calendar day
    (local midnight to midnight). Real plans never start exactly at midnight
    unless made right before it; building the rows directly like this isolates
    virtual_day's plan-in-force selection and SOC integration from horizon()'s
    own start-time rules."""
    t0 = local(day.year, day.month, day.day, 0, 0)
    n = core.expected_steps(day, AMS)
    rows = make_rows(t0, n, buy=buy, sell=sell, pv=pv, status=status, soc0=soc0)
    if p_batt is not None:
        for r, pb in zip(rows, p_batt):
            r["P_batt"] = pb
    if load is not None:
        for r, ld in zip(rows, load):
            r["P_Load"] = ld
    if grid is not None:
        for r, g in zip(rows, grid):
            r["P_grid"] = g
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n,
                                         "tz": AMS, "soc_init": soc0, "soc_final": soc0,
                                         "optim_status": status, "n_predicted_steps": 0,
                                         "pv_gap_steps": 0, "pv_micro_w": pv_micro_w,
                                         "micro_cut": micro_cut, "rows": rows})
    return t0, n


def test_virtual_day_picks_the_plan_in_force_at_each_step(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 2, 13, 0), day, p_batt=[100.0] * 96)
    write_full_day_plan(arch, local(2026, 9, 2, 15, 0), day, p_batt=[200.0] * 96)
    now = local(2026, 9, 2, 20, 0)             # both plans are already in the past
    vd = core.virtual_day(arch, day, AMS, now)
    assert vd is not None
    i14 = core._slot(local(2026, 9, 2, 14, 0))
    i16 = core._slot(local(2026, 9, 2, 16, 0))
    assert vd["p_batt_w"][i14] == pytest.approx(100.0)     # 14:00 < 15:00: the 13:00 plan is in force
    assert vd["p_batt_w"][i16] == pytest.approx(200.0)     # 16:00 >= 15:00: the 15:00 plan takes over
    assert vd["sources"] == [local(2026, 9, 2, 15, 0).isoformat(), local(2026, 9, 2, 13, 0).isoformat()]


def test_virtual_day_future_steps_use_the_newest_plan_available_at_now(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 2, 10, 0), day, p_batt=[50.0] * 96)
    write_full_day_plan(arch, local(2026, 9, 2, 12, 0), day, p_batt=[300.0] * 96)
    # archived with a plan_ts later than `now`: must never win a future step
    # just because it sits chronologically closer to that step than `now` does
    write_full_day_plan(arch, local(2026, 9, 2, 18, 0), day, p_batt=[999.0] * 96)
    now = local(2026, 9, 2, 14, 0)
    vd = core.virtual_day(arch, day, AMS, now)
    i15 = core._slot(local(2026, 9, 2, 15, 0))
    i20 = core._slot(local(2026, 9, 2, 20, 0))
    assert vd["p_batt_w"][i15] == pytest.approx(300.0)
    assert vd["p_batt_w"][i20] == pytest.approx(300.0)
    assert local(2026, 9, 2, 18, 0).isoformat() not in vd["sources"]


def test_virtual_day_skips_a_non_optimal_plan(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 2, 10, 0), day, p_batt=[50.0] * 96)
    write_full_day_plan(arch, local(2026, 9, 2, 15, 0), day, p_batt=[999.0] * 96, status="Infeasible")
    now = local(2026, 9, 2, 20, 0)
    vd = core.virtual_day(arch, day, AMS, now)
    i16 = core._slot(local(2026, 9, 2, 16, 0))
    assert vd["p_batt_w"][i16] == pytest.approx(50.0)      # the 15:00 Infeasible plan never takes over
    assert local(2026, 9, 2, 15, 0).isoformat() not in vd["sources"]


def test_virtual_day_integrates_soc_for_a_constant_charge(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    p, eta_c, capacity = -3000.0, 0.961, 48.2              # charging at 3 kW
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.5, p_batt=[p] * 96)
    now = local(2026, 9, 2, 23, 45)
    vd = core.virtual_day(arch, day, AMS, now, capacity_kwh=capacity, eta_c=eta_c)
    for k in range(1, 6):
        want = 0.5 + (-p / 1000.0) * core.STEP_H * eta_c * k / capacity
        assert vd["soc_pct"][k - 1] == pytest.approx(round(want * 100, 2))
    # matching the closed-form growth for these first 5 steps already proves
    # none of them clamped (a clamp would have broken the exact-formula match)


def test_virtual_day_integrates_soc_for_a_constant_discharge(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    p, eta_d, capacity = 2500.0, 0.957, 48.2                # discharging at 2,5 kW
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.8, p_batt=[p] * 96)
    now = local(2026, 9, 2, 23, 45)
    vd = core.virtual_day(arch, day, AMS, now, capacity_kwh=capacity, eta_d=eta_d)
    for k in range(1, 6):
        want = 0.8 - (p / 1000.0) * core.STEP_H / eta_d * k / capacity
        assert vd["soc_pct"][k - 1] == pytest.approx(round(want * 100, 2))
    # matching the closed-form drain for these first 5 steps already proves
    # none of them clamped (a clamp would have broken the exact-formula match)


def test_virtual_day_lands_on_soc_max_and_books_the_rest_elsewhere(tmp_path):
    """A plan charging 6 kW into a pack at 99 %: the first step takes exactly the
    1 % that fits, every later step charges nothing, and no step is clamped
    (2026-09-08: the settled flows respect the pack, so integrate_soc never has
    to throw energy away)."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.99, p_batt=[-6000.0] * 96)
    now = local(2026, 9, 2, 23, 45)
    vd = core.virtual_day(arch, day, AMS, now)
    assert vd["soc_pct"][0] == pytest.approx(100.0, abs=0.01)
    assert vd["soc_pct"][-1] == pytest.approx(100.0, abs=0.01)   # at the ceiling, not pinned there
    assert vd["clamped_steps"] == 0
    assert vd["p_batt_w"][0] == pytest.approx(-0.01 * core.CAPACITY_KWH * 1000 / core.STEP_H / core.ETA_C, abs=1.0)
    assert vd["p_batt_w"][1] == pytest.approx(0.0, abs=1.0)
    # the 6 kW the pack could not take went to the meter or the strings, not nowhere
    assert vd["p_grid_w"][1] <= -6000.0 + 1.0 or vd["pv_curtail_w"][1] >= 6000.0 - 1.0


def test_virtual_day_grid_uses_actuals_in_the_past_and_forecast_in_the_future(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    pv_plan, load_plan, p_batt = [1000.0] * 96, [500.0] * 96, [200.0] * 96
    # P_grid is deliberately NOT load - pv - P_batt (-700): a real plan's P_grid
    # carries EMHASS's conversion loss on top, so the 50 W offset here is what
    # separates the delta form from a from-scratch balance. Both formulas agree
    # when P_grid happens to equal the naive balance, which is why this fixture
    # sets it explicitly.
    grid_plan = [-650.0] * 96
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, pv=pv_plan, load=load_plan,
                        p_batt=p_batt, grid=grid_plan)
    now = local(2026, 9, 2, 12, 0)
    actual_pv, actual_load = [1234.0] * 96, [777.0] * 96
    vd = core.virtual_day(arch, day, AMS, now, actual_pv_w=actual_pv, actual_load_w=actual_load)
    i_past = core._slot(local(2026, 9, 2, 6, 0))
    i_future = core._slot(local(2026, 9, 2, 18, 0))
    # the plan's own grid, moved by the load error and against the PV error
    assert vd["p_grid_w"][i_past] == pytest.approx(-650.0 + (777.0 - 500.0) - (1234.0 - 1000.0))
    assert vd["p_pv_w"][i_past] == pytest.approx(1234.0) and vd["p_load_w"][i_past] == pytest.approx(777.0)
    # a future step has both deltas at zero, so it is the plan's P_grid exactly
    assert vd["p_grid_w"][i_future] == pytest.approx(-650.0)                     # actuals ignored
    assert vd["p_pv_w"][i_future] == pytest.approx(1000.0) and vd["p_load_w"][i_future] == pytest.approx(500.0)
    assert vd["actuals_used"] is True
    # the plan's OWN forecast survives beside the substituted lane, so the
    # forecast-against-actual chart still has something to compare against
    assert vd["pv_fc_w"][i_past] == pytest.approx(1000.0)
    assert vd["load_fc_w"][i_past] == pytest.approx(500.0)
    assert vd["pv_fc_w"][i_future] == pytest.approx(1000.0)
    t0 = local(2026, 9, 2, 0, 0)
    want_n_past = sum(1 for t in core.step_times(t0, vd["n"]) if t < now)
    assert vd["n_past"] == want_n_past


def _curtailed_day(tmp_path, now, actual_pv, mask, plan_pv=1000.0, peak=None,
                   micro_meas=None, micro_plan=None):
    """One full-day plan on flat forecasts, with a measured PV trace and a
    curtailment mask supplied. Load is measured exactly as forecast, so every
    move in the virtual grid comes from the PV lane alone."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day,
                        pv=[plan_pv] * 96, load=[500.0] * 96,
                        p_batt=[200.0] * 96, grid=[-650.0] * 96,
                        pv_micro_w=micro_plan)
    return core.virtual_day(arch, day, AMS, now, actual_pv_w=actual_pv,
                            actual_load_w=[500.0] * 96, curtailed=mask, pv_peak_w=peak,
                            actual_micro_w=micro_meas)


def test_virtual_day_repairs_only_the_curtailable_half(tmp_path):
    """The Growatt is never throttled, so a curtailed step must not credit it
    with sun it was already making. Repair runs on main alone and the measured
    must-take half is added back untouched.

    Plan: 1.000 W total, 300 W of it the Growatt, so main forecast is 700.
    Measurement at the curtailed step: 250 W total, 300 W... the Growatt alone
    keeps producing while the strings are cut to zero. Potential must come out
    at 700 + 300 = 1.000, NOT at 1.000 scaled off a 250/1.000 ratio."""
    i_curt = core._slot(local(2026, 9, 2, 6, 0))
    actual = [1000.0] * 96
    actual[i_curt] = 300.0                       # strings cut, Growatt still running
    micro = [300.0] * 96
    mask = [0.0] * 96
    mask[i_curt] = 1.0
    vd = _curtailed_day(tmp_path, local(2026, 9, 2, 12, 0), actual, mask,
                        micro_meas=micro, micro_plan=[300.0] * 96)
    assert vd["pv_meas_w"][i_curt] == pytest.approx(300.0)      # metered truth, unchanged
    assert vd["p_pv_w"][i_curt] == pytest.approx(1000.0)        # 700 repaired + 300 must-take
    # only the curtailable half was reconstructed
    assert vd["pv_reconstructed_kwh"] == pytest.approx(0.7 * core.STEP_H, abs=1e-6)


def test_virtual_day_pre_split_plan_uses_the_measured_share_on_both_sides(tmp_path):
    """A plan archived before the split has no pv_micro_w. Feeding its zeros as
    the forecast share would fit the scale on main measured against a gross
    forecast - a mixed basis, and a different one from score_row's. The slice
    and the scoreboard must fit the SAME scale on the same day."""
    i_curt = core._slot(local(2026, 9, 2, 6, 0))
    actual = [1000.0] * 96
    actual[i_curt] = 300.0
    micro = [300.0] * 96
    mask = [0.0] * 96
    mask[i_curt] = 1.0
    pre = _curtailed_day(tmp_path, local(2026, 9, 2, 12, 0), actual, mask,
                         micro_meas=micro, micro_plan=None)          # pre-split plan
    direct, _ = core.pv_potential(actual, [1000.0] * 96, mask, micro_meas_w=micro)
    assert pre["p_pv_w"][i_curt] == pytest.approx(direct[i_curt])


def test_virtual_day_without_a_micro_series_repairs_the_whole_array(tmp_path):
    """Pre-split plans carry no pv_micro_w and no micro measurement; they must
    keep behaving exactly as before rather than silently changing basis."""
    i_curt = core._slot(local(2026, 9, 2, 6, 0))
    actual = [800.0] * 96
    actual[i_curt] = 200.0
    mask = [0.0] * 96
    mask[i_curt] = 1.0
    vd = _curtailed_day(tmp_path, local(2026, 9, 2, 12, 0), actual, mask)
    assert vd["p_pv_w"][i_curt] == pytest.approx(1000.0)
    assert vd["pv_reconstructed_kwh"] == pytest.approx(0.8 * core.STEP_H, abs=1e-6)


def test_virtual_day_substitutes_potential_pv_over_a_curtailed_step(tmp_path):
    """The display slice runs on the same PV basis as the replay lane: a step the
    real pack curtailed is credited with the sun it could have had, so the charts
    and the scoreboard cannot disagree about a curtailed day."""
    i_curt = core._slot(local(2026, 9, 2, 6, 0))
    i_clean = core._slot(local(2026, 9, 2, 7, 0))
    actual = [800.0] * 96
    actual[i_curt] = 200.0
    mask = [0.0] * 96
    mask[i_curt] = 1.0
    vd = _curtailed_day(tmp_path, local(2026, 9, 2, 12, 0), actual, mask)
    # the curtailed step is repaired back to the forecast, so its grid is the
    # plan's own: the shadow EMS is not charged for a cut it never made
    assert vd["pv_meas_w"][i_curt] == pytest.approx(200.0)
    assert vd["p_pv_w"][i_curt] == pytest.approx(1000.0)
    assert vd["p_grid_w"][i_curt] == pytest.approx(-650.0)
    # an uncurtailed shortfall is still a shortfall and still moves the grid
    assert vd["p_pv_w"][i_clean] == pytest.approx(800.0)
    assert vd["p_grid_w"][i_clean] == pytest.approx(-650.0 - (800.0 - 1000.0))
    assert vd["pv_repaired_steps"] == 1
    assert vd["pv_reconstructed_kwh"] == pytest.approx(0.8 * core.STEP_H, abs=1e-6)


def _with_percentiles(rows, p10=0.5, p90=1.3):
    return [{**r, "pv_estimate10": r["pv_estimate"] * p10, "pv_estimate90": r["pv_estimate"] * p90} for r in rows]


def test_pv_series_reads_a_percentile_and_falls_back_to_p50():
    t0 = local(2026, 9, 2, 0, 0)
    rows = _with_percentiles(solcast(t0))
    del rows[4]["pv_estimate10"]                                   # one half-hour without the percentile
    p50, _ = core.pv_series(t0, 12, rows, [])
    p10, gaps = core.pv_series(t0, 12, rows, [], field="pv_estimate10")
    assert gaps == 0
    assert p10[:8] == [pytest.approx(v * 0.5) for v in p50[:8]]
    assert p10[8:10] == p50[8:10]                                  # 02:00-02:30 falls back to P50, not to 0
    assert p10[10:] == [pytest.approx(v * 0.5) for v in p50[10:]]


def test_pv_mix_arithmetic_and_clamp():
    assert core.pv_mix([1000.0, 2000.0], [500.0, 1000.0], 0.2) == [900.0, 1800.0]
    assert core.pv_mix([1000.0], [500.0], 0.0) == [1000.0]
    assert core.pv_mix([1000.0], [500.0], 1.7) == [500.0]          # clamped to P10
    assert core.pv_mix([1000.0], [500.0], -3) == [1000.0]


def test_run_plan_feeds_the_mix_and_archives_the_raw_percentiles(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url)
    inp["solcast_today"] = _with_percentiles(inp["solcast_today"])
    inp["solcast_tomorrow"] = _with_percentiles(inp["solcast_tomorrow"])
    inp["knobs"] = {"pv_p10_mix": 0.2}
    p50, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    res = core.run_plan(inp)
    assert res["ok"], res
    body = stub.posts[0][1]
    assert body["pv_power_forecast"] == [pytest.approx(0.9 * v, abs=0.06) for v in p50]   # 0,8 P50 + 0,2 x 0,5 P50
    doc = core.load_plan(res["archive_path"])
    assert doc["pv_p50_w"] == p50 and doc["pv_p10_mix"] == 0.2
    assert doc["pv_p10_w"] == [pytest.approx(0.5 * v, abs=0.06) for v in p50]
    assert "pv_p90_w" not in doc                                   # computed for nothing until 2026-09-07; dropped
    assert res["pv_p10_mix"] == 0.2 and res["pv_p50_kwh"] > res["pv_mixed_kwh"] > 0
    # the archived rows still say what the planner was fed (the stub echoes its own
    # PV, so only the doc-level lists carry the percentiles); the slice exposes the P50
    assert res["next_day"]["pv_p50_w"] == [pytest.approx(v) for v in p50[n - 96:]]


def test_run_plan_without_percentile_rows_is_plain_p50(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url)
    inp["knobs"] = {"pv_p10_mix": 0.2}
    p50, _ = core.pv_series(t0, n, inp["solcast_today"], inp["solcast_tomorrow"])
    res = core.run_plan(inp)
    assert stub.posts[0][1]["pv_power_forecast"] == p50            # P10 falls back to P50, the mix is a no-op
    doc = core.load_plan(res["archive_path"])
    assert doc["pv_p10_w"] == p50 and doc["pv_p10_mix"] == 0.2


def _add_p50(arch, values):
    """Give the newest archived doc a raw-P50 series (a post-mix document)."""
    import gzip as _gzip, json as _json
    path = core.list_plans(arch)[-1]
    doc = core.load_plan(path)
    doc["pv_p50_w"] = list(values)
    opener = (lambda p: _gzip.open(p, "wt")) if path.endswith(".gz") else (lambda p: open(p, "w"))
    with opener(path) as f:
        _json.dump(doc, f)
    return doc


def test_compact_slice_carries_the_p50_ceiling_or_the_planners_pv(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, pv=[1000.0] * 96)
    doc = core.load_plan(core.list_plans(arch)[-1])
    pre = core.compact_slice(core.day_slice(doc["rows"], day, AMS, 0.5), doc)
    assert pre["pv_p50_w"] == pre["p_pv_w"]                        # a pre-mix doc: the planner's PV WAS the P50
    doc = _add_p50(arch, [1250.0] * 96)
    post = core.compact_slice(core.day_slice(doc["rows"], day, AMS, 0.5), doc)
    assert post["pv_p50_w"] == [1250.0] * 96 and post["p_pv_w"] == [1000.0] * 96


def test_virtual_day_repairs_to_the_p50_not_to_the_mix(tmp_path):
    """The planner is fed the mix; "what Solcast said" is the P50. A throttled
    step is credited with the P50 ceiling, and the forecast lane on the chart
    keeps showing the number the planner actually used."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, pv=[1000.0] * 96, load=[350.0] * 96)
    _add_p50(arch, [1250.0] * 96)
    i_curt = core._slot(local(2026, 9, 2, 6, 0))
    actual = [800.0] * 96
    actual[i_curt] = 200.0
    mask = [0.0] * 96
    mask[i_curt] = 1.0
    vd = core.virtual_day(arch, day, AMS, local(2026, 9, 2, 12, 0), actual, [350.0] * 96, mask)
    assert vd["p_pv_w"][i_curt] == pytest.approx(1250.0)          # raw P50, not the 1000 the planner saw
    assert vd["pv_fc_w"][i_curt] == pytest.approx(1000.0)         # the chart's forecast lane is the planner's number
    assert vd["pv_reconstructed_kwh"] == pytest.approx(1.05 * core.STEP_H, abs=1e-3)


def test_score_row_repairs_against_the_p50(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, pv=[1000.0] * 96, load=[350.0] * 96)
    doc = _add_p50(arch, [1250.0] * 96)
    plan = core.compact_slice(core.day_slice(doc["rows"], day, AMS, 0.5), doc)
    act = dict(DAY_ACTUALS, pv_w=[600.0] * 96, curtailed=[1.0] * 96)
    row = core.score_row(day, plan, {"cash_eur": None}, 48.2, 0.9, None, act)
    assert row["pv_reconstructed_kwh"] == pytest.approx(96 * 0.65 * core.STEP_H, abs=1e-3)   # up to 1250, not 1000


def test_hindsight_day_ceiling_is_the_p50(stub, tmp_path):
    arch = str(tmp_path / "plans")
    pt0, pn = hs_plan(arch)
    _add_p50(arch, [1200.0] * pn)
    stub.plan_fn = hs_echo(pn)
    win = dict(hs_window(pn), curtailed=[1.0] * pn)              # the real array throttled all window long
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn), win, HS_DAY_ACT, 48.0, 0.9)
    assert hs["status"] == "ok"
    assert stub.posts[0][1]["pv_power_forecast"] == [1200.0] * pn   # raw P50 on every masked step


def test_virtual_day_publishes_the_settled_soc_as_of_now(tmp_path):
    """soc_now_pct is the last PAST step's integrated SOC, not the plan's
    prediction. It is what the next solve chains from, which is the only thing
    that lets an hourly re-solve notice the pack fell behind."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, p_batt=[200.0] * 96)
    now = local(2026, 9, 2, 12, 0)
    vd = core.virtual_day(arch, day, AMS, now)
    assert vd["soc_now_pct"] is not None
    assert vd["soc_now_pct"] == vd["soc_pct"][vd["n_past"] - 1]
    # and it is behind the plan, because the plan has been discharging all morning
    assert vd["soc_now_pct"] < vd["soc_start_pct"]


def test_virtual_day_pv_lanes_are_none_ahead_of_now(tmp_path):
    """Nothing to repair in the future, so both lanes stop at `now` and the
    chart can draw them without a past/future split of its own."""
    vd = _curtailed_day(tmp_path, local(2026, 9, 2, 12, 0), [800.0] * 96, [0.0] * 96)
    i_future = core._slot(local(2026, 9, 2, 18, 0))
    assert vd["pv_meas_w"][i_future] is None
    assert vd["p_pv_w"][i_future] == pytest.approx(1000.0)      # the plan's forecast, untouched


def test_virtual_day_repairs_to_raw_solcast_live_and_finished_alike(tmp_path):
    """No daily scale any more (2026-09-07): a running day and a finished
    day credit a masked step with the same raw Solcast."""
    i_curt = core._slot(local(2026, 9, 2, 6, 0))
    actual = [800.0] * 96
    actual[i_curt] = 100.0
    mask = [0.0] * 96
    mask[i_curt] = 1.0
    live = _curtailed_day(tmp_path, local(2026, 9, 2, 12, 0), actual, mask)
    done = _curtailed_day(tmp_path, local(2026, 9, 3, 0, 30), actual, mask)
    assert live["pv_scale"] == 1.0 and done["pv_scale"] == 1.0
    assert live["p_pv_w"][i_curt] == pytest.approx(1000.0) and done["p_pv_w"][i_curt] == pytest.approx(1000.0)


def test_pv_potential_fit_scale_argument_is_inert():
    n = 12
    meas = [800.0] * n
    meas[-1] = 100.0
    sol = [1000.0] * n
    curt = [0] * (n - 1) + [1]
    a, ia = core.pv_potential(meas, sol, curt, fit_scale=False)
    b, ib = core.pv_potential(meas, sol, curt, fit_scale=True)
    assert a == b and ia == ib and a[-1] == 1000.0 and ia["scale"] == 1.0


def test_pv_potential_leaves_the_must_take_half_alone():
    """A curtailed step where the strings were cut to zero but the Growatt kept
    running: the repair must scale only the strings. Forecast 1.000 of which 300
    is must-take, measurement 300 which is ALL must-take, so main is 0 measured
    against 700 forecast and repairs to 700; the answer is 1.000, not a number
    scaled off the combined 300/1.000 ratio."""
    meas = [1000.0, 300.0]
    fc = [1000.0, 1000.0]
    micro = [300.0, 300.0]
    pot, info = pv_pot_call(meas, fc, [0.0, 1.0], micro)
    assert pot[0] == pytest.approx(1000.0)          # clean step untouched
    assert pot[1] == pytest.approx(1000.0)          # 700 repaired + 300 must-take
    assert info["reconstructed_kwh"] == pytest.approx(0.7 * core.STEP_H, abs=1e-6)


def test_pv_potential_without_the_must_take_arg_is_unchanged():
    meas = [1000.0, 300.0]
    fc = [1000.0, 1000.0]
    a, _ = core.pv_potential(meas, fc, [0.0, 1.0])
    b, _ = core.pv_potential(meas, fc, [0.0, 1.0], micro_meas_w=[0.0, 0.0])
    assert a == b


def pv_pot_call(meas, fc, curt, micro):
    return core.pv_potential(meas, fc, curt, micro_meas_w=micro)


def test_pv_potential_ignores_the_peak_a_cycling_array_reaches():
    """The peak guard is retired: on 09-05 the strings cycled between 8 kW and
    nothing at 99 % real SOC, averaging 1,6 to 3,5 kW, and the guard read the
    peak as proof of no throttling. A masked step is repaired whatever its peak."""
    n = 12
    meas = [800.0] * n
    meas[-2] = meas[-1] = 300.0
    sol = [1000.0] * n
    curt = [0] * (n - 2) + [1, 1]
    peak = [900.0] * n
    peak[-2] = 400.0
    pot, info = core.pv_potential(meas, sol, curt, peak)
    assert info["peak_guard"] is False and info["freed_steps"] == 0
    assert pot[-1] == pytest.approx(1000.0) and pot[-2] == pytest.approx(1000.0)
    assert info["repaired_steps"] == 2
    assert core.pv_potential(meas, sol, curt)[0] == pot           # the peak series changes nothing


def test_virtual_day_publishes_the_plans_own_curtailment(tmp_path):
    """P_PV_curtailment is a decision variable, so the harvest is p_pv_w minus it
    and THAT is what balances against load, battery and grid."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    t0 = local(day.year, day.month, day.day, 0, 0)
    n = core.expected_steps(day, AMS)
    rows = make_rows(t0, n, pv=[7586.4] * n)
    for r in rows:
        r["P_PV_curtailment"], r["P_Load"], r["P_batt"], r["P_grid"] = 2304.7, 278.6, -5000.0, 0.0
    core.write_plan_archive(arch, local(2026, 9, 1, 23, 0),
                            {"plan_ts": local(2026, 9, 1, 23, 0).isoformat(), "t0": t0.isoformat(),
                             "n": n, "tz": AMS, "soc_init": 0.5, "soc_final": 0.5,
                             "optim_status": "Optimal", "n_predicted_steps": 0,
                             "pv_gap_steps": 0, "rows": rows})
    # margin=False: with the writer's margin (the default since 2026-09-12) even a
    # future curtailed step banks the declined sun, which is the writer's intent,
    # not the plan's own figure this test pins
    vd = core.virtual_day(arch, day, AMS, local(2026, 9, 2, 2, 0), margin=False)
    i = core._slot(local(2026, 9, 2, 3, 0))                  # a future step with headroom: pure plan
    taken = vd["p_pv_w"][i] - vd["pv_curtail_w"][i]
    assert vd["pv_curtail_w"][i] == pytest.approx(2304.7)
    assert taken == pytest.approx(5281.7)
    # the live balance checked at noon, to within EMHASS's own rounding
    assert taken - vd["p_load_w"][i] + vd["p_batt_w"][i] + vd["p_grid_w"][i] == pytest.approx(3.1, abs=0.2)


def _capped_day(tmp_path, now, actual_pv, actual_load, plan_pv=7203.6, plan_curtail=1798.9,
                plan_load=299.9, plan_batt=-5101.4, plan_grid=0.0, margin=False):
    """A plan that curtails, laid over a day whose sun and load came in elsewhere."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    t0 = local(day.year, day.month, day.day, 0, 0)
    n = core.expected_steps(day, AMS)
    rows = make_rows(t0, n, pv=[plan_pv] * n)
    for r in rows:
        r["P_PV_curtailment"], r["P_Load"] = plan_curtail, plan_load
        r["P_batt"], r["P_grid"] = plan_batt, plan_grid
    core.write_plan_archive(arch, local(2026, 9, 1, 23, 0),
                            {"plan_ts": local(2026, 9, 1, 23, 0).isoformat(), "t0": t0.isoformat(),
                             "n": n, "tz": AMS, "soc_init": 0.5, "soc_final": 0.5,
                             "optim_status": "Optimal", "n_predicted_steps": 0,
                             "pv_gap_steps": 0, "rows": rows})
    return core.virtual_day(arch, day, AMS, now, actual_pv_w=[actual_pv] * n,
                            actual_load_w=[actual_load] * n, margin=margin)


def test_curtailed_step_holds_the_grid_setpoint_and_the_array_absorbs_the_error(tmp_path):
    """The LP picks an export CAP, not a quantity to discard: at 11:30 on 09-05 its
    harvest 5404,7 was its load 299,9 plus its charge 5101,4. The Deye holds that
    cap on a 1 s loop, so a different sun moves the ARRAY, not the meter."""
    vd = _capped_day(tmp_path, local(2026, 9, 2, 3, 0), 6492.0, 686.0)
    i = core._slot(local(2026, 9, 2, 1, 0))                    # a past step, pack still has headroom
    taken = vd["p_pv_w"][i] - vd["pv_curtail_w"][i]
    assert vd["p_pv_w"][i] == pytest.approx(6492.0)             # the sun that was there
    assert taken == pytest.approx(5790.8, abs=0.2)             # harvest 5404,7 + the 386,1 load error
    assert vd["pv_curtail_w"][i] == pytest.approx(701.2, abs=0.2)   # declines LESS, not the plan's 1798,9
    assert vd["p_grid_w"][i] == pytest.approx(0.0, abs=0.2)     # stays on setpoint


def test_curtailed_step_imports_only_what_the_sun_could_not_cover(tmp_path):
    """Past the cap there is nothing left to give back, and the METER STILL DOES
    NOT MOVE.

    REVERSED 2026-09-06, deliberately. This used to assert an import of 790,8 W,
    on the reasoning that once curtailment is exhausted the shortfall has to come
    from somewhere. It does, but not from the grid: under Zero Export To CT with
    grid charging off the pack's charge is a CEILING, not a commitment, so a
    shortfall in the sun simply charges the pack less and the CT rule holds the
    meter at setpoint. The inverter has no instruction that would make it import.

    Booking it to the grid instead is what put 7,69 kWh of import on 2026-09-05
    that no solve ever chose. The cost is that the virtual pack now ends the step
    lower than the plan intended, which is real and is what the hourly re-solve
    exists to catch up."""
    vd = _capped_day(tmp_path, local(2026, 9, 2, 3, 0), 5000.0, 686.0)
    i = core._slot(local(2026, 9, 2, 1, 0))
    assert vd["p_pv_w"][i] == pytest.approx(5000.0)
    assert vd["pv_curtail_w"][i] == pytest.approx(0.0, abs=20.0)  # nothing declined bar the clamp's 1 A quantisation
    assert vd["p_grid_w"][i] == pytest.approx(0.0, abs=0.2)     # the pack takes the shortfall


def test_curtailed_future_step_is_the_plan_untouched(tmp_path):
    """No forecast error ahead of now, so the whole construction collapses back to
    the plan's own numbers."""
    vd = _capped_day(tmp_path, local(2026, 9, 2, 3, 0), 6492.0, 686.0)
    i = core._slot(local(2026, 9, 2, 4, 0))                    # future, and the pack still has headroom
    assert vd["p_pv_w"][i] == pytest.approx(7203.6)
    assert vd["pv_curtail_w"][i] == pytest.approx(1798.9)
    assert vd["p_grid_w"][i] == pytest.approx(0.0)


def test_compact_slice_carries_the_curtailment_lane(tmp_path):
    """The next_day slice comes from compact_slice, not virtual_day, so the
    FUTURE half of the chart needs the same column."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 3)
    made = local(2026, 9, 2, 13, 0)
    t0, n = core.horizon(made, AMS)
    rows = make_rows(t0, n)
    for r in rows:
        r["P_PV_curtailment"] = 1725.5
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n,
                                         "tz": AMS, "soc_init": 0.5, "soc_final": 0.5,
                                         "optim_status": "Optimal", "n_predicted_steps": 0,
                                         "pv_gap_steps": 0, "rows": rows})
    found = core.newest_plan_for_day(arch, day, AMS)
    assert found is not None
    sl = core.compact_slice(found[1], found[0])
    assert all(v == pytest.approx(1725.5) for v in sl["pv_curtail_w"])


def test_newest_plan_for_day_accepts_a_same_day_plan_that_plan_for_day_would_reject(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 3)
    # made the day before: this one is "tomorrow" from its own point of view,
    # so plan_for_day's before-midnight rule does not reject it either
    made_a = local(2026, 9, 2, 13, 0, 30)
    t0a, na = core.horizon(made_a, AMS)
    core.write_plan_archive(arch, made_a, {"plan_ts": made_a.isoformat(), "t0": t0a.isoformat(), "n": na,
                                           "tz": AMS, "soc_init": 0.5, "soc_final": 0.5,
                                           "optim_status": "Optimal", "n_predicted_steps": 0,
                                           "pv_gap_steps": 0, "rows": make_rows(t0a, na)})
    # made ON the day itself: plan_for_day must reject it (plan_ts >= day's
    # midnight), newest_plan_for_day must not
    made_b = local(2026, 9, 3, 10, 0, 0)
    t0b, nb = write_full_day_plan(arch, made_b, day, soc0=0.6)
    doc_a, _ = core.plan_for_day(arch, day, AMS)
    assert doc_a["plan_ts"] == made_a.isoformat()
    doc_b, sl_b = core.newest_plan_for_day(arch, day, AMS)
    assert doc_b["plan_ts"] == made_b.isoformat()
    assert sl_b["n"] == nb


# ---- hindsight and the loss adjustment ---------------------------------------

def test_loss_adjustment_prices_the_quadratic_loss_per_slot():
    rows = [{"P_batt": 10000.0, "P_hybrid_inverter": 2000.0, "unit_load_cost": 0.4, "unit_prod_price": 0.2},
            {"P_batt": 0.0, "P_hybrid_inverter": 0.0, "unit_load_cost": 0.4, "unit_prod_price": 0.2}]
    want = (core.Q_PORT * 100 + core.Q_BRIDGE * 4) * 0.25 * 0.3
    assert core.loss_adjustment(rows) == pytest.approx(want, abs=1e-4)
    # without P_hybrid_inverter the AC side falls back to P_Load - P_grid
    rows = [{"P_batt": 0.0, "P_Load": 500.0, "P_grid": -1500.0, "unit_load_cost": 0.3, "unit_prod_price": 0.1}]
    assert core.loss_adjustment(rows) == pytest.approx(core.Q_BRIDGE * 4 * 0.25 * 0.2, abs=1e-4)


def test_planned_cost_includes_the_loss_adjustment():
    plan = flat_plan(loss_eur=0.5)
    pc = core.planned_cost(plan, 48.0, core.lambda_for(plan["sell"], 0.9))
    assert pc["loss"] == 0.5 and pc["total"] == pytest.approx(2.832 + 0.5)


def test_compact_slice_carries_the_loss_adjustment():
    t0 = local(2026, 9, 2, 0, 0)
    rows = make_rows(t0, 96)
    sl = core.day_slice(rows, date(2026, 9, 2), AMS, 0.5)
    c = core.compact_slice(sl, {"tz": AMS, "plan_ts": "x", "t0": t0.isoformat()})
    assert c["loss_eur"] == pytest.approx(core.loss_adjustment(rows), abs=1e-6)
    assert c["loss_eur"] > 0


def test_fifteen_min_series_aggregates_5min_means_and_holds_gaps():
    day = date(2026, 9, 2)
    base = local(2026, 9, 2, 0, 0).timestamp()
    rows = [{"start": base + 300 * i, "mean": 100.0 * (i + 1)} for i in range(3)]   # step 0: 100/200/300
    rows.append({"start": (base + 900) * 1000.0, "mean": 400.0})                    # step 1, WS milliseconds
    vals, gaps = core.fifteen_min_series(day, AMS, rows)
    assert len(vals) == 96 and vals[0] == pytest.approx(200.0) and vals[1] == pytest.approx(400.0)
    assert vals[2] == pytest.approx(400.0) and gaps == 94                           # gaps hold the last value
    assert core.fifteen_min_series(day, AMS, None) == ([0.0] * 96, 96)


def test_fifteen_min_series_gap_upto_bounds_the_count_for_a_day_still_running():
    """A day in progress is all gaps after now. Judging the whole day sinks an
    otherwise perfect series (live 2026-09-04: 23 future steps failed 73 good
    ones), so the display path bounds the count to the elapsed steps."""
    day = date(2026, 9, 2)
    base = local(2026, 9, 2, 0, 0).timestamp()
    rows = [{"start": base + 300 * i, "mean": 500.0} for i in range(12 * 4 * 3)]     # every 5 min to 12:00
    vals, gaps = core.fifteen_min_series(day, AMS, rows)
    assert gaps == 48                                                               # the whole afternoon
    vals_b, gaps_b = core.fifteen_min_series(day, AMS, rows, gap_upto=core._slot(local(2026, 9, 2, 12, 0)))
    assert gaps_b == 0 and vals_b == vals                                           # values unchanged, count bounded


def hs_plan(arch, made=None):
    """A plan of record whose horizon runs past the scored day, which is the
    whole point of the matched-horizon lane. Returns (t0, n)."""
    made = made or local(2026, 9, 1, 13, 2)
    pt0, pn = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {
        "plan_ts": made.isoformat(), "t0": pt0.isoformat(), "n": pn, "tz": AMS,
        "soc_init": 0.5, "soc_final": 0.5, "optim_status": "Optimal",
        "n_predicted_steps": 0, "pv_gap_steps": 0, "rows": make_rows(pt0, pn),
        "tariff": {"energy_tax": 0.0, "supplier_fee": 0.0, "btw_pct": 0.0, "feedin_fee": 0.0},
        "payload": {"battery_soc_surplus_cost": 0.004, "battery_soc_deficit_threshold": 0.2,
                    "battery_soc_deficit_cost": 0.01, "battery_stress_cost": 0.0075,
                    "inverter_stress_cost": 0.0021}})
    return pt0, pn


def hs_prices(t0, n):
    """Real day-ahead rows for every calendar day the window touches."""
    days, d = [], t0.date()
    end = t0 + timedelta(minutes=15 * n)
    while d <= end.date():
        days += np_rows(local(d.year, d.month, d.day, 0, 0))
        d += timedelta(days=1)
    return days


HS_DAY_ACT = {"grid_w": [800.0] * 96, "batt_dc_w": [0.0] * 96, "pv_w": [1500.0] * 96}


def hs_window(n, grid=800.0, batt=0.0, pv=1000.0):
    return {"grid_w": [grid] * n, "batt_dc_w": [batt] * n, "pv_w": [pv] * n}


def hs_echo(n):
    """A solve that returns the prices it was POSTED, as the add-on does: the
    settlement reads them back off the rows, so a stub with its own defaults
    would silently score the lane in a tariff nobody asked for."""
    return lambda posts: make_rows(local(2026, 9, 2, 22, 45), n,
                                   buy=posts[-1][1]["load_cost_forecast"],
                                   sell=posts[-1][1]["prod_price_forecast"])


def test_hindsight_day_matches_the_plans_horizon_not_the_scored_day(stub, tmp_path):
    """The lane used to solve 00:00-24:00 with soc_final pinned inside the scored
    day, while the plan lane ran 48-72 h with that day mid-horizon. It now solves
    the plan of record's OWN window, so the terminal artifact lands past the day
    for both lanes."""
    arch = str(tmp_path / "plans")
    pt0, pn = hs_plan(arch)
    assert pn > 96                                          # the horizon outlives the scored day
    stub.plan_fn = hs_echo(pn)                              # the MPC stamps rows from NOW
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                            hs_window(pn), HS_DAY_ACT, 48.0, 0.9)
    assert hs["status"] == "ok" and hs["posted"] is True
    body = stub.posts[0][1]
    assert body["prediction_horizon"] == pn                 # the PLAN's horizon, not 96
    assert hs["horizon_steps"] == pn and hs["horizon_start"] == pt0.isoformat()
    # every frame input is the plan of record's, so only the forecasts differ
    assert body["soc_init"] == 0.5 and body["soc_final"] == 0.5
    assert body["battery_soc_surplus_cost"] == 0.004
    assert body["battery_stress_cost"] == 0.0075 and body["inverter_stress_cost"] == 0.0021
    # the plan's tariff on the plan's grid: step 0 is 13:15 on the day BEFORE
    assert body["load_cost_forecast"][0] == pytest.approx(0.153)
    assert body["load_cost_forecast"][43] == pytest.approx(0.1)   # 00:00 of the scored day
    # the LP is fed the EFFECTIVE load, so its own P_grid equals the difference
    # form the scoreboard settles: 800 grid + 0 battery + 1000 PV
    assert body["load_power_forecast"] == [1800.0] * pn and body["pv_power_forecast"] == [1000.0] * pn
    assert hs["eur"] == pytest.approx(hs["cash_eur"] + hs["soc_term_eur"] + hs["loss_eur"], abs=1e-3)


def test_hindsight_treats_the_growatt_as_must_take_like_the_plan_lane(stub, tmp_path):
    arch = str(tmp_path / "plans")
    pt0, pn = hs_plan(arch)
    stub.plan_fn = hs_echo(pn)
    win = dict(hs_window(pn), micro_w=[300.0] * pn)
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn), win, HS_DAY_ACT, 48.0, 0.9)
    assert hs["status"] == "ok"
    body = stub.posts[0][1]
    assert body["pv_power_forecast"] == [700.0] * pn                 # 1000 potential minus the Growatt
    assert body["load_power_forecast"] == [1500.0] * pn              # 1800 effective load minus the Growatt
    # without a Growatt series the lane is unchanged
    stub.posts.clear()
    core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn), hs_window(pn), HS_DAY_ACT, 48.0, 0.9)
    assert stub.posts[0][1]["pv_power_forecast"] == [1000.0] * pn and stub.posts[0][1]["load_power_forecast"] == [1800.0] * pn


def test_hindsight_is_settled_the_same_way_as_the_replayed_lane(stub, tmp_path):
    """An LP on measured PV and load produces the ABSOLUTE trace load-PV-battery,
    which hands hindsight the site's unmetered residual for free. Both lanes go
    through replay_day instead, so the gap compares two battery plans and
    nothing else."""
    arch = str(tmp_path / "plans")
    pt0, pn = hs_plan(arch)
    stub.plan_fn = hs_echo(pn)
    base = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                              hs_window(pn), HS_DAY_ACT, 48.0, 0.9)
    moved = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                               hs_window(pn), dict(HS_DAY_ACT, grid_w=[1300.0] * 96), 48.0, 0.9)
    # the measured trace is the base of the settlement, so moving it moves the lane
    assert moved["cash_eur"] > base["cash_eur"]
    # ... by exactly the extra import, since the battery plan is unchanged
    da, _n = core.price_series(local(2026, 9, 2, 0, 0), 96, hs_prices(pt0, pn), None, None)
    buy, sell = core.tariff(da, 0.0, 0.0, 0.0, 0.0)
    assert moved["cash_eur"] - base["cash_eur"] == pytest.approx(
        core.cash_in_frame([500.0] * 96, buy, sell), abs=1e-3)
    assert moved["soc_term_eur"] == base["soc_term_eur"]      # same trajectory, same carry-over


def test_hindsight_day_failure_paths(stub, tmp_path):
    arch = str(tmp_path / "plans")
    day_act = HS_DAY_ACT
    # no plan of record: there is no horizon to match
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, [], hs_window(4), day_act, 48.0, 0.9)
    assert hs["status"] == "no_plan" and hs["posted"] is False
    pt0, pn = hs_plan(arch)
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                            hs_window(pn - 1), day_act, 48.0, 0.9)
    assert hs["status"] == "no_actuals" and hs["posted"] is False   # the window must cover the horizon
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                            hs_window(pn), dict(day_act, grid_w=None), 48.0, 0.9)
    assert hs["status"] == "no_actuals"                      # no measured trace, no settlement
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn)[:90],
                            hs_window(pn), day_act, 48.0, 0.9)
    assert hs["status"] == "no_prices" and hs["posted"] is False   # a held price step is not hindsight
    stub.plan_fn = lambda posts: make_rows(pt0, pn, status="Infeasible")
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                            hs_window(pn), day_act, 48.0, 0.9)
    assert hs["status"] == "not_optimal" and hs["posted"] is True
    stub.plan_fn = lambda posts: make_rows(pt0, pn - 6)
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                            hs_window(pn), day_act, 48.0, 0.9)
    assert hs["status"] == "solve_failed" and "not this solve" in hs["message"]
    stub.plan_fn = lambda posts: make_rows(pt0, pn + 1)               # one row too many is another solve, not a longer plan
    hs = core.hindsight_day(arch, stub.url, "2026-09-02", AMS, hs_prices(pt0, pn),
                            hs_window(pn), day_act, 48.0, 0.9)
    assert hs["status"] == "solve_failed" and "not this solve" in hs["message"]


def test_pv_potential_is_raw_solcast_on_a_masked_step_and_nothing_else():
    """The site rule (2026-09-07): a masked step takes raw Solcast; the fitted
    daily scale and the peak guard are retired. Numbers are 2026-09-05."""
    clean_meas = [897.0, 2493.0] * 6          # 12 clean steps, ratios 0,88 and 0,78: irrelevant now
    clean_sol = [1016.0, 3212.0] * 6
    curt_meas = [5381.0, 1496.0]
    curt_sol = [6148.0, 7939.0]
    pot, info = core.pv_potential(clean_meas + curt_meas, clean_sol + curt_sol,
                                  [0] * 12 + [1, 1])
    assert info["scale"] == 1.0 and info["scale_source"] == "raw" and info["clean_steps"] == 12
    assert pot[:12] == clean_meas                             # clean steps are untouched, whatever their ratio
    assert pot[12] == pytest.approx(6148.0)                   # raw Solcast, not 0,83 x Solcast
    assert pot[13] == pytest.approx(7939.0)
    assert info["repaired_steps"] == 2 and info["freed_steps"] == 0 and info["peak_guard"] is False
    assert info["reconstructed_kwh"] == pytest.approx(((6148 - 5381) + (7939 - 1496)) * core.STEP_H / 1000, abs=1e-3)
    # a masked step already above Solcast keeps its measurement
    pot, _i = core.pv_potential([7000.0], [6000.0], [1])
    assert pot == [7000.0]


def test_pv_potential_guards():
    n = 20
    # every step masked: raw Solcast throughout
    pot, info = core.pv_potential([100.0] * n, [1000.0] * n, [1] * n)
    assert info["scale_source"] == "raw" and info["scale"] == 1.0 and pot[0] == 1000.0
    # a fractional mask is the recorder's 15-minute mean of the 0/1 sensor
    pot, _i = core.pv_potential([100.0, 100.0], [1000.0, 1000.0], [0.4, 0.6])
    assert pot[0] == 100.0 and pot[1] > 100.0
    # night steps are never repaired, whatever the mask says
    pot, _i = core.pv_potential([0.0], [10.0], [1])
    assert pot == [0.0]
    # a length mismatch degrades to the measurement rather than guessing
    pot, info = core.pv_potential([1.0, 2.0], [1.0], [0, 0])
    assert pot == [1.0, 2.0] and info["scale_source"] == "length_mismatch"


def test_replay_day_repair_term_moves_the_counterfactual_grid():
    """The counterfactual pack had room, so the energy the real array was denied
    leaves through its meter."""
    plan = flat_plan()
    n = plan["n"]
    act = flat_actuals(plan, g_meas=2000.0)
    lam = core.lambda_for(plan["sell"], 0.9)
    base = core.replay_day(plan, act["grid_w"], act["batt_dc_w"], act["pv_w"], 48.0, lam)
    pot = [v + 1000.0 for v in act["pv_w"]]            # 1 kW the array was held back from
    rep = core.replay_day(plan, act["grid_w"], act["batt_dc_w"], act["pv_w"], 48.0, lam,
                          pv_pot_w=pot)
    # 1 kW less import for 24 h at 0,30 EUR/kWh
    assert base["cash"] - rep["cash"] == pytest.approx(24 * 0.30, abs=1e-3)
    assert rep["soc_term"] == base["soc_term"]         # the battery plan is untouched


def test_effective_load_closes_the_metering_residual():
    """L_eff - PV is by construction replay_day's difference base, so the LP's
    own P_grid equals G_replay and it optimises what the scoreboard scores."""
    grid, batt, pv = [800.0] * 4, [1000.0, -1000.0, 0.0, 500.0], [1500.0] * 4
    eff = core.effective_load(grid, batt, pv)
    assert eff[0] == pytest.approx(800 + 989.0 + 1500)        # discharging: eta x DC
    assert eff[1] == pytest.approx(800 - 1000 / 0.989 + 1500, abs=0.1)   # charging: DC / eta
    # the identity: for any battery plan B, (L_eff - PV) - B == G_meas - B + B_meas_ac
    for i in range(4):
        b_ac = batt[i] * core.ETA_BRIDGE if batt[i] >= 0 else batt[i] / core.ETA_BRIDGE
        assert eff[i] - pv[i] == pytest.approx(grid[i] + b_ac, abs=0.1)


def test_window_series_spans_calendar_days(stub, tmp_path):
    t0 = local(2026, 9, 1, 13, 15)
    rows = [{"start": (t0 + timedelta(minutes=5 * k)).astimezone(timezone.utc).isoformat(),
             "mean": 100.0 + k} for k in range(3 * 139)]
    vals, gaps = core.window_series(t0, 139, rows)
    assert len(vals) == 139 and gaps == 0
    assert vals[0] == pytest.approx(101.0)                   # mean of the three 5-minute means
    # the day-anchored helper is the same function with the day's own t0 and n
    day_vals, _ = core.fifteen_min_series(date(2026, 9, 1), AMS, rows)
    assert day_vals[53] == pytest.approx(vals[0])            # 13:15 is step 53 of the day


def test_score_row_records_the_hindsight_lane():
    realised = {"cash_eur": 3.0, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": 40.0, "load_kwh": 10.0}
    act = flat_actuals(flat_plan(), g_meas=1000.0)
    realised = dict(realised, cash_eur=7.2)
    row = core.score_row(date(2026, 9, 2), flat_plan(), realised, 48.0, 0.9, {"status": "ok", "eur": 1.5}, act)
    assert row["hindsight_eur"] == 1.5 and row["hindsight_status"] == "ok" and row["flags"] == ""
    row = core.score_row(date(2026, 9, 2), flat_plan(), realised, 48.0, 0.9, {"status": "no_actuals", "eur": None}, act)
    assert row["hindsight_eur"] is None and row["hindsight_status"] == "no_actuals"
    assert "no_hindsight" in row["flags"]


def test_score_day_keeps_an_ok_hindsight_over_a_failed_rescore(tmp_path):
    arch, csvp = str(tmp_path / "plans"), str(tmp_path / "scores.csv")
    made = local(2026, 9, 1, 13, 2)
    t0, n = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS,
                                         "soc_init": 0.5, "soc_final": 0.5, "optim_status": "Optimal",
                                         "n_predicted_steps": 0, "pv_gap_steps": 0, "rows": make_rows(t0, n)})
    realised = {"cash_eur": 3.0, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": 1.0, "load_kwh": 1.0}
    r1 = core.score_day(arch, csvp, "2026-09-02", AMS, realised, 48.0, 0.9, {"status": "ok", "eur": 1.5})["row"]
    assert r1["hindsight_eur"] == 1.5
    empty = {k: None for k in realised}
    r2 = core.score_day(arch, csvp, "2026-09-02", AMS, empty, 48.0, 0.9, {"status": "solve_failed", "eur": None})["row"]
    assert r2["hindsight_eur"] == 1.5 and r2["hindsight_status"] == "ok" and "no_hindsight" not in r2["flags"]
    r3 = core.score_day(arch, csvp, "2026-09-02", AMS, empty, 48.0, 0.9, None)["row"]
    assert r3["hindsight_eur"] == 1.5
    r4 = core.score_day(arch, csvp, "2026-09-02", AMS, empty, 48.0, 0.9, {"status": "ok", "eur": 1.2})["row"]
    assert r4["hindsight_eur"] == 1.2


def test_rolling_windows_and_capture():
    rows = []
    for k in range(10):
        d = (date(2026, 9, 1) + timedelta(days=k)).isoformat()
        # earnings: actual -6, theoretical max -8, so gap = actual - max = +2 regret
        rows.append({c: None for c in core.SCORE_COLUMNS} | {
            "date": d, "planned_eur": -9.0, "replayed_eur": -6.0, "realised_eur": -3.0,
            "hindsight_eur": -8.0, "gap_eur": 2.0, "forecast_gap_eur": 3.0})
    r = core.rolling(rows)
    w = r["windows"]
    assert w["d1"]["gap"] == 2.0 and w["d7"]["gap"] == 14.0 and w["d30"]["gap"] == 20.0 and w["all"]["gap"] == 20.0
    assert w["all"]["n"] == 10 and w["all"]["replayed"] == -60.0 and w["all"]["hindsight"] == -80.0
    assert w["all"]["planned"] == -90.0 and w["all"]["forecast_gap"] == 30.0
    cap = r["capture"]
    # captured -60 of a possible -80: 75 % of the theoretical maximum
    assert cap["n_days"] == 10 and cap["captured_eur"] == -60.0 and cap["max_eur"] == -80.0 and cap["pct"] == 75.0
    # a net-cost window (hindsight above the -0,50 guard) yields no ratio
    flat = [dict(rows[0], replayed_eur=0.2, hindsight_eur=0.1)]
    assert core.rolling(flat)["capture"]["pct"] is None


# ---- load forecast -----------------------------------------------------------

def diurnal_day(day, tz=AMS, night=100.0, peak=3000.0):
    """A day with an unmistakable shape: `peak` W from 12:00 to 13:00 local,
    `night` W everywhere else. Values counted from local midnight, so a DST
    day is 92 or 100 entries, exactly like fifteen_min_series returns."""
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(tz))
    return [peak if t.hour == 12 else night
            for t in core.step_times(t0, core.expected_steps(day, tz))]


def load_days(last_day, n_days=7, **kw):
    return {last_day - timedelta(days=k): diurnal_day(last_day - timedelta(days=k), **kw)
            for k in range(n_days)}


def test_load_series_is_same_clock_time_not_a_positional_copy():
    """The defect this replaces: EMHASS's naive method pastes the last n
    recorded samples onto the horizon, which at a 12:15 solve lands 12 h out of
    phase. Every step must carry its OWN wall-clock slot instead."""
    day = date(2026, 9, 2)
    t0, n = core.horizon(local(2026, 9, 3, 12, 5), AMS, days_ahead=2)
    out, info = core.load_series(t0, n, AMS, load_days(day))
    assert len(out) == n and info["ref_days"] == 7 and info["missing_slots"] == 0
    for t, v in zip(core.step_times(t0, n), out):
        assert v == pytest.approx(3000.0 if t.hour == 12 else 100.0), t.isoformat()
    # the phase test in one line: midday is the peak, the small hours are not
    noon = [v for t, v in zip(core.step_times(t0, n), out) if (t.hour, t.minute) == (12, 30)]
    small = [v for t, v in zip(core.step_times(t0, n), out) if (t.hour, t.minute) == (3, 30)]
    assert noon and small and min(noon) == 3000.0 and max(small) == 100.0


def test_load_series_repeats_the_same_clock_profile_on_the_day_after():
    day = date(2026, 9, 2)
    t0, n = core.horizon(local(2026, 9, 3, 12, 5), AMS, days_ahead=2)
    out, _ = core.load_series(t0, n, AMS, load_days(day))
    by_day = {}
    for t, v in zip(core.step_times(t0, n), out):
        by_day.setdefault(t.date(), {})[(t.hour, t.minute)] = v
    d1, d2 = sorted(by_day)[1], sorted(by_day)[2]
    shared = set(by_day[d1]) & set(by_day[d2])
    assert shared and all(by_day[d1][k] == by_day[d2][k] for k in shared)


def test_load_series_takes_the_median_so_one_odd_day_cannot_drag_it():
    """Measured 2026-09-03: scaling the profile by the newest day's level made
    the 195-287 step horizon worse (MAE 255 W against 224 W), so the forecast is
    the plain per-slot median and a single hot day must not move it."""
    day = date(2026, 9, 2)
    days = load_days(day)
    days[day] = [v * 50 for v in days[day]]               # yesterday: a meter glitch
    t0, n = core.horizon(local(2026, 9, 3, 12, 5), AMS)
    out, _ = core.load_series(t0, n, AMS, days)
    assert max(out) == pytest.approx(3000.0)
    assert min(out) == pytest.approx(100.0)


def test_load_series_needs_two_days_before_it_will_speak():
    day = date(2026, 9, 2)
    t0, n = core.horizon(local(2026, 9, 3, 12, 5), AMS)
    out, info = core.load_series(t0, n, AMS, load_days(day, n_days=1))
    assert out is None and info["ref_days"] == 1
    assert core.load_series(t0, n, AMS, {})[0] is None
    assert core.load_series(t0, n, AMS, None)[0] is None
    out2, _ = core.load_series(t0, n, AMS, load_days(day, n_days=2))
    assert out2 is not None and len(out2) == n


def test_load_series_blends_the_first_step_with_the_live_reading():
    """Passing a runtime list disables EMHASS's own set_mix_forecast, whose
    alpha and beta are both 0,5, so the blend happens here instead."""
    day = date(2026, 9, 2)
    t0, n = core.horizon(local(2026, 9, 3, 3, 5), AMS)     # a night step: profile 100 W
    out, _ = core.load_series(t0, n, AMS, load_days(day), now_w=900.0)
    assert out[0] == pytest.approx(0.5 * 100.0 + 0.5 * 900.0)
    assert out[1] == pytest.approx(100.0)                 # only the first step is blended
    plain, _ = core.load_series(t0, n, AMS, load_days(day))
    assert plain[0] == pytest.approx(100.0)
    neg, _ = core.load_series(t0, n, AMS, load_days(day), now_w=-50.0)
    assert neg[0] == pytest.approx(50.0)                  # a negative reading floors at 0


def test_load_profile_maps_a_long_dst_day_onto_wall_clock_slots():
    """25-hour day: 02:00-02:59 happens twice and both copies land in the same
    four slots, so the profile still has 96 of them."""
    autumn = date(2026, 10, 25)
    assert core.expected_steps(autumn, AMS) == 100
    profile = core.load_profile({autumn: diurnal_day(autumn)}, AMS)
    assert len(profile) == 96 and profile[core._slot(local(2026, 10, 25, 12, 30))] == 3000.0
    spring = date(2026, 3, 29)                            # 23-hour day: 02:00-02:59 missing
    assert core.expected_steps(spring, AMS) == 92
    profile2 = core.load_profile({spring: diurnal_day(spring)}, AMS)
    assert len(profile2) == 92


def test_a_short_dst_day_has_its_missing_slots_covered_by_the_other_days():
    spring = date(2026, 3, 29)                            # contributes no 02:xx slots
    t0, n = core.horizon(local(2026, 3, 28, 23, 50), AMS)
    out, info = core.load_series(t0, n, AMS, {spring: diurnal_day(spring),
                                              spring - timedelta(days=1): diurnal_day(spring - timedelta(days=1))})
    assert out is not None and len(out) == n and info["missing_slots"] == 0


def test_load_series_holds_the_median_for_a_slot_nothing_covers():
    """Defensive: a truncated history cannot leave a hole in the horizon."""
    day = date(2026, 9, 2)
    days = {d: v[:40] for d, v in load_days(day, n_days=2).items()}   # stops at 10:00
    t0, n = core.horizon(local(2026, 9, 3, 12, 5), AMS)
    out, info = core.load_series(t0, n, AMS, days)
    # slots 0..39 are known, so only tomorrow 00:00-09:45 is covered
    assert len(out) == n and info["missing_slots"] == n - 40
    assert set(out) == {100.0}                                       # the median of the 40 known slots


def test_run_plan_passes_the_load_profile_and_records_its_source(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url)
    inp["load_days"] = load_days(now.date() - timedelta(days=1))
    inp["load_now_w"] = 900.0
    res = core.run_plan(inp)
    assert res["ok"] and res["load_source"] == "profile" and res["load_ref_days"] == 7
    body = stub.posts[0][1]
    lf = body["load_power_forecast"]
    assert len(lf) == n
    assert lf[0] == pytest.approx(0.5 * 100.0 + 0.5 * 900.0)      # 14:15 is a night-value slot
    for t, v in zip(core.step_times(t0, n)[1:], lf[1:]):
        assert v == pytest.approx(3000.0 if t.hour == 12 else 100.0)
    doc = core.load_plan(res["archive_path"])
    assert doc["load_source"] == "profile" and doc["load_ref_days"] == 7
    assert doc["payload"]["load_power_forecast"] == lf


def test_run_plan_without_history_leaves_the_load_to_emhass(stub, tmp_path):
    """The ML switch being on, or a cold start, must not force a list."""
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url))
    assert res["ok"] and res["load_source"] == "emhass" and res["load_ref_days"] == 0
    assert "load_power_forecast" not in stub.posts[0][1]
    assert core.load_plan(res["archive_path"])["load_source"] == "emhass"


# ---- replay: re-run a past day under current logic ---------------------------

def _source_plan(arch, made, tariff_dict):
    """Write an organic plan of record and return (t0, n, raw_da)."""
    t0, n = core.horizon(made, AMS)
    da = [round(0.05 + 0.001 * k, 5) for k in range(n)]
    buy, sell = core.tariff(da, tariff_dict["energy_tax"], tariff_dict["supplier_fee"],
                            tariff_dict["btw_pct"], tariff_dict["feedin_fee"])
    pv = [round(100.0 * k, 1) for k in range(n)]
    payload = core.build_payload(t0, n, 0.5, 0.5, pv, buy, sell)
    core.write_plan_archive(arch, made, {
        "plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS, "soc_init": 0.5,
        "soc_final": 0.5, "tariff": dict(tariff_dict), "n_predicted_steps": 0, "pv_gap_steps": 0,
        "prices_predicted": False, "optim_status": "Optimal", "cost_eur": 0.0,
        "payload": payload, "rows": make_rows(t0, n)})
    return t0, n, da


def _seven_days_before(day, w=400.0):
    return {day - timedelta(days=k): [w] * 96 for k in range(1, 8)}


def test_replay_plan_reruns_on_source_shoes_under_current_tariff(stub, tmp_path):
    arch = str(tmp_path / "plans")
    old = {"energy_tax": 0.0, "supplier_fee": 0.0, "btw_pct": 0.0, "feedin_fee": 0.0}
    made = local(2026, 9, 1, 13, 0, 30)
    t0, n, da = _source_plan(arch, made, old)
    stamped = local(2026, 9, 4, 10, 0)                         # the MPC anchors rows at now, not the source day
    stub.plan_fn = lambda posts: make_rows(stamped, n)
    new = {"energy_tax": 0.11, "supplier_fee": 0.019, "btw_pct": 21.0, "feedin_fee": 0.019}
    out = core.replay_plan(arch, stub.url, "2026-09-02", AMS, new,
                           _seven_days_before(date(2026, 9, 2)), 48.0, 0.9)
    assert out["status"] == "ok" and out["n"] == n and out["load_ref_days"] == 7
    body = stub.posts[-1][1]
    assert body["prediction_horizon"] == n
    assert body["load_power_forecast"][10] == 400.0            # rebuilt profile, never EMHASS naive
    exp_buy, exp_sell = core.tariff(da, 0.11, 0.019, 21.0, 0.019)
    assert body["load_cost_forecast"][5] == pytest.approx(exp_buy[5])   # source DA, current frame
    assert body["prod_price_forecast"][5] == pytest.approx(exp_sell[5])
    assert body["pv_power_forecast"][7] == pytest.approx(700.0)         # source PV preserved
    assert body["battery_stress_cost"] > 0 and body["inverter_stress_cost"] > 0
    # the replay doc is now the plan of record, marked, rows re-stamped to the source horizon
    doc, sl = core.plan_for_day(arch, date(2026, 9, 2), AMS)
    assert doc.get("replay") is True and doc["t0"] == t0.isoformat()
    assert doc["tariff"] == new and doc["load_source"] == "profile"
    assert core._parse_ts(doc["rows"][0]["timestamp"]) == t0.astimezone(timezone.utc)
    assert sl["n"] == 96
    # the untouched organic source is still findable, so a re-replay is idempotent
    src = core.original_plan_for_day(arch, date(2026, 9, 2), AMS)
    assert src.get("replay") is None and src["plan_ts"] == made.isoformat()


def test_replay_plan_refuses_thin_load_history_without_solving(stub, tmp_path):
    arch = str(tmp_path / "plans")
    tf = {"energy_tax": 0.0, "supplier_fee": 0.0, "btw_pct": 0.0, "feedin_fee": 0.0}
    _source_plan(arch, local(2026, 9, 1, 13, 0, 30), tf)
    out = core.replay_plan(arch, stub.url, "2026-09-02", AMS, tf,
                           {date(2026, 9, 1): [400.0] * 96}, 48.0, 0.9)      # one day < LOAD_MIN_REF_DAYS
    assert out["status"] == "insufficient_load" and out["load_ref_days"] == 1
    assert stub.posts == []                                    # never posted a solve


def test_replay_plan_no_source_plan(stub, tmp_path):
    out = core.replay_plan(str(tmp_path / "plans"), stub.url, "2026-09-02", AMS,
                           {"energy_tax": 0.0, "supplier_fee": 0.0, "btw_pct": 0.0, "feedin_fee": 0.0},
                           _seven_days_before(date(2026, 9, 2)), 48.0, 0.9)
    assert out["status"] == "no_plan" and stub.posts == []


def test_original_plan_for_day_skips_replay_docs(stub, tmp_path):
    arch = str(tmp_path / "plans")
    tf = {"energy_tax": 0.0, "supplier_fee": 0.0, "btw_pct": 0.0, "feedin_fee": 0.0}
    made = local(2026, 9, 1, 13, 0, 30)
    _source_plan(arch, made, tf)
    _, n = core.horizon(made, AMS)
    stub.plan_fn = lambda posts: make_rows(local(2026, 9, 4, 10, 0), n)
    core.replay_plan(arch, stub.url, "2026-09-02", AMS, tf, _seven_days_before(date(2026, 9, 2)), 48.0, 0.9)
    doc, _ = core.plan_for_day(arch, date(2026, 9, 2), AMS)
    assert doc.get("replay") is True                          # the record is the replay
    assert core.original_plan_for_day(arch, date(2026, 9, 2), AMS)["plan_ts"] == made.isoformat()


def test_rolled_slices_carries_yesterday_as_a_fully_past_virtual_day(tmp_path):
    """The charts run a fixed 24 h back / 36 h forward window, so the display
    needs the whole of yesterday, not just today from midnight."""
    arch = str(tmp_path / "plans")
    yday, today = date(2026, 9, 1), date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 8, 31, 23, 0), yday, soc0=0.5,
                        pv=[1000.0] * 96, load=[500.0] * 96, grid=[-650.0] * 96)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), today, soc0=0.5,
                        pv=[1000.0] * 96, load=[500.0] * 96, grid=[-650.0] * 96)
    now = local(2026, 9, 2, 12, 0)
    prev_pv, prev_load = [1234.0] * 96, [777.0] * 96
    r = core.rolled_slices(arch, now, AMS, prev_pv_w=prev_pv, prev_load_w=prev_load)
    y = r["yesterday"]
    assert y["date"] == "2026-09-01" and y["slice_start"].startswith("2026-09-01T00:00")
    assert y["n"] == 96 and y["n_past"] == 96          # every step of it is behind us
    assert y["actuals_used"] is True
    # measured PV and load substituted for the WHOLE day, grid moved by both errors
    assert y["p_pv_w"][0] == pytest.approx(1234.0) and y["p_load_w"][0] == pytest.approx(777.0)
    assert y["p_grid_w"][0] == pytest.approx(-650.0 + (777.0 - 500.0) - (1234.0 - 1000.0))
    assert len(y["buy"]) == 96 and len(y["sell"]) == 96
    # its own chain, anchored by virtual_soc_at at ITS midnight, not today's
    assert y["soc_source"] == "virtual" and y["soc_start_pct"] == pytest.approx(50.0)
    assert y["sources"] and r["today"] is not None


def test_rolled_slices_yesterday_is_none_when_the_archive_does_not_reach_back(tmp_path):
    arch = str(tmp_path / "plans")
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), date(2026, 9, 2))
    r = core.rolled_slices(arch, local(2026, 9, 2, 12, 0), AMS)
    assert r["yesterday"] is None and r["today"] is not None


def test_rolled_slices_carries_the_day_after_from_the_newest_plan_covering_it(tmp_path):
    """The forecast-against-actual chart's window reaches 36 h ahead, so from
    noon it shows D+2 and needs a fourth gross slice for it. Same lookup as
    tomorrow: the newest Optimal plan covering the whole day, made whenever."""
    arch = str(tmp_path / "plans")
    today = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), today)
    write_full_day_plan(arch, local(2026, 9, 2, 8, 0), date(2026, 9, 3), pv=[500.0] * 96)
    write_full_day_plan(arch, local(2026, 9, 2, 9, 0), date(2026, 9, 4), pv=[2500.0] * 96, load=[300.0] * 96)
    r = core.rolled_slices(arch, local(2026, 9, 2, 12, 0), AMS)
    a = r["day_after"]
    assert a["date"] == "2026-09-04" and a["slice_start"].startswith("2026-09-04T00:00")
    assert a["n"] == 96 and a["full_day"] is True and "n_past" not in a     # compact_slice: all future
    assert a["p_pv_w"][40] == pytest.approx(2500.0) and a["p_load_w"][40] == pytest.approx(300.0)
    assert r["next_day"]["date"] == "2026-09-03"


def test_rolled_slices_day_after_is_none_when_no_plan_reaches_it(tmp_path):
    """A solve made without a day-3 Solcast list ends at 24:00 of D+1."""
    arch = str(tmp_path / "plans")
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), date(2026, 9, 2))
    write_full_day_plan(arch, local(2026, 9, 2, 8, 0), date(2026, 9, 3))
    r = core.rolled_slices(arch, local(2026, 9, 2, 12, 0), AMS)
    assert r["next_day"] is not None and r["day_after"] is None


def test_a_replay_doc_never_shadows_the_plans_that_actually_ran(tmp_path):
    """replay_plan archives a re-solve under TODAY's filename while keeping the
    source plan's pre-midnight plan_ts, so filename order stops meaning plan_ts
    order. Found live 2026-09-04: the whole of yesterday's virtual lane was
    served by one plan stamped two days earlier, every hourly re-plan of that
    day passed over, because virtual_day scanned reversed(list_plans)."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    # what actually ran: last night's plan, then a re-plan at 12:00 on the day
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.5, p_batt=[-1000.0] * 96)
    made = local(2026, 9, 2, 12, 0, 30)
    t0, n = core.horizon(made, AMS)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n,
                                         "tz": AMS, "soc_init": 0.5, "soc_final": 0.5,
                                         "optim_status": "Optimal", "n_predicted_steps": 42,
                                         "pv_gap_steps": 0, "rows": make_rows(t0, n)})
    # a replay run TOMORROW, re-solving that day: newest file, oldest plan_ts
    later = local(2026, 9, 3, 19, 2)
    t0r = local(2026, 9, 1, 23, 15)
    nr = 96 + 4
    core.write_plan_archive(arch, later, {"plan_ts": local(2026, 9, 1, 23, 0).isoformat(),
                                          "t0": t0r.isoformat(), "n": nr, "tz": AMS,
                                          "soc_init": 0.5, "soc_final": 0.5, "replay": True,
                                          "optim_status": "Optimal", "n_predicted_steps": 99,
                                          "pv_gap_steps": 0, "rows": make_rows(t0r, nr)})
    assert len(core.list_plans(arch)) == 3
    orgs = core.organic_plans(arch)
    assert len(orgs) == 2                                        # the replay is out
    assert [d["plan_ts"] for d in orgs] == sorted((d["plan_ts"] for d in orgs), reverse=True)
    vd = core.virtual_day(arch, day, AMS, local(2026, 9, 3, 19, 30))
    assert made.isoformat() in vd["sources"]                     # the 12:00 re-plan still won its steps
    assert local(2026, 9, 1, 23, 0).isoformat() in vd["sources"]  # and last night's kept the earlier ones
    assert len(vd["sources"]) == 2
    # scoring is a different question: plan_for_day still prefers the newest FILE,
    # which is how a replay deliberately becomes the plan of record
    assert core.plan_for_day(arch, day, AMS)[0].get("replay") is True


def test_deye_response_tou_target_stops_at_the_target_not_the_clamp():
    """2 points of SOC is 3,9 kW over one step, well inside the 8 kW cap, so the
    TARGET binds and the pack stops there."""
    cmd = core.deye_command(p_grid_w=-9000.0, p_batt_w=8000.0, pack_v=51.2, target_soc=0.78)
    r = core.deye_response(cmd, main_pot_w=0.0, micro_w=0.0, load_w=500.0, soc=0.80)
    assert r["batt_w"] == pytest.approx(0.02 * core.CAPACITY_KWH * 1000.0 / core.STEP_H, abs=1.0)


def test_deye_response_tou_power_cap_binds_when_the_target_is_far():
    """5 points would be 9,6 kW, past the cap, so the CAP binds instead."""
    cmd = core.deye_command(p_grid_w=-9000.0, p_batt_w=8000.0, pack_v=51.2, target_soc=0.30)
    r = core.deye_response(cmd, main_pot_w=0.0, micro_w=0.0, load_w=500.0, soc=0.80)
    assert r["batt_w"] == pytest.approx(8000.0, abs=64.0)


def test_deye_response_tou_target_charges_when_the_pack_is_below_it():
    cmd = core.deye_command(p_grid_w=-9000.0, p_batt_w=8000.0, pack_v=51.2, target_soc=0.60)
    r = core.deye_response(cmd, main_pot_w=9000.0, micro_w=0.0, load_w=500.0, soc=0.55)
    assert r["batt_w"] < 0


# ---- rehydrate: republish only the solve the archive knows ---------------------

def _last_run(ts, action="naive-mpc-optim", status="ok"):
    return {"status": status, "timestamp": ts, "action": action, "emhass_version": "0.18.2"}


def test_addon_holds_plan_when_last_run_is_the_archived_solve():
    doc = _last_run("2026-09-07T07:35:03Z")
    assert core.addon_holds_plan(doc, _last_run("2026-09-07T07:35:03Z")) is True


def test_addon_holds_plan_rejects_a_solve_the_archive_never_saw():
    # 09:12 harness solves of old plans, republished at 09:28 as the live plan (2026-09-07)
    doc = _last_run("2026-09-07T07:05:04Z")
    assert core.addon_holds_plan(doc, _last_run("2026-09-07T07:12:13Z")) is False


def test_addon_holds_plan_needs_both_records():
    doc = _last_run("2026-09-07T07:35:03Z")
    assert core.addon_holds_plan(None, _last_run("2026-09-07T07:35:03Z")) is False
    assert core.addon_holds_plan(doc, None) is False
    assert core.addon_holds_plan(doc, _last_run("2026-09-07T07:35:03Z", action="publish-data")) is False
    assert core.addon_holds_plan(doc, _last_run("2026-09-07T07:35:03Z", status="error")) is False


def test_rehydrate_reports_the_newest_organic_plans_last_run(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    res = core.run_plan(plan_inputs(now, tmp_path, stub.url))
    assert res["ok"], res
    doc = core.load_plan(res["archive_path"])
    out = core.rehydrate(str(tmp_path / "plans"), str(tmp_path / "scores.csv"), AMS,
                         (now + timedelta(minutes=20)).isoformat(), 26.0)
    assert out["plan_fresh"] is True
    assert out["newest_last_run"] == doc["last_run"]


# ---- the live knob layer and the A/B walk ------------------------------------------

def test_knobs_defaults_and_overlay():
    d = core.knobs(None)
    assert d["soc_final"] == core.SOC_FINAL_TARGET and d["stress_scale"] == 1.0 and d["soc_target"] == 0.0
    assert d["aux_cut_on_ratio"] == 1.5 and d["pv_p10_mix"] == core.PV_P10_MIX and d["soc_target_at"] == "17:00"
    k = core.knobs({"soc_final": 0.6, "soc_min": None, "unknown": 1})
    assert k["soc_final"] == 0.6 and k["soc_min"] == core.SOC_MIN and "unknown" not in k


def test_soc_target_timestep_inside_the_horizon_only():
    t0 = local(2026, 9, 2, 14, 15)
    assert core.soc_target_timestep(t0, 135, date(2026, 9, 2), "17:00") == 11
    assert core.soc_target_timestep(t0, 135, date(2026, 9, 2), "23:45") == 38
    assert core.soc_target_timestep(t0, 135, date(2026, 9, 2), "13:00") is None       # already past
    assert core.soc_target_timestep(t0, 135, date(2026, 9, 2), "14:15") is None       # at t0: not a target
    assert core.soc_target_timestep(t0, 10, date(2026, 9, 2), "17:00") is None        # beyond a short horizon
    assert core.soc_target_timestep(t0, 135, date(2026, 9, 2), "nonsense") is None


def test_run_plan_applies_the_live_knobs(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url)
    # a settled clock with a full on record yesterday, so the deficit knobs (not the pull) show
    inp["rebalance"] = {"last_full": (now - timedelta(days=1)).isoformat()}
    inp["knobs"] = {"stress_scale": 2.0, "soc_final": 0.6, "weight_battery_discharge": 0.02,
                    "batt_power_max_w": 8000.0, "soc_min": 0.15, "soc_max": 0.95,
                    "soc_target": 1.0, "soc_target_at": "17:00",
                    "deficit_threshold": 0.25, "deficit_cost": 0.02, "surplus_base": 0.01}
    res = core.run_plan(inp)
    assert res["ok"], res
    body = stub.posts[0][1]
    buy, sell = body["load_cost_forecast"], body["prod_price_forecast"]
    pi = sum((b + s_) / 2.0 for b, s_ in zip(buy, sell)) / len(buy)
    assert body["battery_stress_cost"] == pytest.approx(2 * round(12.5 * core.Q_PORT * pi, 5), abs=2e-5)
    assert body["soc_final"] == 0.6 and body["weight_battery_discharge"] == 0.02
    assert body["battery_charge_power_max"] == 8000.0 and body["battery_discharge_power_max"] == 8000.0
    assert body["battery_minimum_state_of_charge"] == 0.15 and body["battery_maximum_state_of_charge"] == 0.95
    assert body["soc_target"] == 1.0 and body["soc_target_timestep"] == 11            # 17:00 from a 14:15 start
    assert body["battery_soc_deficit_threshold"] == 0.25 and body["battery_soc_deficit_cost"] == 0.02
    doc = core.load_plan(res["archive_path"])
    assert doc["knobs"]["soc_final"] == 0.6 and doc["soc_final"] == 0.6 and doc["knobs"]["aux_cut_on_ratio"] == 1.5
    assert res["knobs"]["stress_scale"] == 2.0


def test_run_plan_soc_target_stays_out_when_off_or_past(stub, tmp_path):
    now = datetime.now(Z).replace(hour=14, minute=0, second=4, microsecond=0)
    t0, n = core.horizon(now, AMS)
    stub.plan_fn = lambda posts: make_rows(t0, n)
    inp = plan_inputs(now, tmp_path, stub.url)
    inp["knobs"] = {"soc_target": 0.0, "soc_target_at": "17:00"}
    core.run_plan(inp)
    assert "soc_target" not in stub.posts[0][1] and "soc_target_timestep" not in stub.posts[0][1]
    inp["knobs"] = {"soc_target": 1.0, "soc_target_at": "13:00"}
    core.run_plan(inp)
    assert "soc_target_timestep" not in stub.posts[1][1]


def write_ab_plan(arch, made, pv=1000.0, load=500.0, micro=None, knobs=None, n=None):
    """An archived solve with a payload the A/B can re-run: lists on the horizon,
    stress keys, the must-take lane, optional knobs."""
    t0, nn = core.horizon(made, AMS)
    n = n or nn
    mic = [micro or 0.0] * n
    pay = {"optimization_time_step": 15, "prediction_horizon": n, "soc_init": 0.5, "soc_final": 0.5,
           "pv_power_forecast": [pv - (micro or 0.0)] * n, "load_cost_forecast": [0.28] * n,
           "prod_price_forecast": [0.10] * n, "load_power_forecast": [load - (micro or 0.0)] * n,
           "battery_stress_cost": 0.004, "inverter_stress_cost": 0.001, "battery_soc_surplus_cost": 0.005,
           "battery_soc_deficit_threshold": 0.2, "battery_soc_deficit_cost": 0.01}
    rows = make_rows(t0, n, pv=[pv] * n)
    core.write_plan_archive(arch, made, {"plan_ts": made.isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS,
                                         "soc_init": 0.5, "soc_final": 0.5, "optim_status": "Optimal",
                                         "n_predicted_steps": 0, "pv_gap_steps": 0, "payload": pay, "rows": rows,
                                         "pv_micro_w": mic, "growatt_share": 0.288 if micro else None,
                                         "knobs": knobs or {}})
    return t0, n


def test_ab_solves_picks_by_cadence(tmp_path):
    arch = str(tmp_path / "plans")
    for hh, mm in ((10, 5), (10, 35), (11, 2), (11, 5), (12, 5)):
        write_ab_plan(arch, local(2026, 9, 2, hh, mm))
    day = date(2026, 9, 2)
    t60 = [(t.strftime("%H:%M"), sh) for t, _d, sh in core._ab_solves(arch, day, AMS, 60)]
    assert t60 == [("10:15", 0), ("11:15", 0), ("12:15", 0)]
    t30 = [(t.strftime("%H:%M"), sh) for t, _d, sh in core._ab_solves(arch, day, AMS, 30)]
    assert t30 == [("10:15", 0), ("10:45", 0), ("11:15", 0), ("12:15", 0)]                # 11:02 and 11:05 share a slot
    t15 = [(t.strftime("%H:%M"), sh) for t, _d, sh in core._ab_solves(arch, day, AMS, 15)]
    assert t15[:5] == [("10:15", 0), ("10:30", 1), ("10:45", 0), ("11:00", 1), ("11:15", 0)]
    assert t15[5:9] == [("11:30", 1), ("11:45", 2), ("12:00", 3), ("12:15", 0)]
    assert t15[-1][1] > 40                                                               # the last document runs to midnight in quarters


def test_ab_payload_shifts_and_applies_the_knobs(tmp_path):
    arch = str(tmp_path / "plans")
    made = local(2026, 9, 2, 10, 5)
    t0, n = write_ab_plan(arch, made, pv=1000.0, load=500.0, micro=300.0)
    doc = core.load_plan(core.list_plans(arch)[-1])
    doc["pv_p50_w"] = [1000.0] * n
    doc["pv_p10_w"] = [500.0] * n
    pay, micro, cut_ready, notes, times, kn = core._ab_payload(doc, 2, {}, date(2026, 9, 2), AMS, None)
    assert pay["prediction_horizon"] == n - 2 and len(pay["pv_power_forecast"]) == n - 2 and len(micro) == n - 2
    assert times[0] == t0 + timedelta(minutes=30) and cut_ready is True and notes == []
    assert pay["load_power_forecast"][0] == pytest.approx(200.0)                        # 500 - 300, as archived
    ov = {"stress_scale": 2.0, "soc_final": 0.7, "soc_target": 1.0, "soc_target_at": "17:00",
          "growatt_share": 0.576, "pv_p10_mix": 0.2, "batt_power_max_w": 9000.0, "soc_min": 0.2,
          "weight_battery_discharge": 0.03, "deficit_cost": 0.02, "surplus_base": 0.01}
    pay, micro, cut_ready, notes, times, kn = core._ab_payload(doc, 0, ov, date(2026, 9, 2), AMS, None)
    assert pay["battery_stress_cost"] == pytest.approx(0.008) and pay["inverter_stress_cost"] == pytest.approx(0.002)
    assert pay["soc_final"] == 0.7 and pay["soc_target"] == 1.0
    assert pay["soc_target_timestep"] == core.soc_target_timestep(t0, n, date(2026, 9, 2), "17:00")
    assert pay["battery_charge_power_max"] == 9000.0 and pay["battery_minimum_state_of_charge"] == 0.2
    assert pay["weight_battery_discharge"] == 0.03 and pay["battery_soc_deficit_cost"] == 0.02
    assert pay["battery_soc_surplus_cost"] == pytest.approx(0.01)                         # 0,005 x 0,01 / 0,005
    # the mix pulls the total from 1000 to 900 and the must-take lane scales with it (300 x 2 for the share, x 0,9)
    assert micro[0] == pytest.approx(540.0) and pay["pv_power_forecast"][0] == pytest.approx(360.0)
    assert pay["load_power_forecast"][0] == pytest.approx(500.0 - 540.0)
    assert any("pv_p10_mix" in x for x in notes) and any("growatt_share" in x for x in notes)
    # perfect load foresight replaces the day's load entries
    pay, *_ = core._ab_payload(doc, 0, {"load": "actual"}, date(2026, 9, 2), AMS, [800.0] * 96)
    assert pay["load_power_forecast"][0] == pytest.approx(800.0 - 300.0)


def test_ab_payload_moves_the_archived_target_with_the_shift(tmp_path):
    arch = str(tmp_path / "plans")
    t0, n = write_ab_plan(arch, local(2026, 9, 2, 10, 5), pv=1000.0, load=500.0, micro=300.0)
    doc = core.load_plan(core.list_plans(arch)[-1])
    doc["payload"]["soc_target"], doc["payload"]["soc_target_timestep"] = 0.9, 10
    pay, *_ = core._ab_payload(doc, 0, {}, date(2026, 9, 2), AMS, None)
    assert pay["soc_target_timestep"] == 10                       # unshifted: as archived
    pay, *_ = core._ab_payload(doc, 3, {}, date(2026, 9, 2), AMS, None)
    assert pay["soc_target_timestep"] == 7 and pay["soc_target"] == 0.9   # three steps later, same wall-clock target
    pay, *_ = core._ab_payload(doc, 10, {}, date(2026, 9, 2), AMS, None)
    assert "soc_target" not in pay and "soc_target_timestep" not in pay   # the target is now at or before t0
    pay, *_ = core._ab_payload(doc, 3, {"soc_target": 0.8}, date(2026, 9, 2), AMS, None)
    assert pay["soc_target_timestep"] == core.soc_target_timestep(t0 + timedelta(minutes=45), n - 3, date(2026, 9, 2), "17:00")


def test_ab_summary_finds_the_clock_marks_on_a_dst_day():
    # 2026-10-25 has 100 steps; 17:00 is index 72, not 68, and 10:00 is 44
    n = 100
    soc = [50.0] * n
    soc[72] = 60.0                                                # 17:00 on the long day
    soc[68] = 55.0                                                # what a 96-step index would read
    g = [0.0] * n
    g[44] = 4000.0                                                # 10:00 local, one step: 1 kWh
    c = [0.0] * n
    c[72] = 4000.0                                                # 17:00, outside the 10-17 window
    lanes = {"soc_pct": soc, "p_grid_w": g, "pv_curtail_w": c, "buy": [0.3] * n, "sell": [0.1] * n}
    s = core.ab_summary(lanes, n, 48.2, date(2026, 10, 25), AMS)
    assert s["soc_17h_pct"] == 60.0 and s["import_06_18_kwh"] == 1.0 and s["declined_10_17_kwh"] == 0.0
    assert s["peak_at"] == "17:00"
    naive = core.ab_summary(lanes, n, 48.2)                        # the 96-step arithmetic, an hour off after the fold
    assert naive["soc_17h_pct"] == 55.0 and naive["peak_at"] == "18:00"
    # a plain day gives the same figures either way
    lanes96 = {k: v[:96] for k, v in lanes.items()}
    assert core.ab_summary(lanes96, 96, 48.2, date(2026, 9, 5), AMS) == core.ab_summary(lanes96, 96, 48.2)


def test_ab_walk_reports_a_gap_before_the_first_solve(stub, tmp_path):
    arch = str(tmp_path / "plans")
    # last night's plan ends at 04:00 (its rows are cut), the day's first solve is at 06:05:
    # 04:00-06:00 has no plan in force, so the walk has no settled step to start from
    t0, n = write_ab_plan(arch, local(2026, 9, 1, 23, 5), pv=1000.0, load=500.0, micro=300.0)
    path = core.list_plans(arch)[-1]
    doc = core.load_plan(path)
    keep = [r for r in doc["rows"] if core._parse_ts(r["timestamp"]).astimezone(Z) < local(2026, 9, 2, 4, 0)]
    doc["rows"] = keep
    doc["n"] = len(keep)
    core.write_plan_archive(arch, local(2026, 9, 1, 23, 5), doc)
    write_ab_plan(arch, local(2026, 9, 2, 6, 5), pv=1000.0, load=500.0, micro=300.0)
    act = {"pv_w": [1000.0] * 96, "load_w": [500.0] * 96, "micro_w": [300.0] * 96, "curtailed": [0.0] * 96}
    out = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {}, act, 60, "closed", 48.0)
    assert out["status"] == "no_chain" and "before the first solve" in out["message"]
    assert stub.posts == []                                        # nothing was solved


def test_ab_walk_chains_the_settled_soc_and_writes_nothing(stub, tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.5, pv=[1000.0] * 96, load=[500.0] * 96,
                        p_batt=[-400.0] * 96, grid=[-100.0] * 96)
    write_ab_plan(arch, local(2026, 9, 2, 10, 5), pv=1000.0, load=500.0)
    write_ab_plan(arch, local(2026, 9, 2, 12, 5), pv=1000.0, load=500.0)
    before = core.list_plans(arch)
    stub.plan_fn = lambda posts: make_rows(local(2026, 9, 2, 10, 15), posts[-1][1]["prediction_horizon"],
                                           soc0=posts[-1][1]["soc_init"], pv=[1000.0] * posts[-1][1]["prediction_horizon"])
    act = {"pv_w": [1000.0] * 96, "load_w": [500.0] * 96, "micro_w": [0.0] * 96, "curtailed": [0.0] * 96}
    r = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {}, act, 30, "closed", 48.2)
    assert r["status"] == "ok", r
    assert r["solves"] == 2 and len(r["solve_log"]) == 2
    i1 = core._slot(local(2026, 9, 2, 12, 15))
    assert r["solve_log"][1]["soc_init"] == pytest.approx(r["lanes"]["soc_pct"][i1 - 1] / 100.0, abs=1e-4)
    i0 = core._slot(local(2026, 9, 2, 10, 15))
    assert all(v is not None for v in r["lanes"]["soc_pct"]) and r["lanes"]["believed_soc_pct"][i0] is not None
    assert r["lanes"]["p_grid_w"][i0 - 1] == pytest.approx(-100.0)                      # before the first solve: the executed lane
    sm = r["summary"]
    for k in ("soc_17h_pct", "peak_soc_pct", "peak_at", "declined_10_17_kwh", "import_day_kwh", "export_day_kwh",
              "cash_eur", "soc_term_eur", "total_eur", "lambda_eur_kwh"):
        assert k in sm
    assert sm["total_eur"] == pytest.approx(sm["cash_eur"] + sm["soc_term_eur"], abs=1e-3)
    assert sm["lambda_eur_kwh"] == pytest.approx(0.9 * 0.10, abs=1e-4)                 # the stub's flat 0,10 sell
    assert r["executed"]["soc_17h_pct"] is not None
    assert core.list_plans(arch) == before                                                # nothing archived
    # an override lands in every posted payload
    stub.posts.clear()
    r2 = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {"soc_target": 1.0, "soc_target_at": "17:00"}, act, 30, "closed", 48.2)
    assert r2["status"] == "ok"
    assert [p[1]["soc_target_timestep"] for p in stub.posts] == [27, 19]                 # 17:00 from 10:15 and from 12:15
    assert r2["solve_log"][0]["soc_target_timestep"] == 27
    bad = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {"nope": 1}, act, 30, "closed", 48.2)
    assert bad["status"] == "unknown_override"
    assert core.ab_walk(arch, stub.url, "2026-09-02", AMS, {}, dict(act, pv_w=None), 30, "closed", 48.2)["status"] == "no_actuals"


def test_ab_apply_plan_maps_knobs_and_reports_the_rest():
    applied, skipped = core.ab_apply_plan({"soc_final": 0.6, "soc_target_at": "16:30", "battery_stress_cost": 0.01,
                                           "load": "actual", "zzz": 1})
    assert applied == {"input_number.emhass_soc_final": 0.6, "input_datetime.emhass_soc_target_at": "16:30"}
    assert set(skipped) == {"battery_stress_cost", "load", "zzz"}
    assert core.ab_validate({"soc_final": 1, "battery_stress_cost": 1, "load": "actual"}) == []
    assert core.ab_validate({"foo": 1, "soc_final": 1}) == ["foo"]


def test_pyscript_wrapper_has_no_generator_expressions():
    """pyscript's AST walker raises NotImplementedError on ast_generatorexp; a
    genexp in the wrapper is a runtime failure that no local test would show
    (2026-09-07: the A/B totals died on one after ten minutes of solves)."""
    import ast, os
    path = os.path.join(os.path.dirname(__file__), "..", "ha", "pyscript", "emhass_shadow.py")
    tree = ast.parse(open(path).read())
    offenders = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.GeneratorExp)]
    assert offenders == [], f"generator expressions at lines {offenders}"


def test_ab_store_and_load_roundtrip(tmp_path):
    ab = str(tmp_path / "ab")
    path = core.ab_store(ab, "trial-1", {"label": "trial-1", "days": [{"day": "2026-09-05"}]})
    assert path.endswith("trial-1.json.gz") and core.ab_load(ab, "trial-1")["days"][0]["day"] == "2026-09-05"
    assert core.ab_load(ab, "nope") is None


# ---- no curtailment on residuals while selling pays (2026-09-08) -------------------
# On 2026-09-07 the self_balance command shut export and pinned the clamp to the
# plan's P_batt on every charging step, so 2,3 kWh of sun above forecast was
# thrown away at sell prices of 0,04 to 0,19 while the pack had headroom. The
# rule now: while export pays and the plan declines nothing, the windfall lands
# on whichever flow the plan has bigger, the pack or the meter, and nothing is
# curtailed. Only a non-positive sell price or a planned curtailment shuts
# export, and then the clamp carries the insurance margin.

def test_deye_command_self_balance_at_a_paying_price_keeps_export_on_and_the_clamp_at_nameplate():
    c = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=0.07)
    assert c["intent"] == "self_balance"
    assert c["export_surplus"] is True
    assert c["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A
    assert c["battery_grid_charging"] is False


def test_deye_command_export_dominant_charge_keeps_the_plan_clamp_and_export_on():
    """The 11:30 step: bank 1,6 kW, sell 4,7 kW at 0,098. The clamp holds the
    plan's charge so the windfall goes where the plan's bigger flow goes: out."""
    c = core.deye_command(p_grid_w=-4719.0, p_batt_w=-1631.0, pack_v=51.2, sell=0.098)
    assert c["intent"] == "self_balance"
    assert c["export_surplus"] is True
    assert c["battery_max_charging_current"] == pytest.approx(32.0)     # 1631/51,2 = 31,9


def test_deye_command_self_balance_at_a_negative_price_shuts_export_and_banks_at_nameplate():
    """A NON-CURTAILED STEP IS TAKE-ALL-THE-SUN AT ANY PRICE (2026-09-12).
    11:30 on 09-12: sell just turned negative, the plan charged 5,46 kW at grid
    zero and declined nothing, then went on to IMPORT at 0,023 from 12:15 to
    fill the pack. The clamp sat on the plan's P_batt, the sun came in 770 W
    above Solcast and the virtual Deye threw it away, worth at least the buy
    price it displaced. P_batt on such a step is the forecast surplus, a
    consequence like P_grid, never a target: export off, clamp at nameplate."""
    c = core.deye_command(p_grid_w=0.0, p_batt_w=-5462.0, pack_v=51.2, sell=-0.0042)
    assert c["intent"] == "self_balance"
    assert c["export_surplus"] is False
    assert c["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A


def test_deye_command_a_planned_curtailment_at_a_negative_price_keeps_the_plan_clamp():
    """Only the LP's own decline is a target: the headroom it leaves is on purpose."""
    c = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=-0.01, pv_curtail_w=800.0)
    assert c["export_surplus"] is False
    assert c["battery_max_charging_current"] == pytest.approx(98.0)      # 5000/51,2 = 97,7


def test_deye_command_the_writer_margin_is_need_plus_20pct_or_20A_whichever_is_bigger():
    """5.000 W -> max(6.000, 6.024) = 118 A; 10.000 W -> max(12.000, 11.024) = 234 A.
    Writer policy only: settlement never asks for it."""
    c = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=-0.01, margin=True, pv_curtail_w=1.0)
    assert c["export_surplus"] is False
    assert c["battery_max_charging_current"] == pytest.approx(118.0)
    big = core.deye_command(p_grid_w=0.0, p_batt_w=-10000.0, pack_v=51.2, sell=0.0, margin=True, pv_curtail_w=1.0)
    assert big["battery_max_charging_current"] == pytest.approx(234.0)
    paying = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=0.07, margin=True)
    assert paying["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A  # no clamp to add a margin to
    banking = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=-0.01, margin=True)
    assert banking["battery_max_charging_current"] == core.DEYE_CURRENT_MAX_A  # nor here: nameplate already


def test_deye_command_a_planned_curtailment_shuts_export_even_at_a_paying_price():
    c = core.deye_command(p_grid_w=0.0, p_batt_w=-5000.0, pack_v=51.2, sell=0.07, pv_curtail_w=800.0)
    assert c["export_surplus"] is False


def test_settle_step_windfall_lands_on_the_pack_when_the_plan_banks():
    """12:15 on 09-07: plan banks 6,9 kW at grid zero from 7,2 kW of sun, 8,4 kW
    shows up, the pack is at 14 %. Every extra watt charges; nothing is declined."""
    g, b, c = core.settle_step(0.0, -6903.8, 7176.9, 259.1, 0.0, 8413.0, 186.0, 2201.0, 2201.0, 0.139,
                               sell=0.074)
    assert c == pytest.approx(0.0)
    assert g == pytest.approx(0.0, abs=1.0)
    assert b == pytest.approx(-6903.8 - (8413.0 - 7176.9) - (259.1 - 186.0), abs=1.0)


def test_settle_step_windfall_exports_when_the_plan_sells():
    """11:30 on 09-07: bank 1,6 kW, sell 4,7 kW. 307 W more sun and 41 W less
    load: the meter takes both, the pack stays on the plan, nothing is declined."""
    g, b, c = core.settle_step(-4719.0, -1631.0, 6629.2, 241.2, 0.0, 6936.0, 200.0, 1881.0, 1881.0, 0.10,
                               sell=0.098)
    assert c == pytest.approx(0.0)
    assert b == pytest.approx(-1631.0, abs=1.0)
    assert g == pytest.approx(-4719.0 - (6936.0 - 6629.2) - (241.2 - 200.0), abs=1.0)


def test_settle_step_at_a_negative_price_the_windfall_lands_on_the_pack():
    """Selling pays nothing and the plan declined nothing: export stays shut and
    every extra watt charges. The pack had headroom, the LP had a use for it."""
    g, b, c = core.settle_step(0.0, -5000.0, 5500.0, 500.0, 0.0, 7500.0, 500.0, 0.0, 0.0, 0.5, sell=-0.01)
    assert g == pytest.approx(0.0, abs=1.0)
    assert b == pytest.approx(-7000.0, abs=1.0)
    assert c == pytest.approx(0.0)


def test_settle_step_at_a_negative_price_the_windfall_beyond_a_planned_curtailment_is_declined():
    """The LP itself declined 1.500 W of the forecast: that headroom is a
    decision, the clamp holds the plan's charge and the extra sun joins the
    declined lane."""
    g, b, c = core.settle_step(0.0, -5000.0, 7000.0, 500.0, 1500.0, 7500.0, 500.0, 0.0, 0.0, 0.5, sell=-0.01)
    assert g == pytest.approx(0.0, abs=1.0)
    assert b == pytest.approx(-5000.0, abs=1.0)
    assert c == pytest.approx(1500.0 + 500.0, abs=1.0)


def test_settle_slice_reads_the_sell_lane():
    n = 2
    compact = {"n": n, "soc_start_pct": 50.0,
               "p_grid_w": [0.0] * n, "p_batt_w": [-5000.0] * n,
               "p_pv_w": [5500.0] * n, "p_load_w": [500.0] * n,
               "pv_curtail_w": [0.0] * n, "soc_pct": [50.0] * n,
               "buy": [0.3] * n, "sell": [0.1, -0.01]}
    out = core.settle_slice(compact, [6500.0] * n, [500.0] * n)
    assert out["pv_curtail_w"][0] == pytest.approx(0.0) and out["p_batt_w"][0] == pytest.approx(-6000.0, abs=1.0)
    assert out["pv_curtail_w"][1] == pytest.approx(0.0) and out["p_batt_w"][1] == pytest.approx(-6000.0, abs=1.0)
    assert out["p_grid_w"][1] == pytest.approx(0.0, abs=1.0)          # the same windfall banked, export shut at -0,01


# ---- replaying the writer's margin through the settlement (2026-09-08) -------------

def test_settle_step_with_margin_charges_faster_on_a_curtailed_step():
    """Plan declines 1.500 of 7.000 W at grid zero, charging 5.000. Same sun, same
    load: with the margin the real pack takes 118 A x 51,2 = 6.041,6 W, the plan's
    clamp took 5.017,6 (98 A), so the settled charge grows by the difference and
    the declined lane shrinks by it."""
    g, b, c = core.settle_step(0.0, -5000.0, 7000.0, 500.0, 1500.0, 7000.0, 500.0, 0.0, 0.0, 0.5,
                               sell=-0.01, margin=True)
    assert g == pytest.approx(0.0, abs=1.0)
    assert b == pytest.approx(-5000.0 - (6041.6 - 5017.6), abs=1.0)
    assert c == pytest.approx(1500.0 - (6041.6 - 5017.6), abs=1.0)
    plain = core.settle_step(0.0, -5000.0, 7000.0, 500.0, 1500.0, 7000.0, 500.0, 0.0, 0.0, 0.5, sell=-0.01)
    assert plain[1] == pytest.approx(-5000.0, abs=1.0) and plain[2] == pytest.approx(1500.0, abs=1.0)


def test_settle_step_with_margin_changes_nothing_while_selling_pays():
    a = core.settle_step(0.0, -5000.0, 5500.0, 500.0, 0.0, 6500.0, 500.0, 0.0, 0.0, 0.5, sell=0.07)
    b = core.settle_step(0.0, -5000.0, 5500.0, 500.0, 0.0, 6500.0, 500.0, 0.0, 0.0, 0.5, sell=0.07, margin=True)
    assert a == b


def test_settle_slice_and_virtual_day_pass_the_margin_through(tmp_path):
    n = 2
    compact = {"n": n, "soc_start_pct": 50.0,
               "p_grid_w": [0.0] * n, "p_batt_w": [-5000.0] * n,
               "p_pv_w": [7000.0] * n, "p_load_w": [500.0] * n,
               "pv_curtail_w": [1500.0] * n, "soc_pct": [50.0] * n,
               "buy": [0.02] * n, "sell": [-0.01] * n}
    plain = core.settle_slice(compact, [7000.0] * n, [500.0] * n, margin=False)
    marg = core.settle_slice(compact, [7000.0] * n, [500.0] * n, margin=True)
    assert marg["p_batt_w"][0] < plain["p_batt_w"][0] - 1000.0
    assert marg["pv_curtail_w"][0] < plain["pv_curtail_w"][0] - 1000.0
    vd_plain = _capped_day(tmp_path, local(2026, 9, 2, 3, 0), 7203.6, 299.9, margin=False)
    vd_marg = _capped_day(tmp_path, local(2026, 9, 2, 3, 0), 7203.6, 299.9, margin=True)
    i = core._slot(local(2026, 9, 2, 1, 0))                    # the pack still has headroom here
    assert vd_marg["p_batt_w"][i] < vd_plain["p_batt_w"][i] - 900.0
    assert vd_marg["pv_curtail_w"][i] < vd_plain["pv_curtail_w"][i] - 900.0


def test_clamp_write_holds_the_register_inside_the_deadband():
    """The writer rewrites the charge clamp only when the wanted value has moved
    by more than DEYE_CLAMP_DEADBAND_A from what is standing; inside the band the
    standing value is kept and no write is counted."""
    assert core.DEYE_CLAMP_DEADBAND_A == 20.0
    assert core.clamp_write(100.0, 110.0) == (100.0, False)
    assert core.clamp_write(100.0, 121.0) == (121.0, True)
    assert core.clamp_write(100.0, 79.0) == (79.0, True)
    assert core.clamp_write(None, 50.0) == (50.0, True)                     # nothing standing yet
    assert core.clamp_write(100.0, 130.0, deadband_a=40.0) == (100.0, False)


def test_clamp_write_boundary_and_grid_charge_lift():
    """A move of exactly the deadband is written (2 August 2026: 99 -> 119 A was
    held for four quarters); on a grid-charge step a standing clamp below the
    wanted current is lifted (9 August 2026 13:00: 122 A stood against 142 A
    and capped a paid charge), never lowered inside the deadband."""
    assert core.clamp_write(99.0, 119.0) == (119.0, True)
    assert core.clamp_write(99.0, 118.0) == (99.0, False)
    assert core.clamp_write(122.0, 142.0, lift=True) == (142.0, True)
    assert core.clamp_write(122.0, 130.0, lift=True) == (130.0, True)
    assert core.clamp_write(130.0, 122.0, lift=True) == (130.0, False)
    assert core.clamp_write(122.0, 130.0, lift=False) == (122.0, False)


def test_wanted_clamp_reports_grid_charge():
    amps, lift = core.wanted_clamp(4500.0, -7294.0)
    assert lift is True and amps == pytest.approx(core.deye_amps(7294.0))
    amps, lift = core.wanted_clamp(0.0, -5000.0, sell=-0.01, pv_curtail_w=800.0)
    assert lift is False and amps == pytest.approx(core.deye_amps(5000.0))


def test_settlement_carries_the_margin_by_default():
    """deye.SETTLE_MARGIN drives every settled lane: settle_slice with no margin
    argument charges above the plan on an LP-curtailed step when the sun allows."""
    assert core.SETTLE_MARGIN is True
    n = 4
    compact = {"n": n, "soc_start_pct": 40.0, "p_grid_w": [0.0] * n, "p_batt_w": [-5000.0] * n,
               "p_pv_w": [7000.0] * n, "p_load_w": [300.0] * n, "pv_curtail_w": [1700.0] * n,
               "sell": [-0.01] * n, "pv_micro_w": [0.0] * n, "micro_cut_w": [0.0] * n}
    with_margin = core.settle_slice(compact, [7000.0] * n, [300.0] * n)
    without = core.settle_slice(compact, [7000.0] * n, [300.0] * n, margin=False)
    assert without["p_batt_w"][1] == pytest.approx(-5000.0, abs=1.0)
    assert with_margin["p_batt_w"][1] < -5900.0                     # 20 % / 20 A above the plan's 5 kW



# ---- the pack's own limits close the books (2026-09-08) -------------------------------
# The delta method kept the plan's P_batt on a step where the virtual pack had no
# headroom left (base and actual run were both at zero charge, delta zero, plan
# figure survives), and integrate_soc then clamped the energy away: 4,5 kWh over
# six steps on the Sunday 09-06 margin replay, charged into a pack at 100 % and
# gone. The settled step now respects the pack: charge beyond headroom goes to the
# meter when export pays and to the strings when it does not, discharge below the
# floor comes back from the meter, and the SOC walk lands exactly on the limit.

def test_settle_step_charge_into_a_full_pack_is_declined_when_export_is_off():
    g, b, c = core.settle_step(0.0, -5000.0, 5500.0, 500.0, 0.0, 5500.0, 500.0, 0.0, 0.0, 1.0, sell=-0.01)
    assert b == pytest.approx(0.0, abs=1.0)
    assert g == pytest.approx(0.0, abs=1.0)
    assert c == pytest.approx(5000.0, abs=1.0)


def test_settle_step_charge_into_a_full_pack_exports_when_selling_pays():
    g, b, c = core.settle_step(0.0, -5000.0, 5500.0, 500.0, 0.0, 5500.0, 500.0, 0.0, 0.0, 1.0, sell=0.07)
    assert b == pytest.approx(0.0, abs=1.0)
    assert g == pytest.approx(-5000.0, abs=1.0)
    assert c == pytest.approx(0.0, abs=1.0)


def test_settle_step_takes_exactly_the_headroom_and_lands_on_the_ceiling():
    """1 % of 48,2 kWh is 0,482 kWh; through eta_c that is 2.006 W for a quarter hour."""
    soc = 0.99
    g, b, c = core.settle_step(0.0, -5000.0, 5500.0, 500.0, 0.0, 5500.0, 500.0, 0.0, 0.0, soc, sell=0.07)
    fits = 0.01 * core.CAPACITY_KWH * 1000.0 / core.STEP_H / core.ETA_C
    assert b == pytest.approx(-fits, abs=1.0)
    assert g == pytest.approx(-(5000.0 - fits), abs=1.0)
    soc_after, clamped = core.integrate_soc(soc, b, core.ETA_C, core.ETA_D)
    assert soc_after == pytest.approx(core.SOC_MAX, abs=1e-6) and not clamped


def test_settle_step_discharge_below_the_floor_comes_back_from_the_meter():
    g, b, c = core.settle_step(-9000.0, 8000.0, 1000.0, 500.0, 0.0, 1000.0, 500.0, 0.0, 0.0, core.SOC_MIN, sell=0.25)
    assert b == pytest.approx(0.0, abs=1.0)
    assert g == pytest.approx(-1000.0, abs=1.0)                      # the 8 kW the pack cannot give


def test_settle_slice_pack_balance_closes_over_a_full_day_with_the_margin():
    n = 8
    compact = {"n": n, "soc_start_pct": 96.0,
               "p_grid_w": [0.0] * n, "p_batt_w": [-8000.0] * n,
               "p_pv_w": [9000.0] * n, "p_load_w": [500.0] * n,
               "pv_curtail_w": [500.0] * n, "soc_pct": [96.0] * n,
               "buy": [0.02] * n, "sell": [-0.01] * n}
    out = core.settle_slice(compact, [9000.0] * n, [500.0] * n, margin=True)
    charged = -sum(v for v in out["p_batt_w"] if v < 0) * core.STEP_H / 1000.0 * core.ETA_C
    assert out["soc_pct"][-1] == pytest.approx(100.0, abs=0.01)
    assert charged == pytest.approx((100.0 - 96.0) / 100.0 * core.CAPACITY_KWH, abs=0.02)
    assert sum(out["pv_curtail_w"]) > 0.0                            # the rest was declined, not lost


def test_ab_walk_margin_override_charges_faster_and_chains_the_higher_soc(stub, tmp_path):
    """`margin: true` is an A/B override: the walk settles every step under the
    writer's clamp margin, so on a curtailed step the pack takes more and the
    next solve starts from the higher SOC. It is actuator policy, so ab_apply
    never pushes it into a helper."""
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.5, pv=[7000.0] * 96, load=[500.0] * 96,
                        p_batt=[0.0] * 96, grid=[-6500.0] * 96)          # idle until the walk, so headroom remains
    write_ab_plan(arch, local(2026, 9, 2, 10, 5), pv=7000.0, load=500.0)
    write_ab_plan(arch, local(2026, 9, 2, 12, 5), pv=7000.0, load=500.0)

    def plan(posts):
        pay = posts[-1][1]
        n = pay["prediction_horizon"]
        rows = make_rows(local(2026, 9, 2, 10, 15), n, soc0=pay["soc_init"], pv=[7000.0] * n)
        for r in rows:
            r.update(P_PV_curtailment=1500.0, P_Load=500.0, P_batt=-5000.0, P_grid=0.0, unit_prod_price=-0.01)
        return rows
    stub.plan_fn = plan
    act = {"pv_w": [7000.0] * 96, "load_w": [500.0] * 96, "micro_w": [0.0] * 96, "curtailed": [0.0] * 96}
    base = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {"margin": False}, act, 30, "closed", 48.2)
    marg = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {}, act, 30, "closed", 48.2)   # the live default carries it
    assert base["status"] == "ok" and marg["status"] == "ok", (base.get("message"), marg.get("message"))
    i = core._slot(local(2026, 9, 2, 10, 30))
    assert marg["lanes"]["p_batt_w"][i] < base["lanes"]["p_batt_w"][i] - 900.0
    assert marg["lanes"]["pv_curtail_w"][i] < base["lanes"]["pv_curtail_w"][i] - 900.0
    assert marg["solve_log"][1]["soc_init"] > base["solve_log"][1]["soc_init"]
    applied, skipped = core.ab_apply_plan({"margin": True})
    assert applied == {} and "margin" in skipped


# ---- dwell above 95 % (2026-09-08) -------------------------------------------------
# How long the virtual pack sits near full is the cost side of front-loading, so
# it is counted wherever a settled SOC lane exists: the A/B summary, the live
# day slices and the score row.

def test_soc_dwell_h_counts_quarter_hours_at_or_above_the_threshold():
    assert core.soc_dwell_h([None, 94.9, 95.0, 100.0, 96.0, 50.0]) == pytest.approx(0.75)
    assert core.soc_dwell_h([94.9] * 96) == 0.0
    assert core.soc_dwell_h([100.0] * 96) == pytest.approx(24.0)
    assert core.soc_dwell_h([90.0, 91.0], threshold_pct=90.0) == pytest.approx(0.5)


def test_ab_summary_reports_dwell_above_95():
    n = 96
    lanes = {"soc_pct": [50.0] * 40 + [96.0] * 8 + [50.0] * 48, "p_grid_w": [0.0] * n, "pv_curtail_w": [0.0] * n,
             "buy": [0.2] * n, "sell": [0.1] * n}
    assert core.ab_summary(lanes, n)["dwell_95_h"] == pytest.approx(2.0)


def test_virtual_day_carries_dwell_above_95(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.99, p_batt=[-6000.0] * 96)
    vd = core.virtual_day(arch, day, AMS, local(2026, 9, 3, 0, 0))          # the whole day is past
    assert vd["dwell_95_h"] == pytest.approx(24.0)
    half = core.virtual_day(arch, day, AMS, local(2026, 9, 2, 12, 0))       # only the past half counts
    assert half["dwell_95_h"] == pytest.approx(12.0)


def test_score_row_carries_dwell_above_95():
    plan = flat_plan()
    act = flat_actuals(plan, g_meas=2000.0)
    realised = {"cash_eur": 7.2, "soc_start_pct": 50.0, "soc_end_pct": 50.0, "pv_kwh": 40.0, "load_kwh": 10.0}
    row = core.score_row(date(2026, 9, 2), plan, realised, 48.0, 0.9, actuals=act)
    assert "dwell_95_h" in core.SCORE_COLUMNS
    assert row["dwell_95_h"] == pytest.approx(0.0)            # a flat 50 % day never sits near full


# ---- the clamp deadband inside the settlement (2026-09-08) ---------------------------
# The writer rewrites battery_max_charging_current only when the wanted value has
# moved by more than DEYE_CLAMP_DEADBAND_A from what is standing. The settlement
# now keeps that standing clamp per day and drives the ACTUAL run with it (the
# base run stays on the plan's own command), and counts the writes, so the live
# slices, the scoreboard and the A/B tester settle the way the inverter will
# really be driven.

def test_settle_step_drives_the_actual_run_with_the_standing_clamp():
    """Plan banks 5 kW at grid zero and declines 1,5 of 7 kW, sell negative,
    500 W more sun than forecast. The base run's own 98 A clamp (5.017,6 W)
    already declines 1.482,4 of the plan's 7 kW; with 110 A standing (5.632 W)
    the actual run takes 614,4 W more into the pack and the declined lane
    shrinks by the 114,4 W that the extra sun did not cover. With the plan's
    own 98 A nothing extra charges and the whole 500 W joins the decline."""
    g, b, c = core.settle_step(0.0, -5000.0, 7000.0, 500.0, 1500.0, 7500.0, 500.0, 0.0, 0.0, 0.5,
                               sell=-0.01, clamp_a=110.0)
    assert b == pytest.approx(-5000.0 - 614.4, abs=1.0)
    assert c == pytest.approx(1500.0 - 114.4, abs=1.0)
    assert g == pytest.approx(0.0, abs=1.0)
    plain = core.settle_step(0.0, -5000.0, 7000.0, 500.0, 1500.0, 7500.0, 500.0, 0.0, 0.0, 0.5, sell=-0.01)
    assert plain[1] == pytest.approx(-5000.0, abs=1.0)
    assert plain[2] == pytest.approx(2000.0, abs=1.0)


def test_wanted_clamp_a_is_the_command_clamp():
    assert core.wanted_clamp_a(0.0, -5000.0, sell=-0.01, pv_curtail_w=800.0) == pytest.approx(98.0)
    assert core.wanted_clamp_a(0.0, -5000.0, sell=-0.01, pv_curtail_w=800.0, margin=True) == pytest.approx(118.0)
    assert core.wanted_clamp_a(0.0, -5000.0, sell=-0.01) == core.DEYE_CURRENT_MAX_A
    assert core.wanted_clamp_a(0.0, -5000.0, sell=0.07) == core.DEYE_CURRENT_MAX_A
    assert core.wanted_clamp_a(-3000.0, 0.0, sell=0.07) == 0.0


def test_settle_slice_holds_the_clamp_inside_the_deadband_and_counts_writes():
    n = 3
    compact = {"n": n, "soc_start_pct": 50.0,
               "p_grid_w": [0.0] * n, "p_batt_w": [-5000.0, -5400.0, -7000.0],      # 98, 105, 137 A wanted
               "p_pv_w": [6000.0, 6400.0, 8000.0], "p_load_w": [500.0] * n,
               "pv_curtail_w": [500.0] * n, "soc_pct": [50.0] * n,        # the LP declines 500 W every step
               "buy": [0.02] * n, "sell": [-0.01] * n}
    out = core.settle_slice(compact, [6000.0, 6400.0, 8000.0], [500.0] * n)
    # with the writer's margin (SETTLE_MARGIN, 2026-09-12): 5.000 W -> 6.024 W = 118 A,
    # 5.400 W -> 6.480 W = 127 A held inside the deadband, 7.000 W -> 8.400 W = 164 A written
    assert out["clamp_a"] == [pytest.approx(118.0), pytest.approx(118.0), pytest.approx(164.0)]
    bare = core.settle_slice(compact, [6000.0, 6400.0, 8000.0], [500.0] * n, margin=False)
    assert bare["clamp_a"] == [pytest.approx(98.0), pytest.approx(98.0), pytest.approx(137.0)]
    assert out["clamp_writes"] == 2 and bare["clamp_writes"] == 2
    # the held clamp drives the actual run: the delta against the base run's own
    # 105 A clamp (5.376 W) is -358 W, so the 5,4 kW step settles at 5.041,6 W
    assert bare["p_batt_w"][1] == pytest.approx(-5400.0 + (5376.0 - 5017.6), abs=1.0)
    per_step = core.settle_slice(compact, [6000.0, 6400.0, 8000.0], [500.0] * n, deadband_a=0.0, margin=False)
    assert per_step["clamp_a"] == [pytest.approx(98.0), pytest.approx(105.0), pytest.approx(137.0)]
    assert per_step["clamp_writes"] == 3
    assert per_step["p_batt_w"][1] == pytest.approx(-5400.0, abs=1.0)   # base and actual share the clamp


def test_virtual_day_carries_the_clamp_lane_and_the_write_count(tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.5, pv=[1000.0] * 96, load=[500.0] * 96,
                        p_batt=[0.0] * 96, grid=[-500.0] * 96)
    vd = core.virtual_day(arch, day, AMS, local(2026, 9, 3, 0, 0))
    assert len(vd["clamp_a"]) == 96 and vd["clamp_a"][0] == 0.0          # pv_export holds the pack: clamp 0
    assert vd["clamp_writes"] == 1                                       # written once, never moved


def test_ab_walk_takes_a_clamp_deadband_override_and_reports_writes(stub, tmp_path):
    arch = str(tmp_path / "plans")
    day = date(2026, 9, 2)
    write_full_day_plan(arch, local(2026, 9, 1, 23, 0), day, soc0=0.5, pv=[7000.0] * 96, load=[500.0] * 96,
                        p_batt=[0.0] * 96, grid=[-6500.0] * 96)
    write_ab_plan(arch, local(2026, 9, 2, 10, 5), pv=7000.0, load=500.0)
    write_ab_plan(arch, local(2026, 9, 2, 12, 5), pv=7000.0, load=500.0)

    def plan(posts):
        pay = posts[-1][1]
        n = pay["prediction_horizon"]
        rows = make_rows(local(2026, 9, 2, 10, 15), n, soc0=pay["soc_init"], pv=[7000.0] * n)
        for k, r in enumerate(rows):
            r.update(P_PV_curtailment=1500.0, P_Load=500.0, P_batt=-5000.0 - 300.0 * (k % 3), P_grid=0.0,
                     unit_prod_price=-0.01)
        return rows
    stub.plan_fn = plan
    act = {"pv_w": [7000.0] * 96, "load_w": [500.0] * 96, "micro_w": [0.0] * 96, "curtailed": [0.0] * 96}
    held = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {}, act, 30, "closed", 48.2)
    per_step = core.ab_walk(arch, stub.url, "2026-09-02", AMS, {"clamp_deadband_a": 0.0}, act, 30, "closed", 48.2)
    assert held["status"] == "ok" and per_step["status"] == "ok"
    assert "clamp_a" in held["lanes"] and held["summary"]["clamp_writes"] < per_step["summary"]["clamp_writes"]
    applied, skipped = core.ab_apply_plan({"clamp_deadband_a": 0.0})
    assert applied == {} and "clamp_deadband_a" in skipped


# ---- the ladder: actual, hindsight, omniscient 1 and 2 as chained day walks ----

def ladder_plan(arch, made, days_ahead=2, soc_init=0.5):
    """A plan of record made just before midnight whose horizon reaches two
    days out, as production plans do (t0 23:15, 195 steps)."""
    pt0, pn = core.horizon(made, AMS, days_ahead=days_ahead)
    core.write_plan_archive(arch, made, {
        "plan_ts": made.isoformat(), "t0": pt0.isoformat(), "n": pn, "tz": AMS,
        "soc_init": soc_init, "soc_final": 0.5, "optim_status": "Optimal",
        "n_predicted_steps": 96, "pv_gap_steps": 0, "rows": make_rows(pt0, pn, pv=[700.0] * pn, soc0=soc_init),
        "pv_micro_w": [50.0] * pn, "micro_cut": [False] * pn,
        "tariff": {"energy_tax": 0.0, "supplier_fee": 0.0, "btw_pct": 0.0, "feedin_fee": 0.0},
        "payload": {"battery_soc_surplus_cost": 0.004, "battery_soc_deficit_threshold": 0.2,
                    "battery_soc_deficit_cost": 0.01, "battery_stress_cost": 0.0075,
                    "inverter_stress_cost": 0.0021}})
    return pt0, pn


def ladder_echo():
    """The add-on as the ladder sees it: rows on the posted horizon, the posted
    prices echoed back, SOC drifting down 0,1 % per step from the posted soc_init."""
    def fn(posts):
        body = posts[-1][1]
        n = body["prediction_horizon"]
        return make_rows(local(2026, 9, 2, 0, 0), n, buy=body["load_cost_forecast"],
                         sell=body["prod_price_forecast"], soc0=body["soc_init"])
    return fn


def ladder_inputs(day, n1, n2, win2=True):
    """Exact inputs: the day's own actuals, and the two omniscient windows."""
    act = {"pv_w": [1500.0] * 96, "load_w": [600.0] * 96, "grid_w": [800.0] * 96,
           "batt_dc_w": [0.0] * 96, "curtailed": [0.0] * 96, "micro_w": [100.0] * 96}
    def win(n):
        return {"pv_w": [1000.0] * n, "grid_w": [800.0] * n, "batt_dc_w": [0.0] * n,
                "curtailed": [0.0] * n, "micro_w": [100.0] * n, "soc_pct": [50.0] * n}
    return {day.isoformat(): {"day": act, "win1": win(n1), "win2": win(n2) if win2 else None}}


def ladder_np():
    return (np_rows(local(2026, 9, 2, 0, 0), 100.0) + np_rows(local(2026, 9, 3, 0, 0), 200.0)
            + np_rows(local(2026, 9, 4, 0, 0), 300.0) + np_rows(local(2026, 9, 5, 0, 0), 400.0))


def test_ladder_hindsight_learns_tomorrows_prices_at_13h(stub, tmp_path):
    """Two solves: midnight on the plan of record's own price view (tomorrow
    predicted), 13:00 on the published day-ahead for every remaining step,
    chained through the midnight solve's SOC at 12:45. Today's PV and load are
    exact in both; tomorrow's stay the plan's forecast."""
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    stub.plan_fn = ladder_echo()
    day = date(2026, 9, 2)
    w = core.ladder_windows(arch, day.isoformat(), AMS)
    assert w["n1"] == 192 and w["n2"] == 288                     # D and D+1 measured; the whole box measured
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288), ("hindsight",), 48.0, 0.9)
    row = res["rows"][-1]
    assert row["hindsight_status"] == "ok" and row["hindsight_solves"] == 2, row
    a, b = stub.posts[0][1], stub.posts[1][1]
    assert a["prediction_horizon"] == 288 and b["prediction_horizon"] == 236
    assert a["load_cost_forecast"][0] == pytest.approx(0.100)                              # D, published
    assert a["load_cost_forecast"][96] == 0.28                                             # D+1, the plan's own view
    assert b["load_cost_forecast"][0] == pytest.approx(0.152)                              # 13:00 is Nord Pool step 52
    assert b["load_cost_forecast"][44] == pytest.approx(0.200)                             # tomorrow, now published
    assert b["soc_init"] == pytest.approx(0.498 - 0.051, abs=1e-4)                         # 12:45 of the midnight solve (the pack enters the day at 49,8 %)
    # today exact (1.500 W measured, must-take 100 W off the PV list and the load), tomorrow the plan's 700 W
    assert a["pv_power_forecast"][0] == pytest.approx(1400.0) and a["pv_power_forecast"][96] == pytest.approx(650.0)
    assert a["load_power_forecast"][0] == pytest.approx(800 + 1500 - 100)                  # effective load, net of the Growatt
    assert a["load_power_forecast"][96] == pytest.approx(400.0 - 50.0)
    assert row["hindsight_soc_end_pct"] == pytest.approx(40.4, abs=0.05)
    assert row["hindsight_eur"] == pytest.approx(row["hindsight_cash"] + row["hindsight_loss"])


def test_ladder_omniscient_rungs_run_the_same_cadence_on_exact_inputs(stub, tmp_path):
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    stub.plan_fn = ladder_echo()
    day = date(2026, 9, 2)
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288), ("hindsight", "omni2"), 48.0, 0.9)
    row = res["rows"][-1]
    assert row["hindsight_status"] == "ok" and row["omni2_status"] == "ok"
    # midnight and 13:00 for every rung: nothing new reaches an omniscient lane at
    # 12:45, but the cadence has to be the same or the comparison is not one
    assert row["hindsight_solves"] == 2 and row["omni2_solves"] == 2 and len(stub.posts) == 4
    h, o2 = stub.posts[0][1], stub.posts[2][1]
    assert [p[1]["prediction_horizon"] for p in stub.posts] == [288, 236, 288, 236]
    assert o2["load_cost_forecast"][0] == pytest.approx(0.100) and o2["load_cost_forecast"][96] == pytest.approx(0.200)
    assert o2["load_cost_forecast"][192] == pytest.approx(0.300)                            # D+2, clairvoyance
    assert o2["pv_power_forecast"][100] == pytest.approx(900.0)                             # exact tomorrow too
    assert o2["load_power_forecast"][100] == pytest.approx(800 + 1000 - 100)
    assert h["pv_power_forecast"][100] != pytest.approx(900.0)                              # hindsight: D+1 forecast


def test_ladder_rung_is_pending_until_its_window_is_measured(stub, tmp_path):
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    stub.plan_fn = ladder_echo()
    day = date(2026, 9, 2)
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288, win2=False), ("hindsight", "omni2"), 48.0, 0.9)
    row = res["rows"][-1]
    assert row["hindsight_status"] == "ok" and row["omni2_status"] == "pending" and row["omni2_eur"] is None
    assert len(stub.posts) == 2


def test_ladder_chains_each_rung_from_its_own_midnight(stub, tmp_path):
    """Day two's hindsight starts where day one's hindsight ended; the actual
    lane starts where the plan of record says the virtual pack was."""
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    ladder_plan(arch, local(2026, 9, 2, 23, 2), soc_init=0.3)
    ladder_plan(arch, local(2026, 9, 3, 0, 5), days_ahead=3, soc_init=0.3)
    stub.plan_fn = ladder_echo()
    inputs = dict(ladder_inputs(date(2026, 9, 2), 192, 288), **ladder_inputs(date(2026, 9, 3), 192, 288))
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, ["2026-09-02", "2026-09-03"], AMS,
                          ladder_np(), inputs, ("actual", "hindsight"), 48.0, 0.9)
    d1, d2 = res["rows"]
    assert d1["soc_start_pct"] == 49.8                                              # the virtual pack's first midnight
    assert d1["actual_status"] == "ok" and d2["actual_status"] == "ok"
    # the actual lane chains its own pack too: day two starts where day one's
    # settlement ended, not where the live pack (re-anchored at 29,8) says
    assert d2["soc_start_pct"] == pytest.approx(d1["actual_soc_end_pct"]) and d2["soc_start_pct"] != 29.8
    assert "actual_seam" in d2["flags"]                                             # and the live midnight was >1 % away
    assert stub.posts[0][1]["soc_init"] == 0.498                                     # day one: same start as actual
    assert stub.posts[2][1]["soc_init"] == pytest.approx(d1["hindsight_soc_end_pct"] / 100.0, abs=1e-4)
    assert "hindsight_reset" in d1["flags"] and "hindsight_reset" not in d2["flags"]
    # a rerun over a day whose rungs are all ok is a no-op on the add-on
    n_posts = len(stub.posts)
    core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, ["2026-09-02"], AMS, ladder_np(), inputs,
                    ("actual", "hindsight"), 48.0, 0.9)
    assert len(stub.posts) == n_posts


def test_ladder_csv_round_trip_and_summary(tmp_path):
    path = str(tmp_path / "ladder.csv")
    r1 = {c: None for c in core.LADDER_COLUMNS}
    r1.update(date="2026-09-02", soc_start_pct=50.0, lambda_eur_kwh=0.1, flags="",
              actual_eur=-5.0, actual_status="ok", actual_soc_end_pct=20.0,
              hindsight_eur=-6.0, hindsight_status="ok", hindsight_soc_end_pct=30.0,
              omni2_eur=-6.6, omni2_status="ok", omni2_soc_end_pct=40.0)
    r2 = dict(r1, date="2026-09-03", omni2_status="pending", omni2_eur=None, omni2_soc_end_pct=None)
    core.upsert_ladder(path, r1)
    rows = core.upsert_ladder(path, r2)
    assert [r["date"] for r in core.read_ladder(path)] == ["2026-09-02", "2026-09-03"]
    assert core.read_ladder(path)[1]["omni2_eur"] is None and core.read_ladder(path)[0]["actual_eur"] == -5.0
    s = core.ladder_summary(rows, 48.0)
    w = s["windows"]["all"]
    assert w["n"] == 1                                              # only the day every rung has
    assert w["actual"] == -5.0 and w["hindsight"] == -6.0 and w["omni2"] == -6.6
    assert w["gap_hindsight"] == pytest.approx(1.0)                 # actual - hindsight, the controller's shortfall
    assert w["gap_omni2"] == pytest.approx(0.6) and "gap_omni1" not in w
    # end-of-window pack value against the actual lane, at the lambda of the last
    # day EVERY rung has: 09-03's omni2 is still pending, so the end is 09-02's
    assert s["last_date"] == "2026-09-02"
    assert s["end"]["hindsight_soc_end_pct"] == 30.0 and s["end"]["omni2_soc_end_pct"] == 40.0
    assert s["end"]["hindsight_pack_value_eur"] == pytest.approx((30.0 - 20.0) / 100 * 48.0 * 0.1)


def ladder_row(day, flags="", **eur):
    """One settled ladder row, every rung ok, cash passed per rung."""
    row = {c: None for c in core.LADDER_COLUMNS}
    row.update(date=day, soc_start_pct=50.0, lambda_eur_kwh=0.1, flags=flags)
    for r in core.RUNGS:
        row[f"{r}_eur"] = eur.get(r, eur.get("hindsight"))
        row[f"{r}_cash"] = row[f"{r}_eur"]
        row[f"{r}_loss"] = 0.0
        row[f"{r}_status"] = "ok"
        row[f"{r}_soc_end_pct"] = 20.0
        row[f"{r}_solves"] = 1
    return row


def test_ladder_summary_drops_a_capped_day_but_keeps_a_seam_and_a_reset():
    """A cap is a replay clipped at the grid limit, so that lane did not run and
    the day leaves the sums. A SEAM does not: it measures the virtual actual lane
    drifting from the live pack, which is the normal condition of a chained lane.
    A reset does not either: every rung starts a reset day from the same
    midnight."""
    rows = [
        ladder_row("2026-09-03", flags="actual_reset;actual_seam",
                   actual=-9.0, hindsight=-9.2, omni2=-9.2),
        ladder_row("2026-09-04", actual=-3.0, hindsight=-3.2, omni2=-3.2),
        ladder_row("2026-09-05", flags="omni2_caps",
                   actual=-5.0, hindsight=-5.0, omni2=-9.0),
        ladder_row("2026-09-06", flags="omni2_reset",
                   actual=-4.0, hindsight=-4.3, omni2=-4.3),
    ]
    s = core.ladder_summary(rows, 48.0)
    w = s["windows"]["all"]
    assert w["n"] == 3                                     # everything but the capped day
    assert w["actual"] == pytest.approx(-16.0) and w["hindsight"] == pytest.approx(-16.7)
    assert w["gap_hindsight"] == pytest.approx(0.7)
    assert s["excluded"] == 1
    assert s["last_date"] == "2026-09-06"


def test_ladder_summary_window_holds_n_days_at_the_settled_frontier():
    """A window is the last n days every rung has, not the last n calendar
    rows: a ragged tail must not shorten the sum it is compared against."""
    rows = [ladder_row(f"2026-09-{d:02d}", actual=-1.0, hindsight=-1.1, omni2=-1.1)
            for d in range(1, 10)]
    rows.append(ladder_row("2026-09-10", flags="actual_caps",
                           actual=-9.0, hindsight=-1.1, omni2=-1.1))
    pending = {c: None for c in core.LADDER_COLUMNS}
    pending.update(date="2026-09-11", flags="", soc_start_pct=50.0, lambda_eur_kwh=0.1,
                   actual_eur=-1.0, actual_status="ok", omni2_status="pending")
    rows.append(pending)
    w = core.ladder_summary(rows, 48.0)["windows"]["d7"]
    assert w["n"] == 7                                     # seven real days, not five
    assert w["actual"] == pytest.approx(-7.0)


def ladder_box(arch, made_pre=None, day3=True):
    """Production's two plans for 2026-09-02: the pre-midnight plan of record,
    whose horizon stops at the end of D+1, and the day's first solve at 00:05,
    whose horizon opens to the end of D+2. The live controller ran the second
    one all day, so it is the box every rung has to share."""
    ladder_plan(arch, made_pre or local(2026, 9, 1, 23, 2))
    if day3:
        ladder_plan(arch, local(2026, 9, 2, 0, 5), days_ahead=3)


def test_ladder_hindsight_never_sees_prices_past_the_publication_frontier(stub, tmp_path):
    """The afternoon re-solve used to take the published day-ahead over its WHOLE
    horizon. np_rows is gathered at run time, days later, so every price exists
    and the call succeeded: the lane got D+2's prices a full day before the
    auction cleared. A controller at 12:45 on D knows day-ahead to the end of
    D+1 and nothing beyond."""
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    stub.plan_fn = ladder_echo()
    day = date(2026, 9, 2)
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288), ("hindsight",), 48.0, 0.9)
    assert res["rows"][-1]["hindsight_status"] == "ok", res["rows"][-1]
    a, b = stub.posts[0][1], stub.posts[1][1]
    i13 = 52                                                       # 13:00 is step 52 of the day
    assert a["prediction_horizon"] == 288 and b["prediction_horizon"] == 288 - i13
    # midnight: D published, D+1 and D+2 the plan's own prediction
    assert a["load_cost_forecast"][0] == pytest.approx(0.100)
    assert a["load_cost_forecast"][96] == 0.28 and a["load_cost_forecast"][192] == 0.28
    # 13:00: D+1 has cleared, D+2 has NOT
    assert b["load_cost_forecast"][96 - i13] == pytest.approx(0.200)
    assert b["load_cost_forecast"][192 - i13] == 0.28


def test_ladder_rungs_share_one_box_and_one_terminal(stub, tmp_path):
    """Every rung solves the same three-day box to the same terminal SOC, so the
    rungs differ only in how much of that box is exact. hindsight is not a
    shorter horizon than the lane it bounds: it is the same wall with D known."""
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    stub.plan_fn = ladder_echo()
    day = date(2026, 9, 2)
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288), ("hindsight", "omni2"), 48.0, 0.9)
    row = res["rows"][-1]
    assert all(row[f"{r}_status"] == "ok" for r in ("hindsight", "omni2")), row
    assert [p[1]["prediction_horizon"] for p in stub.posts] == [288, 236] * 2
    assert all(p[1]["soc_final"] == 0.5 for p in stub.posts)
    h, o2 = stub.posts[0][1], stub.posts[2][1]
    # hindsight at midnight: D exact and published, D+1 and D+2 the plan's forecast at its predicted price
    assert h["load_cost_forecast"][0] == pytest.approx(0.100) and h["load_cost_forecast"][192] == 0.28
    assert h["pv_power_forecast"][200] == pytest.approx(650.0)          # forecast D+2
    # omni2: exact and published all the way out
    assert o2["load_cost_forecast"][192] == pytest.approx(0.300)
    assert o2["pv_power_forecast"][200] == pytest.approx(900.0)


def test_ladder_splits_hindsight_into_production_and_consumption(stub, tmp_path):
    """Two diagnostic rungs that decompose the nowcast: one knows the day's sun
    and forecasts its load, the other the mirror. The must-take Growatt follows
    the SUN, not the load, because its output is production."""
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    stub.plan_fn = ladder_echo()
    day = date(2026, 9, 2)
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288), ("hindsight_pv", "hindsight_load"), 48.0, 0.9)
    row = res["rows"][-1]
    assert row["hindsight_pv_status"] == "ok" and row["hindsight_load_status"] == "ok", row
    assert row["hindsight_pv_solves"] == 2 and row["hindsight_load_solves"] == 2
    pv_s, load_s = stub.posts[0][1], stub.posts[2][1]
    assert pv_s["prediction_horizon"] == 288 and load_s["prediction_horizon"] == 288
    # sun known: D's PV measured (1.500 less the 100 W must-take), D's load the plan's 400
    assert pv_s["pv_power_forecast"][0] == pytest.approx(1400.0)
    assert pv_s["load_power_forecast"][0] == pytest.approx(400.0 - 100.0)     # plan's load, MEASURED must-take
    # load known: D's PV the plan's 700, D's load measured, and the must-take is
    # the plan's 50 because the sun it belongs to is the plan's too
    assert load_s["pv_power_forecast"][0] == pytest.approx(650.0)
    assert load_s["load_power_forecast"][0] == pytest.approx(800 + 1500 - 50)


def test_ladder_verdict_survives_a_missing_split_rung(tmp_path):
    """A diagnostic rung must never gate the headline. Completeness is the four
    CORE rungs; a split rung that failed leaves its own sum None and nothing
    else moves."""
    rows = [ladder_row("2026-09-04", actual=-3.0, hindsight=-3.2, omni2=-3.2)]
    rows[0]["hindsight_pv_status"] = "solve_failed"
    rows[0]["hindsight_pv_eur"] = None
    s = core.ladder_summary(rows, 48.0)
    w = s["windows"]["all"]
    assert w["n"] == 1 and w["gap_hindsight"] == pytest.approx(0.2)
    assert w["hindsight_pv"] is None and w["hindsight_load"] == pytest.approx(-3.2)


def test_hindsight_solve_retries_once_when_a_foreign_solve_lands(stub, tmp_path):
    """The ladder posts dozens of solves in a row and reads each back off the
    add-on's opt_res_latest. A neighbouring solve landing in between returns the
    WRONG row count, which the foreign-solve guard rejects - and a rung that
    loses its solve also loses its chain, so the next day resets. One re-post
    costs a few seconds and saves the walk."""
    arch = str(tmp_path / "plans")
    ladder_box(arch)
    day = date(2026, 9, 2)
    echo = ladder_echo()
    calls = {"n": 0}

    def flaky(posts):
        calls["n"] += 1
        rows = echo(posts)
        return rows[:-3] if calls["n"] == 1 else rows      # a foreign solve on the first read only

    stub.plan_fn = flaky
    res = core.ladder_run(arch, str(tmp_path / "ladder.csv"), stub.url, [day.isoformat()], AMS, ladder_np(),
                          ladder_inputs(day, 192, 288), ("omni2",), 48.0, 0.9)
    row = res["rows"][-1]
    assert row["omni2_status"] == "ok", row
    assert calls["n"] == 3                                  # the bad read, its retry, then the 13:00 solve
