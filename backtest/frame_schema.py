"""The frame: the one CSV the offline harness reads. Contract and loader.

One row per quarter hour, UTC, labelled by the START of the interval, on an
unbroken 15-minute grid (a missing quarter is a NaN row, never a missing row).
Every power column is a mean watt over the quarter. The ladder (ladder.py)
plans one day per lane on these columns and settles the plan on the measured
day; the closed replay (replay.py) walks them tick by tick.

Required columns
  ts_utc          ISO 8601 with offset, e.g. 2026-06-10T10:15:00+00:00, the
                  index column. Local-time stamps are converted; naive stamps
                  are taken as UTC.
  load_w          house load, W, positive = consumption. On the reference plant
                  this is the hybrid's UPS/load port, so the microinverter is
                  NOT inside it. Load includes everything the pack and the
                  strings feed; it does not include the inverter's own losses.
  pv_pot_main_w   what the hybrid's strings COULD have made, W. Equal to
                  pv_main_w on every quarter the array was not held back; above
                  it where the pack was full and export was off (the suspect
                  hours). Unknown: copy pv_main_w and the settlement credits no
                  curtailment repair.
  pv_main_w       what the hybrid's strings did make, W, >= 0 (DC-coupled PV
                  behind the hybrid's own MPPTs; the LP's curtailable PV).
  grid_w          the meter, W, + = import from the grid, - = export.
  batt_dc_w       the pack's DC-side power, W, + = discharge, - = charge. The
                  settlement moves it to the AC node with the bridge
                  efficiency (x eta discharging, / eta charging). A house with
                  no battery yet writes 0.
  soc_pct         the pack's state of charge, 0..100. Each ladder day starts
                  its plan from the value on the first quarter of the local
                  day. A house with no battery yet writes a constant (50: the
                  plan then starts and ends every day at the same level, which
                  is the soc_final target the ladder pins).
  da_eur_kwh      the day-ahead price for the quarter, EUR/kWh, the bare
                  auction price (no fee, no tax, no VAT; the tariff adds
                  those). Negative values are real and matter.

Optional columns (filled when absent)
  micro_w         AC-coupled microinverter on the gen port, W, >= 0; 0 if
                  absent. Must-take PV: the LP sees it as negative load, the
                  hybrid bridge never carries it.
  soc_max_pct     hourly maximum of the SOC, held on the four quarters; NaN if
                  absent. Only the fallback curtailment mask reads it.
  suspect         True on every quarter of an hour where the strings may have
                  been held back (the reference rule: hourly SOC maximum at or
                  above 95 % AND a negative price in the hour). False if
                  absent. The settlement's curtailment mask.
  plant           "measured" (default) on quarters the recorded plant is the
                  one you are pricing; "modelled" where the record belongs to
                  an older plant and the columns are a model of the current
                  one. The ladder summarises the two as separate blocks and
                  never mixes them.
  fc_pv_solcast_p50_main_w, fc_pv_solcast_p10_main_w
                  the Solcast forecast of the strings as last issued, W
                  (main strings only, the microinverter's share taken out).
                  Lanes F_sol and F_mix.
  fc_micro_solcast_w
                  the Solcast forecast of the microinverter, W. Unused by the
                  lanes (they hand the LP the measured micro_w); kept for a
                  forecast-only replay.
  fc_pv_om24_main_w, fc_pv_om48_main_w
                  Open-Meteo irradiance issued 24 h and 48 h before the
                  quarter through the plant model (pvmodel.py), W. Lane F_da
                  reads om24 on the scored day and om48 on the day after.
  fc_pv_om0_main_w
                  the freshest Open-Meteo run through the plant model, W; an
                  upper bound on short-lead skill, not a real lane to plan on.
                  Lane F_om0.
  fc_load_ma7_w   the trailing seven-day load profile (frames.load_ma7), W.
                  Every F lane's load. Built from load_w when absent.

Any other column (the reference frame's src_* witness labels, for instance)
is kept as is. A forecast lane whose columns are missing is skipped by
quickcheck with a note; ladder.run reports no_data on such a day.

    from backtest import frame_schema
    df = frame_schema.load("data/frame.csv")      # raises ValueError on a bad frame
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

TZ = "Europe/Amsterdam"                # the local calendar the ladder plans on; PLANT["site"]["timezone"] overrides
INDEX = "ts_utc"
REQUIRED = ["load_w", "pv_pot_main_w", "pv_main_w", "grid_w", "batt_dc_w", "soc_pct", "da_eur_kwh"]
FORECAST = ["fc_pv_solcast_p50_main_w", "fc_pv_solcast_p10_main_w", "fc_micro_solcast_w",
            "fc_pv_om0_main_w", "fc_pv_om24_main_w", "fc_pv_om48_main_w", "fc_load_ma7_w"]
OPTIONAL = {"micro_w": 0.0, "soc_max_pct": np.nan, "suspect": False, "plant": "measured",
            **{c: np.nan for c in FORECAST}}
TEXT = ["plant"]
# the column order the reference builder writes; extras follow it
COLUMNS = ["pv_pot_main_w", "pv_main_w", "micro_w", "load_w", "grid_w", "batt_dc_w", "soc_pct", "soc_max_pct",
           "suspect", "da_eur_kwh"] + FORECAST + ["plant"]
STEP = pd.Timedelta(minutes=15)
PLANT_VALUES = {"measured", "modelled"}


def _try_plant_tz() -> str:
    try:
        from emhasscore.plant import PLANT
        return str(PLANT["site"].get("timezone") or TZ)
    except Exception:       # noqa: BLE001 - the schema must load without the core on the path
        return TZ


def _bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    return s.map(lambda v: str(v).strip().lower() in ("true", "1", "1.0", "yes"))


def validate(df: pd.DataFrame, path: str = "<frame>") -> pd.DataFrame:
    """Check the contract on a frame already in memory and return it with the
    optional columns filled, dtypes fixed and the columns in COLUMNS order
    (extras after). Raises ValueError with every defect listed at once."""
    problems = []
    if not isinstance(df.index, pd.DatetimeIndex):
        problems.append(f"index is not a datetime index (name the column {INDEX})")
    else:
        if df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        else:
            df = df.copy()
            df.index = df.index.tz_convert("UTC")
        df.index.name = INDEX
        if len(df) and not df.index.is_monotonic_increasing:
            df = df.sort_index()
        if df.index.has_duplicates:
            problems.append(f"{int(df.index.duplicated().sum())} duplicate timestamps")
        if len(df) > 1:
            steps = (df.index[1:] - df.index[:-1]).unique()
            if len(steps) != 1 or steps[0] != STEP:
                bad = sorted(set(str(s) for s in steps if s != STEP))[:3]
                problems.append(f"index is not an unbroken 15-minute grid (steps seen: {', '.join(bad)}); "
                                "reindex on pd.date_range(..., freq='15min') and leave gaps as NaN rows")
        off = df.index[(df.index.minute % 15 != 0) | (df.index.second != 0)]
        if len(off):
            problems.append(f"{len(off)} timestamps are not on the quarter (first {off[0]})")
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        problems.append(f"missing required column(s): {', '.join(missing)} (see backtest/frame_schema.py)")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    for c in REQUIRED:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    for c, default in OPTIONAL.items():
        if c not in df.columns:
            df[c] = default
    df["micro_w"] = pd.to_numeric(df["micro_w"], errors="coerce").fillna(0.0).astype(float)
    for c in ["soc_max_pct"] + FORECAST:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["suspect"] = _bool(df["suspect"]).fillna(False).astype(bool)
    df["plant"] = df["plant"].fillna("measured").astype(str).str.strip().replace("", "measured")
    bad_plant = sorted(set(df["plant"]) - PLANT_VALUES)
    if bad_plant:
        raise ValueError(f"{path}: plant column carries {bad_plant}; allowed: {sorted(PLANT_VALUES)}")
    # value sanity, reported not repaired
    for c in ("pv_main_w", "pv_pot_main_w", "micro_w"):
        neg = df[c] < -1.0
        if neg.any():
            problems.append(f"{c} has {int(neg.sum())} negative quarters (PV is >= 0; first {df.index[neg][0]})")
    soc = df["soc_pct"].dropna()
    if len(soc) and ((soc < 0) | (soc > 100)).any():
        problems.append("soc_pct outside 0..100")
    if df["da_eur_kwh"].dropna().abs().max() > 5.0 if df["da_eur_kwh"].notna().any() else False:
        problems.append("da_eur_kwh above 5 EUR/kWh: the column is EUR/kWh, not EUR/MWh")
    if df["fc_load_ma7_w"].isna().all() and df["load_w"].notna().any():
        from .frames import load_ma7
        df["fc_load_ma7_w"] = load_ma7(df["load_w"], tz=_try_plant_tz())
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    extras = [c for c in df.columns if c not in COLUMNS]
    return df[COLUMNS + extras]


def read(path: str) -> pd.DataFrame:
    """Read the CSV as delivered, index parsed to UTC, no validation."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"frame CSV not found: {path} (see backtest/frame_schema.py for the contract)")
    head = pd.read_csv(path, nrows=0)
    if INDEX not in head.columns and head.columns[0] not in ("", "Unnamed: 0"):
        raise ValueError(f"{path}: no {INDEX} column; the first column must be the UTC quarter start")
    index_col = INDEX if INDEX in head.columns else 0
    df = pd.read_csv(path, index_col=index_col, dtype={c: str for c in TEXT if c in head.columns},
                     low_memory=False)
    try:
        df.index = pd.to_datetime(df.index, utc=True)
    except (ValueError, TypeError) as e:
        raise ValueError(f"{path}: {INDEX} does not parse as timestamps ({e})") from e
    return df


