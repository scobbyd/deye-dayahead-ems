# The frame: the one CSV the harness reads

Every offline run (`backtest.quickcheck`, `backtest.ladder`,
`backtest.replay`) reads one CSV: the frame. One row per quarter hour, on an
unbroken 15-minute UTC grid, one column per measured or forecast quantity.
The contract lives in `backtest/frame_schema.py`; this page is the same
contract as a table, with the three ways to build one.

```python
from backtest import frame_schema
df = frame_schema.load("data/frame.csv")   # raises ValueError on a bad frame
```

## The grid

- The index column is `ts_utc`: ISO 8601 with an offset
  (`2026-06-10T10:15:00+00:00`). A stamp with a local offset is converted to
  UTC. A stamp with no offset is taken as UTC. If the column is not named
  `ts_utc`, the first column is taken as the index.
- Each stamp is the **start** of the quarter it labels.
- The grid is unbroken: consecutive rows are exactly 15 minutes apart. A
  quarter you have no data for is a row of NaN, never a missing row. Build
  the index with `pd.date_range(start, end, freq="15min", tz="UTC")` and
  reindex onto it.
- Every power column is the mean watt over the quarter.
- The ladder plans on local calendar days in the plant's timezone
  (`plant.json` `site.timezone`, default `Europe/Amsterdam`). A local day
  counts as "full" only when every quarter of it is present and every
  required column has a value. Start the frame at local midnight so the first
  day is full.

## Columns

Signs: `+` on `grid_w` is import, `+` on `batt_dc_w` is discharge. PV and
load are never negative.

| column | unit | sign | required | default when absent | meaning |
|---|---|---|---|---|---|
| `ts_utc` | ISO 8601 | | yes (index) | | start of the quarter, UTC |
| `load_w` | W | + = consumption | yes | | house load. On the reference plant this is the hybrid's UPS/load port, so the microinverter is not inside it. Includes everything the pack and the strings feed; excludes the inverter's own losses |
| `pv_pot_main_w` | W | >= 0 | yes | | what the hybrid's strings **could** have made. Equal to `pv_main_w` on every quarter the array was not held back; above it where the pack was full and export was off. Unknown: copy `pv_main_w` (the settlement then credits no curtailment repair). A plant model fills the gap (see below) |
| `pv_main_w` | W | >= 0 | yes | | what the hybrid's strings did make (DC-coupled PV behind the hybrid's own MPPTs; the LP's curtailable PV) |
| `grid_w` | W | + = import, - = export | yes | | the meter |
| `batt_dc_w` | W | + = discharge, - = charge | yes | | the pack's DC-side power. The settlement moves it to the AC node through the bridge efficiency (x eta discharging, / eta charging) |
| `soc_pct` | % | 0..100 | yes | | the pack's state of charge. Each ladder day starts its plan from the value on the first quarter of the local day |
| `da_eur_kwh` | EUR/kWh | | yes | | the day-ahead auction price for the quarter, bare: no fee, no tax, no VAT (the tariff adds those). Negative values are real and matter |
| `micro_w` | W | >= 0 | no | 0 | AC-coupled microinverter on the gen port. Must-take PV: the LP sees it as negative load, the hybrid bridge never carries it |
| `soc_max_pct` | % | | no | NaN | hourly maximum of the SOC, held on the four quarters. Only the fallback curtailment mask reads it |
| `suspect` | bool | | no | False | True on every quarter of an hour where the strings may have been held back. The reference rule: hourly SOC maximum at or above 95 % AND a negative price in the hour. The settlement's curtailment mask |
| `plant` | text | | no | `measured` | `measured` on quarters the recorded plant is the one you are pricing; `modelled` where the record belongs to an older plant. The ladder summarises the two as separate blocks and never mixes them |
| `fc_pv_solcast_p50_main_w` | W | >= 0 | no | NaN | Solcast P50 of the strings as last issued (main strings only). Lanes `F_sol`, `F_mix` |
| `fc_pv_solcast_p10_main_w` | W | >= 0 | no | NaN | Solcast P10, same basis. Lane `F_mix` |
| `fc_micro_solcast_w` | W | >= 0 | no | NaN | Solcast forecast of the microinverter. Unused by the lanes; kept for a forecast-only replay |
| `fc_pv_om24_main_w` | W | >= 0 | no | NaN | Open-Meteo irradiance issued 24 h before the quarter, through the plant model. Lane `F_da` on the scored day |
| `fc_pv_om48_main_w` | W | >= 0 | no | NaN | the same at 48 h lead. Lane `F_da` on the day after |
| `fc_pv_om0_main_w` | W | >= 0 | no | NaN | the freshest Open-Meteo run through the plant model. Lane `F_om0` (an upper bound on short-lead skill, not a lane to plan on) |
| `fc_load_ma7_w` | W | >= 0 | no | built | the trailing seven-day load profile: the median of `load_w` at the same local time over the previous seven days, at least four present. Every F lane's load. Built from `load_w` when absent |

