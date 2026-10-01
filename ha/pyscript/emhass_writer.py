"""EMHASS writer: the plan drives the real Deye. Thin pyscript wrapper.

Every quarter hour (+20 s) the tick reads the mode, the nine lever entities
and the pack voltage, hands them with the plan archive to the native core
(emhasscore/writer.py, under task.executor), and in live mode writes the
ordered diff to the inverter with a 150 s readback per field. The tick
document goes to sensor.emhass_writer and /config/emhass/writer/<date>.jsonl;
sensor.emhass_writer_heartbeat is what the YAML dead man watches.

Modes (input_select.emhass_writer_mode): off (the baseline is enforced every
tick), dry (compile and publish the diff, write nothing except a restore still
pending from leaving live, which writes the baseline, never the plan), live
(write). A failed write never aborts the sequence and is retried next tick;
any failed write notifies (plant.json entities.notify). Without a plant.json,
or with input_boolean.emhass_writer_armed off, the writer never writes and
reads every mode as dry.

Services
  pyscript.emhass_writer_tick(restore=False)   one tick; restore=True also executes
                                               the writes when the mode is not live
                                               (a mode change, the startup baseline);
                                               in dry a restore writes the baseline,
                                               never the plan

Spec: an internal design note
"""
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("emhass_core", "/config/pyscript_helpers/emhass_core.py")
core = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(core)

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ARCHIVE = "/config/emhass/plans"
WRITER_DIR = "/config/emhass/writer"
ANCHOR = WRITER_DIR + "/anchor.json"    # the real pack at the live switch; the settled chain re-anchors on it
MODE = "input_select.emhass_writer_mode"
ENTITIES = core.PLANT["entities"]
PACK_V = ENTITIES["batt_voltage"]
PACK_I = ENTITIES["batt_current"]   # + discharges; lets the writer compile at the loaded voltage
STATUS = "sensor.emhass_writer"
HEARTBEAT = "sensor.emhass_writer_heartbeat"
MEMORY = "sensor.emhass_writer_memory"   # the phantom guard's memory (recorder-excluded)
SOC = ENTITIES["soc"]
SOC_FLOOR = "input_number.emhass_soc_min"   # fraction; the planner's floor is the kill's floor
PACK_TEMP_MEAN = "sensor.emhass_pack_temp_1h"   # the heat backstop reads the live 1 h mean, not the ramp's latch
HEAT_CUT_C = "input_number.emhass_heat_cut_c"
HEAT_CUT_KW = "input_number.emhass_heat_cut_kw"   # 0 = backstop off
NOTIFY = ENTITIES["notify"].split(".", 1)[-1]   # the notify service the writer's alerts go to
# No plant file: the writer never writes and the mode reads as dry. The
# reference plant's 240 A clamps must not reach an inverter whose pack nobody
# described (emhasscore/plant.py).
PLANT_SOURCE = core.plant.SOURCE
# The arming switch (emhass_writer.yaml): off, nothing is written in any mode.
ARMED = "input_boolean.emhass_writer_armed"
READBACK_S = 150                   # the "up to 2 minutes" allowance; the power moves within 30 s
POLL_S = 3
TZ = ZoneInfo(hass.config.time_zone)

_COUNTS = {"loaded": False, "counts": {}}
_NOTIFIED = {}                     # status -> last notify time; once per hour per status
_PENDING = {"restore": False}     # a restore begun on leaving live that has not fully read back yet
# The phantom guard's memory (what the writer believes stands, and the last raw
# read) lives in an HA entity, not in a module global. From 2026-09-29 22:30
# two copies of this file ran after a reload and took the ticks in turn, each
# with its own memory: every tick guarded against the write before last, and
# the 12:15 tick of 2026-09-30 skipped a planned grid charge. One entity is
# shared by every copy. It is gone after a restart, which reads as "no memory".
_CEIL = {"soc_floor": None, "heat": None}   # the SOC-floor and heat latches; None after a reload (decided without hysteresis)


def _now():
    return datetime.now(TZ)


def _get(entity_id):
    """state.get that reads a MISSING entity as None instead of raising
    NameError (review finding 2: an unloaded integration or a helper that is
    not there yet must not crash a tick)."""
    try:
        return state.get(entity_id)
    except Exception:
        return None


