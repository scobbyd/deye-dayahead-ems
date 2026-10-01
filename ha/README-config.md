# ha/config.json: the EMHASS add-on configuration

`config.json` is the complete EMHASS add-on configuration of the reference
plant (a Deye SUN-12K-SG04LP3 hybrid with a 48,2 kWh LiFePO4 pack, two PV
strings, one AC-coupled microinverter, a 3 x 25 A grid connection in the
Netherlands). It was generated from the add-on's `config_defaults.json`
(v0.18.1) with the site values applied, and it is shipped verbatim: every
number in it is the reference plant's. It carries no coordinates (the add-on
takes latitude, longitude, altitude and time zone from Home Assistant through
the Supervisor) and no household strings other than the sensor names listed
below.

`ha/scripts/deploy.sh` copies the file to
`/addon_configs/5b918bf2_emhass/config.json` (the add-on reads it at boot
only) and to `/config/emhass/config.json` (the copy the health check compares
the live add-on against). Edit the file and deploy; never save the add-on's
own configuration page, which rewrites the live file with a defaults-expanded
blob and resets keys you did not touch (`ha/README.md`, "Where each knob
lives").

## Keys that describe the plant: set yours

| key | reference value | meaning | plant.json |
|---|---|---|---|
| `battery_nominal_energy_capacity` | 48200 | pack capacity, Wh | `battery.capacity_kwh` x 1000 |
| `battery_charge_power_max`, `battery_discharge_power_max` | 12500 | pack power limit, W each way | `battery.p_nom_kw` x 1000 |
| `battery_charge_efficiency`, `battery_discharge_efficiency` | 0.961, 0.957 | DC stage efficiencies | `battery.eta_charge`, `battery.eta_discharge` |
| `battery_minimum_state_of_charge`, `battery_maximum_state_of_charge` | 0.1, 1.0 | SOC band | `battery.soc_min`, `battery.soc_max` |
| `battery_target_state_of_charge` | 0.5 | horizon-end SOC fallback | `battery.soc_final_target` |
| `inverter_ac_output_max`, `inverter_ac_input_max` | 12000 | the hybrid's DC-AC bridge, W each way | `inverter.p_nom_kw` x 1000 |
| `inverter_efficiency_dc_ac`, `inverter_efficiency_ac_dc` | 0.989 | bridge efficiency | `inverter.eta_bridge` |
| `maximum_power_from_grid`, `maximum_power_to_grid` | 17250 | grid connection, W each way | `grid.cap_w` |
| `pv_module_model`, `pv_inverter_model` | two CSUN295 / Fronius Primo entries | pvlib SAM database names, one per string | none (fallback only) |
| `surface_tilt`, `surface_azimuth` | [34, 39], [109, 288] | string geometry, one per string | `pv.strings.*.tilt`, `.azimuth` |
| `modules_per_string`, `strings_per_inverter` | [33, 33], [1, 1] | string size, one per string | none (fallback only) |
| `weight_battery_discharge` | 0.01 | EUR per kWh cycled (0,48 EUR per full cycle) | none |

The PV model keys (`pv_module_model` ... `strings_per_inverter`) are the
add-on's own PV forecast fallback. The live planner passes the Solcast PV as a
runtime list on every solve, so these keys are only used when a solve is
posted without one (an empty POST from the UI). They are kept sane so such a
solve cannot produce the default grid-charging plan.

## Keys that name your sensors: rename to yours

| key | reference value | what it must be |
|---|---|---|
| `sensor_power_photovoltaics` | `sensor.total_pv_power` | PV power, W, main array plus microinverter |
| `sensor_power_load_no_var_loads` | `sensor.inverter_load_ups_power` | the physical load on the inverter output, W |
| `sensor_power_battery` | `sensor.inverter_battery_power` | battery DC power, W, positive = discharge |
| `sensor_battery_state_of_charge` | `sensor.inverter_battery` | pack SOC, % |
| `sensor_replace_zero`, `sensor_linear_interp` | the same names | the recorder cleaning lists; keep them equal to the above |
| `var_model` | `sensor.inverter_load_ups_power` | the load sensor the ML forecaster fits on (same as the load) |

The same names appear in `entities` in your `plant.json` (which both
pyscript wrappers read) and, for a few of them, hard-coded in the package
YAML (`ha/README.md`, "The entity map"). Rename them in all three places.

## Keys the runtime payload overrides (dead in this file)

Posted with every `naive-mpc-optim` call by `emhasscore`, so the file value
never reaches the solver: `optimization_time_step` (fixed at 15),
`prediction_horizon`, `soc_init`, `soc_final`, `pv_power_forecast`,
`load_power_forecast`, `load_cost_forecast`, `prod_price_forecast`,
`battery_stress_cost`, `inverter_stress_cost`, `battery_soc_surplus_cost`,
`battery_soc_deficit_threshold`, `battery_soc_deficit_cost`,
`weight_battery_discharge`, the battery power limits and SOC bounds (from the
knob helpers). The price keys (`load_cost_forecast_method`,
`load_peak_hour_periods`, `load_peak_hours_cost`, `load_offpeak_hours_cost`,
`production_price_forecast_method`, `photovoltaic_production_sell_price`) are
fallbacks as well; the tariff comes from the helpers.

`inverter_stress_segments` is accepted, echoed by `/get-config` and ignored by
the 0.18.1 `naive-mpc-optim` path (measured 2026-09-03: 2, 10 and 20 segments
give byte-identical objectives).

## Keys to keep as they are

- `set_nodischarge_to_grid: false` is essential; the default forbids export
  timing, which is what the pack is for once netting ends.
- `compute_curtailment: true`: negative prices make curtailment a real lever.
- `method_ts_round: last`, so a 23:50 trigger plans 00:00-23:45.
- `use_websocket: true`, `use_influxdb: false`: history comes from the
  recorder's 5-minute statistics (about 10 days).
- `load_forecast_method: naive` until day 10, then `mlforecaster` (the ML
  keys below it: KNN, `num_lags` 96 in the file; the fit and tune services
  post 288, see the comment on `ML` in `emhass_shadow.py`).
- `number_of_deferrable_loads: 0` and the empty deferrable lists: the
  planner has no controllable loads.
- `data_path` and `heat_topology` are never echoed by `/get-config`; the drift
  check ignores their absence.
