"""frames: the generic builders on synthetic series, and the reference frame's
invariants when a frame is present under data/ (skipped otherwise)."""
import os
import numpy as np, pandas as pd, pytest

pytest.importorskip("pvlib")
from backtest import frames, frame_schema, pvmodel

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
CSV = os.path.join(DATA, "frames_2026_q15.csv")


@pytest.fixture(scope="module")
def df():
    if not os.path.exists(CSV):
        pytest.skip(f"no reference frame at {CSV} (your own frame goes under data/)")
    return frames.load(CSV)


@pytest.fixture(scope="module")
def measured(df):
    return df[df["plant"] == "measured"]


# ---- the reference frame, when present

def test_schema_and_grid(df):
    assert list(df.columns)[:len(frame_schema.COLUMNS)] == frame_schema.COLUMNS
    assert df.index.tz is not None and (df.index[1] - df.index[0]) == pd.Timedelta(minutes=15)
    assert (df.index[1:] - df.index[:-1]).unique().tolist() == [pd.Timedelta(minutes=15)]


def test_plant_column_is_contiguous(df):
    """The modelled rows, when any, all precede the measured ones."""
    assert set(df["plant"]) <= {"modelled", "measured"}
    if "modelled" in set(df["plant"]):
        first_measured = df.index[df["plant"] == "measured"].min()
        assert (df.loc[df.index < first_measured, "plant"] == "modelled").all()
        assert (df.loc[df.index >= first_measured, "plant"] == "measured").all()


def test_core_columns_complete_on_the_measured_plant(measured):
    core = ["pv_pot_main_w", "pv_main_w", "micro_w", "load_w", "grid_w", "batt_dc_w", "soc_pct", "da_eur_kwh"]
    assert measured[core].isna().mean().max() < 0.01, measured[core].isna().mean()


def test_suspect_is_stamped_per_hour(df):
    sus = df[df["suspect"]]
    if not len(sus):
        pytest.skip("no suspect quarter in this frame")
    assert (sus["soc_max_pct"].dropna() >= frames.SOC_SUSPECT_PCT).all()
    # stamped on all four quarters of the hour
    assert (df["suspect"].groupby(df.index.floor("h")).nunique() == 1).all()


def test_potential_never_below_measured(measured):
    # the shaped hours carry the model's intra-hour shape on top of the hourly energy, so the
    # invariant lives at the hour: potential energy never below measured energy
    h = measured[["pv_pot_main_w", "pv_main_w"]].resample("1h").mean().dropna()
    assert (h["pv_pot_main_w"] >= h["pv_main_w"] - 1.0).all()


def test_potential_equals_measurement_on_clean_steps(measured):
    clean = measured[~measured["suspect"] & measured["pv_main_w"].notna()]
    assert (clean["pv_pot_main_w"] == clean["pv_main_w"]).all()


def test_potential_never_above_capacity_on_shaped_hours(measured):
    sus = measured[measured["suspect"]]
    assert (sus["pv_pot_main_w"] <= frames.ARRAY_CAP_W + 0.05).all()


def test_ma7_present_and_in_range(measured):
    # the profile is present on every step and sits in a plausible household load range;
    # the causality property itself is test_ma7_shift_is_causal, on the synthetic series
    d = measured.iloc[20 * 96:21 * 96]
    assert d["fc_load_ma7_w"].notna().all()
    assert 100 < d["fc_load_ma7_w"].mean() < 3000


def test_micro_within_bounds(df):
    # the microinverter is a 3 kW unit on the reference (ruling 2026-09-11), whichever witness the quarter came from
    assert df["micro_w"].between(0.0, frames.MICRO_MAX_W).all()


def test_pvmodel_from_plant():
    """The strings and their scales come from PLANT; the array's capacity is
    the sum of scale x kWp. On the reference plant the fitted scales were
    0,5286 and 0,8094 on 9,8 kWp strings (13.100 W)."""
    from emhasscore.plant import PLANT
    assert set(pvmodel.STRINGS) == set(PLANT["pv"]["strings"])
    assert pvmodel.MICRO_STRING == list(PLANT["pv"]["strings"])[0]
    cap = sum(pvmodel.SCALE[n] * pvmodel.STRINGS[n]["kwp"] for n in pvmodel.STRINGS) * 1000.0
    assert abs(cap - frames.ARRAY_CAP_W) < 1.0 and frames.ARRAY_CAP_W == pvmodel.ARRAY_CAP_W
    assert (pvmodel.LAT, pvmodel.LON) == (PLANT["site"]["latitude"], PLANT["site"]["longitude"])


