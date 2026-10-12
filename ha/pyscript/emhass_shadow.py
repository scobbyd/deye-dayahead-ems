"""EMHASS planner: thin pyscript wrapper. This is the LIVE planner; "shadow" in the
file name and in input_boolean.emhass_shadow_enabled is historical and stays.

Reads HA state and the tariff helpers, calls the Nord Pool action, hands
plain data to the native module emhass_core (task.executor, off the event
loop), writes the results back as states and long-term statistics. EMHASS
itself publishes sensor.emhass_da_* via publish-data. Nothing here writes to
the inverter; pyscript/emhass_writer.py does.

Services
  pyscript.emhass_plan_day(dry_run=False)  plan now -> 24:00 of D+1 (D+2 with Solcast day 3), archive, roll, publish
  pyscript.emhass_ladder_nightly           00:10: walk the actual rung over the last four days
  pyscript.emhass_ladder(start,end,rungs,reprice)
                                           walk the ladder over a range, re-import emhass:ladder_earned_eur
  pyscript.emhass_ladder_hours(start,end)  backfill the ladder's hourly sidecar, re-import the statistics
  pyscript.emhass_today_hours              the running day's settled hours, without a plan run
  pyscript.emhass_ab(start,end,label,overrides,from_live,cadence_min,settle,baseline)
                                           A/B: re-run past days' solves under other knobs, settled closed-loop
  pyscript.emhass_ab_apply(label)          push a stored A/B variant's knobs into the live helpers
  pyscript.emhass_fit / emhass_tune        forecast-model-fit / -tune on the UPS-port load
  pyscript.emhass_health                   healthz + config drift -> binary_sensor.emhass_addon_healthy
  pyscript.emhass_rehydrate                rebuild the plan slices from the archive (runs at startup)
  pyscript.emhass_zonnebonus_seed(used_kwh)  set this year's bonus-eligible export after a backfill

The shadow scoreboard (emhass_score_day, scores.csv, sensor.emhass_score,
sensor.emhass_gap_30d, sensor.emhass_ladder, the emhass:planned, replayed,
realised, gap and hindsight_eur statistics) and the replay services were
retired on 2026-09-27, the day after the writer went live ("Now that we
are live, I don't want the shadow numbers of past year anymore"). The files
under /config/emhass stay.

Spec: an internal design note
"""
# Load the compute as a genuinely NATIVE python module (NOT under pyscript/, which
# would be pyscript-interpreted and rejected by task.executor).
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("emhass_core", "/config/pyscript_helpers/emhass_core.py")
core = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(core)

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import (async_add_external_statistics,
                                                          statistics_during_period)
from homeassistant.components.recorder.models import StatisticMeanType

BASE = "http://5b918bf2-emhass:5000"          # add-on hostname on the hassio network (ingress only)
ARCHIVE = "/config/emhass/plans"
AB_DIR = "/config/emhass/ab"
LADDER = "/config/emhass/ladder.csv"          # the ladder: actual / hindsight / omni2, cash per day
REBALANCE = "/config/emhass/rebalance.json"   # the settled rebalancing clock (core.rebalance_step)
ZONNEBONUS = "/config/emhass/zonnebonus.json" # the sun-bonus kWh counter, per calendar year (core.sun_bonus_step_file)
ZONNEBONUS_SENSOR = "sensor.emhass_zonnebonus_remaining"
P1_EXPORT_TOTAL = "sensor.p1_energy_export_total"   # kWh, cumulative fiscal export (tariff 1 + 2)
WRITER_DIR = "/config/emhass/writer"           # the writer's tick archive, one file per day
WRITER_ANCHOR = "/config/emhass/writer/anchor.json"   # the real pack at the live switch (core.write_anchor)
REPO_CFG = "/config/emhass/config.json"
# Every hardware and household entity this wrapper reads: core.PLANT["entities"]
# (emhasscore/plant.py DEFAULTS, overridden by plant.json).
ENTITIES = core.PLANT["entities"]
NP_ENTRY = ENTITIES["nordpool_entry"]       # Nord Pool config entry
TZ_NAME = hass.config.time_zone
TZ = ZoneInfo(TZ_NAME)
# num_lags 288, not 96 (2026-09-03): the mlforecaster predicts exactly
# num_lags steps and ABORTS the whole optimisation when the horizon is
# longer - ours runs 195-287 steps with the D+2 extension. 288 is also the
# top of the tune grid, so a Sunday tune can never pull it back under the
# horizon. At 96 the ML switch would have failed every run on day 10.
ML = {"model_type": "load_forecast", "var_model": ENTITIES["load"],
      "sklearn_model": "KNeighborsRegressor", "num_lags": 288}
ACTUAL_EXPORT_SW = ENTITIES["export_switch"]             # the other half of the site's rule
# The Solcast site the Growatt shares with the Deye's east string. Registered as
# one 9,8 kWp roof because the free tier allows two sites; the Growatt is ~2,8
# of it. See core.pv_split.
SOLCAST_ESE_SITE = core.PLANT["pv"]["solcast_sites"]["A"]
ACTUAL_PV = ENTITIES["pv"]                              # W
ACTUAL_LOAD = ENTITIES["load"]                   # W
ACTUAL_GRID_IMP = ENTITIES["grid_import"]  # kW, fiscal meter
ACTUAL_GRID_EXP = ENTITIES["grid_export"]  # kW, fiscal meter
ACTUAL_BATT = ENTITIES["batt_power"]                    # W at the DC port, + = discharge
ACTUAL_CURTAIL = ENTITIES["curtailed"]                  # 0/1; a 15-min mean is the fraction
ACTUAL_MICRO = ENTITIES["micro"]             # W, the must-take half (never curtailed)
ACTUAL_SOC = ENTITIES["soc"]                           # % pack state of charge
ACTUAL_BATT_V = ENTITIES["batt_voltage"]                # V at the DC port: the rebalancing clock's top time
# The heat topology (thermal.py). Read at solve time, frozen into the plan.
THERMAL_ENABLED = "input_boolean.heating_emhass_enabled"
AC_CIRCUITS = {"living": "sensor.input_8", "upstairs": "sensor.input_6"}   # IoTaWatt outdoor units by zone, W
ZONE_SENSORS = {"living": "sensor.env4_temperature", "bedroom": "sensor.env3_temperature", "study": "sensor.env1_temperature"}
WEATHER_FC = "sensor.heating_weather_forecast"            # Met.no hourly forecast, attribute `forecast`
OUTDOOR_NOW = "sensor.outdoor_temperature"
# The series core.day_actuals and core.window_actuals read, by the key they
# return them under (imp and exp, the meter's kW halves, come back as grid_w).
DAY_SERIES = {"pv_w": ACTUAL_PV, "load_w": ACTUAL_LOAD, "imp": ACTUAL_GRID_IMP, "exp": ACTUAL_GRID_EXP,
              "batt_dc_w": ACTUAL_BATT, "soc_pct": ACTUAL_SOC, "curtailed": ACTUAL_CURTAIL, "micro_w": ACTUAL_MICRO,
              "batt_v": ACTUAL_BATT_V}
