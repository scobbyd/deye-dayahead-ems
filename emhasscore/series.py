"""The input series: day-ahead prices and the tariff frame, the Solcast percentiles, the P50:P10 mix and
the Growatt split, the recorder's 5-minute statistics folded to the grid, and the same-clock load shape."""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .grid import expected_steps, _parse_ts, _slot, STEP_MIN, step_times
from .plant import PLANT


                      # prices the scoreboard's end-of-day SOC carry-over, so it
                      # must track the capacity the LP plans on or the planned and
                      # scored lanes value the same pack differently.
MAX_PV_GAPS = 8


def price_series(t0, n, np_today, np_tomorrow, epex) -> tuple[list[float], int]:
    """Day-ahead EUR/kWh per step. Nord Pool rows {start, end, price EUR/MWh}
    matched on start; steps without a row take epex_predictor {datetime, value
    EUR/kWh}; a step with neither holds the previous value. Every non-Nord-Pool
    step counts as predicted. Raises if the very first step has no price."""
    by_start = {}
    for row in list(np_today or []) + list(np_tomorrow or []):
        by_start[_parse_ts(row["start"]).timestamp()] = float(row["price"]) / 1000.0
    ep = {_parse_ts(p["datetime"]).timestamp(): float(p["value"]) for p in (epex or [])}
    da, predicted, last = [], 0, None
    for t in step_times(t0, n):
        k = t.timestamp()
        if k in by_start:
            v = by_start[k]
        elif k in ep:
            v, predicted = ep[k], predicted + 1
        elif last is not None:
            v, predicted = last, predicted + 1
        else:
            raise ValueError(f"no price for step {t.isoformat()}")
        da.append(v)
        last = v
    return da, predicted


def tariff(da, energy_tax, supplier_fee, btw_pct, feedin_fee, bonus=None) -> tuple[list[float], list[float]]:
    """Buy and sell EUR/kWh per step, BTW included. `bonus` is the sun-bonus
    mask (sun_bonus_mask): on a masked step a positive feed-in rate earns
    PLANT tariff.sun_bonus_pct on top. A rate at or below zero earns no bonus."""
    f = 1.0 + float(btw_pct) / 100.0
    up = 1.0 + float(PLANT["tariff"]["sun_bonus_pct"]) / 100.0
    buy = [round((p + energy_tax + supplier_fee) * f, 5) for p in da]
    sell = []
    for i, p in enumerate(da):
        rate = p - feedin_fee
        if bonus and bonus[i] and rate > 0:
            rate *= up
        sell.append(round(rate * f, 5))
    return buy, sell


def tariff_frame(da, tf: dict, t0, n) -> tuple[list[float], list[float]]:
    """tariff() from a tariff frame dict on the grid t0 x n: the sun-bonus
    mask applies when the frame carries sun_bonus true (a frame archived
    before the bonus existed has no key and prices without it)."""
    bonus = sun_bonus_mask(t0, n) if tf.get("sun_bonus") else None
    return tariff(da, tf["energy_tax"], tf["supplier_fee"], tf["btw_pct"], tf["feedin_fee"], bonus=bonus)


def solver_buy(buy, sell) -> list[float]:
    """The buy price the LP sees: never below sell. A step where sell beats buy
    (the sun bonus under netting) makes a relaxed import/export binary pay,
    and HiGHS then cannot close the MIP gap. Raising buy to sell overstates an
    import on such a step by the bonus only. The archived rows keep the true
    buy (planning restores it after the solve)."""
    return [max(b, s) for b, s in zip(buy, sell)]


def _sun_events(day: date, lat: float, lon: float) -> tuple[datetime, datetime] | None:
    """Sunrise and sunset (UTC) of `day` at lat/lon, upper limb with standard
    refraction (-0,833 degrees). The sunrise equation, good to about a minute
    at these latitudes. None on a polar day or night."""
    rad = math.radians
    n = (day - date(2000, 1, 1)).days
    j_star = n - lon / 360.0
    m = (357.5291 + 0.98560028 * j_star) % 360.0
    c = 1.9148 * math.sin(rad(m)) + 0.02 * math.sin(rad(2 * m)) + 0.0003 * math.sin(rad(3 * m))
    lam = (m + c + 180.0 + 102.9372) % 360.0
    transit = 2451545.0 + j_star + 0.0053 * math.sin(rad(m)) - 0.0069 * math.sin(rad(2 * lam))
    sin_d = math.sin(rad(lam)) * math.sin(rad(23.4397))
    cos_d = math.cos(math.asin(sin_d))
    cos_w = (math.sin(rad(-0.833)) - math.sin(rad(lat)) * sin_d) / (math.cos(rad(lat)) * cos_d)
    if not -1.0 <= cos_w <= 1.0:
        return None
    w = math.degrees(math.acos(cos_w)) / 360.0

    def utc(jd):
        return datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=jd - 2451545.0)
    return utc(transit - w), utc(transit + w)