def _armed():
    return _get(ARMED) == "on"


def _mode():
    if PLANT_SOURCE is None or not _armed():
        return "dry"
    m = _get(MODE)
    return str(m) if m in ("off", "dry", "live") else "off"


def _num(entity_id, default):
    try:
        return float(state.get(entity_id))
    except Exception:
        return default


def _read(field):
    """One lever as the core wants it: bool for a switch, float for a number,
    str for a select, None when unavailable."""
    eid = core.WRITER_ENTITY[field]
    raw = _get(eid)
    if raw in (None, "unknown", "unavailable", ""):
        return None
    if eid.startswith("switch."):
        return raw == "on"
    if eid.startswith("number."):
        try:
            return float(raw)
        except Exception:
            return None
    return str(raw)


def _standing():
    return {f: _read(f) for f in core.WRITER_ENTITY}


def _write(field, value):
    """Write one lever and wait for the readback. Returns the latency in
    seconds, or None on a timeout (the core marks the tick degraded and the
    next tick retries, because the standing value still differs)."""
    eid = core.WRITER_ENTITY[field]
    dom = eid.split(".", 1)[0]
    if _read(field) is None:          # unavailable on entry: do not wait 150 s for a readback that cannot come
        log.warning(f"emhass writer: {field} is unavailable, write skipped")
        return None
    try:
        if dom == "switch":
            service.call("switch", "turn_on" if value else "turn_off", entity_id=eid)
        elif dom == "number":
            service.call("number", "set_value", entity_id=eid, value=float(value))
        elif dom == "select":
            service.call("select", "select_option", entity_id=eid, option=str(value))
    except Exception as e:           # one failed field must not abort the sequence (the YAML script has continue_on_error)
        log.warning(f"emhass writer: {field} -> {value!r} raised {e!r}")
        return None
    waited = 0
    while waited < READBACK_S:
        task.sleep(POLL_S)
        waited += POLL_S
        if core.same(_read(field), value):
            return waited
    log.warning(f"emhass writer: {field} -> {value!r} did not read back within {READBACK_S} s")
    return None


def _soc_floor():
    """(soc %, floor %) as the latch wants them, None when unavailable."""
    soc = _num(SOC, None)
    floor = _num(SOC_FLOOR, None)
    return soc, (floor * 100.0 if floor is not None else None)


def _heat_cut():
    """(1 h mean pack temperature, cut °C, cut kW), None when unavailable."""
    return _num(PACK_TEMP_MEAN, None), _num(HEAT_CUT_C, None), _num(HEAT_CUT_KW, None)


def _memory_load():
    """(standing, seen) from the memory entity; (None, {}) when it is missing or unreadable."""
    try:
        a = state.getattr(MEMORY) or {}
    except NameError:
        return None, {}
    st = a.get("standing")
    return (dict(st) if isinstance(st, dict) else None), dict(a.get("seen") or {})


def _memory_store(standing, seen, tick_ts):
    state.set(MEMORY, tick_ts, new_attributes={"friendly_name": "EMHASS writer guard memory",
                                               "icon": "mdi:memory", "standing": standing, "seen": seen})


def _publish(doc):
    attrs = {k: doc.get(k) for k in ("tick_ts", "plan_ts", "intent", "next_intent", "held", "record", "diff",
                                     "writes", "written", "failed", "write_counts", "status", "unavailable",
                                     "standing", "suspect", "ceilings", "safety_writes", "soc_floor", "heat_cut")}
    attrs.update(friendly_name="EMHASS writer", icon="mdi:pencil-lock", armed=_armed(), plant=PLANT_SOURCE)
    state.set(STATUS, doc["mode"], new_attributes=attrs)
    state.set(HEARTBEAT, doc["tick_ts"], new_attributes={"friendly_name": "EMHASS writer heartbeat",
                                                          "icon": "mdi:heart-pulse"})


def _notify(status, doc, now):
    last = _NOTIFIED.get(status)
    if last is not None and now - last < timedelta(hours=1):
        return
    _NOTIFIED[status] = now
    service.call("notify", NOTIFY, title=f"EMHASS writer {status}",
                 message=f"mode {doc['mode']}, plan {doc.get('plan_ts')}, unavailable {doc.get('unavailable')}, "
                         f"failed {doc.get('failed')}. Inverter at baseline: {not doc['diff']}.")