WINDOW_SERIES = {"pv_w": ACTUAL_PV, "imp": ACTUAL_GRID_IMP, "exp": ACTUAL_GRID_EXP, "batt_dc_w": ACTUAL_BATT,
                 "curtailed": ACTUAL_CURTAIL, "soc_pct": ACTUAL_SOC, "micro_w": ACTUAL_MICRO}


def _now():
    return datetime.now(TZ)


def _num(entity_id, default):
    try:
        return float(state.get(entity_id))
    except Exception:
        return default


def _log_notes(notes):
    """Log the core's (level, text) notes, in order."""
    for level, text in notes:
        if level == "debug":
            log.debug(text)
        elif level == "info":
            log.info(text)
        else:
            log.warning(text)


def _attr(entity_id, name):
    try:
        return (state.getattr(entity_id) or {}).get(name)
    except Exception:
        return None


def _writer_live():
    try:
        return state.get("input_select.emhass_writer_mode") == "live"
    except Exception:                  # pyscript raises NameError on a missing entity; no helper means no writer
        return False


def _day_was_live(day):
    """Whether the writer drove the inverter on a PAST day (its archive has a
    live tick), which is what decides the real pack for yesterday's slice and
    the ledger, not the mode now."""
    return task.executor(core.day_was_live, WRITER_DIR, day)


def _soc_anchor(day, act, live=None):
    """LIVE MODE ANCHORS THE SETTLED CHAIN ON THE REAL PACK (spec 3.6). The
    writer drops /config/emhass/writer/anchor.json when the mode goes live; on
    that day the virtual pack is reset to the real SOC at the switch step, on
    every later live day it starts from the real midnight SOC (the first
    15-minute mean of sensor.inverter_battery). Outside live mode, or without
    either, None: the archive's own anchor stands. (step, soc_pct) or None.
    `live` overrides the mode-now test for a past day."""
    if not (_writer_live() if live is None else live):
        return None
    soc = act.get("soc_pct") if act else None
    midnight = soc[0] if soc and soc[0] is not None else None
    return task.executor(core.soc_anchor_for_day, task.executor(core.read_anchor, WRITER_ANCHOR), day, midnight)


def _day_record(day, act, live=None):
    """The day's measurements as one core.DayActuals: the recorder series, and
    in live mode the anchor (_soc_anchor) and the measured pack, meter and SOC
    (THE VIRTUAL PACK IS RETIRED IN LIVE MODE, the site 2026-09-26). `live`
    overrides the mode-now test for a past day: yesterday keeps the real pack
    after the midnight rollover (2026-09-27: the first live day went back to
    the virtual pack at 00:13)."""
    live = _writer_live() if live is None else live
    return core.DayActuals.of(act, live, _soc_anchor(day, act, live=live))


REBALANCE_TOP_HOURS = "sensor.emhass_rebalance_top_hours"
REBALANCE_CLOCK = "sensor.emhass_rebalance_clock"


def _publish_rebalance(state_doc, now):
    """The rebalancing clock as two plottable sensors (2026-10-03, the Battery
    tab's balance tiles): counted hours at full in the last 30 days (at the
    top of the bank voltage until 2026-10-10), and the clock in days with its
    schedule phase."""
    p = core.rebalance.published(state_doc, now)
    common = {"phase": p["phase"], "budget_start": p["budget_start"], "stretches": p["stretches"],
              "last_stretch_end": p["last_stretch_end"], "budget_h": core.REBALANCE_BUDGET_H,
              "full_level_pct": core.REBALANCE_FULL_LEVEL * 100.0, "latch_since": p["latch_since"], "released": p["released"],
              "release_hours": p["release_hours"], "release_h": core.REBALANCE_RELEASE_H,
              "release_window_h": core.REBALANCE_RELEASE_WINDOW_H}
    state.set(REBALANCE_TOP_HOURS, p["top_hours"], new_attributes=dict(common, **{
        "friendly_name": "EMHASS balanced, 30 d", "unit_of_measurement": "h", "state_class": "measurement",
        "icon": "mdi:battery-sync"}))
    state.set(REBALANCE_CLOCK, p["clock_days"], new_attributes=dict(common, **{
        "friendly_name": "EMHASS balance clock", "unit_of_measurement": "d", "state_class": "measurement",
        "icon": "mdi:timer-sand"}))


PACK_TEMP_MEAN = "sensor.emhass_pack_temp_1h"
PACK_TEMP_USED = "sensor.emhass_pack_temp_used"


def _pack_temp_latch():
    """Move the latched pack temperature (core.temp_latch) and publish it.
    The latch is an entity, not a module global, so a reload or a second
    copy of this file cannot split it (the writer's guard, 308af7d)."""
    mean = _num(PACK_TEMP_MEAN, None)
    prev = _num(PACK_TEMP_USED, None)
    used = core.temp_latch(prev, mean, _num("input_number.emhass_temp_deadband_c", 2.0))
    if used is None:
        log.warning(f"emhass: no pack temperature ({PACK_TEMP_MEAN} unreadable, no latch yet): the temperature ramp is off")
        return
    state.set(PACK_TEMP_USED, used, new_attributes={
        "friendly_name": "EMHASS pack temperature used", "unit_of_measurement": "°C",
        "device_class": "temperature", "icon": "mdi:thermometer-lines", "mean_1h": mean})


def _knobs():
    """The live knob layer: every helper in core.LIVE_KNOBS, parsed by
    core.parse_knobs, None when unreadable so the core falls back to its constant."""
    raw = {}
    for name, (default, eid) in core.LIVE_KNOBS.items():
        try:
            raw[name] = state.get(eid)
        except Exception:              # pyscript raises NameError on an entity that does not exist yet
            raw[name] = None
    return core.parse_knobs(raw)


def _tariff():
    return {"energy_tax": _num("input_number.emhass_energy_tax_eur_kwh", 0.11),
            "supplier_fee": _num("input_number.emhass_supplier_fee_eur_kwh", 0.02),
            "btw_pct": _num("input_number.emhass_btw_pct", 21.0),
            "feedin_fee": _num("input_number.emhass_feedin_fee_eur_kwh", 0.0),
            "sun_bonus": _sun_bonus_on()}


def _sun_bonus_on():
    """The Zonnebonus checkbox, and this year's bonus kWh not used up. An
    unknown counter (before the first quarter after a restart) reads as not
    used up: the cap is far off for most of a year."""
    try:
        if state.get("input_boolean.emhass_zonnebonus") != "on":
            return False
    except Exception:          # pyscript raises NameError on an entity that does not exist yet
        return False
    return _num(ZONNEBONUS_SENSOR, 1.0) > 0.0


def _zonnebonus_publish(st):
    state.set(ZONNEBONUS_SENSOR, st["remaining_kwh"], new_attributes={
        "friendly_name": "EMHASS Zonnebonus remaining", "icon": "mdi:white-balance-sunny",
        "unit_of_measurement": "kWh", "state_class": "measurement",
        "used_kwh": round(st["used_kwh"], 3), "cap_kwh": st["cap_kwh"], "year": st.get("year"),
        "active_now": st["active_now"], "last_ts": st.get("last_ts")})