# ---- synthetic tests on the pure pieces of frames.py

def _synthetic_load(days=20, start="2026-06-01"):
    idx = pd.date_range(start, periods=days * 96, freq="15min", tz="UTC")
    tod = (np.arange(len(idx)) % 96).astype(float)
    return pd.Series(200.0 + 10.0 * tod, index=idx)


def test_ma7_shift_is_causal():
    # adding 5 kW to day D and every later day must not touch D's profile, nor may
    # adding it to D-8 and earlier; adding it to a majority of D-7 .. D-1 (here
    # D-4 .. D-1) moves the median by exactly 5 kW
    load = _synthetic_load()
    base = frames.load_ma7(load)
    day = pd.Timestamp("2026-06-15", tz="Europe/Amsterdam")
    d_slice = slice(day, day + pd.Timedelta(days=1) - pd.Timedelta(minutes=1))
    later = load.copy()
    later[later.index >= day] += 5000.0
    assert np.allclose(frames.load_ma7(later)[d_slice], base[d_slice], equal_nan=True)
    earlier = load.copy()
    earlier[earlier.index < day - pd.Timedelta(days=7)] += 5000.0
    assert np.allclose(frames.load_ma7(earlier)[d_slice], base[d_slice], equal_nan=True)
    window = load.copy()
    window[(window.index >= day - pd.Timedelta(days=4)) & (window.index < day)] += 5000.0
    assert np.allclose(frames.load_ma7(window)[d_slice], base[d_slice] + 5000.0, equal_nan=True)


def test_ma7_min_four_of_seven():
    load = _synthetic_load()
    day = pd.Timestamp("2026-06-15", tz="Europe/Amsterdam")
    slot = day + pd.Timedelta(hours=10)
    prev_slots = [slot - pd.Timedelta(days=k) for k in range(1, 8)]
    three = load.copy(); three[prev_slots[:4]] = np.nan          # 3 of 7 left
    assert np.isnan(frames.load_ma7(three)[slot])
    four = load.copy(); four[prev_slots[:3]] = np.nan            # 4 of 7 left
    assert not np.isnan(frames.load_ma7(four)[slot])


def test_frame_end_on_dst_days():
    # calendar days, not 24 h: the 23-hour spring day and the 25-hour autumn day end at local midnight
    assert frames.frame_end_utc("2026-03-29") == pd.Timestamp("2026-03-29 21:45", tz="UTC")
    assert frames.frame_end_utc("2026-10-25") == pd.Timestamp("2026-10-25 22:45", tz="UTC")
    assert frames.frame_end_utc("2026-09-08") == pd.Timestamp("2026-09-08 21:45", tz="UTC")


def test_hold_is_bounded():
    # a missing source hour stays missing and nothing is held past the source end
    src = pd.Series([1.0, np.nan, 3.0], index=pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC"))
    grid = pd.date_range("2026-01-01", "2026-01-01 04:00", freq="15min", tz="UTC")
    h = frames._hold(src, grid, "1h")
    assert h["2026-01-01 00:00":"2026-01-01 00:45"].eq(1.0).all()
    assert h["2026-01-01 01:00":"2026-01-01 01:45"].isna().all()
    assert h["2026-01-01 02:00":"2026-01-01 02:45"].eq(3.0).all()
    assert h["2026-01-01 03:00":].isna().all()
    src30 = pd.Series([2.0], index=pd.DatetimeIndex(["2026-01-01 00:00"], tz="UTC"))
    h30 = frames._hold(src30, grid, "30min")
    assert h30["2026-01-01 00:00":"2026-01-01 00:15"].eq(2.0).all() and h30["2026-01-01 00:30":].isna().all()


# ---- ruling 2026-09-11: corrupt microinverter reads are re-inferred from their neighbours, never clipped to a ceiling

def _hourly(values, start="2026-04-13 10:00"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="1h", tz="UTC"), dtype=float)


