# The live side: EMHASS plans the day, the writer drives a Deye

This directory is the Home Assistant half of the repo. The planner runs the
backtest's core (`emhasscore/`) every 15 minutes: it plans the battery for the
rest of today and all of tomorrow (and the day after when Solcast has a third
day) on day-ahead (EPEX) prices and your tariff, and archives every plan. The
writer takes the plan step in force every 15 minutes, compiles it into
register values for a Deye hybrid inverter and, when you arm it and set it
live, writes them through the Solarman integration.

**Warning.** Armed and live, this code writes holding registers on your
inverter: work mode, grid charging, charge and discharge current limits, a
time-of-use target. Those registers latch, and the inverter has no timeout
that undoes them when the controller dies. The licence is MIT, with no
warranty of any kind. You are responsible for every limit (currents, SOC
band, temperatures, grid connection) and for checking that they suit your
battery, your inverter and your wiring. The defaults are one site's values,
not safe values for yours.

## Who this is for

Owners of a Deye three-phase hybrid (SUN-xxK-SG04LP3 family) whose supplier's
EMS used to drive their inverter and no longer does, and who want their own
day-ahead battery control in Home Assistant. It assumes you are at home with
packages, pyscript, SSH into the HA host and a JSON line log.

It was built on one plant: a Deye SUN-12K-SG04LP3, a 48,2 kWh LiFePO4 pack
(51,2 V nominal, 240 A register ceiling), two PV strings, one AC-coupled
microinverter on the generator port, a 3 x 25 A grid connection and Dutch
day-ahead prices. Every site number and entity id lives in one file,
`plant.json`, that you write for your own plant. Status: beta (see "Known
limits").

## What runs where

Everything runs inside the HA OS host.

| piece | what it is | where it lives |
|---|---|---|
| the EMHASS add-on | the linear-programme solver, v0.18.x, from the community add-on repository | Supervisor add-on `5b918bf2_emhass`; answers at `http://5b918bf2-emhass:5000` inside the host only |
| the core | `emhasscore/` (standard library only) and the facade `emhass_core.py`; runs natively under `task.executor` | `/config/pyscript_helpers/` |
| the planner | `emhass_shadow.py`: reads HA state, builds the solve, writes sensors and statistics | `/config/pyscript/` |
| the writer | `emhass_writer.py`: reads the inverter levers, calls the core, writes the registers | `/config/pyscript/` |
| the packages | helpers, the schedule, price sensors, health, the writer's arming switch, mode, kill switch and watchdogs | `/config/packages/emhass/` |
| the plant file | `plant.json`: every site number and entity id | `/config/pyscript_helpers/plant.json` or `/config/emhass/plant.json` |

```
Nord Pool, EpexPredictor, Solcast, recorder statistics
  -> planner (pyscript.emhass_plan_day, solves at :13 :28 :43 :58)
       -> EMHASS add-on: naive-mpc-optim, publish-data
       -> plan archive /config/emhass/plans/<plan_ts>.json.gz
       -> sensor.emhass_da_*, sensor.emhass_plan_*
  -> writer (pyscript.emhass_writer_tick, ticks at :00:20 :15:20 :30:20 :45:20)
       reads the newest plan in the archive whose horizon covers now
       -> compiles the step into a register record, diffs it against the inverter
       -> armed: writes what the mode allows, through the Solarman entities
       -> sensor.emhass_writer, sensor.emhass_writer_heartbeat,
          /config/emhass/writer/<date>.jsonl
  -> Deye (Solarman deye_p3 profile, holding registers)
YAML watchdogs (every 5 minutes, armed only, independent of pyscript)
  -> script.emhass_deye_baseline when the heartbeat stops
```

The writer reads the plan from the archive, not from `sensor.emhass_da_*`. A
solve at :13 is in force from :15. Without fresh plans (`emhass_shadow_enabled`
off, the add-on down) the writer falls back to the baseline after 60 minutes.

The add-on's `/action/*` API is unauthenticated. Containment makes that
acceptable: the host port stays unmapped, `auto_update` stays off, the config
is a file `deploy.sh` copies in, and only `emhass_shadow.py` publishes.
`check.sh contain` verifies this.

## Requirements

| what | why |
|---|---|
| Home Assistant OS, an SSH add-on with `/config` and `/addon_configs` mounted, pyscript (HACS), the EMHASS add-on v0.18.x | the add-on, the scripts, both wrappers, the solver |
| Solarman (HACS, `davidrapan/ha-solarman`) with the `deye_p3` profile | the inverter's sensors and the nine writable levers |
| Nord Pool (built in) | `nordpool.get_prices_for_date`, `sensor.nord_pool_nl_current_price`, `binary_sensor.nord_pool_nl_tomorrow_price_available` |
| Solcast PV Forecast (HACS) | the day sensors with `detailedForecast`, and `solcast_solar.query_forecast_data` |
| EpexPredictor (HACS), recommended | fills the price steps Nord Pool has not published yet. Without it those steps hold the last known price |
| a grid meter (a P1 reader or similar, import and export as two kW sensors), a notify service | the measured grid flow; the alerts |

A plan with more than 8 steps of missing Solcast PV is not solved. Every
measured series must have `state_class: measurement`: the wrappers read the
recorder's 5-minute statistics, which the recorder keeps about 10 days.

## Install order

The install leaves the writer disarmed and in `dry`: it compiles and
publishes, and nothing touches the inverter. Arming is the last step before
going live, in the checklist under "Going live".

1. **The add-on.** Add the community repository
   `https://github.com/davidusb-geek/emhass-add-on` under Settings, Add-ons,
   Add-on store. Install EMHASS. In its Network panel set the host port to
   disabled (null). Turn `auto_update` off. Start it once so
   `/addon_configs/5b918bf2_emhass/` exists.
2. **pyscript.** Install it through HACS. In `configuration.yaml`:

   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```

   The planner imports `homeassistant.components.recorder`, and both
   wrappers read `hass.config.time_zone`.
3. **Solarman.** Install it with the `deye_p3` profile and name the device
   `inverter`, so the entities come out as `sensor.inverter_battery`,
   `number.inverter_battery_max_charging_current` and so on. Another name
   means editing every id in `plant.json` and the YAML hard-codes ("The entity
   map"). Check that the nine levers under "The levers" exist and are
   available: an unavailable lever makes every tick fall back to the baseline.
4. **The core.** Copy `emhasscore/` to `/config/pyscript_helpers/emhasscore/`
   and `ha/pyscript_helpers/emhass_core.py` next to it.
5. **The packages.** Copy `ha/packages/emhass/` to `/config/packages/emhass/`
   (with `homeassistant: packages: !include_dir_named packages` in
   `configuration.yaml`). `input_boolean.emhass_writer_armed` has no
   `initial:`, so a fresh install starts disarmed. Exclude the busy entities
   from the recorder:

   ```yaml
   recorder:
     exclude:
       entities:
         - sensor.emhass_writer
         - sensor.emhass_writer_memory
         - sensor.emhass_plan_yesterday
         - sensor.emhass_plan_today
         - sensor.emhass_plan_next_day
         - sensor.emhass_plan_day_after
   ```

6. **The wrappers.** Copy `ha/pyscript/emhass_shadow.py` and
   `ha/pyscript/emhass_writer.py` to `/config/pyscript/`. They take every
   entity from `plant.json`.
7. **The add-on config.** Edit the sensor names in `ha/config.json` (see
   `README-config.md`), then run `ha/scripts/deploy.sh`.
8. **Restart HA and seed the helpers** with `ha/scripts/seed_helpers.sh` (no
   helper has an `initial:`, and an unseeded `input_number` sits at its
   minimum). Edit the script first: the tariff lines are Dutch 2026 values,
   and `emhass_soc_min`, `emhass_batt_power_max_w`, `emhass_heat_cut_c` and
   `emhass_heat_cut_kw` must suit your pack. It sets the mode to `dry` and
   leaves the writer disarmed.
9. **A dry solve.** `ha/scripts/check.sh run` solves once through the add-on
   without publishing or archiving and prints the horizon, the predicted-price
   steps, the Solcast gaps, the status and the cost. When it says `Optimal`,
   turn on `input_boolean.emhass_shadow_enabled`. The plan archive starts to
   grow and the writer publishes disarmed dry ticks.

The scripts read `HA_URL`, `HA_LLAT` and `HA_SSH` from the environment or a
repo-root `.env` (`ha/scripts/env.sh`). After any change to `plant.json` or
`emhasscore/`, restart Home Assistant: the core reads the plant file once, at
import, and `pyscript.reload` does not watch `pyscript_helpers/`.

## plant.json

`emhasscore/plant.py` holds the reference plant as `DEFAULTS`. Your file is
merged over it key by key, so it only needs what differs.
`plant.example.json` is a full copy of the reference with placeholders for
ids and coordinates. The core takes the first file it finds: the path in
`EMHASS_PLANT_JSON`; `plant.json` one level above the `emhasscore` package
(the repo root for the backtest, `/config/pyscript_helpers/plant.json` in Home
Assistant); `/config/emhass/plant.json`.

With no file the core runs on `DEFAULTS` and the writer writes nothing, armed
or not: it reads every mode as `dry` and logs `no plant.json found` on every
tick. The reference clamps are 240 A; a smaller pack must not inherit them.
The `plant` attribute of `sensor.emhass_writer` shows the file in force
(null when there is none).

| section | what it is | set it? |
|---|---|---|
| `site` | latitude, longitude, altitude, time zone, for the backtest's PV model and solver clock. The live add-on takes coordinates from Home Assistant | must, for the backtest |
| `battery.capacity_kwh`, `p_nom_kw`, `pack_voltage_v` | usable energy; power limit each way as the planner sees it; nominal voltage (the writer uses the measured one and falls back to this) | must |
| `battery.current_max_a` | the current registers' ceiling, and the value both clamps return to in the baseline. Never above what your pack and BMS allow | must |
| `battery.soc_min`, `soc_max`, `soc_final_target` | the planner's SOC band (live values: the helpers `emhass_soc_min`, `_max`); the horizon-end SOC | must; the last may leave |
| `battery.eta_charge`, `eta_discharge`, `r_ohm`, `side_loss_a`, `side_loss_b_kw` | the loss model: stage efficiencies, the pack-plus-cable resistance the port voltage sees, the cell-to-port loss | may leave (the reference's measured values; refit or accept) |
| `inverter.model`, `p_nom_kw` | a label; the DC-AC bridge rating each way | must (the label is optional) |
| `inverter.grid_charge_ac_max_w` | the grid-only charge ceiling at the AC side | must for a model other than the 12 kW one |
| `inverter.eta_bridge`, `q_port_kw_per_kw2`, `q_bridge_kw_per_kw2`, `standby_load_w`, `eta_v2` | the inverter loss model and the stress pricing | may leave (reference values; refit or accept) |
| `registers` | what the current registers deliver against what they ask (below) | may leave; refit on your unit, or neutralise |
| `baseline` | the register state the writer returns the inverter to | must: it is your inverter's resting state |
| `grid.cap_w` | the grid connection, W each way | must |
| `pv.micro_share`, `pv.micro_max_w` | the AC-coupled microinverter: its share of Solcast site `A`, its nameplate. 0 and 0 when you have none | must |
| `pv.p10_mix`, `pv.curtail_soc_pct` | Solcast P10 weight in the planning PV; the SOC at which the Deye starts holding the main array back | may leave |
| `pv.strings` | tilt, azimuth, kWp, scale per string, for the backtest's pvlib model | must, for the backtest |
| `pv.solcast_sites` | your Solcast rooftop ids. The planner fetches site `A` per half hour for the microinverter split | must |
| `tariff` | energy tax, supplier fee, BTW, feed-in fee, for the backtest. The live planner reads helpers | must, for the backtest |
| `entities` | every hardware and household entity the wrappers read | must, where yours differ |
| `writer_entities` | the nine levers | must, if your Solarman device is not named `inverter` |

### The register calibration

The current registers deliver less than they ask, and not by a fixed amount.
On the reference unit (121 binding intervals, 2026-09-26 to 2026-09-29) the
discharge clamp falls short by about 0,030 A per A up to about 150 A, then by
about 4,3 A; the grid charging current delivers 0,951 of what it asks; the
charge clamp about 1 A less. The writer asks for the register value that
delivers the plan's current (`writer.calibrate_amps`). These numbers are one
unit's fit. To turn the correction off, set `discharge_short_slope` 0,
`discharge_short_icpt_a` 0, `grid_charge_gain` 1 and `charge_short_a` 0.

## The entity map

`plant.json` `entities` (reference values in `plant.example.json`):

| key | what it must be |
|---|---|
| `pv`, `load` | PV power, W, everything the house makes; the physical load on the inverter output, W |
| `grid_import`, `grid_export` | the grid meter, kW, one sensor per direction |
| `batt_power`, `batt_voltage`, `batt_current` | at the DC port: W, V, A, positive = discharge. An unavailable voltage makes a degraded tick |
| `soc` | pack SOC, %. Input of the SOC-floor kill and of the live anchor |
| `micro`, `export_switch` | the AC-coupled microinverter, W (a sensor that reads 0 if you have none); the export-surplus switch |
| `curtailed` | `sensor.pv_curtailment_active`, created by `emhass_curtailment.yaml`; leave it |
| `solcast_today`, `solcast_tomorrow`, `solcast_day3` | the Solcast day sensors |
| `epex` | the EpexPredictor sensor (attribute `forecast`) |
| `nordpool_entry`, `nordpool_area` | your Nord Pool config entry id, and the key of the price list `nordpool.get_prices_for_date` returns (`NL`, `DE-LU`, `SE3`, ...) |
| `notify` | the notify service the writer's alerts go to |

Some ids and values are written into the YAML, not read from `plant.json`.
With a Solarman device named `inverter` and the Nord Pool area NL they match;
otherwise edit them by hand:

| file | hard-coded |
|---|---|
| `emhass_writer.yaml` | the nine lever entities; the baseline values in `script.emhass_deye_baseline`, including 240 A for both clamps; `sensor.inverter_battery`, `sensor.inverter_battery_voltage`, `sensor.inverter_microinverter_power`; `notify.notify` |
| `emhass_pack_temp.yaml` | `sensor.inverter_battery_temperature` |
| `emhass_curtailment.yaml` | `sensor.inverter_battery`, `switch.inverter_export_surplus` |
| `emhass_prices.yaml` | `sensor.nord_pool_nl_current_price` |
| `emhass_automations.yaml` | `binary_sensor.nord_pool_nl_tomorrow_price_available`, `notify.notify` |
| `emhass_health.yaml` | `notify.notify` |
| `emhass_epex_capture.yaml` | `region=NL` in the capture URL |
| `ha/config.json` | the add-on's own sensor names (`README-config.md`) |

`script.emhass_deye_baseline` must write the same values as your `plant.json`
`baseline` and `battery.current_max_a`. `tests/test_writer.py` pins the script
to the core's restore order and baseline values, so change both together.

## The writer

### Arming and modes

`input_boolean.emhass_writer_armed` is the master switch. Off means hands off:
nothing in the pyscript writer or the YAML watchdogs writes to the inverter.
The writer reads every mode as `dry` and skips its whole write block (no plan
writes, no ceiling writes, no restore). All five automations that write
carry it as their first condition. While disarmed, the safety backstops are
off too: the SOC-floor kill, the heat backstop, the dead man and the
grid-charge cap do nothing, and registers stay as they stand. Arming takes
effect at the next tick. The kill switch script runs whenever you call it,
armed or not.

`input_select.emhass_writer_mode`, options `dry`, `off`, `live`, decides what
an armed writer writes. A fresh install starts at `dry`.

| mode (armed) | compiles the plan | writes |
|---|---|---|
| `dry` | yes | nothing from the plan. Only (a) a ceiling write that lowers a current (SOC-floor kill, heat backstop) and (b) the baseline after leaving `live`, retried every tick until it reads back. The YAML watchdogs are on |
| `off` | no | the baseline, enforced on every tick: anything that stands off it, including a change you make by hand, is written back |
| `live` | yes | the plan's record |

`off` is not a resting state. To stop the writer and keep the backstops, set
`dry`. While armed, a mode that reads as anything other than the three
options (unknown, unavailable) is taken as `off`. Without a `plant.json` the
pyscript writer writes nothing even when armed, but the armed YAML watchdogs
act: arm only once `plant.json` and the baseline script match.

The tick runs at :00, :15, :30 and :45 plus 20 s, on every mode change, 180 s
after pyscript starts, and at once when the SOC or the 1 h pack temperature
crosses a safety latch. A cron tick gives way to a tick that is still writing.
Switching to `live` writes `/config/emhass/writer/anchor.json` with the real
SOC; in live mode every solve starts from the real pack SOC.

### What one tick does

A tick takes the newest Optimal plan in the archive whose horizon covers now,
compiles its row now and the row after it into register records
(`deye.deye_command`, at the loaded pack voltage, with the calibration and the
charge margin), applies the hold rule and the fallbacks, caps the record with
the ceilings, and diffs it against what the inverter reports (through the
phantom guard and the deadbands). Armed, it then writes the ordered list in
`live`, `off` or a pending restore, and only the ceiling writes in `dry`. It
publishes `sensor.emhass_writer` and the heartbeat, and appends one line to
`/config/emhass/writer/<date>.jsonl`.

The intents, read off the plan's battery and grid power (deadband 100 W each):

| intent | plan step | record (fields off the baseline) |
|---|---|---|
| `self_balance` | charging, grid not importing | export on, charge clamp at nameplate, or at the plan's charge when the step sells more than it banks. When the export rule shuts export: export off, clamp at nameplate, or on a step the solver curtails at the plan's charge plus 20 % or 20 A, whichever is bigger |
| `grid_charge` | charging and importing | `battery_grid_charging` on, grid charging current at the plan's charge, `program_6_soc` 100, charge clamp at nameplate (sun above the forecast is banked); export stays on unless the export rule shuts it |
| `export` | discharging and exporting | work mode Export First, peak shaving off, discharge clamp at the plan's discharge |
| `self_supply` | discharging, not exporting | export surplus off, discharge clamp at the plan's discharge |
| `pv_export` | battery idle | both clamps at 0 A (the pack rests), export on |
| any step the plan cuts the microinverter | | `microinverter_export_cut_off` on |

The export rule: export shuts only when the all-in sell price is negative and
the pack is above 90 %. With `input_boolean.emhass_physics_v2` on, the plan's
battery power is the DC bus and the writer converts it to the port power.

### The levers

Nine fields, each one Solarman entity (`writer_entities`). The tier says what
a field costs if a dead controller leaves it standing.

| field | reference entity | tier | baseline |
|---|---|---|---|
| `export_surplus` | `switch.inverter_export_surplus` | wasteful | on |
| `battery_max_charging_current` | `number.inverter_battery_max_charging_current` | wasteful | `current_max_a` |
| `battery_max_discharging_current` | `number.inverter_battery_max_discharging_current` | wasteful | `current_max_a` |
| `microinverter_export_cut_off` | `switch.inverter_microinverter_export_cut_off` | wasteful | off |
| `work_mode` | `select.inverter_work_mode` | costly | Zero Export To Load |
| `grid_peak_shaving` | `switch.inverter_grid_peak_shaving` | costly | on |
| `battery_grid_charging_current` | `number.inverter_battery_grid_charging_current` | dangerous | 0 A |
| `battery_grid_charging` | `switch.inverter_battery_grid_charging` | dangerous | off |
| `program_6_soc` | `number.inverter_program_6_soc` | dangerous | 5 % |

Entry order is the table's order, dangerous last (the cap, the permission,
then the TOU target), so a controller that dies mid-sequence has set nothing
that spends money. Restore order is dangerous first: `program_6_soc`,
`battery_grid_charging`, `battery_grid_charging_current`, `work_mode`,
`grid_peak_shaving`, then the wasteful fields. A tick writes the fields going
back to the baseline first, in restore order, then those leaving it.

The writer never writes four registers: `energy_pattern`,
`zero_export_power`, `export_surplus_power`, `program_6_power`. Nor does it
write the time-of-use table. On the reference inverter Time of Use is enabled
and all six program start times read 00:00, so program 6 is the slot in force
at every hour and `program_6_soc` alone steers grid charging. Set these by
hand (see "Going live").

Every write is followed by a readback: the tick polls the entity every 3 s for
up to 150 s. On the reference unit the Solarman integration echoes a write
within seconds and the power moves within about 30 s. A write that does not
read back marks the tick `degraded`, the sequence goes on, and the next tick
retries the field because the diff still shows it. A tick with failing writes
can therefore take minutes.

### Safety layers

All of these act only while armed.

| layer | what it does |
|---|---|
| SOC-floor kill | while the real SOC is under `input_number.emhass_soc_min`, the discharge clamp is held at 0 A, in every mode. Released at the floor plus 2 points. A floor of 0 disables it. A SOC crossing triggers a tick at once |
| heat backstop | while the 1 h mean pack temperature (`sensor.emhass_pack_temp_1h`) is at or above `input_number.emhass_heat_cut_c`, the charge clamp, the grid charging current and the discharge clamp are capped at the current that holds the pack port at `input_number.emhass_heat_cut_kw` (rounded down to 5 A), in every mode. Released 1 °C under the cut. 0 kW disables it, which is where an unseeded helper sits |
| stale plan | a plan whose solve started more than 60 minutes ago, or no plan covering now: the baseline. 60 minutes tolerates one lost refresh |
| unavailable lever | any lever, or the pack voltage, unavailable: the tick is `degraded` and the record is the baseline |
| phantom guard | the Solarman settings block now and then returns a cycle of zeros for every register in it. A read that changed with no write of the writer's own since is held at the remembered value for one tick and accepted when the next tick reads it again. A real outside change is acted on one tick late. The memory (`sensor.emhass_writer_memory`) is lost on a restart; the first tick after it runs unguarded |
| hold rule | an intent reaches the registers only when the plan holds it for two consecutive steps |
| leaving live | switching from `live` to `dry` or `off` writes the baseline, and in `dry` retries it every tick until every field reads back. It never writes the plan's values |
| ceilings | the two above go through every deadband: a current above its ceiling is always written down, and a ceiling never raises a value that stands lower |

### The YAML watchdogs

The registers latch and the inverter has no communication-loss timeout, so a
dead pyscript must be undone from YAML. The five automations that write have
`emhass_writer_armed` on as their first condition; none reads `plant.json`.

| automation | when (armed) | does |
|---|---|---|
| `emhass_writer_dead_man` | every 5 minutes: the heartbeat is more than 20 minutes old (a missing heartbeat counts as infinitely old) AND grid charging is on, or work mode is Export First, or program 6 SOC is above 50 | runs `script.emhass_deye_baseline`, notifies |
| `emhass_writer_grid_charge_cap` | every 5 minutes: grid charging on for 35 minutes AND NOT (a heartbeat younger than 16 minutes and the writer's intent `grid_charge`) | runs the baseline script, notifies |
| `emhass_writer_soc_floor_backstop` | every 5 minutes at :30 s: heartbeat older than 20 minutes, SOC under the floor, discharge clamp above 0 A | sets the discharge clamp to 0 A, notifies. Only lowers |
| `emhass_writer_heat_backstop` | every 5 minutes at :45 s: heartbeat older than 20 minutes, 1 h pack temperature at or above the cut | caps the three currents at the cut power over the pack voltage (no calibration, rounded down to 5 A), notifies. Only lowers |
| `emhass_writer_baseline_on_start` | Home Assistant starts and the mode is not `live` | runs the baseline script |
| `emhass_writer_growatt_cut_notify` | the microinverter export cut-off turns on, whoever wrote it | notifies only, armed or not |

`script.emhass_deye_baseline` is the manual kill switch: it writes the nine
levers back to the baseline in restore order, with `continue_on_error` so one
unavailable entity does not stop the rest.

While armed, two things happen even in `dry`: a restart of Home Assistant
writes the baseline, and a grid charge that stands 35 minutes without the
writer holding it (yours, or another controller's) is taken down. pyscript
sets the heartbeat as soon as it starts, so a live record standing across a
restart is not taken down before the writer's startup tick can vouch for it.

### The write budget

The writer treats every register write as wear on the inverter's settings
memory. The endurance of that memory is not known to this project, so the
writer keeps the rate low and counts it.

- Nothing is written that already stands (a current within 0,5 A is the same
  value), and the hold rule keeps one-step intents off the registers.
- Deadbands: 20 A on the charge clamp (a clamp below a wanted grid-charge
  current is always raised), 5 A on the discharge clamp (the amps follow the
  sagging measured voltage), 20 A on the grid charging current (a move to or
  from 0 A is always written). Quantisation is 1 A; a coarser step and a
  segment-mean mode exist in `writer.WRITER_KNOBS` and are off. These, the
  margins, the stale limit and the export rule are constants in
  `emhasscore/deye.py` and `emhasscore/writer.py`, not helpers.
- `write_counts` counts every write sent per field, cumulative, and resumes
  from the archive after a reload. On the reference site's first live day the
  writer sent 24 writes, six of them caused by phantom reads (the guard was
  added that evening).

### Notifications

From the writer (to `entities.notify`, at most one per hour per status): a
tick with a failed write, and in `live` a tick that is `stale` or `degraded`.
Also the SOC-floor kill and the heat backstop when they trip (the release is
silent). These two notify only while armed: disarmed, nothing was capped.
From the YAML (to `notify.notify`): each watchdog when it acts, the
microinverter cut, a planner plan stale for 30 minutes, and the add-on health
check off for an hour (re-nags at 08:00 and 20:00, and an all clear).

### sensor.emhass_writer

State: the mode as the writer read it (`dry` while disarmed or without a
plant file). Attributes, also one JSON line per tick in
`/config/emhass/writer/<date>.jsonl`:

| attribute | meaning |
|---|---|
| `armed`, `plant` | the arming switch; the plant file in force, or null |
| `tick_ts`, `plan_ts` | the tick time; the solve start of the plan in force |
| `status` | `ok` (live), `dry`, `off`, `restore`, `stale`, `degraded` |
| `intent`, `next_intent`, `held` | this step's intent, the next step's, and whether the hold rule fell back to the baseline |
| `record` | the wanted record's fields that differ from the baseline |
| `standing`, `suspect` | what the inverter reported after the phantom guard; the reads the guard held back, as `[field, read, memory]` |
| `diff`, `writes` | field: `[standing, wanted]` for every field that has to move; the ordered write list |
| `safety_writes` | the part of `writes` that brings a field down to a ceiling |
| `written`, `failed` | `[field, value, latency_s]` of this tick's writes |
| `write_counts` | writes sent per field, cumulative |
| `unavailable` | levers (and `pack_v`) that read unavailable |
| `ceilings`, `soc_floor`, `heat_cut` | the active ceilings and the state of the two latches |

## Going live

Do these in order. Until step 5 the writer stays disarmed and nothing in this
package touches the inverter.

1. **Disarmed dry ticks.** After the install, `sensor.emhass_writer` shows
   `armed` false, state `dry`, and an `intent` once a plan is archived.
2. **Write `plant.json`** and restart Home Assistant. The `plant` attribute
   must show its path, and the log must not show `no plant.json found`. Set
   `current_max_a`, `soc_min`, `soc_max`, `p_nom_kw` and the baseline, and
   the helpers `emhass_soc_min`, `emhass_soc_max`, `emhass_batt_power_max_w`,
   `emhass_heat_cut_c`, `emhass_heat_cut_kw`, to values your battery's
   datasheet and BMS allow.
3. **Edit `script.emhass_deye_baseline`** in `emhass_writer.yaml`. It
   hard-codes the clamp current (240 A) and the other baseline values; they
   must match `battery.current_max_a` and `baseline` in your `plant.json`.
   Reload the scripts.
4. **Set the inverter by hand.** Set the four never-written registers to your
   `baseline` values and check your TOU table (see "The levers"). Make sure
   nothing else drives the inverter: your supplier's EMS, a cloud schedule or
   another automation that writes the same registers would fight the writer.
5. **Arm.** Turn on `input_boolean.emhass_writer_armed` with the mode at
   `dry`. From the next tick the ceiling writes and restores act, and so do
   the YAML watchdogs.
6. **Armed dry ticks, several days.** Read the day's file:

   ```bash
   jq -c '[.tick_ts, .status, .intent, .held, .record, .writes]' /config/emhass/writer/2026-10-02.jsonl
   ```

   Compare each intent and record with what you would do at that hour and
   price. Check that no tick is `degraded` or `stale` while the planner runs,
   that `grid_charge` appears only on steps where the plan imports, that the
   currents in `record` stay inside what your pack takes, that `suspect`
   stays rare, and that the `writes` per day are a rate you accept. The diff
   is against whatever the inverter holds now, so the first ticks show a
   large one. Run `script.emhass_deye_baseline` once by hand and watch the
   nine entities read back the baseline (this writes to the inverter).
7. **Live.** Set the mode to `live` at a quiet moment (pack idle, no grid
   charge in the next steps). Watch `written` and `failed`, the levers, the
   pack power and the grid meter through the first planned sale and the
   first planned grid charge.

**To stop**, set the mode to `dry`. The writer restores the baseline and
retries until every field reads back, and the backstops stay on. For an
immediate baseline, also run `script.emhass_deye_baseline`.

**Emergency hands-off**: turn `input_boolean.emhass_writer_armed` off. Nothing
in this package writes to the inverter after that, the backstops included,
and the registers stay as they stand. Run the kill switch script first if you
want the baseline in place.

## What the packages create

| file | creates |
|---|---|
| `emhass_writer.yaml` | `input_boolean.emhass_writer_armed`, `input_select.emhass_writer_mode`, `script.emhass_deye_baseline`, the six automations under "The YAML watchdogs" |
| `emhass_helpers.yaml` | `input_boolean.emhass_shadow_enabled` (the planner's master switch: without it no plan is archived and the writer falls back to the baseline), `emhass_ml_enabled` (load model fit and tune; turn on at day 10), `emhass_salderen` (Dutch net metering: energy tax 0 while on), `emhass_physics_v2` (loss map v2 on every solve and in the writer), four `dash_shadow_*` for a private dashboard; the tariff, knob, temperature-ramp and heat-backstop `input_number`s, one line each in `seed_helpers.sh`; `input_datetime.emhass_soc_target_at`; two A/B scripts |
| `emhass_pack_temp.yaml` | `sensor.emhass_pack_temp_1h`, the 1 h mean pack temperature the ramp and the heat backstop read |
| `emhass_curtailment.yaml` | `sensor.pv_curtailment_active` (1 when the SOC is above 95 % and export is off), for the PV reconstruction |
| `emhass_prices.yaml` | `sensor.emhass_buy_price_now` = (day-ahead + tax + fee) x (1 + BTW), `sensor.emhass_sell_price_now` = (day-ahead - feed-in fee) x (1 + BTW) |
| `emhass_health.yaml` | `sensor.emhass_plan_age_h`, `binary_sensor.emhass_plan_stale`, the stale-plan notification |
| `emhass_epex_capture.yaml` | `input_boolean.emhass_epex_capture_enabled` and a twice-daily capture of the raw EpexPredictor response to `/config/emhass/epex_raw/`. It calls a third-party site (`epexpredictor.batzill.com`) from your HA host; leave the gate off if you do not want that |
| `emhass_automations.yaml` | the schedule below |

pyscript writes `sensor.emhass_last_run`, the day slices
`sensor.emhass_plan_yesterday`, `_today`, `_next_day`, `_day_after`,
`sensor.emhass_pack_temp_used`, `sensor.emhass_ab_last`,
`sensor.emhass_last_fit`, `_last_tune`, `binary_sensor.emhass_addon_healthy`,
`sensor.emhass_writer`, `_writer_heartbeat`, `_writer_memory`, and the
statistics `emhass:ladder_earned_eur`, `emhass:shadow_*` (charge, discharge,
import, export, SOC, PV, load per hour), `emhass:tariff_buy_ct`, `_sell_ct`.
The add-on's `publish-data` writes eleven `sensor.emhass_da_*` entities
(`p_batt`, `soc`, `p_grid`, `p_pv`, `p_load`, `p_curtail`, `p_hybrid`, `buy`,
`sell`, `cost`, `status`), each with the whole horizon in a `forecasts`
attribute. To chart without the private dashboard: the future from those
`forecasts`; the past from the per-step arrays of the day slices (`p_batt_w`,
`p_grid_w`, `soc_pct`, `p_pv_w`, `p_load_w`, `pv_curtail_w`, `buy`, `sell`,
`cost_eur` from `slice_start` at `step_min`).

## The schedule

| automation or trigger | when (local) | what |
|---|---|---|
| `emhass_plan_on_prices` | tomorrow's Nord Pool prices publish (observed 13:00:04) | plan, archive, roll, publish |
| `emhass_plan_fallback` | 15:00, if tomorrow is still unplanned | the same, with predicted prices, flagged |
| `emhass_plan_refresh` | :13, :28, :43, :58 while the add-on is healthy; 4 minutes after a start | re-plan |
| writer cron (pyscript) | :00, :15, :30, :45, plus 20 s | one writer tick |
| `emhass_ladder_nightly` | 00:10 | walk the ladder over the last four days |
| `emhass_fit` / `emhass_tune` | 04:00 / Sunday 03:30, ML switch on | fit the load model on 13 days / tune it |
| `emhass_salderen_apply`, `_end` | on the netting switch; 00:00:10 daily and at start | the Dutch energy tax follows netting, which ends by law on 1 Jan 2027 |
| `emhass_health_degraded`, `_recovered`, `emhass_stale_notify` | health off for 1 h (re-nag 08:00, 20:00), back on; plan stale 30 min | notifications |
| `emhass_epex_capture` | 00:05 and 12:05 | the EpexPredictor forecast as issued, to disk |

pyscript also runs `emhass_health` every 15 minutes, and `emhass_rehydrate`
120 s after a start (the writer's startup tick follows 60 s later). At :13 two
of the running step's three 5-minute statistics buckets have landed.

## The archive under /config/emhass/

It is inside HA backups.

```
/config/emhass/
  plant.json              your site, if you keep it here rather than in /config/pyscript_helpers/
  config.json             the deployed add-on config, what the drift check compares against
  plans/<plan_ts>.json.gz one document per solve (~17 kB, ~100 a day), with the exact payload posted
  writer/<date>.jsonl     one line per writer tick, one file per local day
  writer/anchor.json      the real SOC at the last switch to live
  ladder.csv, ladder_hours.csv, tariff_hours.csv   the ladder per day and per hour, the tariff per hour
  rebalance.json          the settled rebalancing clock (when the pack last sat full)
  ab/<label>.json.gz      A/B runs
  epex_raw/YYYY-MM.jsonl  the EpexPredictor forecast as issued
```

## Where each knob lives

A value in a lower layer is silently beaten by the one above it.

1. **The runtime payload**, from the knob helpers, posted with every solve:
   stress and SOC costs, `soc_final`, `weight_battery_discharge`, battery
   power limits, SOC bounds, the horizon, `soc_init`, the PV, load and prices.
2. **`ha/config.json`**: everything else. Edit, then `deploy.sh`.
3. **The add-on web UI. Never.** A save there rewrites the live file with a
   defaults-expanded blob and takes `binary_sensor.emhass_addon_healthy` off,
   which stops re-planning until the drift is cleared.

## The scripts

- `deploy.sh`: validate `config.json`, copy it to the add-on and to
  `/config/emhass/`, restart the add-on, run the drift checks. Needs `HA_SSH`.
- `check.sh [contain|config|health|actions|run|layers]`: read-only checks;
  exit code = failures.
- `seed_helpers.sh`: set every helper once and the writer mode to `dry`; it
  never arms the writer. Needs `HA_URL`, `HA_LLAT`. Re-running it overwrites
  what is set in HA, including a `live` mode.
- `env.sh`: sourced by the three; reads the repo-root `.env`.

## Known limits

- Beta. The writer went live on the reference site on 2026-09-26: one site,
  one model (SUN-12K-SG04LP3, Solarman `deye_p3`), one unit, which the
  register calibration and the loss model were fitted on. Another model or
  firmware may name or scale the registers differently.
- The intent semantics (Export First needs peak shaving off to sell from the
  pack, grid charging needs the TOU target at 100 %, the grid charging
  current is a floor under sun) were measured on the reference inverter with
  its TOU table and settings. Verify them on yours.
- The writer has no fuse rule and no grid-current limit of its own. The grid
  cap is a solver constraint; the inverter's own protection and your limits
  are the rest.
- The tariff helpers, the net-metering automations and the EpexPredictor
  capture are Dutch. Elsewhere, set the tariff helpers to your own and leave
  `emhass_salderen` off.

## Gotchas worth knowing before the first week

- Lists, not dicts, for every forecast passed to EMHASS: a dict whose
  timestamps miss the add-on's own grid collapses silently to a constant
  while reporting `Optimal`. Every list has exactly `n` items from `t0`.
- DST days have 92 or 100 steps, not 96; the core counts through UTC, the
  writer included.
- The ML forecaster predicts exactly `num_lags` steps and aborts the solve
  when the horizon is longer; the fit posts 288, not the file's 96.
- Passing `load_power_forecast` flips EMHASS to the `list` load method and
  silently overrides the ML forecaster, so the planner passes its own load
  shape only while `emhass_ml_enabled` is off.
- The add-on holds one `opt_res_latest`. A/B solves overwrite it, so every
  such service ends with a live re-plan.
