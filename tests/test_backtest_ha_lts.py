"""ha_lts: the statistics puller onto the frame contract, with the WebSocket
replaced by a fake fetch (no Home Assistant needed)."""
import os
import numpy as np, pandas as pd, pytest

from backtest import frame_schema, ha_lts


def _rows(start, periods, step_min, value, vmax=None):
    t = pd.date_range(start, periods=periods, freq=f"{step_min}min", tz="UTC")
    return [{"start": int(ts.value // 10**6), "mean": value + i * 0.0, "max": (vmax if vmax is not None else value)}
            for i, ts in enumerate(t)]


def fake_fetch(ids, start_utc, end_utc, period, types=("mean", "max")):
    """5-minute rows for the load and PV, hourly for the rest, none for the micro."""
    start = pd.Timestamp(start_utc)
    n_h = int((pd.Timestamp(end_utc) - start) / pd.Timedelta(hours=1))
    out = {}
    for sid in ids:
        if period == "5minute" and sid in ("sensor.load", "sensor.pv"):
            out[sid] = _rows(start, n_h * 12, 5, 500.0 if sid == "sensor.load" else 2000.0)
        elif period == "hour" and sid in ("sensor.imp", "sensor.exp", "sensor.batt", "sensor.soc", "sensor.price"):
            val = {"sensor.imp": 1.2, "sensor.exp": 0.5, "sensor.batt": -800.0, "sensor.soc": 55.0, "sensor.price": 0.12}[sid]
            out[sid] = _rows(start, n_h, 60, val, vmax=60.0 if sid == "sensor.soc" else None)
    return out


ENT = {"load_w": "sensor.load", "pv_main_w": "sensor.pv", "micro_w": "sensor.micro",
       "grid_import_w": {"id": "sensor.imp", "scale": 1000.0}, "grid_export_w": {"id": "sensor.exp", "scale": 1000.0},
       "batt_dc_w": "sensor.batt", "soc_pct": "sensor.soc", "da_eur_kwh": "sensor.price"}


def test_build_from_ha_lands_in_the_contract(tmp_path):
    out = tmp_path / "frame.csv"
    df = ha_lts.build_from_ha("2026-06-10", "2026-06-11", ENT, str(out), fetch=fake_fetch, tz="Europe/Amsterdam")
    assert os.path.exists(out) and len(df) == 2 * 96
    assert df.index[0] == pd.Timestamp("2026-06-09 22:00", tz="UTC")
    assert (df["load_w"] == 500.0).all() and (df["pv_main_w"] == 2000.0).all() and (df["pv_pot_main_w"] == df["pv_main_w"]).all()
    assert (df["micro_w"] == 0.0).all()                                   # absent entity: 0
    assert np.allclose(df["grid_w"], 700.0)                                # 1,2 kW in minus 0,5 kW out, kW -> W
    assert (df["batt_dc_w"] == -800.0).all() and (df["soc_pct"] == 55.0).all() and (df["soc_max_pct"] == 60.0).all()
    assert (df["da_eur_kwh"] == 0.12).all() and not df["suspect"].any() and (df["plant"] == "measured").all()
    again = frame_schema.load(str(out))
    assert list(again.columns)[:len(frame_schema.COLUMNS)] == frame_schema.COLUMNS


def test_build_from_ha_prices_from_csv_and_missing_keys(tmp_path):
    ent = {k: v for k, v in ENT.items() if k != "da_eur_kwh"}
    prices = tmp_path / "da.csv"
    idx = pd.date_range("2026-06-09 22:00", periods=2 * 96, freq="15min", tz="UTC")
    pd.DataFrame({"eur_mwh": 80.0}, index=idx).to_csv(prices, index_label="ts")
    df = ha_lts.build_from_ha("2026-06-10", "2026-06-11", ent, None, str(prices), fetch=fake_fetch, tz="Europe/Amsterdam")
    assert np.allclose(df["da_eur_kwh"], 0.08)
    with pytest.raises(KeyError, match="grid_w"):
        ha_lts.build_from_ha("2026-06-10", "2026-06-10", {k: v for k, v in ent.items() if not k.startswith("grid")},
                             fetch=fake_fetch, tz="Europe/Amsterdam")


def test_settings_read_dotenv(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("HA_URL=https://ha.example.org\nHA_LLAT=tok\n")
    monkeypatch.setattr(ha_lts, "ENV_PATH", str(env))
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_LLAT", raising=False)
    assert ha_lts.settings() == ("wss://ha.example.org/api/websocket", "tok")
    monkeypatch.setenv("HA_URL", "http://ha.example.org:8000")
    assert ha_lts.settings()[0] == "ws://ha.example.org:8000/api/websocket"
    monkeypatch.setattr(ha_lts, "ENV_PATH", str(tmp_path / "none"))
    monkeypatch.delenv("HA_URL")
    with pytest.raises(SystemExit):
        ha_lts.settings()