Any other column (the reference frame's `src_*` witness labels, for
instance) is kept as is.

**The PV potential.** `pv_pot_main_w` is the strings' potential, not the
metered PV: where the pack was full and export was off the hybrid holds the
array back, and a frame that carries the metered PV there makes that
curtailment invisible (no repair in the settlement, none in the no-battery
baseline). `backtest/pvmodel.py` builds the potential from irradiance
through `pv.strings` in `plant.json`: tilt, azimuth and kWp per string, and
`scale`, a fitted correction on the pvlib output (1,0 = nameplate). The
reference roof is in `plant.example.json`: two 9,8 kWp strings, ESE at 34°
tilt and 109° azimuth, WNW at 39° and 288°, both at scale 1,0. A new plant
starts at scale 1,0 and fits its own on clean hours (`frames.potential`
takes the model onto the suspect hours and the measurement everywhere else).

A lane runs only when every column it needs carries at least one value.
`quickcheck` prints `skip <lane>: the frame has no <column>` for the others.
The lanes and their columns:

| lane | horizon | PV columns | load column |
|---|---|---|---|
| `P0` | 1 day | `pv_pot_main_w` | `load_w` |
| `P1` | 2 days | `pv_pot_main_w` | `load_w` |
| `P2` | 3 days | `pv_pot_main_w` | `load_w` |
| `F_da` | 2 days | `fc_pv_om24_main_w`, `fc_pv_om48_main_w` | `fc_load_ma7_w` |
| `F_sol` | 2 days | `fc_pv_solcast_p50_main_w` | `fc_load_ma7_w` |
| `F_mix` | 2 days | `fc_pv_solcast_p50_main_w`, `fc_pv_solcast_p10_main_w` | `fc_load_ma7_w` |
| `F_om0` | 2 days | `fc_pv_om0_main_w` | `fc_load_ma7_w` |

## A house without a battery

The quick check exists for a house that does not have the pack yet. Such a
frame writes:

- `batt_dc_w` = 0 on every row (nothing flowed through a pack).
- `soc_pct` = 50 on every row, constant. The ladder starts every day's plan
  from the first quarter's SOC and pins the horizon end to `soc_final_target`
  (50 % in `plant.example.json`), so with a constant 50 the plan starts and
  ends each day at the same level and no day is credited or charged for
  energy carried in the pack.
- `grid_w` = `load_w` - `pv_main_w` - `micro_w`, or your meter as it is: the
  settlement works in difference form from the measured grid trace (see
  `docs/method.md`), so a real meter with its unmetered circuits is fine.
- `pv_pot_main_w` = `pv_main_w`. Without a pack nothing was held back.

The settlement then removes a battery contribution of zero from your day and
adds the plan's, which is exactly the house with the pack you are pricing.

## What `load()` refuses

`frame_schema.load(path)` validates and raises with every defect listed at
once. It never repairs a value.