def test_micro_corrupt_hours_interpolate_from_neighbours():
    # -9 kW and +18 kW between sound hours come out as their neighbours' linear
    # interpolation; the sound hours are untouched
    s = _hourly([1000.0, -9000.0, 1400.0, 2000.0, 18000.0, 18000.0, 800.0, 0.0])
    out = frames.micro_hourly(s, deye_present=pd.Series(True, index=s.index))
    assert np.allclose(out.to_numpy(), [1000.0, 1200.0, 1400.0, 2000.0, 1600.0, 1200.0, 800.0, 0.0])
    # a sound hour at 2.900 W is under the 3 kW ceiling and stays as measured
    s2 = _hourly([2500.0, 2900.0, 2600.0])
    assert frames.micro_hourly(s2, pd.Series(True, index=s2.index)).tolist() == [2500.0, 2900.0, 2600.0]


def test_micro_night_standby_is_zero_not_corrupt():
    # -5 W at night is the unit's standby draw: 0, and not a value to interpolate over
    s = _hourly([0.0, -5.0, -10.0, 0.0, 30.0])
    out = frames.micro_hourly(s, pd.Series(True, index=s.index))
    assert out.tolist() == [0.0, 0.0, 0.0, 0.0, 30.0]


def test_micro_absent_hours_are_zero_and_count_as_neighbours():
    # the absent family (NaN while the hybrid reports) is 0 by the gap rule, and a corrupt
    # hour on the edge of that window ramps to the window's 0 rather than across it;
    # NaN while the hybrid is absent too stays NaN
    s = _hourly([489.0, -115.0, -59.0, np.nan, np.nan, np.nan])
    deye = pd.Series([True, True, True, True, True, False], index=s.index)
    out = frames.micro_hourly(s, deye)
    assert np.allclose(out.iloc[:5].to_numpy(), [489.0, 326.0, 163.0, 0.0, 0.0])
    assert np.isnan(out.iloc[5])
    # a corrupt hour with no sound hour on one side stays NaN, never a clipped ceiling
    s3 = _hourly([np.nan, 5000.0, 1000.0])
    out3 = frames.micro_hourly(s3, pd.Series([False, True, True], index=s3.index))
    assert np.isnan(out3.iloc[1]) and out3.iloc[2] == 1000.0


# ---- ruling 2026-09-11: the potential is built in the frame from the irradiance model

def _q15(values, start="2026-06-10 10:00"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="15min", tz="UTC"), dtype=float)


def _soc(values, start="2026-06-10 10:00"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="1h", tz="UTC"), dtype=float)


def test_potential_three_regimes():
    """Hour 10: clean, the potential IS the measurement quarter for quarter.
    Hour 11: suspect with the model above the measurement, the hour takes the
    model's energy in the model's shape. Hour 12: suspect with the measurement
    above the model, the measured energy in the model's shape."""
    meas = _q15([1000, 1200, 900, 1100,   3000, 3000, 3000, 3000,   6000, 6000, 6000, 6000])
    model = _q15([800, 800, 800, 800,     3600, 4400, 4000, 4000,   4000, 5000, 6000, 5000])
    soc = _soc([80.0, 96.0, 95.0])
    pot, suspect, src = frames.potential(meas, soc, model)
    assert suspect.tolist() == [False] * 4 + [True] * 8
    assert pot.iloc[:4].tolist() == meas.iloc[:4].tolist() and (src.iloc[:4] == "measured").all()
    assert np.allclose(pot.iloc[4:8].to_numpy(), [3600, 4400, 4000, 4000]) and (src.iloc[4:8] == "shaped").all()
    assert np.allclose(pot.iloc[8:12].to_numpy(), np.array([4000, 5000, 6000, 5000]) * 1.2) and (src.iloc[8:12] == "shaped").all()
    # the hour never sits below the measurement, single quarters may
    assert pot.iloc[8:12].mean() == pytest.approx(6000.0) and pot.iloc[8] < meas.iloc[8]


def test_potential_ratio_cap_goes_flat():
    """A measured hour more than twice the model's is not lent the shape: flat
    at the measured energy. A model hour under 50 W has no shape either."""
    meas = _q15([5000] * 4 + [300] * 4)
    model = _q15([1000, 2000, 3000, 2000,   10, 20, 30, 20])
    soc = _soc([97.0, 97.0])
    pot, suspect, src = frames.potential(meas, soc, model)
    assert pot.iloc[:4].tolist() == [5000.0] * 4 and (src.iloc[:4] == "flat").all()
    assert pot.iloc[4:].tolist() == [300.0] * 4 and (src.iloc[4:] == "flat").all()
    # at a ratio of exactly 2 the shape is still lent
    meas2 = _q15([4000] * 4)
    pot2, _, src2 = frames.potential(meas2, _soc([97.0]), model.iloc[:4])
    assert np.allclose(pot2.to_numpy(), [2000, 4000, 6000, 4000]) and (src2 == "shaped").all()


