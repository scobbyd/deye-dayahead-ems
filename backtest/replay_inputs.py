"""The closed replay's inputs: what ha/pyscript/emhass_shadow.py::_plan_day reads
from HA, rebuilt from the backtest frame so emhasscore.planning.run_plan sees
the same shapes offline.

Frame: one CSV in the frame_schema contract (data/frame.csv by default: the
measured plant and load, the curtailment mask, the two Open-Meteo previous-run
PV lanes). On the reference plant it was the supplier's quarter-hour file
joined with the frame's forecast lanes; a `net_result_eur` column (the
supplier's own settlement per quarter) is read when present and is optional.
All quarter starts in UTC. A local day is 96 quarters in July to September 2026.

Rules (spec 2026-09-11-emhass-closed-replay-design.md):
  actuals     frame quarters folded through emhasscore.series.fifteen_min_series
              with one synthetic 5-minute row per quarter, so the current day's
              future steps hold the last value exactly as the live wrapper's
              _actuals_15min(today, slot) does.
  pv_peak_w   the potential lane plus the Growatt; repair.pv_potential accepts
              and ignores it, as live.
  load_days   the 7 complete local days before today, frame load_w, 96 values.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from emhasscore.grid import expected_steps
from emhasscore.planning import load_exact_key
from emhasscore.series import LOAD_REF_DAYS, fifteen_min_series

from emhasscore.plant import PLANT

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
BT_DATA = os.path.join(ROOT, "data")                       # your frame and caches, gitignored
FRAME_CSV = os.path.join(BT_DATA, "frame.csv")
OM_PREV_CSV = os.path.join(BT_DATA, "om_prev.csv")
KNMI_CSV = os.path.join(BT_DATA, "knmi380_hourly.csv")     # hourly station wind (wind_ms), optional
MIRROR = os.path.join(BT_DATA, "emhass_plans")              # a copy of /config/emhass/plans, for the live knobs
REPLAY_DIR = os.path.join(ROOT, "runs", "replay")

TZ = str(PLANT["site"]["timezone"])
# The plant's tariff (plant.json; on the reference the live helpers
# input_number.emhass_supplier_fee_eur_kwh and emhass_feedin_fee_eur_kwh,
# 0,019 each; 2,3 ct per kWh each way after BTW, 4,6 ct between buy and
# sell). The handoff said all zeros; ruling 2026-09-12: no spread lets the LP
# cycle energy through the grid for free.
TARIFF = {k: float(PLANT["tariff"][k]) for k in ("energy_tax", "supplier_fee", "btw_pct", "feedin_fee")}
FRAME_COLS = ["pv_main_w", "micro_w", "load_w", "pv_pot_main_w", "soc_pct", "suspect", "grid_w"]
OPTIONAL_COLS = ["net_result_eur"]                         # a supplier's own settlement per quarter, when the frame has it
LANE_COLS = ["fc_pv_om24_main_w", "fc_pv_om48_main_w"]
INPUT_COLS = ["pv_main_w", "micro_w", "load_w", "pv_pot_main_w"]


def frame_path(name: str | None) -> str:
    """A run's frame file: None or '' is data/frame.csv, a bare name lives in
    the data dir (so a run's meta.json carries the name, not a host path), an
    absolute path is taken as is."""
    if not name:
        return FRAME_CSV
    return name if os.path.isabs(name) else os.path.join(BT_DATA, name)


def load_frame(frame_csv: str = FRAME_CSV, frames_csv: str | None = None) -> pd.DataFrame:
    """The replay frame from one CSV in the frame_schema contract, UTC quarter
    index named ts_utc: the measured columns, the Open-Meteo lanes and, when
    present, the supplier's net_result_eur. `frames_csv` joins the lanes from
    a second frame file when the first has none (the reference layout)."""
    from backtest import frame_schema
    b = frame_schema.load(frame_csv)
    if frames_csv:
        f = pd.read_csv(frames_csv, index_col=0, parse_dates=True, usecols=["ts_utc"] + LANE_COLS)
        f.index = pd.to_datetime(f.index, utc=True)
        b = b.drop(columns=[c for c in LANE_COLS if c in b.columns]).join(f, how="left")
    df = b[FRAME_COLS + [c for c in OPTIONAL_COLS if c in b.columns] + LANE_COLS]
    df.index.name = "ts_utc"
    # A hole in the plant record (on the reference: 2026-02-15 00:00 to 15:45
    # local, 64 quarters with no PV source at all) is read as dark rather than
    # aborting a walk that crosses it; the load is present.
    hole = df["pv_main_w"].isna()
    if hole.any():
        df.loc[hole, "pv_main_w"] = 0.0
        df.loc[hole & df["pv_pot_main_w"].isna(), "pv_pot_main_w"] = 0.0
        df.attrs["pv_hole_quarters"] = int(hole.sum())
    return df


PV_COLS = ["pv_main_w", "micro_w", "pv_pot_main_w"] + LANE_COLS


def scale_pv(df: pd.DataFrame, scale: float) -> pd.DataFrame:
    """The frame with a smaller (or no) plant: every PV column, measured,
    potential, must-take Growatt and the forecast lanes alike, times `scale`,
    so the controller forecasts the small plant and the settlement meters it.
    1.0 returns the frame untouched."""
    if float(scale) == 1.0:
        return df
    out = df.copy()
    for c in PV_COLS:
        if c in out.columns:
            out[c] = (out[c] * float(scale)).round(1)
    return out


def day_bounds(day: date, tz: str = TZ) -> tuple[datetime, datetime]:
    """[start, end) of the local day in UTC."""
    a = datetime.combine(day, time(0), tzinfo=ZoneInfo(tz))
    return a.astimezone(timezone.utc), (a + timedelta(days=1)).astimezone(timezone.utc)


def day_frame(df: pd.DataFrame, day: date, tz: str = TZ) -> pd.DataFrame:
    """The local day's quarters. Raises ValueError when the day is short or an
    input column has a NaN: the frame is expected complete over the window."""
    a, b = day_bounds(day, tz)
    out = df.loc[(df.index >= a) & (df.index < b)]
    n = expected_steps(day, tz)
    if len(out) != n:
        raise ValueError(f"frame incomplete on {day}: {len(out)} of {n} quarters")
    bad = out[INPUT_COLS].isna().any()
    if bad.any():
        raise ValueError(f"frame has NaN on {day} in {list(bad[bad].index)}")
    return out


def _stat_rows(idx, values) -> list[dict]:
    """One synthetic recorder row per quarter: start epoch seconds, mean, max."""
    return [{"start": t.timestamp(), "mean": float(v), "max": float(v)} for t, v in zip(idx, values)]


def actuals(df: pd.DataFrame, day: date, tz: str = TZ, gap_upto: int | None = None) -> dict:
    """The wrapper's _actuals_15min shape: pv_w (main + micro), load_w, micro_w,
    curtailed (0/1), pv_peak_w (potential + micro), soc_pct; one float per
    step of the local day. With gap_upto = the current slot, only quarters up
    to and including that slot are given and later steps hold the last value,
    as the live recorder read does at that moment."""
    d = day_frame(df, day, tz)
    if gap_upto is not None:
        d = d.iloc[: gap_upto + 1]
    idx = d.index
    got = {}
    series = (("pv_w", d["pv_main_w"] + d["micro_w"]), ("load_w", d["load_w"]), ("micro_w", d["micro_w"]),
              ("curtailed", d["suspect"].astype(float)), ("soc_pct", d["soc_pct"]))
    for key, vals in series:
        got[key], _ = fifteen_min_series(day, tz, _stat_rows(idx, vals), gap_upto)
    got["pv_peak_w"], _ = fifteen_min_series(day, tz, _stat_rows(idx, d["pv_pot_main_w"] + d["micro_w"]),
                                             gap_upto, "max", "max")
    return got


def load_days(df: pd.DataFrame, today: date, tz: str = TZ, n_days: int = LOAD_REF_DAYS) -> dict:
    """{date: [W per step]} for the n complete local days before today."""
    out = {}
    for k in range(n_days, 0, -1):
        d = today - timedelta(days=k)
        out[d] = [round(float(v), 1) for v in day_frame(df, d, tz)["load_w"]]
    return out


def load_exact_w(df: pd.DataFrame, today: date, n_days: int, tz: str = TZ) -> dict:
    """{planning.load_exact_key: W} for the local days today .. today+n_days-1,
    the load the omniscient walk hands run_plan over the whole box."""
    out = {}
    for k in range(n_days):
        fr = day_frame(df, today + timedelta(days=k), tz)
        for t, v in zip(fr.index, fr["load_w"]):
            out[load_exact_key(t.to_pydatetime())] = round(float(v), 1)
    return out


def _quarter(tick: datetime) -> pd.Timestamp:
    u = tick.astimezone(timezone.utc)
    return pd.Timestamp(u.replace(minute=(u.minute // 15) * 15, second=0, microsecond=0))


def load_now_w(df: pd.DataFrame, tick: datetime) -> float:
    return float(df.loc[_quarter(tick), "load_w"])


def soc_at(df: pd.DataFrame, tick: datetime) -> float:
    v = df.loc[_quarter(tick), "soc_pct"]
    if pd.isna(v):
        raise ValueError(f"no seed SOC in the frame at {tick.isoformat()}; start the walk on a day with soc_pct")
    return float(v)


# ---- PV forecast rows -------------------------------------------------------------
#
# No Solcast forecast as issued exists before 2026-09-02, so the planner's PV
# is the Open-Meteo previous-run lane through the calibrated plant model
# (frames.py): previous_day1 for today and tomorrow, previous_day2 for D+2. The
# lanes are the main array only. The live path splits the site total into the
# curtailable strings and the must-take microinverter with pv_split(total,
# ese, share), micro = ese x share, where "ese" is the first Solcast site (the
# one the microinverter shares). Here the microinverter rides on the first
# string of the same model run: ese_rows = ese_string / (1 - share), so micro
# = ese_string x share / (1 - share) and main + micro = total holds. No P10
# exists: P10 = P50 and the 80:20 mix is the identity.
LANES = {0: "fc_pv_om24_main_w", 1: "fc_pv_om24_main_w", 2: "fc_pv_om48_main_w"}
OM_SUFFIX = {0: "_previous_day1", 1: "_previous_day1", 2: "_previous_day2"}
WIND_FALLBACK_MS = 2.0


def ese_fraction(om_prev_csv: str = OM_PREV_CSV, knmi_csv: str = KNMI_CSV) -> dict:
    """{suffix: hourly UTC Series} = the microinverter site's string's share
    (pvmodel.MICRO_STRING, the first string) of the calibrated main array on
    that Open-Meteo run; 0,5 where the array makes nothing."""
    from backtest import pvmodel
    om = pd.read_csv(om_prev_csv, index_col=0, parse_dates=True)
    om.index = pd.to_datetime(om.index, utc=True)
    if os.path.exists(knmi_csv):
        kn = pd.read_csv(knmi_csv, index_col=0, parse_dates=True)
        kn.index = pd.to_datetime(kn.index, utc=True)
        wind = kn["wind_ms"].reindex(om.index).fillna(WIND_FALLBACK_MS)
    else:
        wind = pd.Series(WIND_FALLBACK_MS, index=om.index)
    out = {}
    for suf in ("_previous_day1", "_previous_day2"):
        irr = pd.DataFrame({"ghi": om[f"shortwave_radiation{suf}"], "dhi": om[f"diffuse_radiation{suf}"],
                            "dni": om[f"direct_normal_irradiance{suf}"]}, index=om.index)
        temp = om[f"temperature_2m{suf}"]
        ok = irr.notna().all(axis=1) & temp.notna()
        irr, temp, w = irr[ok], temp[ok], wind[ok]
        mid = irr.index + pd.Timedelta(minutes=30)
        kw = pvmodel.string_kw(irr, pd.DataFrame({"temp_c": temp, "wind_ms": w}, index=irr.index), mid)
        tot = sum(kw.values())
        frac = (kw[pvmodel.MICRO_STRING] / tot).where(tot > 0.01, 0.5)
        out[suf] = frac.reindex(om.index).fillna(0.5)
    return out


def _half_hours(d: pd.DataFrame, col: str) -> pd.Series:
    """30-minute means of a W column, kW, indexed by half-hour start (UTC)."""
    if d[col].isna().any():
        raise ValueError(f"frame lane {col} has NaN on {d.index[0]:%Y-%m-%d}")
    kw = d[col].astype(float).clip(lower=0.0) / 1000.0
    return kw.groupby(kw.index.floor("30min")).mean()


def _rows(kw: pd.Series) -> list[dict]:
    return [{"period_start": t.isoformat(), "pv_estimate": round(float(v), 4),
             "pv_estimate10": round(float(v), 4), "pv_estimate90": round(float(v), 4)}
            for t, v in kw.items()]


def pv_rows(df: pd.DataFrame, frac: dict, day: date, k: int, share: float, tz: str = TZ,
            exact: bool = False) -> tuple[list, list]:
    """(total rows, ESE rows) in detailedForecast shape for local `day` seen at
    horizon offset k (0 today, 1 tomorrow, 2 day3). `exact` is the knowledge
    walk: the day's potential main array and its real Growatt instead of the
    Open-Meteo lane, with ESE rows = micro / share so pv_split returns exactly
    those two."""
    d = day_frame(df, day, tz)
    if exact:
        main = _half_hours(d, "pv_pot_main_w")
        micro = _half_hours(d, "micro_w")
        return _rows(main + micro), _rows(micro / share if share > 0 else micro * 0.0)
    main = _half_hours(d, LANES[k])
    f = frac[OM_SUFFIX[k]].reindex(main.index, method="ffill").fillna(0.5)
    ese_string = main * f
    micro = ese_string * share / (1.0 - share)
    return _rows(main + micro), _rows(ese_string / (1.0 - share))


def solcast_inputs(df: pd.DataFrame, frac: dict, today: date, share: float, tz: str = TZ,
                   exact_days: int = 0) -> dict:
    """The four Solcast-shaped inp keys the wrapper passes; the ESE span goes
    in one list (pv_series merges the three). exact_days = how many horizon
    days from today take the exact plant instead of the forecast lane."""
    out, ese = {}, []
    for k, key in ((0, "solcast_today"), (1, "solcast_tomorrow"), (2, "solcast_day3")):
        tot, e = pv_rows(df, frac, today + timedelta(days=k), k, share, tz, exact=k < exact_days)
        out[key] = tot
        ese += e
    out["solcast_ese_today"] = ese
    return out


# Knowledge walks (ruling 2026-09-13): the ladder's rungs are scored open-loop
# from two solves a day, so the rolling closed-loop controller can beat them.
# These give the same knowledge to the walk itself: PV exact (the potential,
# what the virtual pack can take) for today or the whole box; load exact for
# today through the profile (today's own shape as the reference days) and,
# for omni2, over the whole box through run_plan's load_exact_w override
# (the core's profile repeats one wall-clock shape per horizon day, so the
# ladder's omni2 rung, exact through D+2, needs the per-step key). Prices:
# use the foresight fill with them, they are perfect-knowledge lanes.
KNOWLEDGE = {
    "actual": {"pv_exact_days": 0, "load_exact": False, "load_exact_days": 0},
    "hindsight": {"pv_exact_days": 1, "load_exact": True, "load_exact_days": 0},
    "hindsight_pv": {"pv_exact_days": 1, "load_exact": False, "load_exact_days": 0},
    "hindsight_load": {"pv_exact_days": 0, "load_exact": True, "load_exact_days": 0},
    "omni2": {"pv_exact_days": 3, "load_exact": True, "load_exact_days": 3},
}


# ---- knobs and ticks --------------------------------------------------------------

def live_knobs(mirror: str = MIRROR) -> dict:
    """The knobs of the newest plan doc in the mirror of /config/emhass/plans
    (the live helper values), else the core defaults."""
    from emhasscore.archive import load_plan, plan_heads
    from emhasscore.objective import knobs
    if os.path.isdir(mirror):
        for path, _head in plan_heads(mirror):
            doc = load_plan(path)
            if doc.get("knobs"):
                return knobs(doc["knobs"])
    return knobs({})


def ticks(start_day: date, end_day: date, tz: str = TZ, step_min: int = 30) -> list[datetime]:
    """The live solve schedule: :13 and :43 every hour plus 13:00:04 (the Nord
    Pool publish solve), from 23:43 of the day before start_day to 00:13 of
    the day after end_day, local time.

    `step_min` is the spacing. 30 is the live cadence and the default. 60 keeps
    only the :43 solve of each hour (and the 13:00:04 one, whose trigger 12:43
    survives), which halves the solve count. A walk at 60 is NOT comparable
    with a walk at 30: the actual lane's settled SOC drifts off its plan
    between solves, so a coarser loop corrects that drift later. Exact-knowledge
    lanes barely move. The run's step is frozen in meta.json as tick_min."""
    if step_min not in (30, 60):
        raise ValueError(f"step_min {step_min} not in (30, 60)")
    z = ZoneInfo(tz)
    t = datetime.combine(start_day - timedelta(days=1), time(23, 43), tzinfo=z)
    stop = datetime.combine(end_day + timedelta(days=1), time(0, 13), tzinfo=z)
    out, seen = [], set()
    while t <= stop:
        for tick in ((t, t.replace(hour=13, minute=0, second=4)) if (t.hour, t.minute) == (12, 43) else (t,)):
            # Wall-clock arithmetic across a DST change yields times the clock
            # never showed (02:13 on the spring day) and repeats (the autumn
            # hour). HA's time_pattern fires on the real clock, so a tick whose
            # wall time does not round-trip through UTC is dropped, and two ticks
            # on the same instant collapse to one.
            u = tick.astimezone(timezone.utc)
            if u.astimezone(z).replace(tzinfo=None) != tick.replace(tzinfo=None) or u in seen:
                continue
            seen.add(u)
            out.append(tick)
        t += timedelta(minutes=step_min)
    return out
