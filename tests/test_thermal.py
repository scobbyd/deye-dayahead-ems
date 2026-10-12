"""thermal.py: the house as a second battery. Pure functions over plain data."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import emhass_core as core

AMS = "Europe/Amsterdam"
Z = ZoneInfo(AMS)


def local(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=Z)


SCHED = {"mode": "auto", "holiday_c": 11.0, "night_floor_c": 10.0,
         "slots": {"morning": "07:00", "day": "09:00", "evening": "17:00", "night": "23:00"},
         "targets": {"weekday": {"morning": 18.0, "day": 18.5, "evening": 19.0},
                     "weekend": {"morning": 18.0, "day": 19.0, "evening": 19.5}},
         "away": False, "away_offset_k": -3.0}
UP = {"day_c": 19.0, "night_c": 17.0, "slots": {"morning": "07:00", "night": "23:00"},
      "away": False, "away_offset_k": -3.0}


def _th(**over):
    th = {"enabled": True, "constants": core.load_thermal(path="/nonexistent"), "band_k": 3.0,
          "caps": {"hp_living_w": 1450.0, "hp_multi_w": 2200.0},
          "startup_ct": {"hp": 1.0, "pellet": 10.0},
          "pellet": {"available": False, "cost_ct_kwh": 8.0},
          "living": {"start_c": 19.0, "sched": dict(SCHED)},
          "upstairs": {"enabled": True, "start_c": 19.5, "sched": dict(UP)},
          "weather": {"points": [(local(2026, 10, 9, h, 0).isoformat(), 10.0 + h / 10) for h in range(0, 24)]
                      + [(local(2026, 10, 10, h, 0).isoformat(), 12.0) for h in range(0, 24)]
                      + [(local(2026, 10, 11, h, 0).isoformat(), 11.0) for h in range(0, 24)],
                      "now_c": 10.5}}
    th.update(over)
    return th


# ---- constants ---------------------------------------------------------------

def test_load_thermal_defaults_without_file():
    c = core.load_thermal(path="/nonexistent/thermal.json")
    assert c["zones"]["living"]["loss_coefficient_kw_k"] == 0.225
    assert c["sources"]["pellet"]["min_power_w"] == 5000.0


def test_load_thermal_merges_file(tmp_path):
    p = tmp_path / "thermal.json"
    p.write_text('{"zones": {"living": {"thermal_mass_kwh_k": 7.5}}}')
    c = core.load_thermal(path=str(p))
    assert c["zones"]["living"]["thermal_mass_kwh_k"] == 7.5
    assert c["zones"]["living"]["loss_coefficient_kw_k"] == 0.225      # the rest from DEFAULTS


# ---- schedule ----------------------------------------------------------------

def test_floor_schedule_follows_slots_and_day_type():
    # Friday 2026-10-09 06:00 .. Saturday 2026-10-10 06:00 = 96 steps
    t0 = local(2026, 10, 9, 6, 0)
    f = core.floor_schedule(t0, 96, AMS, SCHED)
    assert len(f) == 96
    assert f[0] == 10.0                     # 06:00 is night (before 07:00)
    assert f[4] == 18.0                     # 07:00 weekday morning
    assert f[12] == 18.5                    # 09:00 weekday day
    assert f[44] == 19.0                    # 17:00 weekday evening
    assert f[68] == 10.0                    # 23:00 night
    assert f[-1] == 10.0                    # Saturday 05:45 still night


def test_floor_schedule_holiday_is_flat():
    f = core.floor_schedule(local(2026, 10, 9, 6, 0), 96, AMS, dict(SCHED, mode="holiday"))
    assert set(f) == {11.0}


def test_floor_schedule_away_offset_only_in_day_slots():
    f = core.floor_schedule(local(2026, 10, 9, 6, 0), 96, AMS, dict(SCHED, away=True))
    assert f[0] == 10.0 and f[12] == 15.5


def test_floor_schedule_dst_day_has_n_values():
    # the autumn change night: 2026-10-24 22:00 .. 2026-10-25 24:00 is 8 + 100 steps
    t0 = local(2026, 10, 24, 22, 0)
    f = core.floor_schedule(t0, 108, AMS, SCHED)
    assert len(f) == 108
    z = ZoneInfo(AMS)
    times = [t.astimezone(z) for t in core.step_times(t0, 108)]
    k = next(i for i, t in enumerate(times) if (t.month, t.day, t.hour, t.minute) == (10, 25, 7, 0))
    assert f[k] == SCHED["targets"]["weekend"]["morning"]           # 2026-10-25 is a Sunday
    assert f[k - 1] == SCHED["night_floor_c"]


def test_upstairs_schedule_and_night_mask():
    f, night = core.upstairs_schedule(local(2026, 10, 9, 6, 0), 96, AMS, UP)
    assert f[0] == 17.0 and night[0] is True
    assert f[4] == 19.0 and night[4] is False
    assert f[68] == 17.0 and night[68] is True


def test_ceiling_band_and_night_band():
    floor = [10.0, 18.0, 17.0]
    assert core.ceiling(floor, 3.0) == [13.0, 21.0, 20.0]
    assert core.ceiling(floor, 3.0, night=[True, False, True], night_band_k=0.5) == [10.5, 21.0, 17.5]


def test_ceiling_night_without_night_band_uses_band_everywhere():
    assert core.ceiling([10.0, 18.0], 3.0, night=[True, False], night_band_k=None) == [13.0, 21.0]


def test_upstairs_schedule_holiday_is_flat_and_keeps_night_mask():
    f, night = core.upstairs_schedule(local(2026, 10, 9, 6, 0), 96, AMS, dict(UP, mode="holiday", holiday_c=11.0))
    assert set(f) == {11.0}
    assert night[0] is True and night[4] is False and night[68] is True


# ---- outdoor forecast --------------------------------------------------------

def test_outdoor_forecast_interpolates_hourly_points_to_the_grid():
    pts = [(local(2026, 10, 10, 10, 0).isoformat(), 10.0), (local(2026, 10, 10, 11, 0).isoformat(), 12.0)]
    vals, held = core.outdoor_forecast(pts, 10.0, local(2026, 10, 10, 10, 0), 4)
    assert vals == [10.0, 10.5, 11.0, 11.5] and held == 0


def test_outdoor_forecast_holds_then_gives_up():
    pts = [(local(2026, 10, 10, 10, 0).isoformat(), 10.0)]
    vals, held = core.outdoor_forecast(pts, None, local(2026, 10, 10, 10, 0), 20)
    assert vals == [10.0] * 20 and held == 19
    vals, held = core.outdoor_forecast(pts, None, local(2026, 10, 10, 10, 0), 40)
    assert vals is None and held == 39


def test_outdoor_forecast_before_first_point_uses_now():
    pts = [(local(2026, 10, 10, 11, 0).isoformat(), 12.0)]
    vals, _ = core.outdoor_forecast(pts, 9.0, local(2026, 10, 10, 10, 0), 4)
    assert vals[0] == 9.0 and vals[-1] == 11.25


def test_outdoor_forecast_drops_malformed_points():
    good = [(local(2026, 10, 10, 10, 0).isoformat(), 10.0), (local(2026, 10, 10, 11, 0).isoformat(), 12.0)]
    bad = [(local(2026, 10, 10, 10, 30).isoformat(), None), ("garbage", 5.0),
           (local(2026, 10, 10, 10, 45).isoformat(), float("nan")), (local(2026, 10, 10, 10, 50).isoformat(), "x")]
    vals, held = core.outdoor_forecast(bad + good, 10.0, local(2026, 10, 10, 10, 0), 4)
    assert vals == [10.0, 10.5, 11.0, 11.5] and held == 0


# ---- startup penalty ---------------------------------------------------------

def test_startup_penalty_converts_ct_per_start():
    # 1 ct per start, 1.450 W at a 0,10 EUR/kWh mean: penalty * 1,45 * 0,10 = 0,01
    assert core.startup_penalty(1.0, 1450.0, 0.10) == pytest.approx(0.01 / (1.45 * 0.10))
    assert core.startup_penalty(1.0, 1450.0, 0.0) == 0.0


# ---- topology ----------------------------------------------------------------

def test_heat_topology_two_zones_two_sources_no_pellet():
    t0 = local(2026, 10, 10, 10, 0)
    topo, info = core.heat_topology(_th(), t0, 96, AMS, 0.10)
    assert info["sent"] is True and info["reason"] == "ok"
    assert [s["id"] for s in topo["sources"]] == ["hp_living", "hp_multi"]
    assert [(f["from"], f["to"]) for f in topo["flows"]] == [("hp_living", "living"), ("hp_multi", "upstairs")]
    assert info["sources"] == {"hp_living": 0, "hp_multi": 1}
    assert info["zones"] == {"living": 0, "upstairs": 1}
    living = topo["storage"][0]
    assert living["thermal_mass"] == 6.0 and living["loss_coefficient"] == 0.225
    assert living["start_temperature"] == 19.0
    assert len(living["min_temperatures"]) == 96 and len(living["max_temperatures"]) == 96
    assert max(living["max_temperatures"]) == 22.5            # Saturday evening floor 19,5 + 3,0
    assert topo["sources"][0]["nominal_power"] == 1450 and topo["sources"][0]["electric"] is True
    assert "cost_tracks" not in topo
    assert len(info["outdoor_c"]) == 96


def test_heat_topology_pellet_joins_when_available():
    topo, info = core.heat_topology(_th(pellet={"available": True, "cost_ct_kwh": 8.0}),
                                    local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    p = topo["sources"][2]
    assert p["id"] == "pellet" and p["electric"] is False and p["cost_track"] == "pellet"
    assert p["min_power"] == 5000 and p["treat_as_semi_cont"] is True and p["max_startups"] == 2
    assert topo["cost_tracks"]["pellet"] == [0.08] * 96
    assert topo["flows"][2] == {"from": "pellet", "to": "living"}
    assert info["sources"]["pellet"] == 2 and info["zones"]["living"] == 0


def test_heat_topology_max_startups_scales_with_horizon_days():
    th = _th(pellet={"available": True, "cost_ct_kwh": 8.0})
    th["weather"]["points"] += [(local(2026, 10, 12, h, 0).isoformat(), 11.0) for h in range(0, 24)]
    topo, _ = core.heat_topology(th, local(2026, 10, 10, 10, 0), 200, AMS, 0.10)
    assert topo["sources"][2]["max_startups"] == 6                 # ceil(200/96) = 3 days x 2


def test_heat_topology_upstairs_start_unavailable_leaves_one_zone():
    th = _th(); th["upstairs"]["start_c"] = None
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert info["sent"] is True and info["upstairs"] is False
    assert [s["id"] for s in topo["storage"]] == ["living"]
    assert info["sources"] == {"hp_living": 0} and info["zones"] == {"living": 0}


def test_heat_topology_plans_upstairs_with_its_master_off():
    # heating_upstairs_enabled gates the executor only; the zone is planned
    th = _th(); th["upstairs"]["enabled"] = False
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert [s["id"] for s in topo["storage"]] == ["living", "upstairs"]
    assert info["upstairs"] is True and info["zones"] == {"living": 0, "upstairs": 1}


def test_heat_topology_disabled_or_missing_start_is_not_sent():
    t0 = local(2026, 10, 10, 10, 0)
    assert core.heat_topology({"enabled": False}, t0, 96, AMS, 0.10) == (None, {"sent": False, "reason": "disabled"})
    th = _th(); th["living"]["start_c"] = None
    topo, info = core.heat_topology(th, t0, 96, AMS, 0.10)
    assert topo is None and info["reason"] == "living start temperature unavailable"


def test_heat_topology_input_error_is_not_sent_with_the_error():
    th = {"enabled": False, "error": "JSONDecodeError('Expecting value', '', 0)"}
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert topo is None and info == {"sent": False, "reason": "error: JSONDecodeError('Expecting value', '', 0)"}


def test_heat_topology_degenerate_zone_constant_skips_the_clamp():
    # mass 0,01 kWh/K: k x UA = 0,25 / 0,01 x 0,225 > 1, the backward pass would divide by <= 0
    th = _th()
    th["constants"]["zones"]["living"]["thermal_mass_kwh_k"] = 0.01
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert info["sent"] is True and info["clamped"]["living"] == "skipped"
    floor = core.floor_schedule(local(2026, 10, 10, 10, 0), 96, AMS, SCHED)
    assert topo["storage"][0]["min_temperatures"] == floor
    assert topo["storage"][0]["max_temperatures"] == core.ceiling(floor, 3.0)
    th["constants"]["zones"]["living"]["thermal_mass_kwh_k"] = 0.0
    _, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert info["clamped"]["living"] == "skipped"


def test_outdoor_forecast_and_heat_topology_on_the_dst_day_have_n_values():
    # 2026-10-25 00:00 .. 24:00 local is 25 h = 100 steps
    t0 = local(2026, 10, 25, 0, 0)
    pts = [((t0 + timedelta(hours=h)).isoformat(), 8.0 + 0.1 * h) for h in range(-1, 28)]
    vals, held = core.outdoor_forecast(pts, 8.0, t0, 100)
    assert len(vals) == 100 and held == 0
    topo, info = core.heat_topology(_th(weather={"points": pts, "now_c": 8.0}), t0, 100, AMS, 0.10)
    assert info["sent"] is True and len(info["outdoor_c"]) == 100
    for z in topo["storage"]:
        assert len(z["min_temperatures"]) == 100 and len(z["max_temperatures"]) == 100


def test_heat_topology_short_forecast_is_not_sent():
    th = _th(); th["weather"] = {"points": [(local(2026, 10, 10, 10, 0).isoformat(), 10.0)], "now_c": 10.0}
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert topo is None and info["reason"].startswith("forecast short")


def test_heat_topology_upstairs_night_band_is_narrow():
    # From 23:00 with the room at the night floor: a room held at the 19,0 day floor
    # cannot fall into a 17,0-17,5 band in one step, so the clamp would lift the
    # ceiling after a setback (see test_clamp_bounds_keeps_the_ceiling_reachable_across_a_setback).
    th = _th(); th["upstairs"]["start_c"] = 17.0
    topo, _ = core.heat_topology(th, local(2026, 10, 10, 23, 0), 96, AMS, 0.10)
    up = topo["storage"][1]
    k = 4                                           # 00:00 = step 4 from 23:00
    assert up["min_temperatures"][k] == 17.0 and up["max_temperatures"][k] == 17.5
    assert up["max_temperatures"][31] > 17.5        # 06:45: the pre-heat ramp toward the 07:00 day floor


def test_heat_topology_only_bad_forecast_points_is_no_forecast():
    th = _th()
    th["weather"] = {"points": [("garbage", 5.0), (local(2026, 10, 10, 10, 0).isoformat(), None)], "now_c": 10.0}
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert topo is None and info == {"sent": False, "reason": "no forecast"}


def test_heat_topology_source_keys_follow_source_order():
    topo, info = core.heat_topology(_th(pellet={"available": True, "cost_ct_kwh": 8.0}),
                                    local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert tuple(info["sources"]) == core.SOURCE_ORDER
    assert list(info["sources"].values()) == [0, 1, 2]


# ---- comfort bounds clamped to the reachable envelope -------------------------

def _flat_th(start, floor_c, out_c, up=False, up_start=19.5):
    pts = [(local(2026, 10, d, h, 0).isoformat(), out_c) for d in (10, 11, 12) for h in range(24)]
    th = _th(living={"start_c": start, "sched": dict(SCHED, mode="holiday", holiday_c=floor_c)},
             weather={"points": pts, "now_c": out_c})
    th["upstairs"] = {"enabled": up, "start_c": up_start if up else None, "sched": dict(UP)}
    return th


def test_heat_topology_lifts_the_ceiling_while_the_zone_cools_from_above():
    # C = 6 kWh/K, UA = 0,225 kW/K, 15 min steps: a 7,3 K difference (21,3 vs 14,0)
    # cools about 0,07 K per step, so the 18,5 ceiling is reached after roughly 40 steps
    # (the exact count, about 64 with the margin and the slowing decay, comes from the arrays).
    topo, info = core.heat_topology(_flat_th(21.3, 15.5, 14.0), local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    z = topo["storage"][0]
    mx, mn = z["max_temperatures"], z["min_temperatures"]
    assert mx[0] == round(21.3 + 0.5, 2)
    assert all(mx[i + 1] <= mx[i] for i in range(len(mx) - 1))
    lifted = [i for i, v in enumerate(mx) if v > 18.5]
    assert lifted and lifted[-1] < len(mx) - 1 and mx[-1] == 18.5
    assert info["clamped"]["living"][0] == len(lifted)
    assert 30 <= len(lifted) <= 90          # the decay slows as the gap closes and the 0,5 K margin adds steps
    assert mn == [15.5] * 96 and info["clamped"]["living"][1] == 0


def test_heat_topology_lowers_the_floor_while_the_zone_heats_from_below():
    topo, info = core.heat_topology(_flat_th(12.0, 18.0, 5.0), local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    z = topo["storage"][0]
    mn, mx = z["min_temperatures"], z["max_temperatures"]
    assert mn[0] == round(12.0 - 0.5, 2)
    # the first 2 transitions carry no heat (the add-on's thermal_inertia lag), so the
    # lowered floor dips with free cooling first, then rises along the full-power path
    assert mn[1] < mn[0] and mn[2] < mn[1]
    assert all(mn[i + 1] >= mn[i] for i in range(2, len(mn) - 1))
    assert mn[-1] == 18.0
    assert info["clamped"]["living"][1] > 0
    assert mx == [21.0] * 96 and info["clamped"]["living"][0] == 0


def test_heat_topology_bounds_untouched_when_start_is_inside_the_band():
    topo, info = core.heat_topology(_flat_th(19.0, 18.0, 10.0), local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    z = topo["storage"][0]
    assert z["min_temperatures"] == [18.0] * 96 and z["max_temperatures"] == [21.0] * 96
    assert info["clamped"]["living"] == [0, 0]


def _clamp_case(floor, start, out_c, mass=6.0, ua=0.225, inertia=0.5, band=3.0):
    """_clamp_bounds on one living-type zone with the hp_living source at 1450 W.
    Returns (min, max, n_max, n_min, step) where step(T, i, heat) is one
    transition of the LP zone model (heat=True: the full-power heat that
    arrives in transition i after the add-on's whole-step lag)."""
    n = len(floor)
    zone = {"thermal_mass": mass, "loss_coefficient": ua, "thermal_inertia": inertia, "start_temperature": start}
    src = {"supply_temperature": 30.0, "carnot_efficiency": 0.46, "nominal_power": 1450}
    out = [out_c] * n
    q = core._hp_kw(src, out)
    lo, hi, n_max, n_min = core._clamp_bounds(zone, q, out, list(floor), core.ceiling(list(floor), band))
    k = 0.25 / mass
    lag = int(inertia / 0.25)

    def step(t, i, heat=False):
        arrive = q[i - lag] if heat and i >= lag else 0.0
        return t - k * ua * (t - out[i]) + k * arrive
    return lo, hi, n_max, n_min, step


def test_clamp_bounds_keeps_the_ceiling_reachable_across_a_setback():
    # The floor holds the room at 15,5 until step 5; at step 6 the setback drops the
    # floor to 10,0 and the plain ceiling (floor + band) to 13,0. Free cooling from
    # 15,5 reaches only about 15,45, so the ceiling at step 6 must stay above that.
    floor = [15.5] * 6 + [10.0] * 6
    lo, hi, n_max, _n_min, step = _clamp_case(floor, start=15.5, out_c=10.0)
    assert lo[4:] == floor[4:]                 # steps 1-3: the start - margin rule (the 2-step lag: no heat yet)
    assert hi[6] >= round(step(lo[5], 5) + core.BOUND_MARGIN_K, 2) - 0.006
    assert hi[6] > lo[5]                       # no one-step collapse below the held floor
    for i in range(len(floor) - 1):
        assert hi[i + 1] >= round(step(lo[i], i) + core.BOUND_MARGIN_K, 2) - 0.006, i
    assert all(h >= l for h, l in zip(hi, lo))
    assert n_max > 0


def test_clamp_bounds_pre_heat_ramp_before_a_morning_floor():
    # Night floor 10,0 for 8 steps, then 18,0. From 12,0 at 5,0 outdoor, full power
    # gains about 0,27 K per step after the 2-step lag: 18,0 is out of reach at step
    # 8, so the morning floor is lowered along the full-power path, and the ceiling
    # before step 8 rises with the pre-heat ramp that path needs.
    floor = [10.0] * 8 + [18.0] * 40
    lo, hi, _n_max, n_min, step = _clamp_case(floor, start=12.0, out_c=5.0)
    assert lo[:8] == [10.0] * 8
    assert lo[8] < 18.0 and n_min > 0
    assert all(lo[i + 1] >= lo[i] for i in range(8, len(lo) - 1))
    assert lo[-1] == 18.0
    for i in range(len(floor)):
        assert hi[i] - lo[i] >= 0.5, i
    for i in range(len(floor) - 1):
        # every floor is reachable at full power from the ceiling of the step before
        assert lo[i + 1] <= step(hi[i], i, heat=True) + 1e-9, i
    assert all(hi[i + 1] >= hi[i] for i in range(1, 8))   # the pre-heat ramp has room
    assert hi[7] > 13.0                                    # above the plain night ceiling


def test_hp_kw_clips_the_cop_like_the_add_on():
    src = {"supply_temperature": 30.0, "carnot_efficiency": 0.46, "nominal_power": 1450}
    q = core._hp_kw(src, [16.9, 5.0, -40.0, 31.0])
    assert q[0] == pytest.approx(1.45 * 8.0)          # Carnot 10,6 clipped to 8
    assert q[1] == pytest.approx(1.45 * 0.46 * 303.15 / 25.0)
    assert q[2] == pytest.approx(1.45 * 1.99, abs=0.01)   # 0,46 x 303,15 / 70 = 1,99, inside [1, 8]
    assert q[3] == pytest.approx(1.45 * 1.0)          # no lift: COP 1


def test_heat_topology_clamps_upstairs_too():
    topo, info = core.heat_topology(_flat_th(19.0, 18.0, 10.0, up=True, up_start=24.0),
                                    local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert info["clamped"]["upstairs"][0] > 0
    assert topo["storage"][1]["max_temperatures"][0] == round(24.0 + 0.5, 2)


# ---- load exclusion ----------------------------------------------------------
from datetime import date


def _days(w, n_days=3, per_day=96):
    return {date(2026, 10, 1 + d): [w] * per_day for d in range(n_days)}


def test_load_series_subtracts_the_excluded_profile_and_floors_at_zero():
    t0 = local(2026, 10, 10, 0, 0)
    base, _ = core.load_series(t0, 96, AMS, _days(800.0))
    ex, info = core.load_series(t0, 96, AMS, _days(800.0), exclude_days=_days(300.0))
    assert base[10] == 800.0 and ex[10] == 500.0
    assert info["excluded_mean_w"] == 300.0
    deep, _ = core.load_series(t0, 96, AMS, _days(200.0), exclude_days=_days(300.0))
    assert deep[10] == 0.0


def test_load_series_first_step_mix_also_excludes_the_live_ac_draw():
    t0 = local(2026, 10, 10, 0, 0)
    vals, _ = core.load_series(t0, 96, AMS, _days(800.0), now_w=1000.0, exclude_days=_days(300.0), exclude_now_w=400.0)
    assert vals[0] == 0.5 * 500.0 + 0.5 * 600.0


def test_load_series_without_exclusion_is_unchanged():
    t0 = local(2026, 10, 10, 0, 0)
    a, ia = core.load_series(t0, 96, AMS, _days(800.0), now_w=900.0)
    b, ib = core.load_series(t0, 96, AMS, _days(800.0), now_w=900.0, exclude_days=None)
    assert a == b and ib.get("excluded_mean_w", 0.0) == 0.0
    assert "excluded_mean_w" not in ia and "excluded_mean_w" not in ib


def test_add_day_series_sums_elementwise_and_pads():
    a = {date(2026, 10, 1): [1.0, 2.0], date(2026, 10, 2): [5.0]}
    b = {date(2026, 10, 1): [10.0], date(2026, 10, 3): [7.0]}
    assert core.add_day_series(a, b) == {date(2026, 10, 1): [11.0, 2.0], date(2026, 10, 2): [5.0], date(2026, 10, 3): [7.0]}


# ---- payload integration -----------------------------------------------------

def test_heat_topology_none_input_is_disabled():
    assert core.heat_topology(None, local(2026, 10, 10, 10, 0), 96, AMS, 0.10) == (None, {"sent": False, "reason": "disabled"})


def test_apply_thermal_adds_keys_only_when_sent():
    p = {"prediction_horizon": 4}
    assert core.apply_thermal(dict(p), None, {"sent": False, "reason": "disabled"}) == p
    topo, info = core.heat_topology(_th(), local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    q = core.apply_thermal(dict(p), topo, info)
    assert q["heat_topology"] is topo and q["outdoor_temperature_forecast"] == info["outdoor_c"]


def test_relax_and_strip_topology_copy_without_touching_the_original():
    topo, info = core.heat_topology(_th(), local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    p = core.apply_thermal({"prediction_horizon": 96}, topo, info)
    r = core.relax_topology(p)
    st, orig = r["heat_topology"]["storage"][0], p["heat_topology"]["storage"][0]
    assert st["min_temperatures"][:8] == [5.0] * 8
    assert st["min_temperatures"][8] == orig["min_temperatures"][8]
    assert st["max_temperatures"][:8] == [35.0] * 8
    assert st["max_temperatures"][8] == orig["max_temperatures"][8]
    assert orig["min_temperatures"][0] != 5.0 and orig["max_temperatures"][0] != 35.0
    s = core.strip_topology(p)
    assert "heat_topology" not in s and "outdoor_temperature_forecast" not in s and "heat_topology" in p


def _plan_with_stub(tmp_path, statuses, **extra):
    """run_plan live (not dry) over the golden fixtures; the solve answers the
    given statuses in turn (the last one repeats; None is an HTTP 400, "transport"
    an HTTP 0).
    Returns (result, payloads, archived doc)."""
    from golden import fixtures as F
    from golden.cases import _plan_inp
    now = datetime.fromisoformat("2026-09-07T10:07:00+02:00")
    t0, _n = core.horizon(now, F.TZ, days_ahead=2)
    calls = []

    def fake_solve(base_url, payload, timeout=180):
        calls.append(payload)
        st = statuses[min(len(calls) - 1, len(statuses) - 1)]
        if st is None:      # the add-on answers an infeasible model with HTTP 400
            return {"ok": False, "status": 400, "message": "naive-mpc-optim HTTP 400: infeasible", "seconds": 0.1}
        if st == "transport":   # no answer: connection refused or a timeout
            return {"ok": False, "status": 0, "message": "naive-mpc-optim HTTP 0: TimeoutError: timed out",
                    "seconds": 180.0}
        if isinstance(st, int):     # another HTTP error the add-on answered with (500: an exception inside EMHASS)
            return {"ok": False, "status": st, "message": f"naive-mpc-optim HTTP {st}: Internal Server Error",
                    "seconds": 0.2}
        return F.StubSolver(t0, status=st)(base_url, payload, timeout)

    with F.tmp_archive() as arch:
        inp = _plan_inp(core, now, arch, dry_run=False, **extra)
        with F.patched_solve(core.run_plan, fake_solve):
            res = core.run_plan(inp)
        doc = core.load_plan(res["archive_path"]) if res.get("archive_path") else None
    return res, calls, doc


def test_run_plan_without_thermal_key_adds_no_thermal_keys(tmp_path):
    res, calls, doc = _plan_with_stub(tmp_path, ["Optimal"])
    assert "thermal" not in res and "load_excluded_w" not in res and "_topo" not in res
    assert "thermal" not in doc and "heat_topology" not in calls[0]


def test_run_plan_disabled_thermal_is_archived_as_disabled(tmp_path):
    res, calls, doc = _plan_with_stub(tmp_path, ["Optimal"], thermal={"enabled": False})
    assert res["thermal"] == {"sent": False, "reason": "disabled", "fallback": None}
    assert doc["thermal"] == res["thermal"] and doc["load_excluded_w"] == 0.0
    assert "heat_topology" not in calls[0] and "_topo" not in res


def test_run_plan_thermal_fallback_strips_after_two_failures(tmp_path):
    now = datetime.fromisoformat("2026-09-07T10:07:00+02:00")
    th = _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                      "now_c": 14.0})
    res, calls, doc = _plan_with_stub(tmp_path, ["Infeasible", "Infeasible", "Optimal"], thermal=th, ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 3
    assert "heat_topology" in calls[0]
    assert calls[1]["heat_topology"]["storage"][0]["min_temperatures"][0] == 5.0
    assert "heat_topology" not in calls[2] and "outdoor_temperature_forecast" not in calls[2]
    assert res["thermal"]["fallback"] == "stripped" and res["thermal"]["sent"] is False
    assert res["optim_status"] == "Optimal" and doc["thermal"]["fallback"] == "stripped"
    assert "_topo" not in res and "_topo" not in doc


def test_run_plan_thermal_fallback_strips_after_two_http_failures(tmp_path):
    th = _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                      "now_c": 14.0})
    res, calls, doc = _plan_with_stub(tmp_path, [None, None, "Optimal"], thermal=th, ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 3
    assert "heat_topology" in calls[0]
    assert calls[1]["heat_topology"]["storage"][0]["min_temperatures"][0] == 5.0
    assert "heat_topology" not in calls[2] and "outdoor_temperature_forecast" not in calls[2]
    assert res["thermal"]["fallback"] == "stripped" and res["thermal"]["sent"] is False
    assert res["optim_status"] == "Optimal" and doc is not None and doc["thermal"]["fallback"] == "stripped"


def test_run_plan_thermal_http_failure_then_relaxed_optimal(tmp_path):
    th = _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                      "now_c": 14.0})
    res, calls, doc = _plan_with_stub(tmp_path, [None, "Optimal"], thermal=th, ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 2 and "heat_topology" in calls[1]
    assert res["thermal"]["fallback"] == "relaxed" and res["thermal"]["sent"] is True
    assert res["optim_status"] == "Optimal" and doc is not None and doc["thermal"]["fallback"] == "relaxed"


def test_run_plan_thermal_three_http_failures_end_without_a_plan(tmp_path):
    th = _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                      "now_c": 14.0})
    res, calls, doc = _plan_with_stub(tmp_path, [None], thermal=th, ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 3 and "heat_topology" not in calls[2]
    assert res["thermal"]["fallback"] == "stripped" and res["ok"] is False and doc is None
    assert "HTTP 400" in res["message"]


def _ac_days():
    from golden import fixtures as F
    return {d: [150.0] * 96 for d in F.load_days(core, date(2026, 9, 7))}


def test_stripped_fallback_restores_the_ac_circuits_to_the_load_forecast(tmp_path):
    th = _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                      "now_c": 14.0})
    _, plain_calls, _ = _plan_with_stub(tmp_path, ["Optimal"])
    unexcluded = plain_calls[0]["load_power_forecast"]
    res, calls, doc = _plan_with_stub(tmp_path, ["Infeasible", "Infeasible", "Optimal"], thermal=th,
                                      ac_days_by_zone={"living": _ac_days()}, ac_now_w_by_zone={"living": 150.0})
    assert calls[0]["load_power_forecast"] == calls[1]["load_power_forecast"] != unexcluded
    assert calls[2]["load_power_forecast"] == unexcluded
    assert res["load_excluded_w"] == 0.0 and doc["load_excluded_w"] == 0.0
    rel, rcalls, rdoc = _plan_with_stub(tmp_path, ["Infeasible", "Optimal"], thermal=th,
                                        ac_days_by_zone={"living": _ac_days()}, ac_now_w_by_zone={"living": 150.0})
    assert rel["thermal"]["fallback"] == "relaxed" and rel["load_excluded_w"] > 0.0
    assert rdoc["load_excluded_w"] == rel["load_excluded_w"]