| error | condition |
|---|---|
| `FileNotFoundError` | the path does not exist |
| `ValueError: no ts_utc column` | the first column is neither `ts_utc` nor unnamed |
| `ValueError: ts_utc does not parse as timestamps` | a stamp `pd.to_datetime` cannot read |
| `ValueError: ... duplicate timestamps` | the index repeats a quarter |
| `ValueError: index is not an unbroken 15-minute grid` | a gap or a different step; reindex on `pd.date_range(..., freq="15min")` and leave gaps as NaN rows |
| `ValueError: ... timestamps are not on the quarter` | minutes not in {0, 15, 30, 45} or non-zero seconds |
| `ValueError: missing required column(s)` | one of the seven required columns is absent |
| `ValueError: plant column carries [...]` | a value other than `measured` or `modelled` |
| `ValueError: <pv column> has N negative quarters` | PV below -1 W |
| `ValueError: soc_pct outside 0..100` | a SOC outside the percentage range |
| `ValueError: da_eur_kwh above 5 EUR/kWh` | the price column is in EUR/MWh, not EUR/kWh |

The index is sorted if it arrives out of order, and local-offset stamps are
converted to UTC; these two are not errors.

A day the ladder cannot settle (a NaN in any column the lane needs, anywhere
in the horizon) is written as `no_data` in the ladder CSV and the run goes
on. A full local day is required for a plan to start on it.

## Building a frame

### (a) From Home Assistant long-term statistics

`backtest/ha_lts.py` pulls the recorder's statistics over the WebSocket API
and writes a frame in the contract. It is the one documented path from a
running Home Assistant to a CSV.

Auth and address, from the environment or from a `.env` file in the repo root
(`KEY=value` lines; `.env` is gitignored):

| variable | value |
|---|---|
| `HA_URL` | the http address of your Home Assistant, port included (`http://<your-ha>:<port>`); an `https` address gives a `wss` socket |
| `HA_LLAT` | a long-lived access token (profile page, security section) |

Nothing else is read. The library needs `websockets` (in `requirements.txt`).

```
python -m backtest.ha_lts list [substring]                       statistic ids the recorder has
python -m backtest.ha_lts hourly <statistic_id> <start> <end>    hourly rows as JSON lines (sum, mean, state)
python -m backtest.ha_lts build --start 2026-06-01 --end 2026-06-30 [--out data/frame.csv]
                                [--prices data/da_prices_q15.csv] [--entities my_entities.json]
```

`build` pulls the entities in the map `ENTITIES` at the top of the module.
The shipped map is the reference plant's and is an example: rename to yours,
or pass `--entities` a JSON file in the same shape.

| key | what it is | in the frame |
|---|---|---|
| `load_w` | the house load sensor, W | `load_w` |
| `pv_main_w` | the hybrid's string PV sensor, W | `pv_main_w`, and `pv_pot_main_w` = the same (no potential model here) |
| `micro_w` | the microinverter sensor, W; drop the key if you have none | `micro_w`, 0 when absent |
| `grid_import_w`, `grid_export_w` | the meter's two power sensors | `grid_w` = import - export |
| `grid_w` | alternative: one signed meter sensor, + = import | `grid_w` |
| `batt_dc_w` | the battery power sensor, + = discharge | `batt_dc_w` |
| `soc_pct` | the SOC sensor | `soc_pct`, and `soc_max_pct` from the hourly maximum |
| `da_eur_kwh` | optional: a price sensor in EUR/kWh | `da_eur_kwh` |

Each map value is `{"id": entity_id, "scale": factor_to_W}` or a bare entity
id at scale 1 (a kW sensor takes `"scale": 1000.0`). Signs are the contract's.

The recorder keeps 5-minute statistics for a short window (about ten days)
and hourly long-term statistics for ever. The puller asks for 5-minute rows
first and falls back to hourly per entity; 5-minute means are averaged onto
the quarter, hourly means are held flat over its four quarters. `build`
prints which period each entity came back with (`5minute`, `hour` or
`absent`).

Prices: either a price entity in the map (its mean over the quarter), or
`--prices` a CSV with a timestamp index and a column of `eur_mwh` per
quarter (the energy-charts download layout; a column named `eur_kwh` is
taken as is). With neither, `da_eur_kwh` is empty, a warning is printed, and
the ladder reports `no_data` until you fill it.