def test_potential_capacity_clip_keeps_the_hour():
    """A shaped quarter above the array's capacity is clipped to it and the
    energy it lost goes to the hour's other quarters in proportion to the
    model, so no quarter exceeds the cap and the hour keeps max(measured,
    model). Scaled to ARRAY_CAP_W so the test holds on any plant."""
    cap = frames.ARRAY_CAP_W
    meas = _q15([cap * 12000 / 13100] * 4)
    model = _q15([cap * 4000 / 13100, cap * 8000 / 13100, cap * 6000 / 13100, cap * 6000 / 13100])   # x2 shape
    soc = _soc([98.0])
    pot, _, src = frames.potential(meas, soc, model)
    assert (src == "shaped_clip").all()
    assert pot.max() == pytest.approx(cap)
    assert pot.mean() == pytest.approx(meas.iloc[0])
    assert pot.iloc[1] == pytest.approx(cap)
    # the other three carry the energy the clip removed, in the model's proportions 4:6:6
    lost = meas.sum() - cap
    assert np.allclose(pot.iloc[[0, 2, 3]].to_numpy(), np.array([4, 6, 6]) / 16 * lost)
    assert (pot <= cap + 1e-6).all()


def test_potential_before_the_plant_is_the_model_floored_at_the_estimate():
    meas = _q15([500, 0, 0, 900])                      # an estimate of the old plant, junk at night included
    model = _q15([0, 0, 2000, 400])
    soc = _soc([np.nan])
    pot, suspect, src = frames.potential(meas, soc, model, plant_from=pd.Timestamp("2026-06-11", tz="UTC"))
    assert pot.tolist() == [500.0, 0.0, 2000.0, 900.0] and (src == "model").all() and not suspect.any()


def test_potential_nan_soc_is_not_suspect_and_nan_measurement_stays_nan():
    meas = _q15([np.nan] * 4 + [100.0] * 4)
    model = _q15([500.0] * 8)
    soc = _soc([np.nan, np.nan])
    pot, suspect, src = frames.potential(meas, soc, model)
    assert not suspect.any() and pot.iloc[:4].isna().all() and (src.iloc[:4] == "").all()
    assert pot.iloc[4:].tolist() == [100.0] * 4


def test_redistribute_clipped():
    q = frames._redistribute_clipped([8000.0, 16000.0, 12000.0, 12000.0], 13100.0)
    assert q.sum() == pytest.approx(48000.0) and q.max() <= 13100.0 + 1e-9
    # every quarter above the cap: the hour sits at the cap
    q2 = frames._redistribute_clipped([14000.0] * 4, 13100.0)
    assert q2.tolist() == [13100.0] * 4
    # nothing to do
    assert frames._redistribute_clipped([1.0, 2.0], 5.0).tolist() == [1.0, 2.0]


def test_solcast_lanes_split_the_first_site():
    """The first site carries the microinverter: main = first x (1 - share) +
    the rest, micro = first x share, 30-minute kW held onto the quarters as W."""
    idx = pd.date_range("2026-06-10 10:00", periods=2, freq="30min", tz="UTC")
    sites = {"a": pd.DataFrame({"p50": [2.0, 4.0], "p10": [1.0, 2.0]}, index=idx),
             "b": pd.DataFrame({"p50": [1.0, 1.0], "p10": [0.5, 0.5]}, index=idx)}
    grid = pd.date_range("2026-06-10 10:00", periods=4, freq="15min", tz="UTC")
    out = frames.solcast_lanes(sites, grid, {"A": "a", "B": "b"}, share=0.25)
    assert out["fc_pv_solcast_p50_main_w"].tolist() == [2500.0, 2500.0, 4000.0, 4000.0]
    assert out["fc_pv_solcast_p10_main_w"].tolist() == [1250.0, 1250.0, 2000.0, 2000.0]
    assert out["fc_micro_solcast_w"].tolist() == [500.0, 500.0, 1000.0, 1000.0]
    with pytest.raises(KeyError):
        frames.solcast_lanes(sites, grid, {"A": "xxxx-xxxx-xxxx-xxxx"})