def test_run_plan_thermal_relaxed_solve_stops_the_fallback(tmp_path):
    th = _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                      "now_c": 14.0})
    res, calls, doc = _plan_with_stub(tmp_path, ["Infeasible", "Optimal"], thermal=th, ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 2 and res["thermal"]["fallback"] == "relaxed" and res["thermal"]["sent"] is True
    assert "heat_topology" in calls[1]


def _th_sept(**over):
    return _th(weather={"points": [(local(2026, 9, d, h, 0).isoformat(), 14.0) for d in range(6, 11) for h in range(24)],
                        "now_c": 14.0}, **over)


def test_run_plan_http_500_relaxes_then_strips_like_an_infeasible_solve(tmp_path):
    """N1 (2026-10-10 final review): an add-on 500 on a payload with the topology
    must not end the quarter with the topology still sent; it walks the same
    relaxed -> stripped ladder as a 400."""
    res, calls, doc = _plan_with_stub(tmp_path, [500, 500, "Optimal"], thermal=_th_sept(),
                                      ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 3 and "heat_topology" in calls[0] and "heat_topology" in calls[1]
    assert "heat_topology" not in calls[2]
    assert res["thermal"]["fallback"] == "stripped" and res["thermal"]["sent"] is False
    assert res["optim_status"] == "Optimal" and doc is not None and doc["thermal"]["fallback"] == "stripped"


def test_run_plan_http_500_then_optimal_is_relaxed(tmp_path):
    res, calls, doc = _plan_with_stub(tmp_path, [500, "Optimal"], thermal=_th_sept(),
                                      ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 2 and res["thermal"]["fallback"] == "relaxed" and res["thermal"]["sent"] is True


def test_run_plan_transport_error_ends_the_quarter_without_relaxing(tmp_path):
    res, calls, doc = _plan_with_stub(tmp_path, ["transport"], thermal=_th_sept(),
                                      ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 1 and "heat_topology" in calls[0]
    assert res["ok"] is False and doc is None and "HTTP 0" in res["message"]
    assert res["thermal"]["fallback"] is None and "_solve_status" not in res and "_topo" not in res


def test_run_plan_infeasible_then_transport_error_stops_after_the_relaxed_solve(tmp_path):
    res, calls, doc = _plan_with_stub(tmp_path, [None, "transport"], thermal=_th_sept(),
                                      ac_days_by_zone={}, ac_now_w_by_zone={})
    assert len(calls) == 2 and "heat_topology" in calls[1]
    assert res["thermal"]["fallback"] == "relaxed" and res["ok"] is False and doc is None


def test_run_plan_raising_heat_topology_is_not_sent_and_the_battery_plans(tmp_path, monkeypatch):
    import sys

    def boom(*a, **k):
        raise ZeroDivisionError("float division by zero")
    monkeypatch.setattr(sys.modules[core.run_plan.__module__], "heat_topology", boom)
    res, calls, doc = _plan_with_stub(tmp_path, ["Optimal"], thermal=_th_sept(),
                                      ac_days_by_zone={"living": _ac_days()}, ac_now_w_by_zone={"living": 150.0})
    assert res["thermal"] == {"sent": False, "reason": "error: ZeroDivisionError('float division by zero')",
                              "fallback": None}
    assert res["ok"] is True and res["optim_status"] == "Optimal" and "heat_topology" not in calls[0]
    assert res["load_excluded_w"] == 0.0 and doc["thermal"]["sent"] is False


def test_run_plan_null_constant_is_not_sent_and_the_battery_plans(tmp_path):
    th = _th_sept()
    th["constants"]["zones"]["living"]["loss_coefficient_kw_k"] = None
    res, calls, doc = _plan_with_stub(tmp_path, ["Optimal"], thermal=th, ac_days_by_zone={}, ac_now_w_by_zone={})
    assert res["thermal"]["sent"] is False and res["thermal"]["reason"].startswith("error: TypeError(")
    assert res["ok"] is True and res["optim_status"] == "Optimal" and "heat_topology" not in calls[0]


def test_run_plan_input_error_is_archived_and_the_battery_plans(tmp_path):
    res, calls, doc = _plan_with_stub(tmp_path, ["Optimal"], thermal={"enabled": False, "error": "OSError()"})
    assert res["thermal"] == {"sent": False, "reason": "error: OSError()", "fallback": None}
    assert res["ok"] is True and doc["thermal"]["reason"] == "error: OSError()"


def _ac_zone_days(w):
    from golden import fixtures as F
    return {d: [w] * 96 for d in F.load_days(core, date(2026, 9, 7))}


def test_run_plan_excludes_the_ac_circuit_of_each_zone_sent(tmp_path):
    by_zone = {"living": _ac_zone_days(150.0), "upstairs": _ac_zone_days(100.0)}
    now_w = {"living": 150.0, "upstairs": 100.0}
    both, _, _ = _plan_with_stub(tmp_path, ["Optimal"], thermal=_th_sept(),
                                 ac_days_by_zone=by_zone, ac_now_w_by_zone=now_w)
    assert both["thermal"]["upstairs"] is True and both["load_excluded_w"] == 250.0
    th = _th_sept(); th["upstairs"]["start_c"] = None                     # the zone is not sent
    living, _, _ = _plan_with_stub(tmp_path, ["Optimal"], thermal=th, ac_days_by_zone=by_zone, ac_now_w_by_zone=now_w)
    assert living["thermal"]["upstairs"] is False and living["load_excluded_w"] == 150.0


def test_run_plan_without_thermal_key_ignores_the_ac_histories(tmp_path):
    _, plain_calls, _ = _plan_with_stub(tmp_path, ["Optimal"])
    res, calls, _ = _plan_with_stub(tmp_path, ["Optimal"], ac_days_by_zone={"living": _ac_zone_days(150.0)},
                                    ac_now_w_by_zone={"living": 150.0})
    assert calls[0]["load_power_forecast"] == plain_calls[0]["load_power_forecast"]
    assert "load_excluded_w" not in res and "thermal" not in res


# ---- step in force and follower doc ------------------------------------------


def _thermal_doc(t0, n, hp, pellet=None, t_liv=None, sent=True, plan_ts=None, reason="ok"):
    """A minimal archived plan with thermal columns. hp/pellet are W per step."""
    rows = []
    for k in range(n):
        ts = (t0 + timedelta(minutes=15 * k)).isoformat()
        r = {"timestamp": ts, "P_grid": 0.0, "P_batt": 0.0, "SOC_opt": 0.5, "unit_load_cost": 0.1, "unit_prod_price": 0.1,
             "optim_status": "Optimal", "P_deferrable0": hp[k], "P_deferrable1": 0.0,
             "predicted_temp_heater0": (t_liv or [19.0] * n)[k], "min_temp_heater0": 18.0, "max_temp_heater0": 21.0,
             "predicted_temp_heater1": 19.5, "min_temp_heater1": 17.0, "max_temp_heater1": 20.0}
        if pellet is not None:
            r["P_deferrable2"] = pellet[k]
        rows.append(r)
    sources = {"hp_living": 0, "hp_multi": 1} | ({"pellet": 2} if pellet is not None else {})
    return {"plan_ts": (plan_ts or t0).isoformat(), "t0": t0.isoformat(), "n": n, "tz": AMS, "rows": rows,
            "optim_status": "Optimal", "replay": False,
            "thermal": {"sent": sent, "reason": reason, "sources": sources, "zones": {"living": 0, "upstairs": 1}}}


def _write(archive, doc):
    core.write_plan_archive(str(archive), datetime.fromisoformat(doc["plan_ts"]), doc)


def test_thermal_step_in_force_picks_row_and_next_row(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    _write(tmp_path, _thermal_doc(t0, 8, hp=[0, 1200, 1200, 0, 0, 0, 0, 0]))
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20))
    assert st["index"] == 1 and st["row"]["P_deferrable0"] == 1200 and st["next_row"]["P_deferrable0"] == 1200
    assert st["sources"] == {"hp_living": 0, "hp_multi": 1} and st["stale"] is False


def test_thermal_step_in_force_skips_plans_without_thermal(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    doc = _thermal_doc(t0, 8, hp=[0] * 8, sent=False)
    _write(tmp_path, doc)
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20)) is None


def test_thermal_step_in_force_stale_after_three_hours(tmp_path):
    t0 = local(2026, 10, 10, 6, 0)
    _write(tmp_path, _thermal_doc(t0, 96, hp=[0] * 96))
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 8, 59))["stale"] is False
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 9, 1))["stale"] is True