@service(supports_response="optional")
def emhass_writer_tick(restore=False, yield_to_running=False):
    """One writer tick: compile the plan step in force, diff it against the Deye, write in live and off mode.
    restore: also execute the writes in dry mode (leaving live); a failed restore is retried every tick.
    yield_to_running: the cron tick gives way to a tick already writing (a mode change wins over it instead)."""
    task.unique("emhass_writer", kill_me=bool(yield_to_running))
    now = _now()
    mode = _mode()
    if not _COUNTS["loaded"]:
        _COUNTS["counts"] = task.executor(core.last_counts, WRITER_DIR, now)
        _COUNTS["loaded"] = True
    step = task.executor(core.step_in_force, ARCHIVE, now) if mode != "off" else None
    raw = _standing()
    # The phantom guard (core.guard_standing): a register that reads differently
    # from what the writer last saw, with no write of its own since, is held at
    # the remembered value for one tick. The Solarman settings block returns a
    # cycle of zeros now and then; a forced update does not re-poll it.
    mem_standing, mem_seen = _memory_load()
    standing, suspect = core.guard_standing(mem_standing, mem_seen, raw)
    if suspect:
        log.warning(f"emhass writer: suspect read held against memory {suspect}")
    # The ceilings (core.writer_ceilings) hold in every mode: the SOC-floor kill
    # holds the discharge clamp at 0 A while the real SOC is under the floor.
    # The heat backstop caps every battery current at the cut power while the
    # 1 h mean pack temperature is at or above the cut (released 1 °C under it).
    soc, floor_pct = _soc_floor()
    was = _CEIL["soc_floor"]
    tripped = core.soc_floor_tripped(was, soc, floor_pct)
    _CEIL["soc_floor"] = tripped
    temp, cut_c, cut_kw = _heat_cut()
    heat_was = _CEIL["heat"]
    hot = core.heat_cut_tripped(heat_was, temp, cut_c)
    _CEIL["heat"] = hot
    pack_v = _num(PACK_V, None)
    heat_a = core.heat_cut_amps(cut_kw, pack_v) if hot else {}
    doc = task.executor(core.writer_tick, mode, standing, step, pack_v, now, _COUNTS["counts"],
                        core.DEYE_CLAMP_DEADBAND_A, suspect, None,
                        core.writer_ceilings(soc_floor_tripped=tripped, heat_cut_a=heat_a),
                        _num(PACK_I, None), bool(restore) or _PENDING["restore"])
    doc["soc_floor"] = {"tripped": tripped, "soc": soc, "floor": floor_pct}
    doc["heat_cut"] = {"tripped": hot, "temp": temp, "cut_c": cut_c, "kw": cut_kw, "active": bool(heat_a)}
    if PLANT_SOURCE is None:
        log.warning("emhass writer: no plant.json found, nothing written (see emhasscore/plant.py)")
    elif not _armed():
        pass                               # disarmed: compile and publish, never write
    elif doc["writes"] and core.wants_writes(mode, restore=restore, pending=_PENDING["restore"]):
        results = []
        for f, v in doc["writes"]:
            results.append((f, v, _write(f, v)))
        doc = core.fold_writes(doc, results)
        _COUNTS["counts"] = doc["write_counts"]
        _PENDING["restore"] = bool(doc["failed"]) and mode != "live"      # retried next tick until it reads back
    elif doc["safety_writes"]:            # dry: only the writes that bring a field down to its ceiling
        doc = core.fold_writes(doc, [(f, v, _write(f, v)) for f, v in doc["safety_writes"]])
        _COUNTS["counts"] = doc["write_counts"]
    elif not doc["writes"]:
        _PENDING["restore"] = False
    believed, seen = core.guard_memory(raw, standing, doc["written"], doc["failed"])
    _memory_store(believed, seen, doc["tick_ts"])
    _publish(doc)
    task.executor(core.append_tick, WRITER_DIR, now, doc)
    if doc["failed"] or (mode == "live" and doc["status"] in ("stale", "degraded")):
        _notify(doc["status"], doc, now)
    if tripped and was is False and _armed():   # the drop only; the release is silent (2026-09-27); disarmed nothing was capped
        _notify_soc_floor(soc, floor_pct, mode)
    if hot and heat_was is False and heat_a and _armed():
        _notify_heat_cut(temp, cut_c, cut_kw, mode)
    log.info(f"emhass writer: {doc['status']} mode {mode} intent {doc['intent']} -> {doc['next_intent']} "
             f"held {doc['held']} record {doc['record']} written {[w[0] for w in doc['written']]} failed {doc['failed']}")
    return {k: doc[k] for k in ("status", "intent", "next_intent", "held", "record", "written", "failed")}


