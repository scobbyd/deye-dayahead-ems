"""The input series: day-ahead prices and the tariff frame, the Solcast percentiles, the P50:P10 mix and
the Growatt split, the recorder's 5-minute statistics folded to the grid, and the same-clock load shape."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
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


def tariff(da, energy_tax, supplier_fee, btw_pct, feedin_fee) -> tuple[list[float], list[float]]:
    f = 1.0 + float(btw_pct) / 100.0
    buy = [round((p + energy_tax + supplier_fee) * f, 5) for p in da]
    sell = [round((p - feedin_fee) * f, 5) for p in da]
    return buy, sell


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
# pv_potential's repair inflates an array that was never suppressed.
#
# Solcast is registered as two sites and the Growatt sits inside the ESE one, so
# the split is geometry rather than a fit: micro = ESE x share. Measured over
# 2026-09-01..04 on uncurtailed steps (SOC < 90 %, so neither array is being
# held back) the Growatt is 0,288 of that site, per-day 0,275..0,308, and
# 0,288 x 9,8 kWp = 2,82 kWp against a ~3 kWp nameplate: two independent routes
# to the same number. It tracks the ESE string and not the WNW one - on
# 2026-09-03 the WNW string ran 3.533 W while the Growatt sat at 416 W, a ratio
# no shared plane could produce.
MICRO_SHARE = float(PLANT["pv"]["micro_share"])   # 0 = no AC-coupled microinverter
GROWATT_SHARE = MICRO_SHARE                        # the reference plant's microinverter is a Growatt; call sites keep this name


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
    """(main_w, micro_w): the curtailable half and the must-take half.

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
# report (docs/reports/2026-08-29-load-forecaster-and-inverter-losses.md) found
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


def load_series(t0, n, tz, day_series, now_w=None) -> tuple[list | None, dict]:
    """Same-clock-time load forecast, W per step, for the whole horizon.

    Returns (values, info); values is None when there is too little history,
    and the caller then omits load_power_forecast so EMHASS falls back to its
    own method. Every horizon day repeats the same wall-clock profile, so D+2
    is shaped like D+1."""
    days = {d: v for d, v in (day_series or {}).items() if v}
    info = {"ref_days": len(days), "missing_slots": 0}
    if len(days) < LOAD_MIN_REF_DAYS:
        return None, info
    profile = load_profile(days, tz)
    if not profile:
        return None, info
    fallback = _median(list(profile.values()))
    out, missing = [], 0
    for t in step_times(t0, n):
        v = profile.get(_slot(t))
        if v is None:
            v, missing = fallback, missing + 1
        out.append(round(max(v, 0.0), 1))
    if now_w is not None and out:
        out[0] = round(LOAD_MIX_ALPHA * out[0] + (1.0 - LOAD_MIX_ALPHA) * max(float(now_w), 0.0), 1)
    info["missing_slots"] = missing
    return out, info
