"""The plant: every site-specific number and entity the core, the wrappers and
the backtest read.

One JSON file describes the site. The core loads it once at import. DEFAULTS
is the reference plant this code was built and calibrated on (a Deye
SUN-12K-SG04LP3 hybrid with a 48,2 kWh LiFePO4 pack); any key left out of the
file falls back to it, so a file only carries what differs.

Search order for the file:
  1. the EMHASS_PLANT_JSON environment variable
  2. plant.json one level up from this package (the repo root of the public
     extraction; pyscript_helpers/ inside Home Assistant)
  3. /config/emhass/plant.json
  4. none: DEFAULTS, and SOURCE is None

SOURCE None is a safety state, not a convenience: the writer does not write
(it runs as dry) until a plant file says what pack and inverter it drives.
The reference clamps are 240 A; a smaller pack must not inherit them.

The package must stay standard-library only (it runs inside pyscript), so the
file is JSON, not YAML.
"""
import json
import os

DEFAULTS = {
    "site": {
        # placeholders on purpose: set your own in plant.json
        "latitude": 52.0,
        "longitude": 5.0,
        "altitude_m": 0.0,
        "timezone": "Europe/Amsterdam",
    },
    "battery": {
        "capacity_kwh": 48.2,          # = config.json battery_nominal_energy_capacity / 1000
        "p_nom_kw": 12.5,              # = config.json battery charge/discharge power max
        "pack_voltage_v": 51.2,        # nominal; the writer passes the measured voltage
        "current_max_a": 240.0,        # the clamp register's ceiling, and the baseline both clamps return to
        "eta_charge": 0.961,           # = config.json battery_charge_efficiency
        "eta_discharge": 0.957,        # = config.json battery_discharge_efficiency
        "soc_min": 0.10,               # = config.json battery_minimum_state_of_charge
        "soc_max": 1.00,               # = config.json battery_maximum_state_of_charge
        "soc_final_target": 0.50,      # horizon-end SOC handed to the LP
        "r_ohm": 0.0046,               # pack plus cable as the port voltage sees it (the loaded-voltage model)
        "side_loss_a": 0.0037,         # cells <-> port loss, kW = A*P + B*P^2 with P the port power in kW
        "side_loss_b_kw": 0.0021,
    },
    "inverter": {
        "model": "Deye SUN-12K-SG04LP3",
        "p_nom_kw": 12.0,              # = config.json inverter_ac_output_max
        "eta_bridge": 0.989,           # measured DC<->AC bridge efficiency
        "q_port_kw_per_kw2": 0.0042,   # quadratic loss on the battery port (stress pricing)
        "q_bridge_kw_per_kw2": 0.0005, # quadratic loss on the AC bridge (stress pricing)
        "standby_load_w": 90.0,        # the inverter's own draw that the load register does not see
        "grid_charge_ac_max_w": 11300.0,   # grid-only charge ceiling at the AC side (loss map v2)
        "eta_v2": {                    # loss map v2 linear parts (the physics_v2 knob)
            "dc_ac": 0.981, "ac_dc": 0.990, "charge": 0.977, "discharge": 0.977,
        },
    },
    "registers": {
        # What the current registers deliver against what they ask (deye.deye_delivered_a).
        # Refit on your own binding intervals, or set the slopes to 0 and the gain to 1
        # for asked = delivered.
        "discharge_short_slope": 0.030,
        "discharge_short_knee_a": 150.0,
        "discharge_short_icpt_a": 0.2,
        "grid_charge_gain": 0.951,
        "charge_short_a": 1.0,
    },
    "baseline": {
        # The register state the writer returns the inverter to (off mode, a
        # fallback, a hold). The two clamps come from battery.current_max_a.
        "work_mode": "Zero Export To Load",
        "energy_pattern": "Load First",
        "zero_export_power": 25.0,
        "export_surplus": True,
        "export_surplus_power": 14500.0,
        "battery_grid_charging": False,
        "battery_grid_charging_current": 0.0,
        "microinverter_export_cut_off": False,
        "grid_peak_shaving": True,
        "program_6_soc": 5.0,
        "program_6_power": 12000.0,
    },
    "grid": {
        "cap_w": 17250,                # = config.json maximum_power_from_grid / _to_grid (3 x 25 A)
    },
    "pv": {
        "micro_share": 0.288,          # AC-coupled microinverter share of the first forecast site (0 = none)
        "micro_max_w": 3000.0,         # microinverter nameplate (0 = none)
        "p10_mix": 0.20,               # weight of the P10 forecast in the planning PV (0..1)
        "curtail_soc_pct": 95.0,       # SOC where the hybrid starts holding the main array back
        "strings": {                   # for the pvlib potential model (backtest only); scale is a
            # fitted correction on the pvlib output (1.0 = nameplate)
            "A": {"tilt": 34, "azimuth": 109, "kwp": 9.8, "scale": 1.0},
            "B": {"tilt": 39, "azimuth": 288, "kwp": 9.8, "scale": 1.0},
        },
        "solcast_sites": {             # resource ids from your Solcast account; placeholders here
            "A": "xxxx-xxxx-xxxx-xxxx",
            "B": "xxxx-xxxx-xxxx-xxxx",
        },
    },
    "tariff": {                        # defaults for the backtest; the live planner reads helpers
        "energy_tax": 0.0,
        "supplier_fee": 0.019,
        "btw_pct": 21.0,
        "feedin_fee": 0.019,
    },
    # Every hardware and household entity the wrappers read. The emhass_*
    # helpers and sensors the packages create keep their names and are not
    # listed. Measured series need state_class: measurement (the recorder's
    # 5-minute statistics are what the actuals read).
    "entities": {
        "pv": "sensor.total_pv_power",                                  # W, main MPPTs + microinverter
        "load": "sensor.inverter_load_ups_power",                       # W, the physical load on the inverter output
        "grid_import": "sensor.p1reader2_p1_reader_2_power_consumed",   # kW, fiscal (P1) meter
        "grid_export": "sensor.p1reader2_p1_reader_2_power_returned",   # kW, fiscal (P1) meter
        "batt_power": "sensor.inverter_battery_power",                  # W at the DC port, + = discharge
        "batt_voltage": "sensor.inverter_battery_voltage",              # V at the DC port
        "batt_current": "sensor.inverter_battery_current",              # A, + = discharge
        "soc": "sensor.inverter_battery",                               # % pack state of charge
        "micro": "sensor.inverter_microinverter_power",                 # W, the must-take half; a 0 W sensor when you have none
        "export_switch": "switch.inverter_export_surplus",              # the inverter's export-surplus switch
        "curtailed": "sensor.pv_curtailment_active",                    # 0/1, created by emhass_curtailment.yaml
        "solcast_today": "sensor.solcast_pv_forecast_forecast_today",   # Solcast integration, attribute detailedForecast
        "solcast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
        "solcast_day3": "sensor.solcast_pv_forecast_forecast_day_3",
        "epex": "sensor.epex_predictor_nl",                             # EpexPredictor integration, attribute forecast
        "nordpool_entry": "01XXXXXXXXXXXXXXXXXXXXXXXX",                 # your Nord Pool config entry id
        "nordpool_area": "NL",                                          # the key of the price list that action returns
        "notify": "notify.notify",                                      # where the writer's alerts go
    },
    # The writer's levers (Solarman deye_p3 profile, device named "inverter").
    "writer_entities": {
        "export_surplus": "switch.inverter_export_surplus",
        "battery_max_charging_current": "number.inverter_battery_max_charging_current",
        "battery_max_discharging_current": "number.inverter_battery_max_discharging_current",
        "microinverter_export_cut_off": "switch.inverter_microinverter_export_cut_off",
        "work_mode": "select.inverter_work_mode",
        "grid_peak_shaving": "switch.inverter_grid_peak_shaving",
        "battery_grid_charging_current": "number.inverter_battery_grid_charging_current",
        "battery_grid_charging": "switch.inverter_battery_grid_charging",
        "program_6_soc": "number.inverter_program_6_soc",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidates():
    env = os.environ.get("EMHASS_PLANT_JSON")
    if env:
        yield env
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.normpath(os.path.join(here, "..", "plant.json"))
    yield "/config/emhass/plant.json"


def find(path: str | None = None) -> str | None:
    """The plant file in force: `path` when it exists, else the first hit of the search order."""
    for p in ([path] if path else _candidates()):
        if p and os.path.isfile(p):
            return p
    return None


def load(path: str | None = None) -> dict:
    """The merged plant dict. `path` forces one file; otherwise the search order above."""
    src = find(path)
    if src is None:
        return _deep_merge(DEFAULTS, {})
    with open(src) as f:
        return _deep_merge(DEFAULTS, json.load(f))


SOURCE = find()
PLANT = load()


def reload(path: str | None = None) -> dict:
    """Re-read the file (tests, or a hardware sweep) and return the new dict.
    Modules that copied values into their own constants must re-derive them."""
    global PLANT, SOURCE
    SOURCE = find(path)
    PLANT = load(path)
    return PLANT
