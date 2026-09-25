"""frame_schema: the contract on a two-day synthetic frame written to CSV."""
import numpy as np, pandas as pd, pytest

from backtest import frame_schema


def synthetic_frame(days=2, start="2026-06-10", tz="Europe/Amsterdam"):
    """Two local days of a plausible house: a PV triangle, a flat load, a
    pack that charges at noon and discharges in the evening, prices with an
    evening peak and a negative noon."""
    t0 = pd.Timestamp(start, tz=tz).tz_convert("UTC")
    idx = pd.date_range(t0, periods=days * 96, freq="15min", tz="UTC", name="ts_utc")
    slot = np.arange(len(idx)) % 96
    pv = 8000.0 * np.clip(1 - np.abs(slot - 48) / 32, 0, None)
    load = 600.0 + 200.0 * (slot >= 72)
    batt = np.where((40 <= slot) & (slot < 56), -3000.0, np.where((72 <= slot) & (slot < 88), 2500.0, 0.0))
    grid = load - pv - batt
    day_kwh = np.cumsum(-batt.reshape(days, 96), axis=1).reshape(-1) * 0.25 / 1000.0   # the pack's day, reset at midnight
    soc = 50.0 + day_kwh / 48.2 * 100.0
    price = 0.08 + 0.15 * ((68 <= slot) & (slot < 84)) - 0.10 * ((44 <= slot) & (slot < 52))
    return pd.DataFrame({"load_w": load, "pv_pot_main_w": pv, "pv_main_w": pv, "grid_w": grid,
                         "batt_dc_w": batt, "soc_pct": soc, "da_eur_kwh": price}, index=idx)


def test_load_fills_optional_columns_and_keeps_extras(tmp_path):
    df = synthetic_frame()
    df["src_note"] = "x"
    p = tmp_path / "frame.csv"
    df.to_csv(p)
    out = frame_schema.load(str(p))
    assert list(out.columns)[:len(frame_schema.COLUMNS)] == frame_schema.COLUMNS
    assert "src_note" in out.columns
    assert (out["micro_w"] == 0.0).all() and (out["plant"] == "measured").all() and not out["suspect"].any()
    assert out["soc_max_pct"].isna().all() and out["fc_pv_om24_main_w"].isna().all()
    assert out.index.tz is not None and out.index.name == "ts_utc"
    # the seven-day load profile is built from load_w when absent: two days give none (fewer than four prior days)
    assert out["fc_load_ma7_w"].isna().all()
    assert out["suspect"].dtype == bool


def test_load_accepts_local_stamps_and_bool_text(tmp_path):
    df = synthetic_frame()
    df.index = df.index.tz_convert("Europe/Amsterdam")
    df["suspect"] = ["True"] * 8 + ["False"] * (len(df) - 8)
    df["plant"] = "modelled"
    p = tmp_path / "frame.csv"
    df.to_csv(p)
    out = frame_schema.load(str(p))
    assert out.index[0] == pd.Timestamp("2026-06-09 22:00", tz="UTC")
    assert out["suspect"].sum() == 8 and (out["plant"] == "modelled").all()


def test_load_names_missing_required_columns(tmp_path):
    df = synthetic_frame().drop(columns=["grid_w", "soc_pct"])
    p = tmp_path / "frame.csv"
    df.to_csv(p)
    with pytest.raises(ValueError, match="grid_w, soc_pct"):
        frame_schema.load(str(p))
    with pytest.raises(FileNotFoundError):
        frame_schema.load(str(tmp_path / "nope.csv"))


def test_load_rejects_a_broken_grid_and_wrong_price_units(tmp_path):
    df = synthetic_frame()
    p = tmp_path / "frame.csv"
    df.drop(df.index[10:14]).to_csv(p)                 # a hole: rows missing instead of NaN
    with pytest.raises(ValueError, match="15-minute grid"):
        frame_schema.load(str(p))
    bad = df.copy(); bad["da_eur_kwh"] = bad["da_eur_kwh"] * 1000.0     # EUR/MWh by mistake
    bad.to_csv(p)
    with pytest.raises(ValueError, match="EUR/MWh"):
        frame_schema.load(str(p))
    bad2 = df.copy(); bad2["plant"] = "old"
    bad2.to_csv(p)
    with pytest.raises(ValueError, match="plant column"):
        frame_schema.load(str(p))


def test_full_local_days_and_available_lanes():
    df = frame_schema.validate(synthetic_frame(days=3))
    days = frame_schema.full_local_days(df, "Europe/Amsterdam")
    assert [d.isoformat() for d in days] == ["2026-06-10", "2026-06-11", "2026-06-12"]
    df.loc[df.index[100], "load_w"] = np.nan          # one hole on the second day
    assert [d.isoformat() for d in frame_schema.full_local_days(df, "Europe/Amsterdam")] == ["2026-06-10", "2026-06-12"]
    lanes = {"P1": {"days": 2, "pv": ["pv_pot_main_w"], "load": "load_w"},
             "F_da": {"days": 2, "pv": ["fc_pv_om24_main_w", "fc_pv_om48_main_w"], "load": "fc_load_ma7_w"},
             "F_mix": {"days": 2, "pv": ["mix"], "load": "fc_load_ma7_w"}}
    ok, missing = frame_schema.available_lanes(df, lanes)
    assert ok == ["P1"]
    assert missing["F_da"] == ["fc_pv_om24_main_w", "fc_pv_om48_main_w", "fc_load_ma7_w"]
    assert "fc_pv_solcast_p50_main_w" in missing["F_mix"]