def _zonnebonus_step(seed_used_kwh=None, read_meter=True):
    kwh = _num(P1_EXPORT_TOTAL, None) if read_meter else None
    st = task.executor(core.sun_bonus_step_file, ZONNEBONUS, _now(), kwh, TZ_NAME, seed_used_kwh)
    _zonnebonus_publish(st)
    return st


@time_trigger("cron(0,15,30,45 * * * *)")
def _zonnebonus_cron():
    task.sleep(20)             # past the boundary, so the P1 counter has its new reading
    _zonnebonus_step()


@service(supports_response="optional")
def emhass_zonnebonus_seed(used_kwh):
    """Set this calendar year's bonus-eligible export (kWh), e.g. from a
    backfill; the counter then carries on from the meter's reading now."""
    st = _zonnebonus_step(seed_used_kwh=float(used_kwh))
    return {k: st.get(k) for k in ("year", "used_kwh", "remaining_kwh", "cap_kwh", "last_kwh", "last_ts")}


def _nordpool(day):
    try:
        r = service.call("nordpool", "get_prices_for_date", blocking=True, return_response=True,
                         config_entry=NP_ENTRY, date=day.isoformat())
        return list((r or {}).get(ENTITIES["nordpool_area"]) or [])
    except Exception as e:
        log.warning(f"emhass: nordpool {day} failed: {e}")
        return []


def _solcast_site(site, start, days=3):
    """The half-hourly forecast for ONE Solcast site, in the same row shape as
    the combined sensor's detailedForecast so pv_series parses it unchanged.

    The per-site breakdown reaches the sensor attributes only as DAILY totals
    (keys named after the Solcast resource ids); the half-hours exist nowhere but this service,
    and turning on the detailed site breakdown instead would roughly triple a
    detailedForecast that is already near the recorder's 16.384 B attribute
    cap. An empty list on failure stands the split down rather than guessing."""
    try:
        r = service.call("solcast_solar", "query_forecast_data", blocking=True, return_response=True,
                         start_date_time=start.isoformat(),
                         end_date_time=(start + timedelta(days=days)).isoformat(), site=site)
        return list((r or {}).get("data") or [])
    except Exception as e:
        log.warning(f"emhass: solcast site {site} failed: {e}")
        return []


def _last_run(action, ok, seconds, message, **extra):
    attrs = dict(friendly_name="EMHASS last run", icon="mdi:history", action=action,
                 status="ok" if ok else "error", seconds=seconds, message=str(message)[:250])
    attrs.update(extra)
    state.set("sensor.emhass_last_run", _now().isoformat(timespec="seconds"), new_attributes=attrs)


def _set_slice(entity_id, sl, name):
    if not sl:
        state.set(entity_id, "unknown", new_attributes={"friendly_name": name, "icon": "mdi:calendar-clock"})
        return
    attrs = dict(sl)
    attrs.update(friendly_name=name, icon="mdi:calendar-clock")
    state.set(entity_id, sl["date"], new_attributes=attrs)


