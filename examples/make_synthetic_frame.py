#!/usr/bin/env python3
"""A synthetic 14-day frame so the quick start runs on a clean clone.

    python examples/make_synthetic_frame.py            # writes examples/frame_synthetic.csv

Deterministic (fixed seed). The house is a plausible Dutch spring home with
NO battery yet, which is the case the quick check exists for:

  load_w          300 .. 2.500 W, a morning and an evening peak, a weekly
                  rhythm (weekend days sit higher through the middle of the
                  day, the weekday morning peak is sharper)
  pv_main_w       a clear-sky bell per string from the PLANT's strings
                  (tilt, azimuth, kWp), scaled per day by a cloud factor and
                  shaped by slow intra-day cloud noise; three overcast days
  pv_pot_main_w   = pv_main_w (nothing was held back: no pack to fill)
  micro_w         0 (no microinverter)
  grid_w          load_w - pv_main_w (no battery, so the meter is the balance)
  batt_dc_w       0 (no battery)
  soc_pct         50, constant (the ladder pins soc_final at 50 %, so every
                  plan starts and ends the day at the same level)
  da_eur_kwh      a duck curve: a night floor, a midday dip that goes below
                  zero on the sunny days, an evening peak; one spiky day
  fc_pv_om24_main_w  the PV with a day-ahead bias and noise
  fc_pv_om48_main_w  the same with a larger bias and more noise
  fc_load_ma7_w   absent: frame_schema builds it from load_w

14 local days on the 15-minute UTC grid, 1.344 rows, chosen in April 2026
(no DST transition inside the window: the library loses steps across
the spring change and the solver has to pad the horizon there). The
frame is validated through backtest.frame_schema before it is written.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from emhasscore.plant import PLANT              # noqa: E402
from backtest import frame_schema               # noqa: E402

SEED = 20260413
FIRST_DAY = date(2026, 4, 13)                   # a Monday; 14 days to Sunday 26 April
DAYS = 14
OUT = os.path.join(HERE, "frame_synthetic.csv")
TZ = str(PLANT["site"]["timezone"])
LAT = float(PLANT["site"]["latitude"])
STRINGS = PLANT["pv"]["strings"]
PV_YIELD = 0.62                                 # W per Wp at full sun in April, module and DC losses included
DECLINATION_DEG = 9.5                           # the sun in mid-April


def grid_index() -> pd.DatetimeIndex:
    """Local midnight of FIRST_DAY through the last quarter of the 14th day, in UTC."""
    t0 = pd.Timestamp(FIRST_DAY, tz=TZ)
    t1 = pd.Timestamp(FIRST_DAY + timedelta(days=DAYS), tz=TZ)
    return pd.date_range(t0.tz_convert("UTC"), t1.tz_convert("UTC") - pd.Timedelta(minutes=15),
                         freq="15min", name="ts_utc")


def _smooth_noise(rng, n: int, scale: float, window: int) -> np.ndarray:
    """Zero-mean noise with a slow shape: white noise through a moving mean."""
    w = rng.normal(0.0, 1.0, n + window)
    s = np.convolve(w, np.ones(window) / window, mode="valid")[:n]
    return scale * s / (s.std() or 1.0)


def clear_sky_w(idx: pd.DatetimeIndex) -> np.ndarray:
    """The strings' clear-sky output, W: sun elevation from a fixed
    declination, the plane-of-array cosine per string, kWp x PV_YIELD."""
    hour_utc = idx.hour + idx.minute / 60.0 + 0.125             # mid-quarter
    solar_noon_utc = 12.0 - float(PLANT["site"]["longitude"]) / 15.0
    h = np.radians(15.0 * (hour_utc - solar_noon_utc))          # hour angle
    lat, dec = np.radians(LAT), np.radians(DECLINATION_DEG)
    sin_el = np.sin(lat) * np.sin(dec) + np.cos(lat) * np.cos(dec) * np.cos(h)
    el = np.arcsin(np.clip(sin_el, -1.0, 1.0))
    # solar azimuth, degrees from north, clockwise
    cos_az = (np.sin(dec) - np.sin(el) * np.sin(lat)) / np.maximum(np.cos(el) * np.cos(lat), 1e-6)
    az = np.degrees(np.arccos(np.clip(cos_az, -1.0, 1.0)))
    az = np.where(h > 0, 360.0 - az, az)
    out = np.zeros(len(idx))
    for s in STRINGS.values():
        tilt, sazim, kwp = np.radians(float(s["tilt"])), np.radians(float(s["azimuth"])), float(s["kwp"])
        cos_inc = (np.sin(el) * np.cos(tilt)
                   + np.cos(el) * np.sin(tilt) * np.cos(np.radians(az) - sazim))
        out += kwp * 1000.0 * PV_YIELD * float(s.get("scale", 1.0)) * np.clip(cos_inc, 0.0, None)
    out[el <= 0] = 0.0
    return out


def build() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    idx = grid_index()
    n = len(idx)
    local = idx.tz_convert(TZ)
    tod = local.hour + local.minute / 60.0                       # local time of day, h
    day_no = np.array([(d - FIRST_DAY).days for d in local.date])
    weekend = np.isin(local.dayofweek, [5, 6])

    # ---- load: base + peaks + weekly rhythm + noise, 300..2500 W
    base = 380.0 + 60.0 * np.sin(2.0 * np.pi * day_no / 7.0)
    morning = np.where(weekend, 700.0 * np.exp(-0.5 * ((tod - 9.0) / 1.3) ** 2),
                       950.0 * np.exp(-0.5 * ((tod - 7.6) / 0.8) ** 2))
    midday = np.where(weekend, 420.0, 90.0) * np.exp(-0.5 * ((tod - 13.5) / 2.2) ** 2)
    evening = 1500.0 * np.exp(-0.5 * ((tod - 18.9) / 1.4) ** 2)
    night = -90.0 * np.exp(-0.5 * ((tod - 3.0) / 2.0) ** 2)
    load = base + morning + midday + evening + night + _smooth_noise(rng, n, 90.0, 3) + rng.normal(0, 35.0, n)
    load = np.clip(load, 300.0, 2500.0)

    # ---- PV: clear sky x daily cloud factor x slow intra-day cloud shape
    clear = clear_sky_w(idx)
    cloud_day = np.array([0.96, 0.90, 0.35, 0.82, 0.93, 0.28, 0.75,
                          0.95, 0.88, 0.60, 0.32, 0.91, 0.97, 0.85])
    shape = 1.0 + _smooth_noise(rng, n, 0.10, 8)
    pv = clear * cloud_day[day_no] * np.clip(shape, 0.55, 1.15)
    pv = np.clip(pv, 0.0, None)
    pv[clear <= 0] = 0.0

    # ---- day-ahead price: hourly slots, a duck curve, negatives on sunny days, one spiky day
    hour = local.hour.to_numpy()
    base_p = 0.085 + 0.006 * np.sin(2.0 * np.pi * day_no / 7.0)
    dip = -0.075 * np.exp(-0.5 * ((hour + 0.5 - 13.0) / 2.3) ** 2)
    sunny = cloud_day[day_no] >= 0.90
    dip = np.where(sunny, dip * 1.25, dip)                       # sunny days push the midday slots below zero
    peak = 0.13 * np.exp(-0.5 * ((hour + 0.5 - 19.0) / 1.4) ** 2)
    morning_p = 0.035 * np.exp(-0.5 * ((hour + 0.5 - 8.0) / 1.2) ** 2)
    night_p = -0.02 * np.exp(-0.5 * ((hour + 0.5 - 3.5) / 2.0) ** 2)
    price = base_p + dip + peak + morning_p + night_p
    price_h = pd.Series(price, index=local).groupby([local.date, hour]).transform("first").to_numpy()
    price_h = price_h + rng.normal(0.0, 0.004, n)                # a small quarter ripple inside the hour
    spiky = day_no == 9                                          # Wednesday 22 April: an evening spike
    price_h = np.where(spiky & (hour >= 18) & (hour <= 20), price_h + 0.28, price_h)
    price_h = np.where(spiky & (hour == 19), price_h + 0.12, price_h)
    price = np.round(price_h, 5)

    # ---- forecasts: the PV with a day-ahead bias and noise
    bias24 = rng.normal(0.0, 0.10, DAYS)[day_no]
    bias48 = rng.normal(0.0, 0.20, DAYS)[day_no]
    fc24 = np.clip(pv * (1.0 + bias24) * (1.0 + _smooth_noise(rng, n, 0.08, 6)), 0.0, None)
    fc48 = np.clip(pv * (1.0 + bias48) * (1.0 + _smooth_noise(rng, n, 0.14, 6)), 0.0, None)
    fc24[clear <= 0] = 0.0
    fc48[clear <= 0] = 0.0

    df = pd.DataFrame(index=idx)
    df["pv_pot_main_w"] = np.round(pv, 1)
    df["pv_main_w"] = np.round(pv, 1)
    df["micro_w"] = 0.0
    df["load_w"] = np.round(load, 1)
    df["grid_w"] = np.round(load - pv, 1)
    df["batt_dc_w"] = 0.0
    df["soc_pct"] = 50.0
    df["da_eur_kwh"] = price
    df["fc_pv_om24_main_w"] = np.round(fc24, 1)
    df["fc_pv_om48_main_w"] = np.round(fc48, 1)
    return df


def main() -> int:
    df = build()
    df.to_csv(OUT, date_format="%Y-%m-%dT%H:%M:%S+00:00")
    checked = frame_schema.load(OUT)                  # raises ValueError on a bad frame
    from backtest.ladder import LANES
    ok, missing = frame_schema.available_lanes(checked, LANES)
    days = frame_schema.full_local_days(checked)
    print(f"wrote {os.path.relpath(OUT, os.getcwd())}: {len(checked)} quarters, {len(days)} full local days "
          f"{days[0]} .. {days[-1]}")
    print(f"lanes available: {', '.join(ok)}; skipped: {', '.join(missing) or 'none'}")
    print(f"load {checked['load_w'].min():.0f}..{checked['load_w'].max():.0f} W, "
          f"pv peak {checked['pv_main_w'].max():.0f} W, "
          f"price {checked['da_eur_kwh'].min():.3f}..{checked['da_eur_kwh'].max():.3f} EUR/kWh, "
          f"{int((checked['da_eur_kwh'] < 0).sum())} negative quarters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