def test_source_word_hold_rule_on_the_pellet_only_and_pellet_gate():
    assert core.source_word(1200, 0, 1200, 0, False) == "AC"
    assert core.source_word(1200, 0, 0, 0, False) == "AC"               # a heat pump has no hold rule
    assert core.source_word(1200, 0, None, 0, False) == "AC"
    assert core.source_word(0, 8000, 0, 8000, True) == "PELLET"
    assert core.source_word(0, 8000, 0, 0, True) == "COAST"             # the pellet's marginal step: held off
    assert core.source_word(1200, 8000, 0, 0, True) == "AC"             # the pellet held off, the heat pump runs
    assert core.source_word(0, 8000, 0, None, True) == "PELLET"         # last row: no hold
    assert core.source_word(1200, 8000, 1200, 8000, True) == "COMBINED"
    assert core.source_word(0, 8000, 0, 8000, False) == "COAST"         # a pellet column with the stove unavailable
    assert core.source_word(30, 0, 30, 0, False) == "COAST"             # under ON_W


def test_source_word_ignores_pellet_when_unavailable():
    assert core.source_word(1200, 8000, 1200, 8000, False) == "AC"


def test_daikin_grid():
    assert core.daikin_grid(19.26, 1.0) == 20.5
    assert core.daikin_grid(19.24, 1.0) == 20.0
    assert core.daikin_grid(40.0, 0.0) == 30.0 and core.daikin_grid(3.0, 0.0) == 10.0


