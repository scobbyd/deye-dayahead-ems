#!/usr/bin/env python3
"""Home Assistant long-term statistics over the WebSocket API, and the frame
built from them: the one documented public path to a frame CSV.

Auth and address: HA_URL (the http address of your Home Assistant, port included) and HA_LLAT (a
long-lived access token) from the environment or from a .env file in the
repo root (KEY=value lines). Nothing else is read.

    python -m backtest.ha_lts list [substring]                   statistic ids
    python -m backtest.ha_lts hourly <statistic_id> <start> <end>  hourly rows as JSON lines
    python -m backtest.ha_lts build --start 2026-06-01 --end 2026-06-30 [--out data/frame.csv]
                                    [--prices data/da_prices_q15.csv] [--entities my_entities.json]

build_from_ha pulls the entities in ENTITIES (rename to yours: the map below
is the reference plant's, an example) as statistics_during_period. The
recorder keeps 5-minute statistics for its short-term window (about ten
days) and hourly long-term statistics for ever, so the puller asks for
5minute first and falls back to hour per entity; 5-minute means are
averaged onto the quarter, hourly means are held flat over its four
quarters (frames._hold). The columns land in the frame_schema contract:

  load_w         mean of the load entity (W)
  pv_main_w      mean of the string PV entity (W)
  pv_pot_main_w  = pv_main_w (no potential model here; frames.potential adds one)
  micro_w        mean of the microinverter entity (W), 0 when the map has none
  grid_w         import minus export (W, + = import); one net entity is fine too
  batt_dc_w      mean of the battery power entity (W, + = discharge)
  soc_pct        mean of the SOC entity, soc_max_pct its hourly maximum
  da_eur_kwh     the price: a price entity in the map (EUR/kWh mean) or a
                 CSV (ts,eur_mwh per quarter, the energy-charts layout) via
                 --prices; NaN with a warning when neither is given
  suspect        False (no curtailment witness); plant "measured"

Each map value is {"id": entity_id, "scale": factor to W} or a bare entity
id at scale 1. Sign conventions are frame_schema.py's.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlparse

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
ENV_PATH = os.path.join(ROOT, ".env")

# the reference plant's statistic ids: rename to yours (or pass --entities a JSON file in this shape)
ENTITIES = {
    "load_w": "sensor.inverter_load_ups_power",               # the hybrid's load port, W
    "pv_main_w": "sensor.inverter_pv_power",                  # the hybrid's strings, W
    "micro_w": "sensor.inverter_microinverter_power",         # AC-coupled microinverter, W; drop the key if none
    "grid_import_w": {"id": "sensor.p1reader2_p1_reader_2_power_consumed", "scale": 1000.0},   # kW -> W
    "grid_export_w": {"id": "sensor.p1reader2_p1_reader_2_power_returned", "scale": 1000.0},
    # "grid_w": "sensor.inverter_grid_power",                 # alternative: one signed meter entity, + = import
    "batt_dc_w": "sensor.inverter_battery_power",             # + = discharge
    "soc_pct": "sensor.inverter_battery",
    # "da_eur_kwh": "sensor.nord_pool_nl_current_price",      # optional: a price entity in EUR/kWh
}
SOC_KEY = "soc_pct"


def _dotenv() -> dict:
    out = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def settings() -> tuple[str, str]:
    """(websocket url, token) from HA_URL and HA_LLAT in the environment or .env."""
    env = {**_dotenv(), **{k: v for k, v in os.environ.items() if k in ("HA_URL", "HA_LLAT")}}
    url, tok = env.get("HA_URL"), env.get("HA_LLAT")
    if not url or not tok:
        sys.exit("set HA_URL (http://<your-ha>:<port>) and HA_LLAT (a long-lived access token) in the environment "
                 f"or in {ENV_PATH}")
    u = urlparse(url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}/api/websocket", tok


async def ws_call(commands: list) -> list:
    """Authenticate, run a list of command dicts, return the list of results."""
    import websockets
    ws_url, token = settings()
    results = []
    async with websockets.connect(ws_url, max_size=2**27) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            sys.exit(f"auth failed: {msg}")
        for i, cmd in enumerate(commands, start=1):
            cmd = {"id": i, **cmd}
            await ws.send(json.dumps(cmd))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == i:
                    if not resp.get("success"):
                        sys.exit(f"command failed: {resp}")
                    results.append(resp.get("result"))
                    break
    return results


def _entity(spec) -> tuple[str, float]:
    if isinstance(spec, dict):
        return str(spec["id"]), float(spec.get("scale", 1.0))
    return str(spec), 1.0


def statistics(ids: list, start_utc, end_utc, period: str, types=("mean", "max")) -> dict:
    """{statistic_id: [{start ms, mean, max}, ...]} for one period."""
    (res,) = asyncio.run(ws_call([{
        "type": "recorder/statistics_during_period",
        "start_time": pd.Timestamp(start_utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": pd.Timestamp(end_utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statistic_ids": ids, "period": period, "types": list(types)}]))
    return res or {}


def _series(rows: list, key: str = "mean") -> pd.Series:
    return pd.Series({pd.Timestamp(r["start"], unit="ms", tz="UTC"): r.get(key) for r in rows}, dtype=float).sort_index()


def onto_grid(rows: list, grid: pd.DatetimeIndex, period: str, key: str = "mean") -> pd.Series:
    """Statistic rows onto the 15-minute grid: 5-minute rows averaged per
    quarter, hourly rows held flat over four quarters, never past the source."""
    from .frames import _hold
    s = _series(rows, key)
    if not len(s):
        return pd.Series(float("nan"), index=grid)
    if period == "5minute":
        q = s.groupby(s.index.floor("15min")).mean()
        return q.reindex(grid)
    return _hold(s, grid, "1h")


def pull(entities: dict, start_utc, end_utc, grid: pd.DatetimeIndex, fetch=statistics) -> tuple[pd.DataFrame, dict]:
    """(frame columns on the grid, {key: period used}). Each entity is asked
    for 5-minute statistics first, hourly when the recorder has none over
    the range. `fetch` is statistics(), replaceable in tests."""
    specs = {k: _entity(v) for k, v in entities.items()}
    ids = sorted({sid for sid, _ in specs.values()})
    end_q = pd.Timestamp(end_utc) + pd.Timedelta(hours=1)
    five = fetch(ids, start_utc, end_q, "5minute")
    hourly_ids = [i for i in ids if not five.get(i)]
    hour = fetch(hourly_ids, start_utc, end_q, "hour") if hourly_ids else {}
    out, used = pd.DataFrame(index=grid), {}
    for key, (sid, scale) in specs.items():
        rows, period = (five.get(sid), "5minute") if five.get(sid) else (hour.get(sid, []), "hour")
        used[key] = period if rows else "absent"
        out[key] = onto_grid(rows, grid, period) * scale
        if key == SOC_KEY:
            out["soc_max_pct"] = onto_grid(rows, grid, period, "max") if period == "hour" \
                else onto_grid(rows, grid, period, "max").groupby(grid.floor("h")).transform("max")
    return out, used


def prices_from_csv(path: str, grid: pd.DatetimeIndex) -> pd.Series:
    """da_eur_kwh from a CSV of (ts, eur_mwh) quarter rows, the energy-charts
    cache layout pricefill.load_da reads; a column named eur_kwh is taken as is."""
    t = pd.read_csv(path, index_col=0)
    t.index = pd.to_datetime(t.index, utc=True)
    col = "eur_kwh" if "eur_kwh" in t.columns else t.columns[0]
    s = pd.to_numeric(t[col], errors="coerce")
    if col != "eur_kwh":
        s = s / 1000.0
    return s.reindex(grid)


def build_from_ha(start, end, entities: dict | None = None, out_csv: str | None = None,
                  prices_csv: str | None = None, tz: str | None = None, fetch=statistics) -> pd.DataFrame:
    """The frame from 00:00 local on `start` through the end of local day
    `end` (ISO days), written to `out_csv` when given. Returns the validated
    frame (frame_schema) so a bad pull fails here, not in the ladder."""
    from . import frame_schema
    from .frames import TZ as PLANT_TZ, frame_end_utc, write
    tz = tz or PLANT_TZ
    entities = dict(entities or ENTITIES)
    start_utc = pd.Timestamp(start, tz=tz).tz_convert("UTC")
    end_utc = frame_end_utc(end, tz)
    grid = pd.date_range(start_utc, end_utc, freq="15min", tz="UTC", name="ts_utc")
    print(f"pulling {len(entities)} statistics {start_utc:%Y-%m-%d %H:%M}Z .. {end_utc:%Y-%m-%d %H:%M}Z "
          f"({len(grid)} quarters)", flush=True)
    cols, used = pull(entities, start_utc, end_utc, grid, fetch)
    for k, p in used.items():
        print(f"  {k:<14} {p}", flush=True)
    df = pd.DataFrame(index=grid)
    for k in ("load_w", "pv_main_w", "batt_dc_w", "soc_pct"):
        if k not in cols:
            raise KeyError(f"the entity map needs {k!r}")
        df[k] = cols[k]
    df["pv_pot_main_w"] = df["pv_main_w"]
    df["micro_w"] = cols["micro_w"] if "micro_w" in cols else 0.0
    if "grid_w" in cols:
        df["grid_w"] = cols["grid_w"]
    elif "grid_import_w" in cols and "grid_export_w" in cols:
        df["grid_w"] = cols["grid_import_w"] - cols["grid_export_w"]
    else:
        raise KeyError("the entity map needs grid_w or the grid_import_w / grid_export_w pair")
    df["soc_max_pct"] = cols.get("soc_max_pct")
    if "da_eur_kwh" in cols:
        df["da_eur_kwh"] = cols["da_eur_kwh"]
    elif prices_csv:
        df["da_eur_kwh"] = prices_from_csv(prices_csv, grid)
    else:
        print("warning: no price source (a da_eur_kwh entity or --prices); da_eur_kwh is empty and the ladder "
              "reports no_data until you fill it", flush=True)
        df["da_eur_kwh"] = float("nan")
    df["suspect"] = False
    df["plant"] = "measured"
    df = frame_schema.validate(df, out_csv or "<build_from_ha>")
    if out_csv:
        write(df, out_csv)
        print(f"wrote {out_csv}: {len(df)} rows", flush=True)
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("needle", nargs="?", default="")
    p = sub.add_parser("hourly"); p.add_argument("statistic_id"); p.add_argument("start"); p.add_argument("end")
    p = sub.add_parser("build")
    p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.add_argument("--out", default=os.path.join(ROOT, "data", "frame.csv"))
    p.add_argument("--prices", default=None, help="CSV of quarter prices (ts,eur_mwh) when no price entity is mapped")
    p.add_argument("--entities", default=None, help="JSON file with the entity map (default: ENTITIES in this module)")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        (res,) = asyncio.run(ws_call([{"type": "recorder/list_statistic_ids"}]))
        for s in res:
            if a.needle.lower() in s["statistic_id"].lower():
                print(json.dumps(s))
    elif a.cmd == "hourly":
        for row in statistics([a.statistic_id], a.start, a.end, "hour", ("sum", "mean", "state")).get(a.statistic_id, []):
            print(json.dumps(row))
    else:
        ents = json.load(open(a.entities)) if a.entities else None
        build_from_ha(a.start, a.end, ents, a.out, a.prices)
    return 0


if __name__ == "__main__":
    sys.exit(main())
