"""EMHASS shadow planner: thin pyscript wrapper.

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


def _now():
    return datetime.now(TZ)


def _num(entity_id, default):
    try:
        return float(state.get(entity_id))
    except Exception:
        return default


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


def _measured(act, prefix="actual_", live=None):
    """THE VIRTUAL PACK IS RETIRED IN LIVE MODE (2026-09-26): the settled
    chain's past steps take the measured pack power (DC, + discharge), the
    fiscal meter (W, + import) and the SOC from the recorder. Outside live
    mode, or without the series, {} and the simulation stands. With
    prefix="prev_" the same three lanes for yesterday's slice; `live`
    overrides the mode-now test for a past day."""
    if not (_writer_live() if live is None else live) or not act:
        return {}
    batt, soc, grid = act.get("batt_dc_w"), act.get("soc_pct"), act.get("grid_w")   # grid_w: _actuals_15min's P1 net, W
    if batt is None or soc is None or grid is None:
        return {}
    return {prefix + "batt_w": batt, prefix + "grid_w": grid, prefix + "soc_pct": soc}


def _yesterday_lanes(prev, yday):
    """Yesterday's anchor and measured lanes for the display slice, so a live
    day keeps the real pack after the midnight rollover (2026-09-27: the first
    live day went back to the virtual pack at 00:13). The anchor is the live
    switch step when the writer went live that day, else yesterday's real
    midnight SOC."""
    live = _day_was_live(yday)
    out = dict(_measured(prev, prefix="prev_", live=live))
    anchor = _soc_anchor(yday, prev, live=live)
    if anchor is not None:
        out["prev_soc_anchor"] = anchor
    return out


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
    """The live knob layer: every helper in core.LIVE_KNOBS, None when unreadable
    so the core falls back to its constant. input_datetime reads as 'HH:MM:SS'."""
    out = {}
    for name, (default, eid) in core.LIVE_KNOBS.items():
        try:
            raw = state.get(eid)
        except Exception:              # pyscript raises NameError on an entity that does not exist yet
            raw = None
        if raw in (None, "unknown", "unavailable", ""):
            out[name] = None
        elif eid.startswith("input_datetime."):
            out[name] = str(raw)[:5]
        elif eid.startswith("input_boolean."):
            out[name] = 1.0 if raw == "on" else 0.0
        else:
            try:
                out[name] = float(raw)
            except Exception:
                out[name] = None
    return out


def _tariff():
    return {"energy_tax": _num("input_number.emhass_energy_tax_eur_kwh", 0.11),
            "supplier_fee": _num("input_number.emhass_supplier_fee_eur_kwh", 0.02),
            "btw_pct": _num("input_number.emhass_btw_pct", 21.0),
            "feedin_fee": _num("input_number.emhass_feedin_fee_eur_kwh", 0.0)}


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
    makes run_plan leave the load to EMHASS."""
    n_days = n_days or core.LOAD_REF_DAYS
    first = today - timedelta(days=n_days)
    a = datetime.combine(first, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    b = datetime.combine(today, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    try:
        stats = task.executor(statistics_during_period, hass, a, b, {ACTUAL_LOAD}, "5minute", None, {"mean"})
    except Exception as e:
        log.warning(f"emhass: load history fetch failed: {e}")
        return {}
    rows = (stats or {}).get(ACTUAL_LOAD) or []
    out = {}
    for k in range(n_days):
        day = first + timedelta(days=k)
        vals, gaps = task.executor(core.fifteen_min_series, day, TZ_NAME, rows)
        if gaps <= core.MAX_PV_GAPS:
            out[day] = vals
        else:
            log.debug(f"emhass: load history skips {day}: {gaps} gap steps")
    return out


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
    # Measured series first, because soc_init now depends on them. CLOSING THE
    # CHAIN (2026-09-06): the next solve starts from the SETTLED SOC - what the
    # pack would actually be holding after the plan's own commands met the real
    # sun and load - and not from the previous plan's SOC_opt, which is what it
    # PREDICTED. Chaining off the prediction is why an hourly re-solve never
    # noticed the pack falling behind: it was re-solving a fiction, correctly.
    act = _actuals_15min(today, core._slot(now))
    anchor = _soc_anchor(today, act)
    measured = _measured(act)
    settled = task.executor(core.virtual_day, ARCHIVE, today, TZ_NAME, now,
                            act.get("pv_w"), act.get("load_w"), act.get("curtailed"),
                            act.get("pv_peak_w"), act.get("micro_w"), soc_anchor=anchor, **measured)
    s_now = (settled or {}).get("soc_now_pct")
    v_soc, v_src = (None, None) if s_now is not None else task.executor(core.virtual_soc_at, ARCHIVE, t0)
    if s_now is not None:
        soc, soc_source = float(s_now), "settled"
    elif v_soc is not None:
        soc, soc_source = v_soc * 100.0, "virtual"
    else:
        soc = _num(ACTUAL_SOC, None)
        if soc is None:
            _last_run("plan", False, 0, f"chain broken and {ACTUAL_SOC} unavailable")
            return {"ok": False, "message": f"chain broken and {ACTUAL_SOC} unavailable"}
        had_plans = bool(task.executor(core.list_plans, ARCHIVE))
        soc_source = "reanchored" if had_plans else "real"
        if had_plans:
            log.warning("emhass: virtual SOC chain broken (no archived plan covers %s); reanchored on the real pack", t0.isoformat())
    # LIVE MODE PLANS FROM THE REAL PACK (spec 2026-09-26, section 3.6). The
    # writer executes this plan on the inverter, so the solve must start from
    # what the pack actually holds, not from the settled virtual chain (which
    # stood at 100 % while the real pack read 82 % on go-live day). The
    # virtual chain keeps settling for the display slices either way; in
    # live mode its drift is the writer's own execution error.
    if _writer_live():
        real = _num(ACTUAL_SOC, None)
        if real is not None:
            soc, soc_source = float(real), "real"
        else:
            log.warning("emhass: writer live but %s unavailable; planning from the %s SOC", ACTUAL_SOC, soc_source)
    inp = {"now": now.isoformat(), "tz": TZ_NAME, "base_url": BASE, "archive_dir": ARCHIVE, "dry_run": bool(dry_run),
           "np_today": _nordpool(today), "np_tomorrow": _nordpool(today + timedelta(days=1)),
           "epex": _attr(ENTITIES["epex"], "forecast") or [],
           "solcast_today": _attr(ENTITIES["solcast_today"], "detailedForecast") or [],
           "solcast_tomorrow": _attr(ENTITIES["solcast_tomorrow"], "detailedForecast") or [],
           "solcast_day3": _attr(ENTITIES["solcast_day3"], "detailedForecast") or [],
           "soc_pct": soc, "soc_source": soc_source, "tariff": _tariff(),
           "soc_anchor": anchor, **measured}
    # The curtailable half only. pv_series merges the three ESE lists, so the
    # whole span goes in one of them.
    inp["solcast_ese_today"] = _solcast_site(
        SOLCAST_ESE_SITE, now.replace(hour=0, minute=0, second=0, microsecond=0))
    # The live knob layer (core.LIVE_KNOBS): growatt share, P50:P10 mix, stress
    # scale, SOC costs, terminal and intermediate SOC targets, the cut thresholds.
    # Unreadable helpers fall back to the core constants.
    _pack_temp_latch()
    inp["knobs"] = _knobs()
    # The rebalancing clock on the SETTLED pack (2026-09-15): today's settled
    # slice is folded into /config/emhass/rebalance.json every tick, and the
    # core prices the SOC knobs off that state instead of the plan chain. No
    # state on record reads as overdue: the pack is assumed to need a balance.
    inp["rebalance"] = task.executor(core.rebalance_step, REBALANCE, settled, inp["knobs"].get("rebalance_dwell_h"))
    # The running day's hours for the period charts (2026-09-17): the settled
    # slice's past steps, written to the hourly sidecar and imported now, so
    # the shadow lanes reach `now` like the hero graphs; the ladder's nightly
    # row replaces them. Skipped on a dry run.
    if settled and not dry_run:
        _write_today_hours(settled, today)
    # Measured PV and load for today, fetched above because soc_init depends on
    # them. They also feed the display slice, so the chart shows what the virtual
    # pack ran against rather than what the plan forecast.
    inp["actual_pv_w"], inp["actual_load_w"] = act.get("pv_w"), act.get("load_w")
    # The curtailment mask rides along: virtual_day substitutes POTENTIAL PV over
    # the past, the same basis the ladder's replay lane settles on, so the
    # charts and the ledger cannot disagree on a curtailed day.
    inp["curtailed"], inp["pv_peak_w"] = act.get("curtailed"), act.get("pv_peak_w")
    # The must-take half, so the repair scales the strings and not the Growatt.
    inp["micro_w"] = act.get("micro_w")
    # Yesterday too, for the display slice that fills the charts' 24 h look-back
    # (2026-09-04). Every step of it is in the past, so it is measured PV
    # and load throughout; the recorder's 5-minute window reaches back ~10 days.
    prev = _actuals_15min(today - timedelta(days=1))
    inp["prev_pv_w"], inp["prev_load_w"] = prev.get("pv_w"), prev.get("load_w")
    inp["prev_curtailed"], inp["prev_pv_peak_w"] = prev.get("curtailed"), prev.get("pv_peak_w")
    inp["prev_micro_w"] = prev.get("micro_w")
    inp.update(_yesterday_lanes(prev, today - timedelta(days=1)))
    # Own same-clock load shape, unless the ML switch is on: passing
    # load_power_forecast flips EMHASS to load_forecast_method 'list', which
    # would silently override the mlforecaster the switch exists to enable.
    if state.get("input_boolean.emhass_ml_enabled") != "on":
        inp["load_days"] = _load_history_days(today)
        inp["load_now_w"] = _num(ACTUAL_LOAD, None)
    res = task.executor(core.run_plan, inp)
    keys = ("ok", "message", "t0", "n", "n_predicted_steps", "pv_gap_steps", "optim_status", "cost_eur",
            "seconds", "archive_path", "prices_predicted", "soc_source", "load_source", "load_ref_days",
            "pv_split", "pv_micro_kwh", "pv_ese_gap_steps", "aux_cut_active", "aux_cut_ratio",
            "aux_cut_curtail_kwh", "aux_cut_aux_kwh", "aux_cut_steps",
            "pv_p10_mix", "pv_p50_kwh", "pv_mixed_kwh", "knobs", "days_since_full")
    summary = {k: res.get(k) for k in keys}
    if not res.get("ok"):
        log.error(f"emhass: plan failed: {res.get('message')}")
        _last_run("plan-dry-run" if dry_run else "plan", False, res.get("seconds", 0), res.get("message"),
                  horizon_start=res.get("t0"), horizon_steps=res.get("n"),
                  n_predicted_steps=res.get("n_predicted_steps"), optim_status=res.get("optim_status"),
                  soc_source=soc_source)
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
              aux_cut_ratio=res.get("aux_cut_ratio"))
    log.info(f"emhass: plan ok {summary}")
    return summary


def _actuals_15min(day, gap_upto=None):
    """The day's measured series as W per 15-minute step, from the recorder's
    5-minute means: {pv_w, load_w} feed the virtual day and the display slices,
    {pv_w, grid_w, batt_dc_w} the ladder's replay. Any series with more than MAX_PV_GAPS
    held steps is left out of the dict rather than returned - a lane built on
    gap-holds is worse than no lane, and each lane then fails on its own. The
    recorder keeps 5-minute statistics ~10 days, plenty for scoring yesterday."""
    ids = {ACTUAL_PV, ACTUAL_LOAD, ACTUAL_GRID_IMP, ACTUAL_GRID_EXP, ACTUAL_BATT, ACTUAL_SOC,
           ACTUAL_CURTAIL, ACTUAL_MICRO}
    a = datetime.combine(day, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    b = a + timedelta(days=2)                    # cover DST long days too
    try:
        stats = task.executor(statistics_during_period, hass, a, b, ids, "5minute", None,
                              {"mean", "max"})
    except Exception as e:
        log.warning(f"emhass: actuals statistics fetch failed: {e}")
        return {}
    got = {}
    for key, eid in (("pv_w", ACTUAL_PV), ("load_w", ACTUAL_LOAD), ("imp", ACTUAL_GRID_IMP),
                     ("exp", ACTUAL_GRID_EXP), ("batt_dc_w", ACTUAL_BATT), ("soc_pct", ACTUAL_SOC),
                     ("curtailed", ACTUAL_CURTAIL), ("micro_w", ACTUAL_MICRO)):
        vals, gaps = task.executor(core.fifteen_min_series, day, TZ_NAME, (stats or {}).get(eid), gap_upto)
        if gaps > core.MAX_PV_GAPS:
            log.warning(f"emhass: actuals too gappy for {day}: {eid} {gaps} steps")
        else:
            got[key] = vals
    # The 5-minute PEAK of the array inside each step, which is the only thing
    # that separates a throttled array from a cloudy one - see core.pv_potential.
    pk, pk_gaps = task.executor(core.fifteen_min_series, day, TZ_NAME,
                                (stats or {}).get(ACTUAL_PV), gap_upto, "max", "max")
    if pk_gaps <= core.MAX_PV_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp", None), got.pop("exp", None)
    if imp is not None and exp is not None:
        # the P1 reader reports kW; everything else in the pipeline is W, + = import
        got["grid_w"] = [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(len(imp))]
    if got.get("curtailed") is None and got.get("soc_pct") is not None:
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
        got["curtailed"] = _fallback_mask(midnight, len(got["soc_pct"]), got["soc_pct"])
    return got


def _export_off(t0, n):
    """1,0 per step where switch.inverter_export_surplus was OFF at the step start.
    The switch keeps no statistics, only states, so this reads its history rather
    than the recorder's 5-minute rolls. None when nothing is recorded for the
    window, which is the caller's signal to drop back to the SOC half alone."""
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
    pts = [(st.last_changed.timestamp(), st.state)
           for st in (hist or {}).get(ACTUAL_EXPORT_SW, []) if st.state in ("on", "off")]
    pts.sort()
    if not pts:
        return None
    out, j, cur = [], 0, pts[0][1]
    for i in range(n):
        ts = (a + timedelta(minutes=core.STEP_MIN * i)).timestamp()
        while j < len(pts) and pts[j][0] <= ts:
            cur = pts[j][1]
            j += 1
        out.append(1.0 if cur == "off" else 0.0)
    return out


def _fallback_mask(t0, n, soc_pct):
    """Curtailment mask for a window that predates sensor.pv_curtailment_active
    (which only started recording at 14:00 on 2026-09-05).

    BOTH halves of the site's rule, not just SOC. The SOC-only version was loose by
    design, on the argument that pv_potential's max(measured, scaled) guard would
    neutralise a mask that over-fired. 09-05 disproved that: SOC sat above 95 %
    from the small hours, the fallback flagged the whole day, and with Solcast
    running high the guard never bit - five steps were credited with sun while
    the array was demonstrably free, peaking at 1,4x the ceiling it was supposedly
    held back from. So the fallback now reads the export switch's own history and
    applies the same two conditions the live sensor does; only a window with no
    recorded switch state at all falls back to SOC alone."""
    off = _export_off(t0, n)
    m = min(n, len(soc_pct))
    if off is None:
        log.warning(f"emhass: no export-switch history at {t0}, curtailment mask is SOC-only")
        return [1.0 if float(soc_pct[i]) > core.PV_CURTAIL_SOC_PCT else 0.0 for i in range(m)]
    return [1.0 if (float(soc_pct[i]) > core.PV_CURTAIL_SOC_PCT and off[i] >= 0.5) else 0.0
            for i in range(m)]


def _window_actuals(t0, n):
    """{pv_w, grid_w, batt_dc_w} as W per step over n steps from t0, crossing
    whatever calendar days the plan's horizon spans. No load series: the 20/20
    lane runs on core.effective_load, which is built from these three and so
    never touches the contested load entity."""
    a = t0.astimezone(timezone.utc)
    b = a + timedelta(minutes=core.STEP_MIN * n)
    ids = {ACTUAL_PV, ACTUAL_GRID_IMP, ACTUAL_GRID_EXP, ACTUAL_BATT, ACTUAL_CURTAIL, ACTUAL_SOC,
           ACTUAL_MICRO}
    try:
        stats = task.executor(statistics_during_period, hass, a, b, ids, "5minute", None,
                              {"mean", "max"})
    except Exception as e:
        log.warning(f"emhass: hindsight window fetch failed: {e}")
        return None
    got = {}
    for key, eid in (("pv_w", ACTUAL_PV), ("imp", ACTUAL_GRID_IMP),
                     ("exp", ACTUAL_GRID_EXP), ("batt_dc_w", ACTUAL_BATT),
                     ("curtailed", ACTUAL_CURTAIL), ("soc_pct", ACTUAL_SOC),
                     ("micro_w", ACTUAL_MICRO)):
        vals, gaps = task.executor(core.window_series, t0, n, (stats or {}).get(eid))
        if gaps > core.MAX_PV_GAPS:
            if key == "curtailed":
                got[key] = None           # fall back to the SOC-only mask below
                continue
            if key == "micro_w":
                got[key] = None           # no must-take series: repair the whole array, as before
                continue
            log.warning(f"emhass: hindsight window too gappy: {eid} {gaps} of {n} steps")
            return None
        got[key] = vals
    if got.get("curtailed") is None:
        got["curtailed"] = _fallback_mask(t0, n, got["soc_pct"])
    pk, pk_gaps = task.executor(core.window_series, t0, n, (stats or {}).get(ACTUAL_PV),
                                None, "max", "max")
    if pk_gaps <= core.MAX_PV_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp"), got.pop("exp")
    got["grid_w"] = [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(n)]
    return got


def _ladder_inputs(day):
    """The day's actuals plus the two omniscient windows, each None until its
    horizon is fully in the past (the recorder cannot have it before then, and
    the core reports the rung as pending rather than warning about gaps)."""
    act = _actuals_15min(day)
    # A day the writer drove is the real pack in the ledger (2026-09-27): the
    # actual rung takes the live anchor and the measured lanes.
    live = _day_was_live(day)
    marks = {"anchor": _soc_anchor(day, act, live=live), "measured": bool(live and _measured(act, live=live))}
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
    series = task.executor(core.ladder_earned_series, LADDER, TZ_NAME)
    data, total = [], 0.0
    for iso, v in series:
        total += v
        data.append({"start": datetime.fromisoformat(iso), "state": round(v, 4), "sum": round(total, 4)})
    if not data:
        return
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
    settled = task.executor(core.virtual_day, ARCHIVE, today, TZ_NAME, now,
                            act.get("pv_w"), act.get("load_w"), act.get("curtailed"),
                            act.get("pv_peak_w"), act.get("micro_w"), soc_anchor=_soc_anchor(today, act),
                            **_measured(act))
    if not settled:
        return {"status": "no_chain"}
    _write_today_hours(settled, today)
    return {"status": "ok", "n_past": settled.get("n_past")}


def _import_ladder_shadow():
    series = task.executor(core.ladder_shadow_series, LADDER)
    for key, sid, unit, name in SHADOW_STATS:
        rows = series.get(key) or []
        if not rows:
            continue
        data = [{"start": datetime.fromisoformat(iso), "mean": round(v, 4), "min": round(v, 4), "max": round(v, 4)}
                for iso, v in rows]
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
    fresh CSV to rebuild. Shares the plan lock: run it off the :13/:43 minutes."""
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
    plan: the :13/:43 refresh (:05/:35 until 2026-09-08), and the startup
    rehydrate 120 s after a pyscript reload when the add-on holds a stray solve
    (it re-plans; on 2026-09-07 that re-plan killed the first live A/B run
    through the shared lock). So: step off the refresh minutes, then wait while
    a plan is actually running."""
    m = _now().minute
    if m % 30 in (11, 12, 13, 14, 15):
        task.sleep(60 * ((16 - m % 30) % 30))
    waited = 0
    while _PLAN_BUSY["since"] is not None and waited < 300:
        task.sleep(5)
        waited += 5


def _ab_store(label, payload):
    return task.executor(core.ab_store, AB_DIR, label, payload)      # off the event loop


def _ab_load(label):
    return task.executor(core.ab_load, AB_DIR, label)


def _ab_totals(days):
    keys = ("declined_10_17_kwh", "declined_day_kwh", "growatt_cut_kwh", "import_day_kwh", "import_06_18_kwh",
            "export_day_kwh", "cash_eur", "soc_term_eur", "total_eur")
    ok = [d for d in days if d.get("status") == "ok"]
    # list comprehensions, never generator expressions: pyscript's AST walker has
    # no ast_generatorexp (see reference_pyscript_patterns); this bit on 09-07
    tot = {k: round(sum([float(d["summary"][k] or 0.0) for d in ok]), 3) for k in keys}
    soc17 = [d["summary"]["soc_17h_pct"] for d in ok if d["summary"].get("soc_17h_pct") is not None]
    tot["soc_17h_mean_pct"] = round(sum(soc17) / len(soc17), 1) if soc17 else None
    tot["days_ok"] = len(ok)
    return tot


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
    tot, base_tot = _ab_totals(days), (_ab_totals(base_days) if baseline else None)
    delta = ({k: (round(tot[k] - base_tot[k], 3) if isinstance(tot.get(k), (int, float)) and isinstance(base_tot.get(k), (int, float)) else None)
              for k in tot} if baseline else None)
    result = {"label": label, "start": str(start), "end": last.isoformat(), "overrides": ov, "from_live": bool(from_live),
              "cadence_min": int(cadence_min), "settle": str(settle), "ran_at": _now().isoformat(timespec="seconds"),
              "seconds": round((_now() - t_all).total_seconds(), 1), "totals": tot, "baseline_totals": base_tot,
              "delta": delta, "days": days, "baseline_days": base_days}
    path = _ab_store(label, result)
    slim = {k: v for k, v in result.items() if k not in ("days", "baseline_days")}
    slim["days"] = [{"day": x["day"], "status": x["status"], "summary": x.get("summary"),
                     "executed": x.get("executed"), "solves": x.get("solves"), "notes": x.get("notes")} for x in days]
    slim["baseline_days"] = [{"day": x["day"], "status": x["status"], "summary": x.get("summary")} for x in base_days]
    slim.update(friendly_name="EMHASS A/B last run", icon="mdi:ab-testing", path=path)
    state.set("sensor.emhass_ab_last", label, new_attributes=slim)
    ok = all([x["status"] == "ok" for x in days])
    _last_run("ab", ok, result["seconds"], f"{label} {start}..{last}: {tot['days_ok']}/{len(days)} days ok")
    emhass_plan_day()                                     # the add-on's latest result is a past-day solve: restore the live horizon
    return slim


@service(supports_response="optional")
def emhass_ab_apply(label=None):
    """Push a stored A/B variant's overrides into the live helpers. Default: the
    label of the last run. Raw runtime keys and diagnostics are reported as
    skipped; the next :13/:43 solve runs on the new values."""
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
    res = task.executor(core.rehydrate, ARCHIVE, TZ_NAME, _now().isoformat(),
                        _num("input_number.emhass_plan_stale_hours", 26.0),
                        _act.get("pv_w"), _act.get("load_w"),
                        _prev.get("pv_w"), _prev.get("load_w"),
                        _act.get("curtailed"), _prev.get("curtailed"),
                        _act.get("pv_peak_w"), _prev.get("pv_peak_w"),
                        _act.get("micro_w"), _prev.get("micro_w"),
                        soc_anchor=_soc_anchor(_now().date(), _act), **_measured(_act),
                        **_yesterday_lanes(_prev, _now().date() - timedelta(days=1)))
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
    task.sleep(120)            # let the add-on, recorder and Nord Pool settle after a restart
    emhass_rehydrate()


@time_trigger("cron(*/15 * * * *)")
def _emhass_health_cron():
    emhass_health()