def test_follower_doc_targets_the_end_of_block_temperature(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    d = _thermal_doc(t0, 8, hp=[1200] * 8, t_liv=[19.0, 19.3, 19.6, 19.9, 20.2, 20.5, 20.8, 21.0])
    for r in d["rows"]:
        r["P_deferrable1"] = 900.0                  # the multi runs, so the upstairs target is the predicted temperature
    _write(tmp_path, d)
    now = local(2026, 10, 10, 10, 20)
    st = core.thermal_step_in_force(str(tmp_path), now)
    doc = core.follower_doc(st, now, 1.0, False, True)
    assert doc["source"] == "AC"
    assert doc["target_living"] == core.daikin_grid(21.0, 1.0)       # the block runs to the end: its highest prediction
    assert doc["target_upstairs"] == core.daikin_grid(19.5, 0.0)
    a = doc["attrs"]
    assert a["p_hp_living_w"] == 1200 and a["t_living_pred"] == 19.3 and a["t_living_min"] == 18.0
    assert len(a["times"]) == 8 and len(a["t_living_pred_series"]) == 8 and a["plan_ts"] == t0.isoformat()
    assert a["status"] == "ok"


def test_follower_doc_without_a_step_is_off_and_stale_is_reported(tmp_path):
    now = local(2026, 10, 10, 10, 20)
    doc = core.follower_doc(None, now, 1.0, False, True)
    assert doc["source"] == "off" and doc["target_living"] is None and doc["attrs"]["status"] == "no plan"
    t0 = local(2026, 10, 10, 6, 0)
    _write(tmp_path, _thermal_doc(t0, 96, hp=[1200] * 96))
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 9, 30))
    doc = core.follower_doc(st, local(2026, 10, 10, 9, 30), 1.0, False, True)
    assert doc["source"] == "stale" and doc["attrs"]["status"] == "stale"