def sun_bonus_mask(t0, n, lat=None, lon=None) -> list[bool]:
    """Per step: the whole quarter lies between sunrise and sunset at the
    bonus reference point (PLANT tariff.sun_bonus_lat/lon, the supplier's
    location, not the site's)."""
    lat = float(PLANT["tariff"]["sun_bonus_lat"] if lat is None else lat)
    lon = float(PLANT["tariff"]["sun_bonus_lon"] if lon is None else lon)
    days, mask = {}, []
    for t in step_times(t0, n):
        u = t.astimezone(timezone.utc)
        d = u.date()
        if d not in days:
            days[d] = _sun_events(d, lat, lon)
        ev = days[d]
        mask.append(ev is not None and ev[0] <= u and u + timedelta(minutes=STEP_MIN) <= ev[1])
    return mask


SUN_BONUS_MAX_STEP_KWH = 6.0     # above the 17,25 kW fuse for a quarter: a counter jump, not export


def sun_bonus_count(st: dict | None, now: datetime, export_kwh: float, tz: str) -> dict:
    """One step of the per-calendar-year sun-bonus counter. `st` is the last
    state {year, used_kwh, last_kwh, last_ts}; `export_kwh` the meter's
    cumulative export now. The export since the last reading counts in
    proportion to how much of that interval lies on bonus quarters, so a
    reading late by a minute or a restart gap still splits right. A counter
    that runs backwards or jumps more than a fuse can carry is re-anchored
    without counting. The year is the local calendar year; a new one starts
    the count at zero."""
    st = dict(st or {})
    year = now.astimezone(ZoneInfo(tz)).year
    if st.get("year") != year:
        st["used_kwh"] = 0.0 if st.get("year") is not None else float(st.get("used_kwh") or 0.0)
        st["year"] = year
    last_kwh, last_ts = st.get("last_kwh"), st.get("last_ts")
    st["last_kwh"], st["last_ts"] = float(export_kwh), now.isoformat()
    if last_kwh is None or last_ts is None:
        return st
    t1 = datetime.fromisoformat(last_ts)
    delta = float(export_kwh) - float(last_kwh)
    span = (now - t1).total_seconds()
    quarters = span / (STEP_MIN * 60.0)
    if delta <= 0 or span <= 0 or delta > SUN_BONUS_MAX_STEP_KWH * max(1.0, quarters):
        return st
    u1 = t1.astimezone(timezone.utc)
    q0 = u1 - timedelta(minutes=u1.minute % STEP_MIN, seconds=u1.second, microseconds=u1.microsecond)
    n = int((now - q0).total_seconds() // (STEP_MIN * 60)) + 1
    on = 0.0
    for t, m in zip(step_times(q0, n), sun_bonus_mask(q0, n)):
        a, b = max(t, t1), min(t + timedelta(minutes=STEP_MIN), now)
        if m and b > a:
            on += (b - a).total_seconds()
    st["used_kwh"] = round(float(st.get("used_kwh") or 0.0) + delta * on / span, 4)
    return st


def pv_series(t0, n, detailed_today, detailed_tomorrow, detailed_day3=None,
              field: str = "pv_estimate") -> tuple[list[float], int]:
    """W per step: the Solcast half-hour containing the step, kW x 1000.
    A step without a half-hour is 0 W and counted as a gap. `field` picks the
    percentile (pv_estimate = P50, pv_estimate10, pv_estimate90); a row that
    lacks it falls back to its P50, so an integration configured without the
    percentile attributes degrades to the plain forecast rather than to 0 W."""
    half = {}
    for p in list(detailed_today or []) + list(detailed_tomorrow or []) + list(detailed_day3 or []):
        v = p.get(field)
        if v is None:
            v = p["pv_estimate"]
        half[_parse_ts(p["period_start"]).timestamp()] = float(v) * 1000.0
    pv, gaps = [], 0
    for t in step_times(t0, n):
        u = t.astimezone(timezone.utc)
        hh = u.replace(minute=(u.minute // 30) * 30, second=0, microsecond=0).timestamp()
        if hh in half:
            pv.append(round(half[hh], 1))
        else:
            pv.append(0.0)
            gaps += 1
    return pv, gaps


# The array is not one thing (2026-09-06). The Deye's two strings throttle
# continuously once the pack is full and export is shut; the Growatt on the gen
# port does not throttle at all, and its output reaches the meter whatever the
# rest of the system wants. Handing EMHASS one combined series with
# compute_curtailment on tells the solver it can shed ~2,8 kWp it cannot touch,
# so every plan overstates its ability to dodge a negative price, and
# pv_potential's repair inflates an array that was never suppressed. (The
# repair still runs on the hybrid's half only. The LP's freedom to shed the
# Growatt is accepted since 2026-10-12, see MICRO_IN_LOAD below: it never
# bound in 36 days and the netting that denied it cost far more under the
# 0.18.5 clip.)
#
# Solcast is registered as two sites and the Growatt sits inside the ESE one, so
# the split is geometry rather than a fit: micro = ESE x share. Measured over
# 2026-09-01..04 on uncurtailed steps (SOC < 90 %, so neither array is being
# held back) the Growatt is 0,288 of that site, per-day 0,275..0,308, and
# 0,288 x 9,8 kWp = 2,82 kWp against a ~3 kWp nameplate: two independent routes
# to the same number. It tracks the ESE string and not the WNW one - on
# 2026-09-03 the WNW string ran 3.533 W while the Growatt sat at 416 W, a ratio
# no shared plane could produce.
GROWATT_SHARE = float(PLANT["pv"]["micro_share"])   # the reference plant's microinverter is a Growatt; 0 = none


# How the Growatt reaches the LP (the site's ruling 2026-10-12). EMHASS 0.18.5
# (installed 2026-10-10) clips a negative load_power_forecast to 0 W before the
# solve (upstream #1135, unconditional), so the negative-load convention above
# deleted the Growatt's output above house load from every sunny plan: 3 to
# 8 kWh of surplus a day in the archive, while the freedom the split denied the
# LP never bound (no planned curtailment step in the 36 days it was live, and
# at most 0,44 EUR over 40 days had the LP curtailed the whole micro on every
# negative-price step). False hands the LP the whole sun and the whole load and
# keeps the Growatt series for the binary aux cut only; True is the legacy
# netting for an add-on up to 0.18.1 or the library solver. A run pins either
# with micro_in_load on its inputs (the golden cases pin True).
MICRO_IN_LOAD = bool(PLANT["pv"]["micro_in_load"])


# The planner's production number is a P50:P10 MIX (2026-09-07). Not a
# forecast-accuracy device - on the 09-05 rolling re-solve an 80:20 mix moved
# neither the midday charge plateau nor the 17:00 top-up, because the LP
# plateaus at need / hours whenever forecast surplus exceeds the pack's need,
# whatever the surplus is. It front-loads the charge a little and leaves the
# last ten percent of the pack open longer, which the site prefers to the actuator
# taking all free sun (D2) for the dwell it saves above 90 %. The raw P50 stays
# the ceiling for the throttling repair and the hindsight lane: "what Solcast
# said" means P50, not the number the planner was fed. Runtime value from
# input_number.emhass_pv_p10_mix; this is the fallback when the helper is
# missing.
PV_P10_MIX = float(PLANT["pv"]["p10_mix"])


def pv_mix(p50_w, p10_w, mix: float) -> list[float]:
    """(1 - mix) x P50 + mix x P10 per step, mix clamped to [0, 1]."""
    m = min(max(float(mix), 0.0), 1.0)
    return [round((1.0 - m) * float(a) + m * float(b), 1) for a, b in zip(p50_w, p10_w)]


def pv_split(pv_total_w, pv_ese_w, share: float) -> tuple[list[float], list[float]]:
    """(main_w, micro_w): the hybrid's half and the microinverter's half. In
    the field the second is must-take; how the LP hears it is MICRO_IN_LOAD.

    The two ALWAYS sum to the combined series. The ESE reading decides only how
    the total is divided, never how big it is, so a stale or mismatched site
    fetch can misattribute a kWh but can never invent one. share outside
    [0, 1] is clamped; a micro larger than the whole array is capped at it."""
    if len(pv_ese_w) != len(pv_total_w):
        raise ValueError(f"pv_ese_w has {len(pv_ese_w)} items, pv_total_w has {len(pv_total_w)}")
    k = min(max(float(share), 0.0), 1.0)
    main, micro = [], []
    for tot, ese in zip(pv_total_w, pv_ese_w):
        m = min(max(float(ese) * k, 0.0), max(float(tot), 0.0))
        micro.append(round(m, 1))
        main.append(round(max(float(tot) - m, 0.0), 1))
    return main, micro


def fifteen_min_series(day: date, tz: str, stat_rows, gap_upto: int | None = None,
                       field: str = "mean", agg: str = "mean") -> tuple[list[float], int]:
    """Mean W per 15-minute step of `day` from recorder 5-minute statistics rows
    ({start, mean}; start accepted as seconds, milliseconds or ISO). A step
    with no 5-minute mean holds the previous step's value (0 before the first
    real value) and counts as a gap. Returns (values, gap_steps).

    gap_upto bounds the gap COUNT to steps below that index; the values are
    still the full day. A day still running is all gaps after now, which would
    fail every caller's gap test on an otherwise perfect series - scoring reads
    a finished day and leaves this None, the display path passes the current
    step (found live 2026-09-04: 23 future steps sank a clean 73-step series)."""
    z = ZoneInfo(tz)
    t0 = datetime.combine(day, time(0), tzinfo=z)
    return window_series(t0, expected_steps(day, tz), stat_rows, gap_upto, field, agg)


def window_series(t0: datetime, n: int, stat_rows, gap_upto: int | None = None,
                  field: str = "mean", agg: str = "mean") -> tuple[list[float], int]:
    """fifteen_min_series over an ARBITRARY window: n steps from t0, which is how
    the hindsight lane reads a plan's whole horizon (48-72 h, crossing two or
    three calendar days) rather than one day at a time.

    field/agg pick which recorder statistic to read and how to fold the three
    5-minute rows of a step into one number. The mean/mean default is every
    energy lane; max/max is the PV PEAK inside a step, which is the only thing
    that can tell a throttled array from a cloudy one - see pv_potential."""
    base = t0.astimezone(timezone.utc).timestamp()
    acc: list[list[float]] = [[] for _ in range(n)]
    for r in (stat_rows or []):
        s, m = r.get("start"), r.get(field)
        if s is None or m is None:
            continue
        s = _parse_ts(s).timestamp() if isinstance(s, str) else float(s)
        if s > 1e12:                      # WS-style milliseconds
            s /= 1000.0
        k = int((s - base) // (STEP_MIN * 60))
        if 0 <= k < n:
            acc[k].append(float(m))
    out, gaps, last = [], 0, 0.0
    for k in range(n):
        if acc[k]:
            last = max(acc[k]) if agg == "max" else sum(acc[k]) / len(acc[k])
        elif gap_upto is None or k < gap_upto:
            gaps += 1
        out.append(round(last, 1))
    return out, gaps


# EMHASS's own `naive` method is a POSITIONAL copy: the last n recorded samples
# pasted onto the horizon (forecast.Forecast._get_load_forecast_naive). The MPC
# grid anchors at now, so with a horizon longer than a day the copy lands
# (24 h - the local hour of the solve) out of phase. Verified 2026-09-03 against
# all 20 archived plans: correlation with the recorded load is 1,0000, and the
# shift is 0 h at the 00:15 solve, 12 h at 12:15, 11 h at 13:15. So the midday
# refresh was planning tonight on daytime load and tomorrow midday on night
# load. method_ts_round cannot fix it: it shifts the labels by at most one
# step, never which samples get copied.
#
# The fix is to build the shape here and pass it as load_power_forecast, which
# also flips EMHASS to load_forecast_method 'list'. The forecast is the median
# of the last LOAD_REF_DAYS days at the SAME wall-clock slot, and nothing else.
#
# Measured 2026-09-03 over 56 origins spread across the clock, each scored on
# every step of a real 195-287 step horizon against the recorded load:
#   naive (what this replaces)        MAE 374 W, worst 436 W at a 12:00 origin
#   same-clock median, 7 days         MAE 224 W, flat 207-241 W at every origin
#   the same plus a lag-1 level scale MAE 255 W
# So the phase error costs ~40 % of the load forecast's accuracy, and it is the
# ORIGIN HOUR that drives naive's error, exactly as the positional copy
# predicts. A lag-1 daily level correction was tried because the site's own
# report (an internal design note) found
# the daily LEVEL dominated by lag 1, autocorrelation +0,581 against +0,045 at
# lag 7; on this horizon it made things worse and was dropped. No day-of-week
# split either: at ~15 days of recorder history each weekday holds two samples.
LOAD_REF_DAYS = 7


LOAD_MIN_REF_DAYS = 2


# Passing a runtime list disables EMHASS's own set_mix_forecast, which blends
# the first step 50/50 with the live reading, so that blend is done here.
LOAD_MIX_ALPHA = 0.5


def _median(vals) -> float:
    s = sorted(float(v) for v in vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def load_profile(day_series: dict, tz: str) -> dict:
    """Median W per wall-clock 15-minute slot across the given days.

    day_series maps a date to that day's values counted from local midnight,
    exactly what fifteen_min_series returns, so a DST day carries 92 or 100
    entries and still lands on the right wall-clock slots."""
    z = ZoneInfo(tz)
    buckets: dict[int, list] = {}
    for day, vals in (day_series or {}).items():
        vals = [float(v) for v in (vals or [])]
        if not vals:
            continue
        for t, v in zip(step_times(datetime.combine(day, time(0), tzinfo=z), len(vals)), vals):
            buckets.setdefault(_slot(t), []).append(v)
    return {k: _median(v) for k, v in buckets.items()}


def load_series(t0, n, tz, day_series, now_w=None, exclude_days=None, exclude_now_w=None) -> tuple[list | None, dict]:
    """Same-clock-time load forecast, W per step, for the whole horizon.

    Returns (values, info); values is None when there is too little history,
    and the caller then omits load_power_forecast so EMHASS falls back to its
    own method. Every horizon day repeats the same wall-clock profile, so D+2
    is shaped like D+1.

    exclude_days: the same shape as day_series for a load the solve models
    on its own (the AC circuits once the heat topology is sent); its
    same-clock profile is subtracted slot by slot, floored at zero, and
    exclude_now_w comes off the live reading in the first-step mix.
    info["excluded_mean_w"] (mean W removed) is present only when an
    exclusion is requested, so the default path's info is unchanged."""
    days = {d: v for d, v in (day_series or {}).items() if v}
    info = {"ref_days": len(days), "missing_slots": 0}
    if exclude_days or exclude_now_w:
        info["excluded_mean_w"] = 0.0
    if len(days) < LOAD_MIN_REF_DAYS:
        return None, info
    profile = load_profile(days, tz)
    if not profile:
        return None, info
    ex_days = {d: v for d, v in (exclude_days or {}).items() if v}
    if ex_days:
        ex = load_profile(ex_days, tz)
        if ex:
            info["excluded_mean_w"] = round(sum(ex.values()) / len(ex), 1)
            profile = {k: max(v - ex.get(k, 0.0), 0.0) for k, v in profile.items()}
    fallback = _median(list(profile.values()))
    out, missing = [], 0
    for t in step_times(t0, n):
        v = profile.get(_slot(t))
        if v is None:
            v, missing = fallback, missing + 1
        out.append(round(max(v, 0.0), 1))
    if now_w is not None and out:
        live = max(float(now_w) - float(exclude_now_w or 0.0), 0.0)
        out[0] = round(LOAD_MIX_ALPHA * out[0] + (1.0 - LOAD_MIX_ALPHA) * live, 1)
    info["missing_slots"] = missing
    return out, info


def sun_bonus_step_file(path: str, now: datetime, export_kwh, tz: str, seed_used_kwh=None) -> dict:
    """The wrapper's one call per quarter for the sun-bonus counter: load the
    state file, step it with the meter's cumulative export (None: no reading,
    no step), save, and return it with cap_kwh, remaining_kwh and active_now
    (the quarter holding `now` is a bonus quarter). `seed_used_kwh` replaces
    this year's count, for a backfill. A missing or unreadable file starts at
    zero and anchors on the first reading."""
    try:
        with open(path) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = None
    if seed_used_kwh is not None:
        st = dict(st or {}, used_kwh=round(float(seed_used_kwh), 4),
                  year=now.astimezone(ZoneInfo(tz)).year)
    if export_kwh is not None:
        st = sun_bonus_count(st, now, float(export_kwh), tz)
    st = dict(st or {})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)
    cap = float(PLANT["tariff"]["sun_bonus_cap_kwh"])
    used = float(st.get("used_kwh") or 0.0)
    u = now.astimezone(timezone.utc)
    q = u - timedelta(minutes=u.minute % STEP_MIN, seconds=u.second, microseconds=u.microsecond)
    return dict(st, used_kwh=used, cap_kwh=cap, remaining_kwh=round(max(0.0, cap - used), 3),
                active_now=bool(sun_bonus_mask(q, 1)[0]))