def load(path: str) -> pd.DataFrame:
    """The frame at `path`, validated and with the optional columns filled."""
    return validate(read(path), path)


def available_lanes(df: pd.DataFrame, lanes: dict) -> tuple[list, dict]:
    """(lanes whose columns carry at least one value, {lane: missing columns})
    for a LANES table shaped as ladder.LANES."""
    ok, missing = [], {}
    for name, spec in lanes.items():
        cols = ["fc_pv_solcast_p50_main_w", "fc_pv_solcast_p10_main_w"] if spec["pv"] == ["mix"] else list(spec["pv"])
        cols = cols + [spec["load"]]
        gone = [c for c in cols if c not in df.columns or df[c].notna().sum() == 0]
        if gone:
            missing[name] = gone
        else:
            ok.append(name)
    return ok, missing


def full_local_days(df: pd.DataFrame, tz: str | None = None, plant: str | None = "measured") -> list:
    """The local calendar days the frame covers from their first to their last
    quarter (DST days at their real step count), restricted to rows whose
    plant column equals `plant` (None = any) and whose required columns are
    all present. Sorted list of datetime.date."""
    tz = tz or _try_plant_tz()
    d = df if plant is None else df[df["plant"] == plant]
    d = d[d[REQUIRED].notna().all(axis=1)]
    if not len(d):
        return []
    local = d.index.tz_convert(tz)
    counts = pd.Series(1, index=local).groupby(local.date).size()
    expected = pd.Series({day: int((pd.Timestamp(day, tz=tz) + pd.Timedelta(days=1) - pd.Timestamp(day, tz=tz)) / STEP)
                          for day in counts.index})
    return sorted(counts.index[counts.to_numpy() == expected.reindex(counts.index).to_numpy()])