def test_thermal_step_in_force_skips_stripped_plans_and_plans_without_columns(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    doc = _thermal_doc(t0, 8, hp=[1200] * 8)
    doc["thermal"]["fallback"] = "stripped"
    _write(tmp_path, doc)
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20)) is None
    bare = _thermal_doc(t0 + timedelta(minutes=15), 8, hp=[1200] * 8)
    for r in bare["rows"]:
        for key in [k for k in r if "heater" in k or k.startswith("P_deferrable")]:
            del r[key]
    _write(tmp_path, bare)
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20))
    assert st is None
    assert core.follower_doc(st, local(2026, 10, 10, 10, 20), 1.0, False, True)["source"] == "off"


def test_run_plan_thermal_not_sent_leaves_load_unreduced(tmp_path):
    _, plain_calls, _ = _plan_with_stub(tmp_path, ["Optimal"])
    th = _th()
    th["living"]["start_c"] = None
    res, calls, _doc = _plan_with_stub(tmp_path, ["Optimal"], thermal=th, ac_days_by_zone={"living": _ac_days()}, ac_now_w_by_zone={"living": 150.0})
    assert res["thermal"]["sent"] is False
    assert calls[0]["load_power_forecast"] == plain_calls[0]["load_power_forecast"]
    assert res["load_excluded_w"] == 0.0