`suspect` is written False (no curtailment witness) and `plant` is
`measured`. The frame is validated before it is written, so a bad pull fails
in `build`, not in the ladder.

A house with no battery yet has no battery sensors to map. Pull the rest,
then set the two columns:

```python
import pandas as pd
from backtest import frame_schema
df = pd.read_csv("data/frame.csv", index_col=0)
df.index = pd.to_datetime(df.index, utc=True)
df["batt_dc_w"] = 0.0
df["soc_pct"] = 50.0
frame_schema.validate(df).to_csv("data/frame.csv")
```

(`build` requires the `batt_dc_w` and `soc_pct` keys in the map; point them
at any sensor and overwrite as above, or build the frame from a CSV as in
(b).)

### (b) From any CSV by mapping columns

Any quarter-hour or finer export works: a supplier's portal, a meter logger,
an inverter cloud download. Rename the columns to the contract, put the
stamps on the UTC quarter grid, fill what you do not have, and validate.

```python
import pandas as pd
from backtest import frame_schema

raw = pd.read_csv("my_export.csv")
raw["ts"] = pd.to_datetime(raw["time"]).dt.tz_localize("Europe/Amsterdam", ambiguous="infer")
raw = raw.set_index("ts").tz_convert("UTC")

df = pd.DataFrame({
    "load_w": raw["consumption_kw"] * 1000.0,
    "pv_main_w": raw["solar_kw"] * 1000.0,
    "grid_w": (raw["import_kw"] - raw["export_kw"]) * 1000.0,   # + = import
    "da_eur_kwh": raw["price_eur_mwh"] / 1000.0,
})
df = df.resample("15min").mean()                                  # finer data: average onto the quarter
grid = pd.date_range(df.index[0].floor("D"), df.index[-1], freq="15min", tz="UTC", name="ts_utc")
df = df.reindex(grid)                                             # gaps become NaN rows
df["pv_pot_main_w"] = df["pv_main_w"]                             # no curtailment witness
df["batt_dc_w"] = 0.0                                             # no battery yet
df["soc_pct"] = 50.0
df = frame_schema.validate(df)                                    # raises with every defect named
df.to_csv("data/frame.csv", date_format="%Y-%m-%dT%H:%M:%S+00:00")
```

Hourly data can be held flat over the four quarters
(`df.resample("15min").ffill()` before the reindex), but the plan then sees
an hourly load and PV; expect a flatter day than the meter recorded.

Forecast columns are optional. With only the required columns the P lanes
run (perfect foresight). To run `F_da` you need a PV forecast as it was
issued the day before, in W, on the same grid, in `fc_pv_om24_main_w` and
`fc_pv_om48_main_w`; `backtest/pvmodel.py` turns Open-Meteo irradiance into
those through the plant's strings. The load profile `fc_load_ma7_w` is built
for you.

Start the frame at least a week before the first day you want scored: by
default `quickcheck` skips the first seven full days so the seven-day load
profile exists, and stops at the last full day minus the lane's horizon
(`P1` needs D+1 in the frame, `P2` needs D+2). `--start` and `--end`
override the range.

### (c) The synthetic example

`examples/frame_synthetic.csv` is a generated frame (`examples/make_synthetic_frame.py`,
fixed seed) so the quick start runs on a clean clone: 14 local days in April
2026, 1.344 rows, a house without a battery. Load between 300 and 2.500 W
with a morning and an evening peak and a weekly rhythm; PV from a clear-sky
bell per string of `plant.example.json` with three overcast days; a
duck-curve price with a few negative midday quarters on the sunny days and
one spiky evening; `grid_w` = load - PV; `batt_dc_w` 0; `soc_pct` 50;
`fc_pv_om24_main_w` and `fc_pv_om48_main_w` as the PV with day-ahead bias
and noise. It carries no Solcast columns, so `F_sol`, `F_mix` and `F_om0` are
skipped and `P0`, `P1`, `P2`, `F_da` run.

The numbers it produces mean nothing about any real house. It is there to
prove the pipeline and to show the table shapes.
