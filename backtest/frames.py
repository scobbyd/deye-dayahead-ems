"""Generic builders for the 15-minute frame the ladder reads (contract: frame_schema.py).

One row per quarter hour, UTC, labelled by interval start. This module holds
the pieces that do not depend on one household's data sources: the
step-hold onto the 15-minute grid, the Open-Meteo lanes at the plant's site
through the plant model (pvmodel.py), the trailing seven-day load profile,
the microinverter's hourly clean-up, the PV potential builder and its
capacity clip, and coverage / write / load helpers.

The one documented public path to a frame is Home Assistant long-term
statistics: backtest/ha_lts.py pulls load, PV, meter, battery and SOC over
the WebSocket API and writes the CSV; the pieces here fill the forecast
columns and the potential when you want the F lanes.

The reference frame (29.656 rows, 4 November 2025 to 8 September 2026) was
built by a household pipeline that merged a supplier's quarter-hour export,
the CAMS 15-minute irradiance service and a KNMI weather station; that
pipeline is not published. What it established still governs the columns
here:

  pv_pot_main_w   on a clean hour the measurement quarter for quarter; on a
                  suspect hour the larger of the measured and the modelled
                  hour energy laid on the model's intra-hour shape, quarters
                  capped at the array's capacity (ARRAY_CAP_W) with the
                  excess moved within the hour (potential()).
  suspect         the site rule of 2026-09-11: an hour whose hourly SOC
                  maximum reached the curtailment SOC (95 %) AND whose
                  effective export price was negative. Without a price
                  witness potential() uses the SOC-only mask, which over-fires
                  (744 hours against 201 on the reference season).
  micro_w         the AC-coupled microinverter is a 3 kW unit, so an hourly
                  mean below -10 W or above 3.000 W is a corrupt register
                  read; each corrupt hour is re-inferred as the linear
                  interpolation between the nearest sound hours on both
                  sides, then the physical clip [0, 3000] W (micro_hourly,
                  ruling 2026-09-11). A ceiling never stands in for a value.
  fc_load_ma7_w   for local day D and local wall-clock slot s, the median of
                  load_w at s over the seven previous local days D-7 .. D-1,
                  at least four of the seven present (load_ma7). The spring
                  DST day simply has no 02:xx slots; the repeated autumn hour
                  averages into one.
  fc_pv_om*_main_w
                  Open-Meteo previous-day runs (24 h and 48 h lead) and the
                  freshest run, hourly irradiance and temperature at the
                  plant's site through pvmodel.main_array_kw, held x4 onto
                  the grid (open_meteo, om_lane_kw). Hours without a wind
                  series take WIND_FALLBACK_MS.
  frame end       the frame runs from 00:00Z on `start` through the end of
                  local day `end` (frame_end_utc), so no column is built past
                  its source.

Two recorder gaps of the reference plant are documented in the household
pipeline; a generic frame marks its own gaps as NaN rows.
"""
import json
import os
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

from emhasscore.plant import PLANT

from . import frame_schema, pvmodel

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(ROOT, "data")                    # your frame and caches, gitignored
FRAMES_CSV = os.path.join(DATA, "frame.csv")
SOLCAST_JSON = os.path.join(DATA, "solcast.json")    # the HA Solcast integration's cache, copied from /config/solcast_solar/
OM_PREV_CSV = os.path.join(DATA, "om_prev.csv")
OM_DAY0_CSV = os.path.join(DATA, "om_day0.csv")

TZ = str(PLANT["site"]["timezone"])
MICRO_SHARE = float(PLANT["pv"]["micro_share"])       # the microinverter's share of the first Solcast site
SOLCAST_SITES = dict(PLANT["pv"]["solcast_sites"])    # {name: resource id}; the first is the site the micro sits on
OM_LAT, OM_LON = float(PLANT["site"]["latitude"]), float(PLANT["site"]["longitude"])
OM_VARS = ["shortwave_radiation", "diffuse_radiation", "direct_normal_irradiance", "temperature_2m"]
WIND_FALLBACK_MS = 2.0         # where no station wind exists, as in the reconstruction's lead ladder
MICRO_MAX_W = float(PLANT["pv"]["micro_max_w"])       # the microinverter's nameplate; 99,9 % of its sound hours sit under 2.750 W on the reference
MICRO_STANDBY_W = -10.0         # night standby reads -1 .. -10 W and is 0; below this is a corrupt register read
SOC_SUSPECT_PCT = float(PLANT["pv"]["curtail_soc_pct"])   # the fallback mask: an hour whose SOC maximum touched this may have been throttled
SHAPE_RATIO_MAX = 2.0           # hourly potential over hourly model beyond which the model's shape is not lent
MODEL_SHAPE_MIN_W = 50.0        # a model hour under this has no shape to lend: the hour is flat
ARRAY_CAP_W = pvmodel.ARRAY_CAP_W   # the main array's effective capacity, sum(scale x kWp) (13.100 W on the reference)

