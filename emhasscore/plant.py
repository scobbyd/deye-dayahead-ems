"""The plant: every site-specific number the core and the backtest read.

One JSON file describes the site. The core loads it once at import. The
values shipped in DEFAULTS are the reference plant this code was built on
(a Deye SUN-12K hybrid with a 48,2 kWh LiFePO4 pack); any key left out of the
file falls back to that reference, so a minimal file works.

Search order for the file:
  1. the EMHASS_PLANT_JSON environment variable
  2. plant.json next to the repo root (two levels up from this file)
  3. /config/emhass/plant.json (inside Home Assistant)
  4. the DEFAULTS below

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
        "capacity_kwh": 48.2,          # config.json battery_nominal_energy_capacity / 1000
        "p_nom_kw": 12.5,              # config.json battery charge/discharge power max
        "pack_voltage_v": 51.2,        # nominal pack voltage (Deye current registers work in A)
        "current_max_a": 240.0,        # Deye register ceiling at p_nom_kw
        "eta_charge": 0.961,           # DC stage, charge
        "eta_discharge": 0.957,        # DC stage, discharge
        "soc_min": 0.10,
        "soc_max": 1.00,
        "soc_final_target": 0.50,      # horizon-end SOC handed to the LP
    },
    "inverter": {
        "model": "Deye SUN-12K-SG04LP3",
        "p_nom_kw": 12.0,              # config.json inverter_ac_output_max / _input_max
        "eta_bridge": 0.989,           # measured DC<->AC bridge efficiency
        "q_port_kw_per_kw2": 0.003,    # quadratic loss on the battery port
        "q_bridge_kw_per_kw2": 0.0012, # quadratic loss on the AC bridge
    },
    "grid": {
        "cap_w": 17250,                # config.json maximum_power_from_grid / _to_grid (3 x 25 A)
    },
    "pv": {
        "micro_share": 0.288,          # AC-coupled microinverter share of the first forecast site (0 = none)
        "micro_max_w": 3000.0,         # microinverter nameplate (0 = none)
        "p10_mix": 0.20,               # weight of the P10 forecast in the planning PV (0..1)
        "curtail_soc_pct": 95.0,       # SOC where the hybrid starts holding the main array back
        "strings": {                   # for the pvlib potential model (backtest only); the
            # reference roof: two 9,8 kWp strings, ESE and WNW. scale is a fitted correction
            # on the pvlib output (1.0 = nameplate); fit yours on clean hours or leave 1.0
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
    yield os.path.join(here, "..", "plant.json")
    yield "/config/emhass/plant.json"


def load(path: str | None = None) -> dict:
    """The merged plant dict. `path` forces one file; otherwise the search order above."""
    paths = [path] if path else list(_candidates())
    for p in paths:
        if p and os.path.isfile(p):
            with open(p) as f:
                return _deep_merge(DEFAULTS, json.load(f))
    return _deep_merge(DEFAULTS, {})


PLANT = load()
SOURCE = next((p for p in ([os.environ.get("EMHASS_PLANT_JSON")] + list(_candidates())) if p and os.path.isfile(p)), None)


def reload(path: str | None = None) -> dict:
    """Re-read the file (tests, or a hardware sweep) and return the new dict.
    Modules that copied values into their own constants must re-derive them."""
    global PLANT
    PLANT = load(path)
    return PLANT
