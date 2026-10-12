"""Heating follower: the EMHASS thermal plan step in force, published for the
hybrid-heating executor. Thin pyscript wrapper.

Every quarter hour (+40 s, after the inverter writer's +20 s tick) the tick
reads the newest plan that carried the heat topology, takes the row in force
and the rest of its horizon (emhasscore/thermal.py), and publishes:

  sensor.heating_emhass_plan            AC / PELLET / COMBINED / COAST / stale / off, with the step's
                                        source powers, predicted and bound temperatures, predicted
                                        heat demand, and the horizon series for the dashboard lane
  sensor.heating_emhass_target_living   the living setpoint on the Daikin grid: the highest predicted
                                        temperature to the end of the running source block, the comfort
                                        floor on a COAST step (plus input_number.heating_daikin_target_correction)
  sensor.heating_emhass_target_upstairs the same for the upstairs zone (the multi's block), no correction
  sensor.heating_follower_heartbeat     the tick time

Nothing here commands a device. packages/climate/hybrid-heating reads these
when input_select.heating_planner is emhass and keeps its own safety layers.

Services
  pyscript.heating_follower_tick    one tick

Spec: an internal design note
"""
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("emhass_core", "/config/pyscript_helpers/emhass_core.py")
core = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(core)

from datetime import datetime
from zoneinfo import ZoneInfo

ARCHIVE = "/config/emhass/plans"
PLANNER = "input_select.heating_planner"
PLAN = "sensor.heating_emhass_plan"
TARGET_LIVING = "sensor.heating_emhass_target_living"
TARGET_UPSTAIRS = "sensor.heating_emhass_target_upstairs"
HEARTBEAT = "sensor.heating_follower_heartbeat"
CORRECTION = "input_number.heating_daikin_target_correction"
PELLET_AVAILABLE = "input_boolean.heating_pellet_available"
UPSTAIRS_ENABLED = "input_boolean.heating_upstairs_enabled"
NOTIFY = core.PLANT["entities"]["notify"].split(".", 1)[-1]
TZ = ZoneInfo(hass.config.time_zone)
_EPISODE = {"notified": False}      # one notification per stale/off episode in emhass mode


def _now():
    return datetime.now(TZ)


def _num(entity_id, default):
    try:
        return float(state.get(entity_id))
    except Exception:
        return default


def _is_on(entity_id):
    try:
        return state.get(entity_id) == "on"
    except Exception:
        return False


def _text(entity_id, default):
    try:
        v = state.get(entity_id)
        return default if v in (None, "unknown", "unavailable") else str(v)
    except Exception:
        return default


def _set_target(entity_id, value, name):
    attrs = {"friendly_name": name, "icon": "mdi:thermostat", "unit_of_measurement": "°C",
             "device_class": "temperature"}
    state.set(entity_id, "unknown" if value is None else value, new_attributes=attrs)


@service(supports_response="optional")
def heating_follower_tick():
    """One follower tick: publish the thermal plan step in force and the setpoints."""
    task.unique("heating_follower")
    now = _now()
    try:
        step = task.executor(core.thermal_step_in_force, ARCHIVE, now)
        doc = core.follower_doc(step, now, _num(CORRECTION, 1.0), _is_on(PELLET_AVAILABLE),
                                _is_on(UPSTAIRS_ENABLED))
    except Exception as exc:
        log.warning(f"heating follower: plan read failed, publishing off: {exc!r}")
        doc = {"source": "off", "target_living": None, "target_upstairs": None,
               "attrs": {"tick_ts": now.isoformat(timespec="seconds"), "status": "read error", "plan_ts": None}}
    planner = _text(PLANNER, "legacy")
    attrs = dict(doc["attrs"], friendly_name="Heating EMHASS plan", icon="mdi:home-thermometer-outline",
                 planner=planner)
    state.set(PLAN, doc["source"], new_attributes=attrs)
    _set_target(TARGET_LIVING, doc["target_living"], "Heating EMHASS target living")
    _set_target(TARGET_UPSTAIRS, doc["target_upstairs"], "Heating EMHASS target upstairs")
    state.set(HEARTBEAT, now.isoformat(timespec="seconds"),
              new_attributes={"friendly_name": "Heating follower heartbeat", "icon": "mdi:heart-pulse"})
    degraded = doc["source"] in ("stale", "off")
    if degraded and planner == "emhass":
        if not _EPISODE["notified"]:
            _EPISODE["notified"] = True
            service.call("notify", NOTIFY, title="Heating: EMHASS plan " + doc["source"],
                         message=f"{doc['attrs'].get('status')}. Until a fresh plan arrives the legacy planner takes the living "
                                 f"and the upstairs units park.")
    elif not degraded:
        _EPISODE["notified"] = False
    log.info(f"heating follower: {doc['source']} living {doc['target_living']} upstairs {doc['target_upstairs']} "
             f"plan {doc['attrs'].get('plan_ts')}")
    return {"source": doc["source"], "target_living": doc["target_living"], "target_upstairs": doc["target_upstairs"]}


@time_trigger("cron(0,15,30,45 * * * *)")
def _heating_follower_cron():
    task.sleep(40)                 # the writer ticks at +20 s; the inverter intent lands first
    heating_follower_tick()


@state_trigger(f"{PLANNER}")
def _heating_follower_planner(value=None, **kwargs):
    heating_follower_tick()


@time_trigger("startup")
def _heating_follower_startup():
    task.sleep(90)                 # after the planner's rehydrate
    heating_follower_tick()