def _older_thermal_and_newer_stripped(tmp_path):
    t9 = local(2026, 10, 10, 9, 0)
    _write(tmp_path, _thermal_doc(t9, 16, hp=[1200] * 16))
    t10 = local(2026, 10, 10, 10, 0)
    stripped = _thermal_doc(t10, 16, hp=[1200] * 16)
    stripped["thermal"]["fallback"] = "stripped"
    _write(tmp_path, stripped)
    return t9


def test_thermal_step_in_force_falls_back_to_the_older_thermal_plan(tmp_path):
    t9 = _older_thermal_and_newer_stripped(tmp_path)
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20))
    assert st["plan_ts"] == t9.isoformat() and st["index"] == 5 and st["stale"] is False


def test_thermal_step_in_force_older_plan_fallback_reports_stale(tmp_path):
    t9 = _older_thermal_and_newer_stripped(tmp_path)
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 12, 5))
    assert st["plan_ts"] == t9.isoformat() and st["stale"] is True


def test_follower_doc_single_step_living_block_runs_to_its_end_of_step_temperature(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    now = local(2026, 10, 10, 10, 20)
    _write(tmp_path, _thermal_doc(t0, 8, hp=[0, 1200, 0, 0, 0, 0, 0, 0],
                                  t_liv=[19.0, 19.0, 19.4, 19.3, 19.2, 19.1, 19.0, 18.9]))
    doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, False, True)
    assert doc["source"] == "AC" and doc["target_living"] == core.daikin_grid(19.4, 1.0)
    assert doc["attrs"]["hp_multi_on"] is False


