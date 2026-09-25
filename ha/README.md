# The live side: EMHASS as a shadow EMS inside Home Assistant

This directory is the Home Assistant half of the repo. The same planner core
that the offline backtest uses (`emhasscore/`) runs inside Home Assistant
every half hour, plans the battery for the rest of today and all of tomorrow
under your tariff, publishes the plan as sensors, archives it, and every
night scores the plan against what your house actually did. It never writes
to the inverter. That is what "shadow" means: you watch it for months before
you decide whether to let it drive.

It was built on one plant (a Deye SUN-12K hybrid, a 48,2 kWh pack, two PV
strings, one microinverter, Dutch day-ahead prices) and it shows: the entity
names, the tariff helpers and the microinverter split are that plant's. Every
one of them is a named value you change once.

## What runs where

Three pieces, all inside the HA OS host. Nothing runs outside it.

| piece | what it is | where it lives |
|---|---|---|
| the EMHASS add-on | the linear-programme solver, v0.18.x, from the community add-on repository | Supervisor add-on `5b918bf2_emhass`; answers at `http://5b918bf2-emhass:5000` inside the host only |
| pyscript | `emhass_shadow.py`, the wrapper: reads HA state, builds the solve, writes sensors and statistics; `emhass_core.py`, the facade over `emhasscore/`, runs natively under `task.executor` | `/config/pyscript/`, `/config/pyscript_helpers/` |
| the package | helpers, the tariff sensors, the realised-cost meter, health, the schedule | `/config/packages/emhass/` |

```
HA automations (packages/emhass/emhass_automations.yaml)
  -> pyscript services (pyscript/emhass_shadow.py)
       -> emhass_core facade -> emhasscore/ (native, off the event loop)
            -> HTTP to the add-on: naive-mpc-optim, publish-data, healthz
            -> /config/emhass/: plans, scores.csv, ladder.csv, ...
       -> HA states (sensor.emhass_*), long-term statistics (emhass:*)
  EMHASS publish-data -> sensor.emhass_da_* (eleven entities)
```

The add-on's `/action/*` API is unauthenticated. Containment is what makes
that acceptable: the host port stays unmapped (ingress only), `auto_update`
stays off, the configuration is a file in this repo that `deploy.sh` copies
in, and only `emhass_shadow.py` ever publishes. `check.sh contain` verifies
this.

## Install order

1. **The add-on.** Add the community repository
   `https://github.com/davidusb-geek/emhass-add-on` under Settings, Add-ons,
   Add-on store. Install EMHASS. In its Network panel set the host port to
   disabled (null). Turn `auto_update` off. Start it once so
   `/addon_configs/5b918bf2_emhass/` exists.