COLS = list(frame_schema.COLUMNS)
TEXT_COLS = list(frame_schema.TEXT)
FLOAT_DECIMALS = {c: 1 for c in COLS if c not in ("suspect", "da_eur_kwh", *TEXT_COLS)}
FLOAT_DECIMALS["da_eur_kwh"] = 5


# ---------------------------------------------------------------- helpers
def _utc(ts):
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _last_full_local_day(exclusive_end_utc, tz=TZ):
    """The last local calendar day that ends at or before a UTC instant."""
    local = _utc(exclusive_end_utc).tz_convert(tz)
    return (local.normalize() - pd.Timedelta(days=1)).date()


HOLD_LIMIT = {"1h": 3, "30min": 1, "5min": 0}   # quarters a source stamp may cover: never past the source end


def _hold(s, grid, freq):
    """Step-hold a coarser series onto the 15-minute grid. Missing source
    slots stay missing: resample creates them as NaN and the positional
    ffill of reindex carries the NaN, not the previous value. The limit
    stops the last source value from being held past the source end."""
    return s.resample(freq).mean().reindex(grid, method="ffill", limit=HOLD_LIMIT[freq])


def frame_end_utc(end_day, tz=TZ):
    """Last 15-minute slot of local calendar day `end_day`, in UTC. Calendar
    arithmetic, so DST transition days (23 h or 25 h) end at local midnight."""
    end_day = pd.Timestamp(end_day).date()
    return pd.Timestamp(end_day + timedelta(days=1), tz=tz).tz_convert("UTC") - pd.Timedelta(minutes=15)


def _covers(path, end_utc, start_utc=None):
    """The cached file's index reaches end_utc, and start_utc when given."""
    if not os.path.exists(path):
        return False
    idx = pd.to_datetime(pd.read_csv(path, index_col=0).index, utc=True)
    if len(idx) == 0 or idx.max() < end_utc:
        return False
    return start_utc is None or idx.min() <= start_utc


def _read_utc(path):
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


# ---------------------------------------------------------------- sources
def cams_15min(path):
    """A CAMS Radiation Service 15-minute export as mean W/m2 per slot,
    interval-start UTC: ghi, dhi, dni (their BNI). The file integrates Wh/m2
    over 15 minutes, hence x4; the parser is the reconstruction's."""
    cams = pd.read_csv(path, sep=";", comment="#", header=None,
                       names=["period", "toa", "cs_ghi", "cs_bhi", "cs_dhi", "cs_bni", "ghi", "bhi", "dhi", "bni", "rel"])
    cams.index = pd.to_datetime(cams["period"].str.split("/").str[0], utc=True)
    cams.index.name = "ts_utc"
    irr = pd.DataFrame({"ghi": cams["ghi"], "dhi": cams["dhi"], "dni": cams["bni"]}) * 4.0
    return irr[~irr.index.duplicated(keep="last")].sort_index()


def cams_model_w(irr, met):
    """The calibrated main-array model on a 15-minute irradiance grid, W:
    hourly station temperature (temp_c) and wind (wind_ms) interpolated onto
    the quarters (flat beyond the station's ends), solar position at
    mid-slot, as in the reconstruction."""
    met = met[["temp_c", "wind_ms"]].reindex(met.index.union(irr.index)).interpolate(limit_direction="both")
    met = met.reindex(irr.index)
    kw = pvmodel.main_array_kw(irr, met, irr.index + pd.Timedelta(minutes=7.5))
    return (kw * 1000.0).rename("model_w")