def _notify_soc_floor(soc, floor_pct, mode):
    service.call("notify", NOTIFY, title="Battery SOC floor: discharge stopped",
                 message=f"SOC {soc} % against a floor of {floor_pct:.0f} % (writer {mode}). "
                         f"Discharge clamp held at 0 A until {floor_pct + core.SOC_FLOOR_RELEASE_PTS:.0f} %.")


def _notify_heat_cut(temp, cut_c, cut_kw, mode):
    service.call("notify", NOTIFY, title="Battery heat backstop: power capped",
                 message=f"Pack {temp} °C (1 h mean) at or above the {cut_c:.1f} °C cut (writer {mode}). "
                         f"Charge and discharge capped at {cut_kw:.1f} kW until {cut_c - core.HEAT_CUT_RELEASE_C:.1f} °C.")


@state_trigger(f"{PACK_TEMP_MEAN}")
def _emhass_writer_heat(value=None, **kwargs):
    # A crossing of the cut acts now, not at the next quarter hour (the SOC-floor pattern).
    temp, cut_c, _kw = _heat_cut()
    if _CEIL["heat"] is None:
        return                     # the next cron tick sets the latch after a reload
    if core.heat_cut_tripped(_CEIL["heat"], temp, cut_c) != _CEIL["heat"]:
        emhass_writer_tick(yield_to_running=True)


@state_trigger(f"{SOC}")
def _emhass_writer_soc(value=None, **kwargs):
    # A SOC crossing acts now, not at the next quarter hour. Only a tick whose
    # latch would change runs, so the SOC's own updates cost no writes.
    soc, floor_pct = _soc_floor()
    if _CEIL["soc_floor"] is None:
        return                     # the next cron tick sets the latch after a reload
    if core.soc_floor_tripped(_CEIL["soc_floor"], soc, floor_pct) != _CEIL["soc_floor"]:
        emhass_writer_tick(yield_to_running=True)


@time_trigger("cron(0,15,30,45 * * * *)")
def _emhass_writer_cron():
    task.sleep(20)                 # the plan refresh at :13 and :43 is in force from the boundary
    emhass_writer_tick(yield_to_running=True)   # never cut a restore in flight (review finding 3)


@state_trigger(f"{MODE}")
def _emhass_writer_mode(value=None, old_value=None, **kwargs):
    # Leaving live restores the baseline (and keeps retrying in dry until it
    # reads back); an off tick enforces the baseline every time anyway.
    if old_value == "live" and value != "live":
        _PENDING["restore"] = True
    # Only a real switch from off or dry drops the anchor. After a restart the
    # helper is restored from unknown to live and this trigger fires too; that
    # is not a switch, and overwriting the anchor would move the settled
    # chain's reset point to the restart (12:41 on go-live day, seen live).
    if value == "live" and old_value in ("off", "dry"):
        soc = _num(SOC, None)
        if soc is not None:            # the scoreboard's virtual pack re-anchors here (spec 3.6)
            task.executor(core.write_anchor, ANCHOR, _now(), soc)
            log.info(f"emhass writer: live anchor written at {soc} %")
    emhass_writer_tick(restore=(old_value == "live"))


@time_trigger("startup")
def _emhass_writer_startup():
    # The heartbeat does not survive a restart, and the dead man reads a
    # missing heartbeat as infinitely old: a sale standing across a restart
    # would be taken down at the next 5-minute check, three minutes before
    # this tick could vouch for it. pyscript is alive from here, so say so.
    state.set(HEARTBEAT, _now().isoformat(timespec="seconds"),
              new_attributes={"friendly_name": "EMHASS writer heartbeat", "icon": "mdi:heart-pulse",
                              "startup": True})
    task.sleep(180)                # 60 s after the shadow rehydrate (120 s)
    emhass_writer_tick()