2. **pyscript.** Install the pyscript custom component through HACS. In
   `configuration.yaml`:

   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```

   Both are required: the wrapper imports `homeassistant.components.recorder`
   (statistics and history) and reads `hass.config.time_zone`.
3. **The core.** Copy the repo's `emhasscore/` directory to
   `/config/pyscript_helpers/emhasscore/` and `ha/pyscript_helpers/emhass_core.py`
   next to it. The facade loads the package by path; pyscript never
   interprets it.
4. **The plant.** Copy `plant.example.json` to `/config/emhass/plant.json`
   and set your site, pack, inverter, grid cap, Solcast ids and tariff
   defaults. Keys you leave out fall back to the reference plant.
5. **The package.** Copy `ha/packages/emhass/` to `/config/packages/emhass/`
   (with `homeassistant: packages: !include_dir_named packages` in
   `configuration.yaml`). Rename the outside entities: each file lists the
   ones it expects in its header comment.
6. **The wrapper.** Copy `ha/pyscript/emhass_shadow.py` (and, if you want the
   ladder period helper, `ladder_ui.py`) to `/config/pyscript/`. Edit the
   `ENTITIES` dict at the top.
7. **The add-on config.** Edit the sensor names in `ha/config.json` (see
   `README-config.md`), then `ha/scripts/deploy.sh`. It copies the file to
   the add-on and to `/config/emhass/config.json`, restarts the add-on and
   runs the drift checks.
8. **Restart HA**, then seed the helpers: `ha/scripts/seed_helpers.sh`. The
   helpers carry no `initial:` on purpose (a restart would reset tuned
   values), and an `input_number` without one sits at its minimum until set.
9. **A dry run.** `ha/scripts/check.sh run` solves once through the live
   add-on without publishing and prints the horizon, the predicted-price step
   count, the Solcast gaps, the status and the cost. When it says `Optimal`,
   turn on `input_boolean.emhass_shadow_enabled`.

The scripts read `HA_URL`, `HA_LLAT` (a long-lived token) and `HA_SSH` (the
ssh target of the HA OS host, through an SSH add-on) from the environment or
a `.env` file at the repo root. `ha/scripts/env.sh` documents them.

### Integrations the wrapper reads

- **Nord Pool** (built in): the `nordpool.get_prices_for_date` action and
  `binary_sensor.nord_pool_nl_tomorrow_price_available`. Put your config
  entry id and area in `ENTITIES`. Prices are EUR/MWh with UTC starts; the
  core maps each 15-minute step to its row.
- **Solcast PV Forecast** (HACS): `sensor.solcast_pv_forecast_forecast_today`,
  `_tomorrow`, `_day_3` with the `detailedForecast` attribute, and the
  `solcast_solar.query_forecast_data` action for the per-site split. A plan
  with more than 8 steps of missing PV is not solved.
- **EPEX Predictor** (HACS): `sensor.epex_predictor_nl`, attribute
  `forecast`. Fills the price steps Nord Pool has not published yet (D+2, or
  tomorrow before 13:00); those steps are counted in `n_predicted_steps`.

## The entity map

Every hardware entity the wrapper reads is one dict, `ENTITIES`, at the top
of `ha/pyscript/emhass_shadow.py`. The values shipped are the reference
plant's.

| key | reference entity | what it must be |
|---|---|---|
| `pv` | `sensor.total_pv_power` | PV power, W, everything the house makes |
| `load` | `sensor.inverter_load_ups_power` | the physical load on the inverter output, W |
| `grid_import`, `grid_export` | `sensor.p1reader2_p1_reader_2_power_consumed`, `_returned` | fiscal meter, kW, one sensor per direction |
| `batt_power` | `sensor.inverter_battery_power` | battery DC power, W, positive = discharge |
| `soc` | `sensor.inverter_battery` | pack SOC, % |
| `micro` | `sensor.inverter_microinverter_power` | the AC-coupled microinverter, W (a sensor that reads 0 if you have none) |
| `export_switch` | `switch.inverter_export_surplus` | the inverter's export-surplus switch |
| `curtailed` | `sensor.pv_curtailment_active` | created by the package (`emhass_realised.yaml`); leave it |
| `solcast_today`, `solcast_tomorrow`, `solcast_day3` | `sensor.solcast_pv_forecast_forecast_*` | the Solcast integration's day sensors |
| `epex` | `sensor.epex_predictor_nl` | the EPEX Predictor sensor |
| `nordpool_entry`, `nordpool_area` | a config entry id, `NL` | your Nord Pool config entry and bidding zone |

The measured series must have `state_class: measurement`: the wrapper reads
their 5-minute recorder statistics, not their history. The recorder keeps
those about 10 days, which bounds how far back a day can be re-scored.

The same names appear in `ha/config.json` (the add-on's own history fetch)
and in the header of each package file. The Solcast rooftop id of the string
the microinverter shares comes from `plant.json` (`pv.solcast_sites.A`).

## What the package creates

`emhass_helpers.yaml`

- `input_boolean.emhass_shadow_enabled` (the master switch),
  `emhass_ml_enabled` (gates fit and tune; turn on at day 10),
  `emhass_salderen` (netting still in force: while on, the energy tax is 0),
  and four `dash_shadow_*` expanders that only the private dashboard read.
- `input_number`: the tariff (`emhass_energy_tax_eur_kwh`,
  `emhass_supplier_fee_eur_kwh`, `emhass_btw_pct`,
  `emhass_feedin_fee_eur_kwh`, negative = a feed-in bonus), the scoring
  (`emhass_lambda_frac`, `emhass_plan_stale_hours`), the PV model
  (`emhass_growatt_share`, `emhass_pv_p10_mix`), the rebalancing clock
  (`emhass_rebalance_dwell_h`), and the live knob layer that every solve
  reads and the A/B tester can move: `emhass_stress_scale`,
  `emhass_surplus_base`, `emhass_deficit_threshold`, `emhass_deficit_cost`,
  `emhass_soc_final`, `emhass_weight_battery_discharge`,
  `emhass_batt_power_max_w`, `emhass_soc_min`, `emhass_soc_max`,
  `emhass_soc_target`, the four `emhass_aux_cut_*` thresholds of the
  microinverter cut rule, and `emhass_ladder_offset` (dashboard only).
- `input_datetime.emhass_soc_target_at`, `input_select.emhass_ladder_grain`.
- `script.emhass_ab_test_live_yesterday`, `script.emhass_ab_apply_last`.

`emhass_prices.yaml`: `sensor.emhass_buy_price_now`,
`sensor.emhass_sell_price_now` (EUR/kWh, from the Nord Pool current price and
the helpers: buy = (DA + tax + fee) x (1 + BTW), sell = (DA - feed-in fee) x
(1 + BTW)).

`emhass_realised.yaml`: `sensor.emhass_grid_cost_rate` (EUR/h),
`sensor.emhass_grid_cost` (integration), `sensor.emhass_grid_cost_daily`
(utility meter, net), `sensor.emhass_soc_midnight`, `sensor.emhass_pv_daily_kwh`,
`sensor.pv_curtailment_active` (1 when SOC > 95 % and export is off, every
30 s: the site rule for a throttled array).

`emhass_health.yaml`: `sensor.emhass_plan_age_h`,
`binary_sensor.emhass_plan_stale`.

`emhass_epex_capture.yaml`: `input_boolean.emhass_epex_capture_enabled` and a
shell command that appends the raw EPEX Predictor response to
`/config/emhass/epex_raw/YYYY-MM.jsonl` twice a day. It calls a third-party
site (`epexpredictor.batzill.com`) from your HA host; leave the gate off if
you do not want that. The backtest reads these files to price a horizon with
the forecast as it stood.

pyscript writes: `sensor.emhass_last_run`, `sensor.emhass_plan_yesterday`,
`_today`, `_next_day`, `_day_after` (the rolled day slices),
`sensor.emhass_score`, `sensor.emhass_gap_30d`, `sensor.emhass_ladder`,
`sensor.emhass_ab_last`, `sensor.emhass_last_fit`, `sensor.emhass_last_tune`,
`binary_sensor.emhass_addon_healthy`; and the long-term statistics
`emhass:planned_eur`, `emhass:replayed_eur`, `emhass:realised_eur`,
`emhass:gap_eur`, `emhass:hindsight_eur`, `emhass:ladder_earned_eur`, the
seven `emhass:shadow_*` lanes (charge, discharge, import, export, SOC, PV,
load per hour) and `emhass:tariff_buy_ct`, `emhass:tariff_sell_ct`.

## The automations and their cadence

| id | when (local) | what |
|---|---|---|
| `emhass_plan_on_prices` | tomorrow's Nord Pool prices publish (observed 13:00:04) | plan, archive, roll, publish |
| `emhass_plan_fallback` | 15:00, if tomorrow is still unplanned | the same, with predicted prices for the missing steps, flagged |
| `emhass_plan_refresh` | every 30 min at :13 and :43, while the add-on is healthy | re-solve from the settled SOC of the virtual pack |
| `emhass_score` | 00:10 | score yesterday, backfill hindsight, extend the ladder, re-plan |
| `emhass_fit` | 04:00, ML switch on | `forecast-model-fit` on 13 days of load |
| `emhass_tune` | Sunday 03:30, ML switch on | `forecast-model-tune`, 10 trials |
| `emhass_salderen_apply`, `emhass_salderen_end` | on the netting switch; 00:00:10 daily | the energy tax follows netting; netting ends by law on 1 Jan 2027 |
| `emhass_health_degraded`, `emhass_health_recovered` | health off for 1 h, re-nag 08:00 and 20:00; back on | a notification while re-planning is stopped, and the all clear |
| `emhass_stale_notify` | plan stale for 30 min | a notification |
| `emhass_epex_capture` | 00:05 and 12:05 | the EPEX forecast as issued, to disk |

pyscript itself: `emhass_health` every 15 minutes (healthz, config drift),
and 120 s after every reload `emhass_rehydrate` rebuilds the rolled slices
and score sensors from the archive.

Why :13 and :43: the horizon starts at the next quarter, so a :13 solve is in
force from :15, and by :13 two of the running step's three 5-minute
statistics buckets have landed, so the settled SOC sees most of it. Scoring
is immune to cadence: the plan of record for a day is the last solve before
that day's midnight, always after 13:00 with real prices.

## The archive under /config/emhass/

Everything the shadow EMS writes. It is inside HA backups.

```
/config/emhass/
  plant.json              your site (copied from plant.example.json)
  config.json             the deployed add-on config, what the drift check compares against
  plans/<plan_ts>.json.gz one document per solve (~17 kB, ~54 a day): the exact payload posted,
                          the tariff helpers, the result table, last-run, soc_source, flags
  scores.csv              one row per scored day: planned, replayed, realised, hindsight, gap, flags
  ladder.csv              the ladder: the actual lane per day (hindsight, omni2 kept when solved)
  ladder_hours.csv        the actual lane per hour (feeds emhass:ladder_earned_eur and the shadow lanes)
  tariff_hours.csv        buy and sell per hour, deep past (feeds emhass:tariff_*_ct)
  rebalance.json          the settled rebalancing clock (when the pack last sat full)
  ab/<label>.json.gz      A/B runs
  epex_raw/YYYY-MM.jsonl  the EPEX forecast as issued, two lines a day
