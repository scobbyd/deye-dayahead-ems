"""The plant file: the reference DEFAULTS are the numbers the core was built and
calibrated on, a file only carries what differs, and no file is a safety state
the writer reads (it does not write)."""
import json

import emhass_core as core
from emhasscore import plant


def test_the_core_constants_are_the_reference_plant():
    # the golden pin is built on these; a default that moves moves every plan
    assert core.CAPACITY_KWH == 48.2 and (core.SOC_MIN, core.SOC_MAX) == (0.10, 1.00)
    assert (core.ETA_C, core.ETA_D) == (0.961, 0.957)
    assert (core.Q_PORT, core.Q_BRIDGE) == (0.0042, 0.0005)
    assert core.DEYE_CURRENT_MAX_A == 240.0 and core.DEYE_PACK_V == 51.2 and core.DEYE_PACK_R_OHM == 0.0046
    assert core.GROWATT_SHARE == 0.288 and core.PV_P10_MIX == 0.20 and core.PV_CURTAIL_SOC_PCT == 95.0
    assert (core.BATT_SIDE_A, core.BATT_SIDE_B_KW) == (0.0037, 0.0021)
    assert core.deye.DEYE_GRID_CHARGE_GAIN == 0.951


def test_the_baseline_takes_both_clamps_from_the_pack_ceiling():
    assert core.DEYE_BASELINE == {
        "work_mode": "Zero Export To Load", "energy_pattern": "Load First", "zero_export_power": 25.0,
        "export_surplus": True, "export_surplus_power": 14500.0,
        "battery_max_charging_current": 240.0, "battery_max_discharging_current": 240.0,
        "battery_grid_charging": False, "battery_grid_charging_current": 0.0,
        "microinverter_export_cut_off": False, "grid_peak_shaving": True,
        "program_6_soc": 5.0, "program_6_power": 12000.0,
    }


def test_the_writer_entity_map_covers_every_field():
    assert set(core.WRITER_ENTITY) == set(core.WRITER_FIELDS)
    assert core.WRITER_ENTITY["work_mode"] == "select.inverter_work_mode"


def test_a_file_overrides_only_what_it_carries(tmp_path):
    f = tmp_path / "plant.json"
    f.write_text(json.dumps({"battery": {"capacity_kwh": 10.0, "current_max_a": 100.0},
                             "entities": {"soc": "sensor.my_soc"}}))
    p = plant.load(str(f))
    assert p["battery"]["capacity_kwh"] == 10.0 and p["battery"]["current_max_a"] == 100.0
    assert p["battery"]["eta_charge"] == 0.961                     # untouched keys keep the reference
    assert p["entities"]["soc"] == "sensor.my_soc" and p["entities"]["load"] == "sensor.inverter_load_ups_power"
    assert plant.find(str(f)) == str(f)
    assert plant.DEFAULTS["battery"]["capacity_kwh"] == 48.2      # the merge does not mutate DEFAULTS


def test_no_file_is_no_source(tmp_path):
    assert plant.find(str(tmp_path / "missing.json")) is None
    assert plant.load(str(tmp_path / "missing.json")) == plant.DEFAULTS
