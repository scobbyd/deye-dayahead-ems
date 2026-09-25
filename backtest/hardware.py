"""A different pack or hybrid inverter for one replay run.

Two numbers describe the site's storage hardware to the walk: the pack's
nominal capacity in kWh (config.json battery_nominal_energy_capacity, 48,2 =
three 16 kWh modules) and the hybrid inverter's AC nominal in kW (12, the
Deye SG04LP3-12K). Both sit in several places at once, so a run that pretends
another set has to move all of them together, and they are frozen in the run's
meta.json like the knobs, the tariff and the plant scale:

  LP        plant_conf battery_nominal_energy_capacity, inverter_ac_output_max
            and _input_max, battery_charge/discharge_power_max; and the knob
            batt_power_max_w the payload posts (12.500 W for the 12 kW Deye,
            i.e. nominal + 500 W, kept as that rule)
  pack      capacity_kwh passed to virtual_day / ladder_run explicitly (their
            defaults are bound at import)
  actuator  deye.DEYE_CURRENT_MAX_A, the register's ceiling, scaled from its
            shipped 240 A at 12,5 kW (336 A for a 17 kW unit)
  stress    objective.P_NOM_INV_KW, the inverter stress cost's nominal

What does NOT scale: the loss map's Q_PORT / Q_BRIDGE (calibrated on the 12 kW
Deye; a 17 kW unit would have its own), the 20 A clamp margin and deadband,
the grid connection (17.250 W, 3 x 25 A), the BMS current per module. A run
with a bigger inverter therefore carries the 12 kW unit's loss shape.
"""
from __future__ import annotations

from emhasscore.plant import PLANT

DEFAULT = {"capacity_kwh": float(PLANT["battery"]["capacity_kwh"]), "inverter_kw": float(PLANT["inverter"]["p_nom_kw"])}
_SHIPPED_A = float(PLANT["battery"]["current_max_a"])          # the register's ceiling at the shipped pack power
_SHIPPED_W = float(PLANT["battery"]["p_nom_kw"]) * 1000.0


def normalize(hw: dict | None) -> dict:
    out = dict(DEFAULT)
    for k in DEFAULT:
        if hw and hw.get(k) is not None:
            out[k] = float(hw[k])
    return out


def is_default(hw: dict) -> bool:
    return all(abs(float(hw[k]) - DEFAULT[k]) < 1e-9 for k in DEFAULT)


def batt_power_max_w(hw: dict) -> float:
    """The knob's rule: the inverter's nominal plus 500 W (12 kW -> 12.500)."""
    return round(float(hw["inverter_kw"]) * 1000.0 + 500.0, 1)


def apply(hw: dict) -> None:
    """Move the module-level constants the settlement and the stress cost read
    at call time. Idempotent; the default hardware restores the shipped values."""
    from emhasscore import deye, objective
    hw = normalize(hw)
    objective.P_NOM_INV_KW = float(hw["inverter_kw"])
    # the register's ceiling scales with the inverter from its shipped 240 A at 12,5 kW
    deye.DEYE_CURRENT_MAX_A = float(round(_SHIPPED_A * batt_power_max_w(hw) / _SHIPPED_W))


def knobs_for(knobs: dict, hw: dict) -> dict:
    hw = normalize(hw)
    if is_default(hw):
        return knobs
    return dict(knobs, batt_power_max_w=batt_power_max_w(hw))


def patch_solver(solver, hw: dict) -> None:
    """The LP's plant: capacity in Wh, the inverter's AC limits and the pack's
    power limits, on the solver's already-built params."""
    hw = normalize(hw)
    if is_default(hw):
        return
    pc = solver.params["plant_conf"]
    pc["battery_nominal_energy_capacity"] = round(float(hw["capacity_kwh"]) * 1000.0)
    pc["inverter_ac_output_max"] = round(float(hw["inverter_kw"]) * 1000.0)
    pc["inverter_ac_input_max"] = round(float(hw["inverter_kw"]) * 1000.0)
    pc["battery_charge_power_max"] = batt_power_max_w(hw)
    pc["battery_discharge_power_max"] = batt_power_max_w(hw)


def label(hw: dict) -> str:
    """The run-dir suffix: '' for the site as built, else _c<kWh>_i<kW>."""
    hw = normalize(hw)
    if is_default(hw):
        return ""
    return f"_c{hw['capacity_kwh']:g}_i{hw['inverter_kw']:g}"