def _load_history_days(today, n_days=None):
    """{date: W per 15-minute step} for the last n COMPLETE days before `today`,
    from the recorder's 5-minute means. Today itself is left out: its future
    steps would come back as the gap-hold of the last real value and would drag
    the wall-clock profile sideways. Returns {} when the fetch fails, which
    makes run_plan leave the load to EMHASS. Gappy days: core.load_history_days."""
    n_days = n_days or core.LOAD_REF_DAYS
    first = today - timedelta(days=n_days)
    a = datetime.combine(first, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    b = datetime.combine(today, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    try:
        stats = task.executor(statistics_during_period, hass, a, b, {ACTUAL_LOAD}, "5minute", None, {"mean"})
    except Exception as e:
        log.warning(f"emhass: load history fetch failed: {e}")
        return {}
    out, notes = task.executor(core.load_history_days, today, n_days, TZ_NAME, (stats or {}).get(ACTUAL_LOAD) or [])
    _log_notes(notes)
    return out


def _history_days(entity_id, today, n_days=None):
    """{date: W per 15-minute step} for the last n complete days of any power
    entity, from the recorder's 5-minute means (the load history's shape)."""
    n_days = n_days or core.LOAD_REF_DAYS
    first = today - timedelta(days=n_days)
    a = datetime.combine(first, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    b = datetime.combine(today, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    try:
        stats = task.executor(statistics_during_period, hass, a, b, {entity_id}, "5minute", None, {"mean"})
    except Exception as e:
        log.warning(f"emhass: history fetch failed for {entity_id}: {e}")
        return {}
    out, notes = task.executor(core.load_history_days, today, n_days, TZ_NAME, (stats or {}).get(entity_id) or [])
    _log_notes(notes)
    return out


def _ac_history_by_zone(today):
    """Each zone's AC circuit history, same shape as the load history. run_plan
    subtracts a zone's circuit only when the topology sends that zone."""
    out = {}
    for zone, e in AC_CIRCUITS.items():
        out[zone] = _history_days(e, today)
    return out


def _text(entity_id, default):
    try:
        v = state.get(entity_id)
        return default if v in (None, "unknown", "unavailable") else str(v)
    except Exception:
        return default


def _living_sched():
    return {"mode": _text("input_select.heating_mode", "auto"),
            "holiday_c": _num("input_number.heating_target_holiday", 11.0),
            "night_floor_c": 10.0,
            "slots": {s: _text(f"input_datetime.heating_slot_{s}_start", d)[:5]
                      for s, d in (("morning", "07:00"), ("day", "09:00"), ("evening", "17:00"), ("night", "23:00"))},
            "targets": {dt: {s: _num(f"input_number.heating_target_{dt}_{s}", 18.0) for s in ("morning", "day", "evening")}
                        for dt in ("weekday", "weekend")},
            "away": _text("sensor.heating_away", "false") == "true",
            "away_offset_k": _num("input_number.heating_target_presence_away_offset", -3.0)}


def _upstairs_sched(living):
    return {"day_c": _num("input_number.heating_upstairs_target_day", 19.0),
            "night_c": _num("input_number.heating_upstairs_target_night", 17.0),
            "slots": {"morning": living["slots"]["morning"], "night": living["slots"]["night"]},
            "mode": living["mode"], "holiday_c": living["holiday_c"],
            "away": living["away"], "away_offset_k": living["away_offset_k"]}


def _mean(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _forecast_points():
    """(ISO time, degrees C) points: the hourly forecast, then two points per
    later day from the optional daily forecast (the night low at 04:00 local,
    the day high at 14:00 local), so a horizon past the hourly span is covered."""
    fc = _attr(WEATHER_FC, "forecast") or []
    if not isinstance(fc, list):
        fc = []
    points = [(f.get("datetime"), f.get("temperature")) for f in fc
              if isinstance(f, dict) and f.get("datetime") and f.get("temperature") is not None]
    last_date = None
    try:
        last_date = datetime.fromisoformat(str(points[-1][0])).astimezone(TZ).date() if points else None
    except ValueError:
        pass
    if last_date is None:
        return points
    for d in _attr(WEATHER_FC, "forecast_daily") or []:
        try:
            day = datetime.fromisoformat(str(d.get("datetime"))).astimezone(TZ).date()
            low, high = d.get("templow"), d.get("temperature")
        except (ValueError, AttributeError):
            continue
        if day <= last_date or low is None or high is None:
            continue
        for hour, v in ((4, low), (14, high)):
            points.append((datetime.combine(day, datetime.min.time(), tzinfo=TZ).replace(hour=hour).isoformat(), v))
    return points


def _thermal_inp():
    """The heat topology's inputs (core.heat_topology), or disabled. A failure
    here (a broken thermal.json, a malformed forecast attribute) is logged and
    reads as disabled with the error: the battery solve runs without the topology."""
    if _text(THERMAL_ENABLED, "off") != "on":
        return {"enabled": False}
    try:
        return _thermal_inp_read()
    except Exception as e:
        log.warning(f"emhass: thermal inputs failed, solving without the topology: {e!r}")
        return {"enabled": False, "error": repr(e)}


def _thermal_inp_read():
    living = _living_sched()
    return {"enabled": True, "constants": core.load_thermal(),
            "band_k": _num("input_number.heating_bank_band_k", 3.0),
            "caps": {"hp_living_w": _num("input_number.heating_hp_living_cap_w", 1450.0),
                     "hp_multi_w": _num("input_number.heating_hp_multi_cap_w", 2200.0)},
            "startup_ct": {"hp": _num("input_number.heating_hp_startup_ct", 1.0),
                           "pellet": _num("input_number.heating_pellet_startup_ct", 10.0)},
            "pellet": {"available": _text("input_boolean.heating_pellet_available", "off") == "on",
                       "cost_ct_kwh": _num("input_number.heating_pellet_cost_per_kwh", 8.0)},
            "living": {"start_c": _num(ZONE_SENSORS["living"], None), "sched": living,
                       "ceiling_c": _num("input_number.heating_bank_ceiling_c", None)},
            "upstairs": {"enabled": _text("input_boolean.heating_upstairs_enabled", "off") == "on",
                         "start_c": _mean([_num(ZONE_SENSORS["bedroom"], None), _num(ZONE_SENSORS["study"], None)]),
                         "sched": _upstairs_sched(living)},
            "weather": {"points": _forecast_points(), "now_c": _num(OUTDOOR_NOW, None)}}


THERMAL_BRIEF = ("sent", "reason", "fallback", "clamped", "upstairs", "living_ceiling_c")


def _thermal_brief(th):
    """The thermal summary for the log line: never the outdoor array."""
    if not isinstance(th, dict):
        return th
    return {k: th.get(k) for k in THERMAL_BRIEF if k in th}


_PLAN_BUSY = {"since": None}       # set while emhass_plan_day is solving; the A/B waits on it


@service(supports_response="optional")
def emhass_plan_day(dry_run=False):
    """Plan from now to 24:00 of D+1 (D+2 when Solcast day 3 is in) with EMHASS, archive, roll the day slices, publish.
    dry_run: solve only and return the summary (used by ha/scripts/check.sh run)."""
    task.unique("emhass_plan_day")
    _PLAN_BUSY["since"] = _now()
    try:
        return _plan_day(dry_run)
    finally:
        _PLAN_BUSY["since"] = None


def _plan_day(dry_run=False):
    now = _now()
    if now.minute % 15 == 0 and now.second < 2:      # never race EMHASS across a step boundary
        task.sleep(2)
        now = _now()
    today = now.date()
    t0, _n = core.horizon(now, TZ_NAME)
    # Measured series first, because soc_init depends on them: the solve starts
    # from the SETTLED SOC (closing the chain, 2026-09-06), or from the real
    # pack in live mode. core.plan_soc decides, with the archive reads.
    act = _actuals_15min(today, core._slot(now))
    rec = _day_record(today, act)
    settled = task.executor(core.virtual_day, ARCHIVE, today, TZ_NAME, now, rec)
    src = task.executor(core.plan_soc, ARCHIVE, t0, (settled or {}).get("soc_now_pct"), _num(ACTUAL_SOC, None),
                        _writer_live(), ACTUAL_SOC)
    _log_notes(src["notes"])
    if src["error"]:
        _last_run("plan", False, 0, src["error"])
        return {"ok": False, "message": src["error"]}
    soc, soc_source = src["soc_pct"], src["soc_source"]
    inp = {"now": now.isoformat(), "tz": TZ_NAME, "base_url": BASE, "archive_dir": ARCHIVE, "dry_run": bool(dry_run),
           "np_today": _nordpool(today), "np_tomorrow": _nordpool(today + timedelta(days=1)),
           "epex": _attr(ENTITIES["epex"], "forecast") or [],
           "solcast_today": _attr(ENTITIES["solcast_today"], "detailedForecast") or [],
           "solcast_tomorrow": _attr(ENTITIES["solcast_tomorrow"], "detailedForecast") or [],
           "solcast_day3": _attr(ENTITIES["solcast_day3"], "detailedForecast") or [],
           "soc_pct": soc, "soc_source": soc_source, "tariff": _tariff()}
    # The curtailable half only. pv_series merges the three ESE lists, so the
    # whole span goes in one of them.
    inp["solcast_ese_today"] = _solcast_site(
        SOLCAST_ESE_SITE, now.replace(hour=0, minute=0, second=0, microsecond=0))
    # The live knob layer (core.LIVE_KNOBS): growatt share, P50:P10 mix, stress
    # scale, SOC costs, terminal and intermediate SOC targets, the cut thresholds.
    # Unreadable helpers fall back to the core constants.
    _pack_temp_latch()
    inp["knobs"] = _knobs()
    # The rebalancing clock (rebalance.py): today's settled quarters of the
    # inverter SOC are folded into /config/emhass/rebalance.json every tick; a
    # quarter at or above REBALANCE_FULL_LEVEL is time at full, and the clock is
    # the age of the newest REBALANCE_BUDGET_H (one quarter) of it, so reaching
    # 100 % resets it (2026-10-10; the bank voltage 2026-10-03..10-10).
    # No state on record reads as overdue.
    inp["rebalance"] = task.executor(core.rebalance_step, REBALANCE, (settled or {}).get("slice_start"),
                                     act.get("soc_pct"), int((settled or {}).get("n_past") or 0),
                                     core.REBALANCE_FULL_LEVEL * 100.0, inp["knobs"].get("rebalance_dwell_h"), now)
    _publish_rebalance(inp["rebalance"], now)
    # The running day's hours for the period charts (2026-09-17): the settled
    # slice's past steps, written to the hourly sidecar and imported now, so
    # the shadow lanes reach `now` like the hero graphs; the ladder's nightly
    # row replaces them. Skipped on a dry run.
    if settled and not dry_run:
        _write_today_hours(settled, today)
    # Today's record, fetched above because soc_init depends on it, also feeds
    # the display slice, so the chart shows what the virtual pack ran against
    # rather than what the plan forecast. The curtailment mask rides along:
    # virtual_day substitutes POTENTIAL PV over the past, the same basis the
    # ladder's replay lane settles on, so the charts and the ledger cannot
    # disagree on a curtailed day; the must-take half, so the repair scales the
    # strings and not the Growatt. run_plan takes them as flat inp keys.
    inp.update(rec.to_inp("actual_"))
    # Yesterday too, for the display slice that fills the charts' 24 h look-back
    # (2026-09-04). Every step of it is in the past, so it is measured PV
    # and load throughout; the recorder's 5-minute window reaches back ~10 days.
    yday = today - timedelta(days=1)
    prev = _actuals_15min(yday)
    inp.update(_day_record(yday, prev, _day_was_live(yday)).to_inp("prev_"))
    # Own same-clock load shape, unless the ML switch is on: passing
    # load_power_forecast flips EMHASS to load_forecast_method 'list', which
    # would silently override the mlforecaster the switch exists to enable.
    if state.get("input_boolean.emhass_ml_enabled") != "on":
        inp["load_days"] = _load_history_days(today)
        inp["load_now_w"] = _num(ACTUAL_LOAD, None)
    # The heat topology and each zone's AC circuit history (subtracted from the
    # load profile only for the zones the topology actually sends; planning decides).
    inp["thermal"] = _thermal_inp()
    if inp["thermal"].get("enabled") and "load_days" in inp:
        inp["ac_days_by_zone"] = _ac_history_by_zone(today)
        inp["ac_now_w_by_zone"] = {zone: _num(e, 0.0) for zone, e in AC_CIRCUITS.items()}
    res = task.executor(core.run_plan, inp)
    keys = ("ok", "message", "t0", "n", "n_predicted_steps", "pv_gap_steps", "optim_status", "cost_eur",
            "seconds", "archive_path", "prices_predicted", "soc_source", "load_source", "load_ref_days",
            "pv_split", "pv_micro_kwh", "pv_ese_gap_steps", "aux_cut_active", "aux_cut_ratio",
            "aux_cut_curtail_kwh", "aux_cut_aux_kwh", "aux_cut_steps",
            "pv_p10_mix", "pv_p50_kwh", "pv_mixed_kwh", "knobs", "days_since_full", "thermal", "load_excluded_w")
    summary = {k: res.get(k) for k in keys}
    if not res.get("ok"):
        log.error(f"emhass: plan failed: {res.get('message')}")
        _last_run("plan-dry-run" if dry_run else "plan", False, res.get("seconds", 0), res.get("message"),
                  horizon_start=res.get("t0"), horizon_steps=res.get("n"),
                  n_predicted_steps=res.get("n_predicted_steps"), optim_status=res.get("optim_status"),
                  soc_source=soc_source, thermal=res.get("thermal"))
        return summary
    if not dry_run:
        _set_slice("sensor.emhass_plan_yesterday", res.get("yesterday"), "EMHASS plan yesterday")
        _set_slice("sensor.emhass_plan_today", res.get("today"), "EMHASS plan today")
        _set_slice("sensor.emhass_plan_next_day", res.get("next_day"), "EMHASS plan next day")
        _set_slice("sensor.emhass_plan_day_after", res.get("day_after"), "EMHASS plan day after")
        pub = task.executor(core.publish, BASE)
        summary["published"] = pub.get("ok")
        if not pub.get("ok"):
            log.warning(f"emhass: publish-data failed: {pub}")
    _last_run("plan-dry-run" if dry_run else "plan", True, res["seconds"], "ok",
              horizon_start=res["t0"], horizon_steps=res["n"], n_predicted_steps=res["n_predicted_steps"],
              pv_gap_steps=res["pv_gap_steps"], optim_status=res["optim_status"], cost_eur=res["cost_eur"],
              soc_source=soc_source, load_source=res.get("load_source"),
              load_ref_days=res.get("load_ref_days"), aux_cut_active=res.get("aux_cut_active"),
              aux_cut_ratio=res.get("aux_cut_ratio"), thermal=res.get("thermal"))
    log.info(f"emhass: plan ok {dict(summary, thermal=_thermal_brief(summary.get('thermal')))}")
    return summary


def _actuals_15min(day, gap_upto=None):
    """The day's measured series as W per 15-minute step, from the recorder's
    5-minute means: {pv_w, load_w} feed the virtual day and the display slices,
    {pv_w, grid_w, batt_dc_w} the ladder's replay. Any series with more than MAX_PV_GAPS
    held steps is left out (core.day_actuals). A day the curtailment sensor does
    not cover takes the fallback mask. The recorder keeps 5-minute statistics
    ~10 days, plenty for scoring yesterday."""
    a = datetime.combine(day, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    b = a + timedelta(days=2)                    # cover DST long days too
    try:
        stats = task.executor(statistics_during_period, hass, a, b, set(DAY_SERIES.values()), "5minute", None,
                              {"mean", "max"})
    except Exception as e:
        log.warning(f"emhass: actuals statistics fetch failed: {e}")
        return {}
    got, notes = task.executor(core.day_actuals, day, TZ_NAME, stats, DAY_SERIES, gap_upto)
    _log_notes(notes)
    if got.get("curtailed") is None and got.get("soc_pct") is not None:
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
        got["curtailed"] = _fallback_mask(midnight, len(got["soc_pct"]), got["soc_pct"])
    return got


def _export_off(t0, n):
    """1,0 per step where switch.inverter_export_surplus was OFF at the step start
    (core.export_off). The switch keeps no statistics, only states, so this reads
    its history rather than the recorder's 5-minute rolls. None when nothing is
    recorded for the window."""
    a = t0.astimezone(timezone.utc)
    b = a + timedelta(minutes=core.STEP_MIN * n)
    try:
        hist = task.executor(state_changes_during_period, hass, a, b, ACTUAL_EXPORT_SW,
                             True, False, None, True)
    except Exception as e:
        log.warning(f"emhass: export-switch history fetch failed: {e}")
        return None
    # A list comprehension, not a generator expression: pyscript's AST walker
    # raises NotImplementedError on ast_generatorexp (see reference_pyscript_patterns).
    pts = [(st.last_changed.timestamp(), st.state) for st in (hist or {}).get(ACTUAL_EXPORT_SW, [])]
    return task.executor(core.export_off, t0, n, pts)


def _fallback_mask(t0, n, soc_pct):
    """Curtailment mask for a window that predates sensor.pv_curtailment_active:
    both halves of the site's rule on the export switch's history, SOC alone when
    the switch has no recorded state (core.fallback_mask)."""
    off = _export_off(t0, n)
    if off is None:
        log.warning(f"emhass: no export-switch history at {t0}, curtailment mask is SOC-only")
    return task.executor(core.fallback_mask, soc_pct, off, n)


def _window_actuals(t0, n):
    """{pv_w, grid_w, batt_dc_w, ...} as W per step over n steps from t0, crossing
    whatever calendar days the plan's horizon spans (core.window_actuals). None
    when a required series is too gappy; a gappy curtailment series takes the
    fallback mask."""
    a = t0.astimezone(timezone.utc)
    b = a + timedelta(minutes=core.STEP_MIN * n)
    try:
        stats = task.executor(statistics_during_period, hass, a, b, set(WINDOW_SERIES.values()), "5minute", None,
                              {"mean", "max"})
    except Exception as e:
        log.warning(f"emhass: hindsight window fetch failed: {e}")
        return None
    got, notes = task.executor(core.window_actuals, t0, n, stats, WINDOW_SERIES)
    _log_notes(notes)
    if got is not None and got.get("curtailed") is None:
        got["curtailed"] = _fallback_mask(t0, n, got["soc_pct"])
    return got


def _ladder_inputs(day):
    """The day's actuals plus the two omniscient windows, each None until its
    horizon is fully in the past (the recorder cannot have it before then, and
    the core reports the rung as pending rather than warning about gaps)."""
    act = _actuals_15min(day)
    # A day the writer drove is the real pack in the ledger (2026-09-27): the
    # actual rung takes the live anchor and the measured lanes.
    rec = _day_record(day, act, _day_was_live(day))
    marks = {"anchor": rec.soc_anchor, "measured": rec.measured}
    w = task.executor(core.ladder_windows, ARCHIVE, day.isoformat(), TZ_NAME)
    if not w:
        return {"day": act, "win1": None, "win2": None, **marks}
    t0, now = datetime.fromisoformat(w["t0"]), _now()
    out = {"day": act, **marks}
    for key, n in (("win1", w["n1"]), ("win2", w["n2"])):
        end = t0 + timedelta(minutes=core.STEP_MIN * n)
        out[key] = _window_actuals(t0, n) if end <= now else None
    return out


def _ladder_walk(days, rungs=None, reprice=None):
    """Run the ladder over `days` (date objects, any order) and re-import
    emhass:ladder_earned_eur. Returns the core's result; the caller re-plans
    when it reports posted (the solves overwrite the add-on's opt_res_latest).
    sensor.emhass_ladder, the ladder card's copy of the walk, was retired with
    the card on 2026-09-27; the new ledger reads the statistic."""
    days = sorted(days)
    inputs = {}
    for d in days:
        inputs[d.isoformat()] = _ladder_inputs(d)
    np_rows, d = [], days[0] - timedelta(days=1)
    while d <= days[-1] + timedelta(days=3):
        np_rows += _nordpool(d)
        d += timedelta(days=1)
    rungs = tuple(rungs) if rungs else core.RUNGS
    res = task.executor(core.ladder_run, ARCHIVE, LADDER, BASE, [x.isoformat() for x in days], TZ_NAME, np_rows,
                        inputs, rungs, core.CAPACITY_KWH, _num("input_number.emhass_lambda_frac", 0.9), 180,
                        reprice)
    _import_ladder_earned()
    return res


LADDER_EARNED = "emhass:ladder_earned_eur"


def _import_ladder_earned():
    """emhass:ladder_earned_eur: the actual lane as EUR EARNED (positive = money
    in, the dashboard's sign, negated once in the core) for the period cards:
    hour rows from ladder_hours.csv where the live archive replayed the day,
    the day's value at 00:00 for the imported history. Re-imported whole each
    time, like the emhass:* lanes, so a re-scored day keeps every later sum."""
    rows = task.executor(core.ladder_earned_rows, LADDER, TZ_NAME)
    if not rows:
        return
    data = [dict(r, start=datetime.fromisoformat(r["start"])) for r in rows]
    meta = {"mean_type": StatisticMeanType.NONE, "has_sum": True, "name": "EMHASS ladder earned (what we ran)",
            "source": "emhass", "statistic_id": LADDER_EARNED, "unit_class": None, "unit_of_measurement": "EUR"}
    async_add_external_statistics(hass, meta, data)
    _import_ladder_shadow()


# The shadow lanes as recorder statistics (2026-09-17): the settled virtual
# pack per hour, replayed from the archive like the cash, so the period charts
# can scroll the shadow's own history instead of the emhass_da_* entities
# (whose recorder history the nightly ladder solves pollute between 00:00 and
# ~05:00: the add-on publishes the past-day solve until the re-plan). Means,
# split by direction so a day or month mean stays a magnitude. The tariff
# pair reaches into the deep past through tariff_hours.csv.
SHADOW_STATS = (("charge_kw", "emhass:shadow_charge_kw", "kW", "EMHASS shadow charge"),
                ("discharge_kw", "emhass:shadow_discharge_kw", "kW", "EMHASS shadow discharge"),
                ("import_kw", "emhass:shadow_import_kw", "kW", "EMHASS shadow import"),
                ("export_kw", "emhass:shadow_export_kw", "kW", "EMHASS shadow export"),
                ("soc_pct", "emhass:shadow_soc_pct", "%", "EMHASS shadow SOC"),
                ("pv_kw", "emhass:shadow_pv_kw", "kW", "EMHASS shadow PV harvest"),
                ("load_kw", "emhass:shadow_load_kw", "kW", "EMHASS shadow load"),
                ("buy_ct", "emhass:tariff_buy_ct", "ct/kWh", "EMHASS tariff buy"),
                ("sell_ct", "emhass:tariff_sell_ct", "ct/kWh", "EMHASS tariff sell"))


def _write_today_hours(settled, day):
    try:
        hours, lanes = task.executor(core.today_hours, settled, day, TZ_NAME)
        if hours:
            task.executor(core.upsert_ladder_hours, core.hours_path(LADDER), day.isoformat(), hours, lanes)
            _import_ladder_earned()
    except Exception as e:
        log.warning(f"emhass: today's hours not written: {e}")


@service(supports_response="optional")
def emhass_today_hours():
    """Write the running day's settled hours (cash and the shadow lanes) to the
    sidecar and re-import the statistics, without a plan run."""
    now = _now()
    today = now.date()
    act = _actuals_15min(today, core._slot(now))
    settled = task.executor(core.virtual_day, ARCHIVE, today, TZ_NAME, now, _day_record(today, act))
    if not settled:
        return {"status": "no_chain"}
    _write_today_hours(settled, today)
    return {"status": "ok", "n_past": settled.get("n_past")}


def _import_ladder_shadow():
    series = task.executor(core.ladder_shadow_rows, LADDER)
    for key, sid, unit, name in SHADOW_STATS:
        rows = series.get(key) or []
        if not rows:
            continue
        data = [dict(r, start=datetime.fromisoformat(r["start"])) for r in rows]
        meta = {"mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": name, "source": "emhass",
                "statistic_id": sid, "unit_class": None, "unit_of_measurement": unit}
        async_add_external_statistics(hass, meta, data)


# 2026-09-24: "remove omni, hindsight etc from the comparisons, our EMS is
# good enough as is". The nightly walk solves the ACTUAL lane only, which is
# what the ledger and emhass:ladder_earned_eur read. The other rungs keep their
# columns and their already-settled history in ladder.csv (the schema derives
# from core.RUNGS, so shrinking that would delete them); they simply stop being
# solved, which also cuts the nightly solve count by four fifths. Pass rungs
# explicitly to pyscript.emhass_ladder to walk one again.
NIGHTLY_RUNGS = ["actual"]


def _ladder_default_days(lookback=4):
    """Yesterday and the days before it whose omniscient rungs may only now have
    their windows measured; every rung that is already ok is skipped by the core."""
    y = _now().date() - timedelta(days=1)
    return [y - timedelta(days=k) for k in range(lookback)]


@service(supports_response="optional")
def emhass_ladder_nightly():
    """The 00:10 walk: the actual rung over yesterday and the three days before
    it, then emhass:ladder_earned_eur. Re-plans when a solve reached the
    add-on (the solves overwrite its opt_res_latest). Took over the nightly
    ladder from emhass_score_day on 2026-09-27."""
    t = _now()
    days = _ladder_default_days()
    try:
        lad = _ladder_walk(days, NIGHTLY_RUNGS)
    except Exception as e:
        log.warning(f"emhass: nightly ladder failed: {e}")
        _last_run("ladder", False, round((_now() - t).total_seconds(), 1), f"nightly: {e}")
        return {"ok": False, "message": str(e)}
    seconds = round((_now() - t).total_seconds(), 1)
    d7 = (lad.get("summary") or {}).get("windows", {}).get("d7")
    log.info(f"emhass: nightly ladder: {lad['solves']} solves, d7 {d7}")
    _last_run("ladder", True, seconds, f"nightly {days[-1]}..{days[0]}: {lad['solves']} solves")
    if lad.get("posted"):
        emhass_plan_day()
    return {"ok": True, "days": [d.isoformat() for d in sorted(days)], "solves": lad["solves"], "seconds": seconds}


@service(supports_response="optional")
def emhass_ladder(start=None, end=None, rungs=None, reprice=False):
    """Walk the ladder (actual, hindsight, omni2 + the two nowcast halves) over [start, end], end
    default yesterday, start default four days before it. `rungs` is a comma
    list to run a subset. Rungs already ok on a day are not re-solved; pass a
    fresh CSV to rebuild. Shares the plan lock: run it off the :13/:28/:43/:58 minutes."""
    task.unique("emhass_plan_day")
    e = datetime.fromisoformat(str(end)).date() if end else (_now().date() - timedelta(days=1))
    s = datetime.fromisoformat(str(start)).date() if start else e - timedelta(days=3)
    days = [s + timedelta(days=k) for k in range((e - s).days + 1)]
    rung_list = [r.strip() for r in str(rungs).split(",") if r.strip()] if rungs else None
    # reprice: settle the actual lane at the LIVE tariff instead of the prices
    # the plans in force carried. The ledger is a shadow EMS, so it reads at
    # the settings we believe are correct; leave it off to keep a day priced
    # as it was solved. Days already ok are skipped, so trim the CSV first.
    tf = _tariff() if str(reprice).lower() in ("1", "true", "yes", "on") else None
    t = _now()
    res = _ladder_walk(days, rung_list, tf)
    seconds = round((_now() - t).total_seconds(), 1)
    w = (res.get("summary") or {}).get("windows", {}).get("all", {})
    _last_run("ladder", True, seconds, f"{s}..{e}: {res['solves']} solves, n {w.get('n')} "
              f"actual {w.get('actual')} hindsight {w.get('hindsight')} omni2 {w.get('omni2')}")
    if res.get("posted"):
        emhass_plan_day()          # the ladder's solves overwrote opt_res_latest; restore the live horizon
    return {"days": [d.isoformat() for d in days], "rungs": list(rung_list or core.RUNGS), "solves": res["solves"],
            "seconds": seconds, "summary": res["summary"], "rows": res["rows"]}


@service(supports_response="optional")
def emhass_ladder_hours(start=None, end=None):
    """Backfill the ladder's hourly sidecar over [start, end] (end default
    yesterday, start default nine days before it: the recorder's 5-minute
    actuals reach about ten days back) and re-import emhass:ladder_earned_eur.
    Replays the settled actual lane only; no solves, ladder.csv untouched."""
    e = datetime.fromisoformat(str(end)).date() if end else (_now().date() - timedelta(days=1))
    s = datetime.fromisoformat(str(start)).date() if start else e - timedelta(days=9)
    days = [s + timedelta(days=k) for k in range((e - s).days + 1)]
    inputs = {d.isoformat(): {"day": _actuals_15min(d)} for d in days}
    res = task.executor(core.ladder_hours_fill, ARCHIVE, LADDER, [d.isoformat() for d in days], TZ_NAME, inputs,
                        core.CAPACITY_KWH, _num("input_number.emhass_lambda_frac", 0.9))
    _import_ladder_earned()
    ok = [d for d, r in res.items() if r.get("status") == "ok"]
    log.info(f"emhass: ladder hours filled for {len(ok)} of {len(days)} days")
    return {"days": res, "filled": len(ok)}


def _off_refresh_minute():
    """Never post ad-hoc solves while a plan may be solving: the add-on has one
    opt_res_latest, and a solve landing between production's POST and its GET
    would hand it the wrong plan (or fail it as misaligned). Two sources of a
    plan: the :13/:28/:43/:58 refresh (:13/:43 until 2026-09-27, :05/:35 until 2026-09-08), and the startup
    rehydrate 120 s after a pyscript reload when the add-on holds a stray solve
    (it re-plans; on 2026-09-07 that re-plan killed the first live A/B run
    through the shared lock). So: step off the refresh minutes, then wait while
    a plan is actually running."""
    wait = core.refresh_wait_s(_now().minute)
    if wait:
        task.sleep(wait)
    waited = 0
    while _PLAN_BUSY["since"] is not None and waited < 300:
        task.sleep(5)
        waited += 5


def _ab_store(label, payload):
    return task.executor(core.ab_store, AB_DIR, label, payload)      # off the event loop


def _ab_load(label):
    return task.executor(core.ab_load, AB_DIR, label)


@service(supports_response="optional")
def emhass_ab(start, end=None, label="ab", overrides=None, from_live=False, cadence_min=30,
              settle="closed", baseline=True):
    """A/B on past days: every archived solve of each day re-run under `overrides`
    (knob names from core.LIVE_KNOBS, or raw EMHASS runtime keys), each starting
    from the SOC the settled walk had reached, each step settled against the real
    day through the virtual Deye. from_live=True seeds the overrides from the
    current helpers ("what would today's settings have done"). The baseline is the
    same walk on the archived settings. Never touches the plan archive;
    results go to /config/emhass/ab/<label>.json.gz and
    sensor.emhass_ab_last, and the live horizon is re-planned afterwards."""
    _off_refresh_minute()                                  # BEFORE taking the lock, so a running plan is never killed
    task.unique("emhass_plan_day")
    ov = dict(overrides or {})
    if from_live:
        # A measured input (a sensor.* knob, pack_temp_used) is not a setting:
        # every replayed solve keeps its own archived value.
        live = {k: v for k, v in _knobs().items()
                if v is not None and not core.LIVE_KNOBS[k][1].startswith("sensor.")}
        live.update(ov)
        ov = live
    bad = core.ab_validate(ov)
    if bad:
        _last_run("ab", False, 0, f"unknown override keys: {', '.join(bad)}")
        return {"ok": False, "message": f"unknown override keys: {', '.join(bad)}"}
    d = datetime.fromisoformat(str(start)).date()
    last = datetime.fromisoformat(str(end)).date() if end else d
    days, base_days, t_all = [], [], _now()
    while d <= last:
        act = _actuals_15min(d)
        r = task.executor(core.ab_walk, ARCHIVE, BASE, d.isoformat(), TZ_NAME, ov, act, int(cadence_min), str(settle),
                          core.CAPACITY_KWH)
        days.append(r)
        if baseline:
            b = task.executor(core.ab_walk, ARCHIVE, BASE, d.isoformat(), TZ_NAME, {}, act, int(cadence_min),
                              str(settle), core.CAPACITY_KWH)
            base_days.append(b)
        log.info(f"emhass: ab {label} {d}: {r['status']} " + (str(r.get("summary")) if r["status"] == "ok" else str(r.get("message"))))
        d += timedelta(days=1)
    head = {"label": label, "start": str(start), "end": last.isoformat(), "overrides": ov, "from_live": bool(from_live),
            "cadence_min": int(cadence_min), "settle": str(settle), "ran_at": _now().isoformat(timespec="seconds"),
            "seconds": round((_now() - t_all).total_seconds(), 1)}
    result, slim = task.executor(core.ab_result, head, days, base_days, bool(baseline))
    path = _ab_store(label, result)
    slim.update(friendly_name="EMHASS A/B last run", icon="mdi:ab-testing", path=path)
    state.set("sensor.emhass_ab_last", label, new_attributes=slim)
    tot = result["totals"]
    _last_run("ab", tot["days_ok"] == len(days), result["seconds"],
              f"{label} {start}..{last}: {tot['days_ok']}/{len(days)} days ok")
    emhass_plan_day()                                     # the add-on's latest result is a past-day solve: restore the live horizon
    return slim


@service(supports_response="optional")
def emhass_ab_apply(label=None):
    """Push a stored A/B variant's overrides into the live helpers. Default: the
    label of the last run. Raw runtime keys and diagnostics are reported as
    skipped; the next :13/:28/:43/:58 solve runs on the new values."""
    task.unique("emhass_plan_day")
    label = label or state.get("sensor.emhass_ab_last")
    stored = _ab_load(label) if label not in (None, "unknown", "unavailable") else None
    if not stored:
        _last_run("ab-apply", False, 0, f"no stored A/B run {label!r}")
        return {"ok": False, "message": f"no stored A/B run {label!r}"}
    applied, skipped = core.ab_apply_plan(stored.get("overrides") or {})
    before = {eid: state.get(eid) for eid in applied}
    for eid, v in applied.items():
        if eid.startswith("input_datetime."):
            service.call("input_datetime", "set_datetime", entity_id=eid, time=f"{str(v)[:5]}:00")
        elif eid.startswith("input_boolean."):
            service.call("input_boolean", "turn_on" if float(v) > 0.5 else "turn_off", entity_id=eid)
        else:
            service.call("input_number", "set_value", entity_id=eid, value=float(v))
    log.warning(f"emhass: A/B {label!r} pushed live: {applied} (before {before}); skipped {skipped}")
    _last_run("ab-apply", True, 0, f"{label}: {len(applied)} knobs applied, {len(skipped)} skipped")
    return {"ok": True, "label": label, "applied": applied, "before": before, "skipped": skipped}


def _ml_state(entity_id, name, res):
    attrs = dict(friendly_name=name, icon="mdi:brain", status="ok" if res["ok"] else "error",
                 seconds=res["seconds"], message=res["message"], last_run=res.get("last_run"))
    state.set(entity_id, _now().isoformat(timespec="seconds"), new_attributes=attrs)


@service
def emhass_fit():
    """forecast-model-fit on the last 13 days of sensor.inverter_load_ups_power, with backtest."""
    task.unique("emhass_ml")
    payload = dict(ML)
    payload.update(historic_days_to_retrieve=13, perform_backtest=True)
    res = task.executor(core.ml_action, BASE, "forecast-model-fit", payload, 1800)
    _ml_state("sensor.emhass_last_fit", "EMHASS last fit", res)
    _last_run("fit", res["ok"], res["seconds"], res["message"])


@service
def emhass_tune():
    """forecast-model-tune (10 trials) on the same data; Sundays only, from the ML switch."""
    task.unique("emhass_ml")
    payload = dict(ML)
    payload.update(historic_days_to_retrieve=13, n_trials=10)
    res = task.executor(core.ml_action, BASE, "forecast-model-tune", payload, 3600)
    _ml_state("sensor.emhass_last_tune", "EMHASS last tune", res)
    _last_run("tune", res["ok"], res["seconds"], res["message"])


@service
def emhass_health():
    """healthz (last run younger than 26 h) plus live config against ha/config.json."""
    res = task.executor(core.health, BASE, REPO_CFG, 93600)
    state.set("binary_sensor.emhass_addon_healthy", "on" if res["ok"] else "off",
              new_attributes=dict(friendly_name="EMHASS add-on healthy", icon="mdi:heart-pulse",
                                  healthz=res.get("healthz"), config_drift=res.get("drift"),
                                  last_run_age_h=res.get("last_run_age_h"),
                                  checked=_now().isoformat(timespec="seconds")))


@service
def emhass_rehydrate():
    """After a restart: rebuild the rolled slices from the archive, republish the
    add-on's last plan if it is still fresh, run health."""
    _act = _actuals_15min(_now().date(), core._slot(_now()))
    _prev = _actuals_15min(_now().date() - timedelta(days=1))
    yday = _now().date() - timedelta(days=1)
    res = task.executor(core.rehydrate, ARCHIVE, TZ_NAME, _now().isoformat(),
                        _num("input_number.emhass_plan_stale_hours", 26.0),
                        _day_record(_now().date(), _act), _day_record(yday, _prev, _day_was_live(yday)))
    _set_slice("sensor.emhass_plan_yesterday", res.get("yesterday"), "EMHASS plan yesterday")
    _set_slice("sensor.emhass_plan_today", res.get("today"), "EMHASS plan today")
    _set_slice("sensor.emhass_plan_next_day", res.get("next_day"), "EMHASS plan next day")
    _set_slice("sensor.emhass_plan_day_after", res.get("day_after"), "EMHASS plan day after")
    if res.get("plan_fresh"):
        # Fresh is a statement about the archive. Before asking the add-on to
        # republish, check it still holds THAT solve: anything that solved since
        # (a harness run, a hindsight or replay whose re-plan never came) would
        # be served as the live plan otherwise (2026-09-07, seven minutes of a
        # midnight-anchored sun on every day-3 lane). On a mismatch the live
        # plan is one 3 s solve away, so take it rather than serve a stranger.
        st, lr = task.executor(core.emhass_get, BASE, "/api/v1/last-run")
        if core.addon_holds_plan(res.get("newest_last_run"), lr if st == 200 else None):
            pub = task.executor(core.publish, BASE)
            log.info(f"emhass: republished last plan after restart: {pub}")
        else:
            log.warning("emhass: the add-on's last solve is not the plan of record "
                        f"(add-on {str(lr)[:120]}); re-planning instead of republishing")
            emhass_plan_day()
    emhass_health()


@time_trigger("startup")
def _emhass_startup():
    _zonnebonus_step(read_meter=False)     # republish the counter; the next quarter steps it
    task.sleep(120)            # let the add-on, recorder and Nord Pool settle after a restart
    emhass_rehydrate()


@time_trigger("cron(*/15 * * * *)")
def _emhass_health_cron():
    emhass_health()