def test_follower_doc_coast_step_targets_the_comfort_floor(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    now = local(2026, 10, 10, 10, 20)
    _write(tmp_path, _thermal_doc(t0, 8, hp=[1200, 0, 1200, 0, 0, 0, 0, 0]))
    doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, False, True)
    assert doc["source"] == "COAST" and doc["target_living"] == core.daikin_grid(18.0, 1.0)


def test_follower_doc_single_step_multi_block_is_on(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    now = local(2026, 10, 10, 10, 20)
    d = _thermal_doc(t0, 8, hp=[1200] * 8)
    d["rows"][1]["P_deferrable1"] = 900.0                       # multi on this step, off the next
    d["rows"][2]["predicted_temp_heater1"] = 19.8
    _write(tmp_path, d)
    doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, False, True)
    assert doc["source"] == "AC" and doc["attrs"]["hp_multi_on"] is True
    assert doc["target_upstairs"] == core.daikin_grid(19.8, 0.0)
    assert core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, False, False)["target_upstairs"] is None
    d["rows"][1]["P_deferrable1"] = 0.0                         # multi off this step: the upstairs floor
    other = tmp_path / "off"
    other.mkdir()
    _write(other, d)
    doc = core.follower_doc(core.thermal_step_in_force(str(other), now), now, 1.0, False, True)
    assert doc["attrs"]["hp_multi_on"] is False and doc["target_upstairs"] == core.daikin_grid(17.0, 0.0)


def test_follower_doc_end_of_block_target_stops_at_the_block_end(tmp_path):
    # living on at steps 1-3; the end of step 3 is row 4 (20,4); row 5 (22,0) is after the block
    t0 = local(2026, 10, 10, 10, 0)
    _write(tmp_path, _thermal_doc(t0, 8, hp=[0, 1200, 1200, 1200, 0, 0, 0, 0],
                                  t_liv=[19.0, 19.2, 19.6, 20.1, 20.4, 22.0, 20.0, 19.8]))
    for minute, want in ((20, 20.4), (35, 20.4), (50, 20.4)):
        now = local(2026, 10, 10, 10, minute)
        doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, False, True)
        assert doc["source"] == "AC" and doc["target_living"] == core.daikin_grid(want, 1.0)


def test_follower_doc_pellet_block_target(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    _write(tmp_path, _thermal_doc(t0, 8, hp=[0] * 8, pellet=[0, 8000, 8000, 0, 0, 0, 0, 0],
                                  t_liv=[19.0, 19.0, 19.8, 20.6, 20.4, 20.2, 20.0, 19.8]))
    now = local(2026, 10, 10, 10, 20)
    doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, True, True)
    assert doc["source"] == "PELLET" and doc["target_living"] == core.daikin_grid(20.6, 1.0)
    now = local(2026, 10, 10, 10, 35)                           # the pellet's last step: held off
    doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, True, True)
    assert doc["source"] == "COAST" and doc["target_living"] == core.daikin_grid(18.0, 1.0)


def test_follower_doc_multi_on_keeps_predicted_targets_and_next_row_without_column_is_off(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    now = local(2026, 10, 10, 10, 20)
    d = _thermal_doc(t0, 8, hp=[1200] * 8)
    for r in d["rows"]:
        r["P_deferrable1"] = 900.0
    _write(tmp_path, d)
    doc = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 0.0, False, True)
    assert doc["attrs"]["hp_multi_on"] is True and doc["target_upstairs"] == core.daikin_grid(19.5, 0.0)
    st = core.thermal_step_in_force(str(tmp_path), now)
    st["next_row"] = {"timestamp": "x"}                         # a next row without columns: no hold on a heat pump
    assert core.follower_doc(st, now, 0.0, False, True)["source"] == "AC"


def test_thermal_step_in_force_skips_malformed_docs(tmp_path):
    t9 = local(2026, 10, 10, 9, 0)
    _write(tmp_path, _thermal_doc(t9, 16, hp=[1200] * 16))
    bad = _thermal_doc(local(2026, 10, 10, 10, 0), 8, hp=[1200] * 8)
    del bad["t0"]
    _write(tmp_path, bad)
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20))
    assert st["plan_ts"] == t9.isoformat()
    short = _thermal_doc(local(2026, 10, 10, 10, 5), 8, hp=[1200] * 8)
    short["n"] = 96
    _write(tmp_path, short)
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 12, 10))["plan_ts"] == t9.isoformat()


