"""Load a pyscript/ wrapper as a plain module on the dev box, where neither
pyscript nor Home Assistant is installed.

The harness injects the pyscript globals the wrappers use (state, service,
task, log, hass and the trigger decorators), stubs the three recorder modules
the planner imports, and redirects the wrapper's file-path import of
/config/pyscript_helpers/emhass_core.py to the repo's facade. The wrapper
source is executed unchanged.

Every HA-visible effect is appended to `Harness.effects` in call order:
state.set, service.call, log.*, async_add_external_statistics, task.sleep,
task.unique. Reads (state.get, the recorder queries) are not effects.

The facade purges emhasscore* from sys.modules each time it executes, so each
load gets its own fresh copy of the core. load() puts sys.modules and sys.path
back exactly as it found them afterwards: the rest of the suite keeps the
emhass_core and emhasscore module objects it imported. EMHASS_PLANT_JSON is
cleared for the load, so the core always reads the repo's
pyscript_helpers/plant.json, whatever the environment holds.

Usage:
    h = Harness(now=datetime(...))
    h.set_state("input_select.emhass_writer_mode", "off")
    mod = h.load("emhass_shadow.py")
    mod.emhass_plan_day()
    h.effects
"""
from __future__ import annotations

import enum
import importlib.util
import os
import pathlib
import sys
import types
from datetime import datetime

# The live tree (tests/ under the config repo, wrappers in
# pyscript/) or the public extraction (tests/ under the repo root, wrappers in
# ha/pyscript/): the first ancestor that holds either layout.
REPO = next(p for p in pathlib.Path(__file__).resolve().parents
            if (p / "pyscript").is_dir() or (p / "ha" / "pyscript").is_dir())
_HA = REPO if (REPO / "pyscript").is_dir() else REPO / "ha"
PYSCRIPT_DIR = _HA / "pyscript"
CORE_ON_VM = "/config/pyscript_helpers/emhass_core.py"
CORE_IN_REPO = _HA / "pyscript_helpers" / "emhass_core.py"
HA_STUBS = ("homeassistant", "homeassistant.components", "homeassistant.components.recorder",
            "homeassistant.components.recorder.history", "homeassistant.components.recorder.statistics",
            "homeassistant.components.recorder.models")


def _decorator(*args, **kwargs):
    """A pyscript decorator as a pass-through, used bare (@service) or with
    arguments (@service(supports_response="optional"), @time_trigger("startup"))."""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda fn: fn


class _State:
    """pyscript's `state`. get and getattr raise NameError on an entity that is
    not set, as pyscript does. set stores the value as a string, as HA does,
    and records the effect with the value as given."""

    def __init__(self, h):
        self._h = h

    def get(self, entity_id):
        parts = entity_id.split(".")
        eid = ".".join(parts[:2])
        if eid not in self._h.states:
            raise NameError(f"name '{entity_id}' is not defined")
        value, attrs = self._h.states[eid]
        if len(parts) > 2:
            return attrs.get(parts[2])
        return value

    def getattr(self, entity_id):
        if entity_id not in self._h.states:
            raise NameError(f"name '{entity_id}' is not defined")
        return dict(self._h.states[entity_id][1])

    def set(self, entity_id, value=None, new_attributes=None, **kwargs):
        attrs = dict(new_attributes) if new_attributes is not None else None
        self._h.effects.append({"kind": "state.set", "entity_id": entity_id, "state": value,
                                "attributes": attrs, "kwargs": dict(kwargs)})
        old = self._h.states.get(entity_id, (None, {}))[1]
        self._h.states[entity_id] = (str(value), attrs if attrs is not None else old)


class _Service:
    """pyscript's `service`: a decorator, and `service.call`. A call answers
    from Harness.responses[(domain, name)] (a function of the call's keyword
    arguments) and returns None when no responder is set."""

    def __init__(self, h):
        self._h = h

    def __call__(self, *args, **kwargs):
        return _decorator(*args, **kwargs)

    def call(self, domain, name, **kwargs):
        self._h.effects.append({"kind": "service.call", "domain": domain, "service": name, "data": dict(kwargs)})
        fn = self._h.responses.get((domain, name))
        return fn(**kwargs) if fn else None