```

A plan archive file holds the exact `payload` posted, so any solve is
reproducible against the add-on. The backtest's replay and hindsight lanes
read this directory when you sync it to the dev box.

## What you get

The eleven `sensor.emhass_da_*` entities, written by the add-on's
`publish-data` seconds after every solve. Each carries a `forecasts`
attribute: the whole horizon as `{date, value}` rows, 15-minute steps.

| entity | unit | meaning |
|---|---|---|
| `sensor.emhass_da_p_batt` | W | battery power, + discharge |
| `sensor.emhass_da_soc` | % | pack SOC along the plan |
| `sensor.emhass_da_p_grid` | W | grid power, + import |
| `sensor.emhass_da_p_pv` | W | the PV the planner was fed |
| `sensor.emhass_da_p_load` | W | the load forecast |
| `sensor.emhass_da_buy`, `sensor.emhass_da_sell` | EUR/kWh | the tariff per step |
| `sensor.emhass_da_cost` | EUR | the cost function over the horizon |
| `sensor.emhass_da_status` | text | `Optimal` or not |
| `sensor.emhass_da_p_curtail` | W | PV the planner chose to throw away |
| `sensor.emhass_da_p_hybrid` | W | power through the DC-AC bridge |

The scoreboard: `sensor.emhass_score` (yesterday's gap, EUR, the whole row as
attributes) and `sensor.emhass_gap_30d` (the 30-day sum; attributes carry
the last seven rows). `scores.csv` holds the full history. The lanes:

- `planned`: the plan's own cost on its own forecasts;
- `replayed`: the plan's battery decisions settled against the measured day
  through the Deye actuator model (the virtual pack);
- `realised`: the fiscal meter's flows in the same tariff frame (what the
  incumbent controller did);
- `hindsight`: a 20/20 solve on the measured day, matched horizon;
- `gap`: replayed minus hindsight, how far the shadow fell short of perfect
  knowledge. Positive = money left on the table.

`sensor.emhass_ladder` sums the same per week, month and year.

## Not included: the dashboard

The live system has a dashboard tab with the plan as a 60-hour window (24 h
back, 36 h forward), the forecast against the measurement, a 30-day
scorecard and the ladder ledger. It depends on a private theme and card stack
(button-card, apexcharts-card, card-mod with a token palette) and is not
shipped. `ladder_ui.py` and the `dash_shadow_*` and `emhass_ladder_*`
helpers exist for it; they are harmless without it.

What to chart instead, with any history or apexcharts card:

- the future: `sensor.emhass_da_soc`, `sensor.emhass_da_p_batt`,
  `sensor.emhass_da_p_grid`, `sensor.emhass_da_buy`, `sensor.emhass_da_sell`
  from their `forecasts` attribute (apexcharts `data_generator`);
- the past: `sensor.emhass_plan_yesterday` and `_today` carry per-step arrays
  (`p_batt_w`, `p_grid_w`, `soc_pct`, `p_pv_w`, `p_load_w`, `pv_fc_w`,
  `load_fc_w`, `buy`, `sell`, `cost_eur`) from `t0` at `step_min`; the past
  steps are measured PV and load with the virtual pack settled against them,
  the `*_fc_w` lanes are what the plan forecast;
- the score: the `emhass:planned_eur`, `emhass:replayed_eur`,
  `emhass:realised_eur`, `emhass:hindsight_eur`, `emhass:gap_eur` statistics,
  one value per day, and the `emhass:shadow_*` lanes per hour (the statistics
  graph card takes external statistic ids);
- health: `binary_sensor.emhass_addon_healthy`, `binary_sensor.emhass_plan_stale`,
  `sensor.emhass_last_run`, `sensor.emhass_plan_age_h`.

The plan slices and `sensor.emhass_ladder` should be excluded from the
recorder (`recorder: exclude: entities:`): their attributes change every
solve and recording them is churn.

## Where each knob lives

Three layers hold EMHASS settings, and a value in a lower layer is silently
beaten by the one above it.

1. **The runtime payload** (`emhasscore`, read from the knob helpers). Posted
   with every solve, so the file value is dead: stress costs, SOC costs,
   `soc_final`, `weight_battery_discharge`, the battery power limits and SOC
   bounds, the horizon, `soc_init`, the PV, load and price lists.
2. **`ha/config.json`.** Everything the payload does not set: capacity,
   efficiencies, `set_nodischarge_to_grid`, `compute_curtailment`, the sensor
   names, the ML settings. Edit, then `deploy.sh`.
3. **The add-on web UI. Never.** A save there rewrites the live file with a
   defaults-expanded blob, resets keys you did not touch, list-wraps scalars,
   and takes `binary_sensor.emhass_addon_healthy` off, which stops the
   half-hourly re-plan until the drift is cleared.

## The scripts

- `deploy.sh`: validate `config.json`, copy it to the add-on and to
  `/config/emhass/`, restart the add-on, wait, run the drift checks.
  Needs `HA_SSH`.
- `check.sh [contain|config|health|actions|run|layers]`: read-only checks;
  exit code = failures. `actions` and `layers` run on the repo alone.
- `seed_helpers.sh`: set every helper once. Needs `HA_URL`, `HA_LLAT`.
- `env.sh`: sourced by the three; reads the repo-root `.env`.

## Gotchas worth knowing before the first week

- Lists, not dicts, for every forecast passed to EMHASS: a dict whose
  timestamps miss the add-on's own grid collapses silently to a constant
  while reporting `Optimal`. Every list has exactly `n` items from `t0`.
- DST days have 92 or 100 steps, not 96; the core counts through UTC.
- The ML forecaster predicts exactly `num_lags` steps and aborts the solve
  when the horizon is longer; the fit posts 288, not the file's 96.
- Passing `load_power_forecast` flips EMHASS to the `list` load method and
  silently overrides the ML forecaster, so the wrapper passes its own load
  shape only while `emhass_ml_enabled` is off.
- The add-on holds one `opt_res_latest`. Hindsight, replay and A/B solves
  overwrite it, so every such service ends with a live re-plan, and ad-hoc
  solves step off the :13/:43 minutes.
- pyscript's AST walker has no generator-expression handler; the wrapper uses
  list comprehensions throughout.
- `pyscript.reload` does not watch `pyscript_helpers/`; after changing the
  core, touch `pyscript/emhass_shadow.py`. The facade purges a cached
  `emhasscore` from `sys.modules` so that touch reloads the package too.