def test_thermal_step_in_force_newest_disabled_plan_is_off_at_once(tmp_path):
    t9 = local(2026, 10, 10, 9, 0)
    _write(tmp_path, _thermal_doc(t9, 16, hp=[1200] * 16))
    _write(tmp_path, _thermal_doc(local(2026, 10, 10, 10, 0), 16, hp=[0] * 16, sent=False, reason="disabled"))
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20)) is None
    err = tmp_path / "err"
    err.mkdir()
    _write(err, _thermal_doc(t9, 16, hp=[1200] * 16))
    _write(err, _thermal_doc(local(2026, 10, 10, 10, 0), 16, hp=[0] * 16, sent=False, reason="error: OSError()"))
    assert core.thermal_step_in_force(str(err), local(2026, 10, 10, 10, 20)) is None


def test_thermal_step_in_force_newest_plan_without_thermal_is_off_at_once(tmp_path):
    t9 = local(2026, 10, 10, 9, 0)
    _write(tmp_path, _thermal_doc(t9, 16, hp=[1200] * 16))
    bare = _thermal_doc(local(2026, 10, 10, 10, 0), 16, hp=[0] * 16)
    del bare["thermal"]
    _write(tmp_path, bare)
    assert core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20)) is None


@pytest.mark.parametrize("reason", ["living start temperature unavailable", "no forecast", "forecast short by 30 steps"])
def test_thermal_step_in_force_sensor_gap_serves_the_older_thermal_plan(tmp_path, reason):
    t9 = local(2026, 10, 10, 9, 0)
    _write(tmp_path, _thermal_doc(t9, 16, hp=[1200] * 16))
    _write(tmp_path, _thermal_doc(local(2026, 10, 10, 10, 0), 16, hp=[0] * 16, sent=False, reason=reason))
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20))
    assert st["plan_ts"] == t9.isoformat() and st["stale"] is False


def test_thermal_step_in_force_scan_stops_at_the_first_plan_older_than_stale(tmp_path, monkeypatch):
    import sys
    t0 = local(2026, 10, 10, 6, 0)
    _write(tmp_path, _thermal_doc(t0, 96, hp=[1200] * 96))                          # thermal, 06:00
    for h in range(7, 15):                                                          # 07:00 .. 14:00: sensor gaps
        _write(tmp_path, _thermal_doc(local(2026, 10, 10, h, 0), 96, hp=[0] * 96, sent=False, reason="no forecast"))
    now = local(2026, 10, 10, 14, 20)
    assert core.thermal_step_in_force(str(tmp_path), now) is None                    # 11:00 is the last plan opened
    arch = sys.modules[core.thermal_step_in_force.__module__.rsplit(".", 1)[0] + ".archive"]
    opened = []
    real = arch.load_plan
    monkeypatch.setattr(arch, "load_plan", lambda path: opened.append(path) or real(path))
    assert core.thermal_step_in_force(str(tmp_path), now) is None
    assert len(opened) == 4                                                          # 14, 13, 12 and 11 h


def test_thermal_step_in_force_skips_non_dict_thermal_and_rows(tmp_path):
    t9 = local(2026, 10, 10, 9, 0)
    _write(tmp_path, _thermal_doc(t9, 16, hp=[1200] * 16))
    odd = _thermal_doc(local(2026, 10, 10, 9, 30), 16, hp=[1200] * 16)
    odd["thermal"] = "sent"
    _write(tmp_path, odd)
    rows = _thermal_doc(local(2026, 10, 10, 10, 0), 16, hp=[1200] * 16)
    rows["rows"][5] = "garbage"
    _write(tmp_path, rows)
    st = core.thermal_step_in_force(str(tmp_path), local(2026, 10, 10, 10, 20))
    assert st["plan_ts"] == t9.isoformat()
    assert core.follower_doc(st, local(2026, 10, 10, 10, 20), 1.0, False, True)["source"] == "AC"


def test_follower_doc_non_finite_values_publish_as_none(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    d = _thermal_doc(t0, 4, hp=[1200] * 4)
    d["rows"][2]["P_deferrable1"] = float("inf")
    d["rows"][3]["predicted_temp_heater1"] = float("nan")
    d["rows"][3]["heating_demand_heater0"] = float("nan")
    st = {"plan_ts": t0.isoformat(), "t0": t0.isoformat(), "n": 4, "index": 0, "row": d["rows"][0],
          "next_row": d["rows"][1], "rows": d["rows"], "sources": d["thermal"]["sources"],
          "zones": d["thermal"]["zones"], "stale": False}
    a = core.follower_doc(st, local(2026, 10, 10, 10, 5), 1.0, False, True)["attrs"]
    assert a["p_hp_multi_w_series"][2] is None and a["t_upstairs_pred_series"][3] is None
    assert a["q_living_w_series"][3] is None
    assert core._r0(float("nan")) is None and core._r2(float("-inf")) is None and core._r0(12.4) == 12


def test_follower_doc_exposes_the_predicted_heat_demand(tmp_path):
    t0 = local(2026, 10, 10, 10, 0)
    now = local(2026, 10, 10, 10, 20)
    d = _thermal_doc(t0, 4, hp=[1200] * 4)
    for k, r in enumerate(d["rows"]):
        r["heating_demand_heater0"] = 1000.0 + k
        r["heating_demand_heater1"] = 400.0 + k
    _write(tmp_path, d)
    a = core.follower_doc(core.thermal_step_in_force(str(tmp_path), now), now, 1.0, False, True)["attrs"]
    assert a["q_living_w"] == 1001.0 and a["q_upstairs_w"] == 401.0
    assert a["q_living_w_series"] == [1000.0, 1001.0, 1002.0, 1003.0]
    assert a["q_upstairs_w_series"] == [400.0, 401.0, 402.0, 403.0]
    plain = tmp_path / "plain"
    plain.mkdir()
    _write(plain, _thermal_doc(t0, 4, hp=[1200] * 4))               # rows without the column
    a = core.follower_doc(core.thermal_step_in_force(str(plain), now), now, 1.0, False, True)["attrs"]
    assert a["q_living_w"] is None and a["q_upstairs_w"] is None
    assert a["q_living_w_series"] == [None] * 4 and a["q_upstairs_w_series"] == [None] * 4


def test_heat_topology_living_absolute_ceiling():
    th = _th(); th["living"]["ceiling_c"] = 24.0
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    living = topo["storage"][0]
    assert len(living["max_temperatures"]) == 96
    assert set(living["max_temperatures"]) == {24.0}
    assert info["living_ceiling_c"] == 24.0 and info["band_k"] == 3.0
    # the clamp still runs: a start above the ceiling stays feasible
    th["living"]["start_c"] = 26.0
    topo, _ = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    assert topo["storage"][0]["max_temperatures"][0] >= 26.0 - 1.0


def test_heat_topology_living_ceiling_absent_keeps_band():
    th = _th()
    topo, info = core.heat_topology(th, local(2026, 10, 10, 10, 0), 96, AMS, 0.10)
    living = topo["storage"][0]
    assert max(living["max_temperatures"]) == 22.5            # Saturday evening floor 19,5 + 3,0
    assert len(set(living["max_temperatures"])) > 1           # follows the floor, not one flat value
    assert info["living_ceiling_c"] is None