class _Task:
    """pyscript's `task`: executor calls the function in place; unique and
    sleep are recorded and do nothing."""

    def __init__(self, h):
        self._h = h

    def executor(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def unique(self, name, **kwargs):
        self._h.effects.append({"kind": "task.unique", "name": name, "kwargs": dict(kwargs)})

    def sleep(self, seconds):
        self._h.effects.append({"kind": "task.sleep", "seconds": seconds})


class _Log:
    """pyscript's `log`. %-style arguments are applied, as the logger does."""

    def __init__(self, h):
        self._h = h

    def _rec(self, level, msg, *args):
        self._h.effects.append({"kind": "log", "level": level, "message": (str(msg) % args) if args else str(msg)})

    def debug(self, msg, *args):
        self._rec("debug", msg, *args)

    def info(self, msg, *args):
        self._rec("info", msg, *args)

    def warning(self, msg, *args):
        self._rec("warning", msg, *args)

    def error(self, msg, *args):
        self._rec("error", msg, *args)


class StatisticMeanType(enum.IntEnum):
    """The recorder's StatisticMeanType, the two members the wrapper uses."""
    NONE = 0
    ARITHMETIC = 1


class Harness:
    """One HA instance as the wrapper sees it.

    states     {entity_id: (state string, attributes)}; set with set_state
    stats      {statistic_id: rows} for statistics_during_period, rows in the
               WS shape the wrapper receives ({start ms, mean, max}); a query
               returns the rows with start in [start, end)
    history    {entity_id: [State-like objects with .state and .last_changed]}
               for state_changes_during_period; a query returns the objects
               with last_changed before end
    responses  {(domain, service): fn(**data)} for service.call
    now        the wrapper's clock (_now), a tz-aware datetime
    effects    the recorded effects, in order
    """

    def __init__(self, now: datetime | None = None, tz: str = "Europe/Amsterdam"):
        self.now = now
        self.tz = tz
        self.states: dict = {}
        self.stats: dict = {}
        self.history: dict = {}
        self.responses: dict = {}
        self.effects: list = []
        self.hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone=tz))

    def set_state(self, entity_id, value, attributes=None):
        self.states[entity_id] = (str(value), dict(attributes or {}))

    # ---- the recorder ----------------------------------------------------------------

    def _statistics_during_period(self, hass, start_time, end_time, statistic_ids, period, units, types_):
        a, b = start_time.timestamp() * 1000.0, end_time.timestamp() * 1000.0
        out = {}
        for sid in statistic_ids:
            rows = [r for r in self.stats.get(sid) or [] if a <= r["start"] < b]
            if rows:
                out[sid] = rows
        return out

    def _state_changes_during_period(self, hass, start_time, end_time=None, entity_id=None, no_attributes=False,
                                     descending=False, limit=None, include_start_time_state=True):
        rows = [s for s in self.history.get(entity_id) or [] if end_time is None or s.last_changed < end_time]
        return {entity_id: rows} if rows else {}

    def _async_add_external_statistics(self, hass, metadata, statistics):
        self.effects.append({"kind": "statistics", "metadata": dict(metadata), "statistics": list(statistics)})

    def _ha_modules(self):
        mods = {name: types.ModuleType(name) for name in HA_STUBS}
        mods["homeassistant.components.recorder.history"].state_changes_during_period = \
            self._state_changes_during_period
        st = mods["homeassistant.components.recorder.statistics"]
        st.statistics_during_period = self._statistics_during_period
        st.async_add_external_statistics = self._async_add_external_statistics
        mods["homeassistant.components.recorder.models"].StatisticMeanType = StatisticMeanType
        return mods

    # ---- loading ---------------------------------------------------------------------

    def load(self, filename: str) -> types.ModuleType:
        """Execute pyscript/<filename> as a fresh module with the pyscript globals,
        and fix its clock to self.now. Returns the module; it is not put in sys.modules."""
        path = PYSCRIPT_DIR / filename
        saved_modules, saved_path = dict(sys.modules), list(sys.path)
        real_spec = importlib.util.spec_from_file_location

        def spec_from_file_location(name, location=None, *args, **kwargs):
            if str(location) == CORE_ON_VM:
                location = str(CORE_IN_REPO)
            return real_spec(name, location, *args, **kwargs)

        mod = types.ModuleType("pyscript_" + path.stem)
        mod.__file__ = str(path)
        mod.__dict__.update(state=_State(self), service=_Service(self), task=_Task(self), log=_Log(self),
                            hass=self.hass, time_trigger=_decorator, state_trigger=_decorator)
        saved_plant = os.environ.pop("EMHASS_PLANT_JSON", None)
        sys.modules.update(self._ha_modules())
        importlib.util.spec_from_file_location = spec_from_file_location
        try:
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
        finally:
            importlib.util.spec_from_file_location = real_spec
            for name in [m for m in sys.modules if m not in saved_modules]:
                del sys.modules[name]
            for name, m in saved_modules.items():
                if sys.modules.get(name) is not m:
                    sys.modules[name] = m
            sys.path[:] = saved_path
            if saved_plant is not None:
                os.environ["EMHASS_PLANT_JSON"] = saved_plant
        mod._now = lambda: self.now
        return mod