def solcast_sites(path=SOLCAST_JSON):
    """Last-issued Solcast forecasts per site from the HA integration's
    solcast.json: 30-minute frames of p50, p10 in kW keyed by resource id.
    The integration only kept P50 in its first week, so P10 is masked out
    before the first period that carries one."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} missing; copy /config/solcast_solar/solcast.json from Home Assistant there")
    doc = json.load(open(path))
    sites = {}
    for sid, info in doc["siteinfo"].items():
        df = pd.DataFrame(info["forecasts"])
        df.index = pd.to_datetime(df["period_start"], utc=True)
        df = df[["pv_estimate", "pv_estimate10"]].rename(columns={"pv_estimate": "p50", "pv_estimate10": "p10"})
        df = df[~df.index.duplicated(keep="last")].sort_index()
        first_p10 = df.index[df["p10"] > 0].min()
        df.loc[df.index < first_p10, "p10"] = np.nan
        sites[sid] = df
    return sites


def solcast_lanes(sites, grid, site_ids=None, share=MICRO_SHARE):
    """The three Solcast frame columns on the grid, W: main-string P50 and
    P10 (the first site less the microinverter's share, plus every other
    site) and the microinverter's P50 (the first site times the share).
    `site_ids` defaults to PLANT pv.solcast_sites in order."""
    ids = list((site_ids or SOLCAST_SITES).values())
    missing = [i for i in ids if i not in sites]
    if missing:
        raise KeyError(f"Solcast site id(s) {missing} not in the cache; set pv.solcast_sites in plant.json")
    first, rest = sites[ids[0]], [sites[i] for i in ids[1:]]
    main = {}
    for q in ("p50", "p10"):
        s = first[q] * (1 - share)
        for r in rest:
            s = s + r[q]
        main[q] = _hold(s, grid, "30min") * 1000.0
    return {"fc_pv_solcast_p50_main_w": main["p50"], "fc_pv_solcast_p10_main_w": main["p10"],
            "fc_micro_solcast_w": _hold(first["p50"] * share, grid, "30min") * 1000.0}


def _om_get(url, hourly, start_date, end_date):
    r = requests.get(url, params={"latitude": OM_LAT, "longitude": OM_LON, "timezone": "UTC",
                                  "hourly": ",".join(hourly), "start_date": str(start_date),
                                  "end_date": str(end_date), "models": "best_match"}, timeout=120)
    r.raise_for_status()
    h = r.json()["hourly"]
    # radiation is the mean of the preceding hour: label by interval start
    idx = pd.to_datetime(h["time"], utc=True) - pd.Timedelta(hours=1)
    df = pd.DataFrame({k: v for k, v in h.items() if k != "time"}, index=idx, dtype=float)
    df.index.name = "ts_utc"
    return df


def open_meteo(start_utc, end_utc, prev_csv=OM_PREV_CSV, day0_csv=OM_DAY0_CSV):
    """Previous-runs (previous_day1, previous_day2) and freshest-run hourly
    irradiance and temperature at the plant's site, cached to om_prev.csv
    and om_day0.csv under data/; a cache that stops short at either end is
    pulled again over the range."""
    start_date = (start_utc - pd.Timedelta(days=1)).date()
    end_date = min((end_utc + pd.Timedelta(days=1)).date(), pd.Timestamp.now(tz="UTC").date())
    if _covers(prev_csv, end_utc, start_utc):
        prev = _read_utc(prev_csv)
    else:
        print(f"pulling Open-Meteo previous runs {start_date} .. {end_date}", flush=True)
        prev = _om_get("https://previous-runs-api.open-meteo.com/v1/forecast",
                       [f"{v}_previous_day{d}" for d in (1, 2) for v in OM_VARS], start_date, end_date)
        os.makedirs(os.path.dirname(prev_csv), exist_ok=True)
        prev.to_csv(prev_csv)
        time.sleep(1.0)
    if _covers(day0_csv, end_utc, start_utc):
        day0 = _read_utc(day0_csv)
    else:
        print(f"pulling Open-Meteo freshest run {start_date} .. {end_date}", flush=True)
        day0 = _om_get("https://historical-forecast-api.open-meteo.com/v1/forecast", OM_VARS, start_date, end_date)
        os.makedirs(os.path.dirname(day0_csv), exist_ok=True)
        day0.to_csv(day0_csv)
    return prev, day0


def om_lane_kw(src, suffix, wind, label="lane"):
    """One Open-Meteo hourly lane through the calibrated plant model, kW.
    Hours without a station wind take WIND_FALLBACK_MS, and say so."""
    irr = pd.DataFrame({"ghi": src[f"shortwave_radiation{suffix}"], "dhi": src[f"diffuse_radiation{suffix}"],
                        "dni": src[f"direct_normal_irradiance{suffix}"]}, index=src.index)
    w = wind.reindex(src.index) if wind is not None else pd.Series(np.nan, index=src.index)
    n_fallback = int(w.isna().sum())
    if n_fallback:
        print(f"note: {label}: {n_fallback} of {len(w)} h have no station wind and use {WIND_FALLBACK_MS} m/s "
              f"(wind ends {w.last_valid_index()})", flush=True)
    met = pd.DataFrame({"temp_c": src[f"temperature_2m{suffix}"], "wind_ms": w.fillna(WIND_FALLBACK_MS)}, index=src.index)
    ok = irr.notna().all(axis=1) & met["temp_c"].notna()
    kw = pvmodel.main_array_kw(irr[ok], met[ok], irr.index[ok] + pd.Timedelta(minutes=30))
    return kw.reindex(src.index)


def om_lanes(start_utc, end_utc, grid, wind=None):
    """The three Open-Meteo frame columns on the grid, W (fc_pv_om0_main_w,
    fc_pv_om24_main_w, fc_pv_om48_main_w). `wind` is an hourly m/s series
    (UTC) or None."""
    prev, day0 = open_meteo(start_utc, end_utc)
    prev, day0 = prev.loc[start_utc:end_utc], day0.loc[start_utc:end_utc]
    return {"fc_pv_om0_main_w": _hold(om_lane_kw(day0, "", wind, "fc_pv_om0_main_w"), grid, "1h") * 1000.0,
            "fc_pv_om24_main_w": _hold(om_lane_kw(prev, "_previous_day1", wind, "fc_pv_om24_main_w"), grid, "1h") * 1000.0,
            "fc_pv_om48_main_w": _hold(om_lane_kw(prev, "_previous_day2", wind, "fc_pv_om48_main_w"), grid, "1h") * 1000.0}


def micro_hourly(micro, deye_present):
    """The microinverter's hourly series with its corrupt register reads
    re-inferred (ruling 2026-09-11). Hours where the whole sensor family was
    absent while the hybrid reported are 0 (its counter shows it effectively
    off). Hours below MICRO_STANDBY_W or above MICRO_MAX_W are corrupt reads
    and take the linear interpolation in time between the nearest sound hours
    on both sides, the zeroed absent hours counting as sound; a corrupt hour
    with no sound hour on one side stays NaN. Only the corrupt hours are
    touched. Then the physical clip [0, MICRO_MAX_W], which lifts the night
    standby readings to 0."""
    s = micro.where(micro.notna() | ~deye_present, 0.0)
    corrupt = (s < MICRO_STANDBY_W) | (s > MICRO_MAX_W)
    filled = s.mask(corrupt).interpolate(method="time", limit_area="inside")
    return s.where(~corrupt, filled).clip(0.0, MICRO_MAX_W)


def load_ma7(load_w, tz=TZ):
    """Trailing seven-day profile: for local day D and local time-of-day s,
    the median of load_w at s over D-7 .. D-1, at least four of the seven
    present. Nonexistent DST slots simply have no row on that day; the
    repeated autumn hour averages into one wall-clock slot."""
    local = load_w.tz_convert(tz)
    keys = pd.MultiIndex.from_arrays([local.index.date, local.index.strftime("%H:%M")], names=["date", "tod"])
    pivot = pd.Series(local.to_numpy(), index=keys).groupby(level=["date", "tod"]).mean().unstack("tod").sort_index()
    prof = pivot.shift(1).rolling(7, min_periods=4).median()
    vals = prof.stack(future_stack=True).reindex(keys).to_numpy()
    return pd.Series(vals, index=load_w.index)


# ---------------------------------------------------------------- the potential
def _hour_of(idx):
    return idx.floor("h")


def _redistribute_clipped(q, cap):
    """Four (or fewer) quarters of one hour whose sum must be kept: the
    quarters above `cap` are set to it and the energy they lost is spread
    over the others in proportion to their value, again until no quarter
    exceeds the cap. If every quarter would exceed it the hour sits at the
    cap (an energy the array cannot carry)."""
    q = np.asarray(q, dtype=float).copy()
    target = q.sum()
    fixed = np.zeros(len(q), dtype=bool)
    for _ in range(len(q)):
        over = (q > cap + 1e-9) & ~fixed
        if not over.any():
            return q
        q[over] = cap
        fixed |= over
        free = ~fixed
        if not free.any() or q[free].sum() <= 0:
            return q
        q[free] *= (target - cap * fixed.sum()) / q[free].sum()
    return q


def potential(pv_main_w, soc_max_h, model_w, plant_from=None):
    """The potential builder: pv_pot_main_w, suspect and src_pot on the frame
    grid. pv_main_w and model_w are 15-minute W on the grid, soc_max_h the
    hourly SOC maximum (NaN = no statistic, not suspect). The mask is
    SOC-only (no price witness here): an hour is suspect when its SOC maximum
    is at or above SOC_SUSPECT_PCT. On a suspect hour the energy is
    max(measured, model) in the model's shape, flat past a shape ratio of
    SHAPE_RATIO_MAX or under MODEL_SHAPE_MIN_W of model, clipped at
    ARRAY_CAP_W with the energy kept in the hour. Before `plant_from` (a UTC
    instant, None = never) the rows belong to an older plant and the
    potential is the model floored at the measurement, labelled `model`."""
    grid = pv_main_w.index
    hours = _hour_of(grid)
    suspect_h = (soc_max_h >= SOC_SUSPECT_PCT).reindex(hours.unique()).fillna(False).astype(bool)
    suspect = pd.Series(suspect_h.reindex(hours).to_numpy(), index=grid)
    meas_h = pv_main_w.groupby(hours).mean()
    model_h = model_w.groupby(hours).mean()
    pot_h = pd.Series(np.fmax(meas_h.to_numpy(), model_h.reindex(meas_h.index).to_numpy()), index=meas_h.index)
    ratio_h = (pot_h / model_h.reindex(pot_h.index)).replace([np.inf, -np.inf], np.nan)
    flat_h = ratio_h.isna() | (ratio_h > SHAPE_RATIO_MAX) | (model_h.reindex(pot_h.index) < MODEL_SHAPE_MIN_W)
    # per quarter
    ratio_q = ratio_h.reindex(hours).to_numpy()
    flat_q = flat_h.reindex(hours).fillna(True).to_numpy()
    pot_flat_q = pot_h.reindex(hours).to_numpy()
    shaped_q = model_w.to_numpy() * ratio_q
    pot = np.where(flat_q, pot_flat_q, shaped_q)
    src = np.where(flat_q, "flat", "shaped").astype(object)      # object: numpy would truncate longer labels
    # the capacity clip, energy kept inside the hour
    over = pot > ARRAY_CAP_W
    if over.any():
        hours_arr = np.asarray(hours)
        for h in np.unique(hours_arr[over]):
            m = hours_arr == h
            pot[m] = _redistribute_clipped(pot[m], ARRAY_CAP_W)
            src[m] = "shaped_clip"
    pot = np.where(suspect.to_numpy(), pot, pv_main_w.to_numpy())
    src = np.where(suspect.to_numpy(), src, "measured")
    if plant_from is not None:
        # before the plant: the current plant's model, floored at the record of the old one
        pre = np.asarray(grid < _utc(plant_from))
        pot = np.where(pre, np.fmax(model_w.to_numpy(), pv_main_w.to_numpy()), pot)
        src = np.where(pre, "model", src)
    src = np.where(np.isnan(pot), "", src)
    return (pd.Series(pot, index=grid), suspect, pd.Series(src, index=grid, dtype=object))


# ---------------------------------------------------------------- inspect, write, load
def coverage(df):
    """Per-column presence and the first/last stamped value; a text column
    counts its non-empty rows."""
    rows = []
    for c in df.columns:
        s = df[c]
        if c in TEXT_COLS or c.startswith("src_"):
            s = s.where(s.astype(str) != "")
        rows.append({"column": c, "present_pct": round(100.0 * s.notna().mean(), 2),
                     "first_valid": s.first_valid_index(), "last_valid": s.last_valid_index()})
    return pd.DataFrame(rows).set_index("column")


def sources(df):
    """Row counts per label for every text column (plant and any src_*)."""
    return {c: df[c].value_counts(dropna=False).to_dict() for c in df.columns if c in TEXT_COLS or c.startswith("src_")}


def write(df, path=FRAMES_CSV):
    out = df.copy()
    out.index.name = "ts_utc"
    out = out.round({c: d for c, d in FLOAT_DECIMALS.items() if c in out.columns})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out.to_csv(path)
    return path


def load(path=FRAMES_CSV):
    """The validated frame (frame_schema.load), the optional columns filled."""
    return frame_schema.load(path)
