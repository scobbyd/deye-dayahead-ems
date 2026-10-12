"""Thermal: the house as a second battery.

Builds the heat_topology block the live solve sends to EMHASS (0.18.5): two
building zones (living, upstairs) with measured thermal mass and loss
coefficient, three sources (the living heat pump, the upstairs multi, the
pellet stove on its own fixed cost track), hard comfort bands per step, and
the outdoor forecast on the plan grid. The read-back of the resulting rows as
the thermal step in force for the heating follower is at the end of this module.

Physics knobs (thermal.json) carry measured values only. Conservatism is the
startup penalties and the band width, nothing else.

Units: W in payloads and rows; kWh/K and kW/K for the zones; degrees C;
EUR/kWh for prices; ct at the knob boundary only.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .archive import ARCHIVE_REACH, iter_organic_plans
from .grid import STEP_MIN, _parse_ts, step_times
from .plant import _deep_merge

THERMAL_DEFAULTS = {
    "zones": {
        "living": {"thermal_mass_kwh_k": 6.0, "loss_coefficient_kw_k": 0.225, "thermal_inertia_h": 0.5},
        "upstairs": {"thermal_mass_kwh_k": 4.0, "loss_coefficient_kw_k": 0.12, "thermal_inertia_h": 0.5,
                     "night_band_k": 0.5},
    },
    "sources": {
        # Carnot on the supply temperature, fitted to the EN 14825 part-load
        # points (-7: 3,26, 2: 4,98, 7: 6,30, 12: 7,74): supply 30, eta 0,46
        # lands within 13 % of each. The identification refits both.
        "hp_living": {"supply_temperature_c": 30.0, "carnot_efficiency": 0.46, "min_power_w": 300.0},
        "hp_multi": {"supply_temperature_c": 30.0, "carnot_efficiency": 0.46, "min_power_w": 400.0},
        "pellet": {"nominal_w": 10000.0, "min_power_w": 5000.0, "efficiency": 1.0, "max_starts_per_day": 2},
    },
}
SEARCH = ("/config/emhass/thermal.json",)
SOURCE_ORDER = ("hp_living", "hp_multi", "pellet")
HOLD_MAX_STEPS = 24            # 6 h of held forecast before the topology is dropped
FOLLOWER_STALE_MIN = 180.0     # used by thermal_step_in_force; the follower drops to the legacy planner after this
# Not-sent reasons the follower looks past to an older thermal plan (a sensor
# or forecast gap); any other not-sent plan (disabled, an input error) is off now.
CONTINUITY_REASONS = ("living start temperature unavailable", "upstairs start temperature unavailable",
                      "no forecast", "forecast short")
BOUND_MARGIN_K = 0.5           # slack around the reachable envelope (rounding, the min_power gap)
COP_MIN, COP_MAX = 1.0, 8.0    # the add-on clips the Carnot COP to this range
ON_W = 50.0                    # used by source_word; a source under this is off (the Daikin idles at 20-40 W)


def load_thermal(path: str | None = None) -> dict:
    """THERMAL_DEFAULTS overlaid with thermal.json: the given path, else
    EMHASS_THERMAL_JSON, else /config/emhass/thermal.json, else nothing."""
    candidates = [path] if path else [os.environ.get("EMHASS_THERMAL_JSON"), *SEARCH]
    base = json.loads(json.dumps(THERMAL_DEFAULTS))     # a copy: the result never aliases the defaults
    for p in candidates:
        if p and os.path.exists(p):
            with open(p) as f:
                return _deep_merge(base, json.load(f))
    return base


# ---- comfort schedule --------------------------------------------------------

def _hhmm(s: str) -> str:
    return str(s)[:5]


def _slot_of(t_local: datetime, slots: dict) -> str:
    t = t_local.strftime("%H:%M")
    if t >= _hhmm(slots["night"]) or t < _hhmm(slots["morning"]):
        return "night"
    if t >= _hhmm(slots.get("evening", "99:99")):
        return "evening"
    if t >= _hhmm(slots.get("day", "99:99")):
        return "day"
    return "morning"


def floor_schedule(t0: datetime, n: int, tz: str, sched: dict) -> list[float]:
    """The living floor per step: the hybrid-heating base target over the
    horizon (the target_for_datetime macro in custom_templates/heating.jinja):
    holiday flat, night the coast floor, else the weekday/weekend slot target
    with the away offset."""
    z = ZoneInfo(tz)
    out = []
    for t in step_times(t0, n):
        tl = t.astimezone(z)
        if sched.get("mode") == "holiday":
            out.append(float(sched.get("holiday_c", 11.0)))
            continue
        slot = _slot_of(tl, sched["slots"])
        if slot == "night":
            out.append(float(sched.get("night_floor_c", 10.0)))
            continue
        day_type = "weekend" if tl.weekday() >= 5 else "weekday"
        base = float(sched["targets"][day_type][slot])
        if sched.get("away"):
            base += float(sched.get("away_offset_k", -3.0))
        out.append(round(base, 1))
    return out


def upstairs_schedule(t0: datetime, n: int, tz: str, sched: dict) -> tuple[list[float], list[bool]]:
    """The upstairs floor per step and the night mask: night_c from the night
    slot start to the morning slot start, day_c otherwise, the away offset
    on day steps. The optional keys mode and holiday_c (absent means auto):
    in holiday mode the floor is the flat holiday_c on every step, and the
    night mask is still computed."""
    z = ZoneInfo(tz)
    holiday = sched.get("mode") == "holiday"
    floor, night = [], []
    for t in step_times(t0, n):
        tl = t.astimezone(z)
        is_night = _slot_of(tl, {"night": sched["slots"]["night"], "morning": sched["slots"]["morning"]}) == "night"
        v = float(sched["night_c"]) if is_night else float(sched["day_c"])
        if holiday:
            v = float(sched.get("holiday_c", 11.0))
        elif not is_night and sched.get("away"):
            v += float(sched.get("away_offset_k", -3.0))
        floor.append(round(v, 1))
        night.append(is_night)
    return floor, night


def ceiling(floor: list[float], band_k: float, night: list[bool] | None = None,
            night_band_k: float | None = None) -> list[float]:
    out = []
    for k, f in enumerate(floor):
        b = float(band_k)
        if night is not None and night_band_k is not None and night[k]:
            b = float(night_band_k)
        out.append(round(f + b, 1))
    return out


# ---- outdoor forecast --------------------------------------------------------

def _clean_points(points) -> list:
    """The usable (UTC time, degrees C) points sorted by time: a point with a
    bad timestamp, a missing, non-numeric or NaN temperature is dropped."""
    pts = []
    for p in points or []:
        try:
            ts, v = p
            v = float(v)
            if math.isnan(v) or math.isinf(v):
                continue
            pts.append((_parse_ts(ts).astimezone(timezone.utc), v))
        except (TypeError, ValueError):
            continue
    return sorted(pts, key=lambda x: x[0])


def outdoor_forecast(points: list, now_c: float | None, t0: datetime, n: int) -> tuple[list[float] | None, int]:
    """Hourly (time, degrees C) points onto the plan grid by linear
    interpolation. Malformed points are dropped first. Before the first point
    the live reading at t0 ramps linearly to the first point (the first point
    holds when there is no live reading). After the last point the last value
    holds for at most HOLD_MAX_STEPS steps, beyond that the forecast is None.
    Returns (values, steps held past the last point)."""
    pts = _clean_points(points)
    if not pts:
        return None, n
    out, held = [], 0
    j = 0
    for t in step_times(t0, n):
        tu = t.astimezone(timezone.utc)
        while j + 1 < len(pts) and pts[j + 1][0] <= tu:
            j += 1
        if tu < pts[0][0]:
            if now_c is None:
                out.append(pts[0][1])
            else:
                t_a = t0.astimezone(timezone.utc)
                frac = (tu - t_a).total_seconds() / max((pts[0][0] - t_a).total_seconds(), 1.0)
                out.append(round(float(now_c) + frac * (pts[0][1] - float(now_c)), 2))
        elif j + 1 < len(pts):
            (ta, va), (tb, vb) = pts[j], pts[j + 1]
            frac = (tu - ta).total_seconds() / max((tb - ta).total_seconds(), 1.0)
            out.append(round(va + frac * (vb - va), 2))
        else:
            if tu > pts[-1][0]:
                held += 1
            out.append(pts[-1][1])
    if held > HOLD_MAX_STEPS:
        return None, held
    return out, held


# ---- topology ----------------------------------------------------------------

def startup_penalty(ct_per_start: float, nominal_w: float, mean_buy: float) -> float:
    """EMHASS prices a start at penalty x nominal kW x the step's buy price.
    Convert a ct-per-start knob at the horizon's mean buy price."""
    if mean_buy <= 0.0 or nominal_w <= 0.0 or ct_per_start <= 0.0:
        return 0.0
    return (ct_per_start / 100.0) / ((nominal_w / 1000.0) * mean_buy)


def _not_sent(reason: str) -> tuple[None, dict]:
    return None, {"sent": False, "reason": reason}


def _clamp_bounds(zone: dict, q_kw: list[float], outdoor: list[float], floor: list[float], ceil: list[float]) -> tuple[list[float], list[float], int, int]:
    """Clamp a zone's floor and ceiling to what the LP can reach. Returns
    (floor, ceiling, ceiling steps lifted, floor steps lowered).

    The model is the add-on's (0.18.5 shared tank): T[i] is the temperature at
    the start of step i, the loss of step i uses T[i] and outdoor[i], and the
    heat of step i arrives in transition i + lag (lag = whole steps of
    thermal_inertia, truncated), so the first lag transitions carry no source
    heat. Three passes:
    1. floors: a floor above the full-power path from the start (less the
       margin) is lowered to that path;
    2. need, backward: the lowest temperature at the start of step i from
       which every later (lowered) floor is still reachable at full power;
    3. envelope, forward: the coolest feasible path, free cooling held up at
       the floor and at need. The ceiling stays at least the margin above it,
       so a setback after a held floor and a pre-heat ramp before a morning
       floor stay feasible.

    A zone whose constants give no stable step (mass not above zero, or
    k x UA at or above 1) is returned unchanged with None for both counts:
    the clamp is skipped, never raised."""
    n = len(floor)
    dt_h = STEP_MIN / 60.0
    mass = float(zone["thermal_mass"])
    ua = float(zone["loss_coefficient"])
    if not (mass > 0.0 and math.isfinite(ua) and 1.0 - dt_h / mass * ua > 0.0):
        return list(floor), list(ceil), None, None
    k = dt_h / mass
    lag = max(0, min(int(float(zone.get("thermal_inertia", 0.0)) / dt_h), n - 1))
    arrive = [q_kw[i - lag] if i >= lag else 0.0 for i in range(n)]   # full-power heat of transition i
    start = float(zone["start_temperature"])
    new_min, t_full = [], start
    for i in range(n):
        new_min.append(min(floor[i], round(t_full - BOUND_MARGIN_K, 2)))
        t_full = t_full - k * ua * (t_full - outdoor[i]) + k * arrive[i]
    need = list(new_min)
    for i in range(n - 2, -1, -1):
        # T + k * (arrive - ua * (T - out)) = need[i + 1], solved for T
        need[i] = max(new_min[i], (need[i + 1] - k * arrive[i] - k * ua * outdoor[i]) / (1.0 - k * ua))
    new_max, env = [], start
    for i in range(n):
        if i > 0:                         # T[0] is pinned to the start
            env = max(env, new_min[i], need[i])
        new_max.append(max(ceil[i], round(env + BOUND_MARGIN_K, 2)))
        env = env - k * ua * (env - outdoor[i])
    n_max = sum(h > c for h, c in zip(new_max, ceil))
    n_min = sum(lo < f for lo, f in zip(new_min, floor))
    return new_min, new_max, n_max, n_min


def _hp_kw(src: dict, outdoor: list[float]) -> list[float]:
    """Heat pump thermal kW per step: cap x Carnot COP at the supply
    temperature, clipped to [COP_MIN, COP_MAX] and COP_MIN without lift, as
    the add-on computes it."""
    sup = float(src["supply_temperature"])
    eta = float(src["carnot_efficiency"])
    cap = float(src["nominal_power"]) / 1000.0
    out = []
    for t in outdoor:
        lift = sup - t
        cop = eta * (sup + 273.15) / lift if lift > 0 else COP_MIN
        out.append(cap * min(COP_MAX, max(COP_MIN, cop)))
    return out


def heat_topology(th: dict, t0: datetime, n: int, tz: str, mean_buy: float) -> tuple[dict | None, dict]:
    """The heat_topology block and its info, or (None, info) when it is not
    sent: disabled, an input error (th["error"], reason "error: ..."), the
    living start temperature unavailable, or the forecast short of the
    horizon. The upstairs zone is planned whenever its start temperature is
    known; heating_upstairs_enabled gates the upstairs executor only, never
    the plan. info carries sent, reason, sources {id: k}, zones {id: k of its
    first flow}, outdoor_c (the list the payload gets), upstairs (the zone
    was sent), clamped {zone: [ceiling steps lifted, floor steps lowered] or
    "skipped"}."""
    if th and th.get("error"):
        return _not_sent(f"error: {th['error']}")
    if not th or not th.get("enabled"):
        return _not_sent("disabled")
    c = th.get("constants") or load_thermal()
    living = th.get("living") or {}
    if living.get("start_c") is None:
        return _not_sent("living start temperature unavailable")
    up = th.get("upstairs") or {}
    up_on = up.get("start_c") is not None and isinstance(up.get("sched"), dict)
    w = th.get("weather") or {}
    if not _clean_points(w.get("points")):
        return _not_sent("no forecast")
    outdoor, held = outdoor_forecast(w.get("points") or [], w.get("now_c"), t0, n)
    if outdoor is None:
        return _not_sent(f"forecast short by {held} steps")
    band = float(th.get("band_k", 3.0))
    caps = th.get("caps") or {}
    pen = th.get("startup_ct") or {}
    zl, zu = c["zones"]["living"], c["zones"]["upstairs"]
    sl, sm, sp = c["sources"]["hp_living"], c["sources"]["hp_multi"], c["sources"]["pellet"]

    floor_l = floor_schedule(t0, n, tz, living["sched"])
    ceil_c = living.get("ceiling_c")
    try:
        ceil_c = float(ceil_c) if ceil_c is not None else None
    except (TypeError, ValueError):
        ceil_c = None
    if ceil_c is not None and not math.isfinite(ceil_c):
        ceil_c = None
    max_l = [ceil_c] * n if ceil_c is not None else ceiling(floor_l, band)
    storage = [{"id": "living", "thermal_mass": float(zl["thermal_mass_kwh_k"]),
                "loss_coefficient": float(zl["loss_coefficient_kw_k"]),
                "thermal_inertia": float(zl["thermal_inertia_h"]),
                "start_temperature": float(living["start_c"]),
                "min_temperatures": floor_l, "max_temperatures": max_l,
                "comfort_sense": "heat"}]
    sources = [{"id": "hp_living", "type": "heatpump", "electric": True,
                "nominal_power": int(round(float(caps.get("hp_living_w", 1450.0)))),
                "min_power": int(round(float(sl["min_power_w"]))), "treat_as_semi_cont": False,
                "supply_temperature": float(sl["supply_temperature_c"]),
                "carnot_efficiency": float(sl["carnot_efficiency"]),
                "startup_penalty": startup_penalty(float(pen.get("hp", 0.0)), float(caps.get("hp_living_w", 1450.0)), mean_buy)}]
    flows = [{"from": "hp_living", "to": "living"}]
    if up_on:
        floor_u, night = upstairs_schedule(t0, n, tz, up["sched"])
        storage.append({"id": "upstairs", "thermal_mass": float(zu["thermal_mass_kwh_k"]),
                        "loss_coefficient": float(zu["loss_coefficient_kw_k"]),
                        "thermal_inertia": float(zu["thermal_inertia_h"]),
                        "start_temperature": float(up["start_c"]),
                        "min_temperatures": floor_u,
                        "max_temperatures": ceiling(floor_u, band, night, float(zu.get("night_band_k", 0.5))),
                        "comfort_sense": "heat"})
        sources.append({"id": "hp_multi", "type": "heatpump", "electric": True,
                        "nominal_power": int(round(float(caps.get("hp_multi_w", 2200.0)))),
                        "min_power": int(round(float(sm["min_power_w"]))), "treat_as_semi_cont": False,
                        "supply_temperature": float(sm["supply_temperature_c"]),
                        "carnot_efficiency": float(sm["carnot_efficiency"]),
                        "startup_penalty": startup_penalty(float(pen.get("hp", 0.0)), float(caps.get("hp_multi_w", 2200.0)), mean_buy)})
        flows.append({"from": "hp_multi", "to": "upstairs"})
    topo = {"extend_deferrable_loads": False, "sources": sources, "storage": storage, "consumers": [], "flows": flows}
    pellet = th.get("pellet") or {}
    if pellet.get("available"):
        days = max(1, math.ceil(n / 96.0))
        sources.append({"id": "pellet", "type": "constant_efficiency", "electric": False,
                        "nominal_power": int(round(float(sp["nominal_w"]))),
                        "min_power": int(round(float(sp["min_power_w"]))), "treat_as_semi_cont": True,
                        "efficiency": float(sp["efficiency"]), "cost_track": "pellet",
                        "startup_penalty": startup_penalty(float(pen.get("pellet", 0.0)), float(sp["nominal_w"]), mean_buy),
                        "max_startups": int(sp["max_starts_per_day"]) * days})  # sent as specified; the executor enforces the pellet cycle cap
        flows.append({"from": "pellet", "to": "living"})
        topo["cost_tracks"] = {"pellet": [round(float(pellet.get("cost_ct_kwh", 8.0)) / 100.0, 4)] * n}
    by_id = {x["id"]: x for x in sources}
    clamped = {}
    for z in storage:
        q = [0.0] * n
        for f in flows:
            if f["to"] != z["id"]:
                continue
            src = by_id[f["from"]]
            if src["type"] == "heatpump":
                add = _hp_kw(src, outdoor)
            else:
                add = [float(src["nominal_power"]) / 1000.0 * float(src["efficiency"])] * n
            q = [a + b for a, b in zip(q, add)]
        z["min_temperatures"], z["max_temperatures"], n_max, n_min = _clamp_bounds(
            z, q, outdoor, z["min_temperatures"], z["max_temperatures"])
        clamped[z["id"]] = "skipped" if n_max is None else [n_max, n_min]
    src_idx = {f["from"]: k for k, f in enumerate(flows)}
    zone_idx = {}
    for k, f in enumerate(flows):
        zone_idx.setdefault(f["to"], k)
    info = {"sent": True, "reason": "ok", "sources": src_idx, "zones": zone_idx, "outdoor_c": outdoor,
            "held_steps": held, "band_k": band, "living_ceiling_c": ceil_c, "pellet": bool(pellet.get("available")), "upstairs": up_on,
            "clamped": clamped}
    return topo, info


RELAX_STEPS = 8


def apply_thermal(payload: dict, topo: dict | None, info: dict) -> dict:
    """The topology and the outdoor forecast onto the solve payload when sent."""
    if topo and info.get("sent"):
        payload["heat_topology"] = topo
        payload["outdoor_temperature_forecast"] = list(info["outdoor_c"])
    return payload


def relax_topology(payload: dict, steps: int = RELAX_STEPS) -> dict:
    """A copy of the payload whose zone comfort bounds are released for the
    first `steps` steps (floor 5 C, ceiling 35 C): a room already outside its
    band must not make the whole solve infeasible."""
    out = dict(payload)
    topo = json.loads(json.dumps(payload.get("heat_topology") or {}))
    for st in topo.get("storage", []):
        for key, value in (("min_temperatures", 5.0), ("max_temperatures", 35.0)):
            vals = list(st.get(key) or [])
            for k in range(min(steps, len(vals))):
                vals[k] = value
            st[key] = vals
    out["heat_topology"] = topo
    return out


def strip_topology(payload: dict) -> dict:
    """A copy of the payload without the topology and the outdoor forecast."""
    out = dict(payload)
    out.pop("heat_topology", None)
    out.pop("outdoor_temperature_forecast", None)
    return out


# ---- the thermal step in force ----------------------------------------------

def _continuity(th: dict) -> bool:
    """A not-sent plan the follower looks past to an older thermal plan: the
    topology stripped after an infeasible solve, or a sensor or forecast gap."""
    return th.get("fallback") == "stripped" or str(th.get("reason") or "").startswith(CONTINUITY_REASONS)


def _step_of(doc: dict, th: dict, now_utc: datetime, stale_min: float) -> dict | None:
    """The step in force from one thermal plan, or None when the plan carries
    no thermal columns, is malformed, or its horizon misses `now`."""
    rows = doc.get("rows")
    sources = th.get("sources") or {}
    if not isinstance(rows, list) or not rows or not isinstance(sources, dict) or not sources:
        return None
    for r in rows:
        if not isinstance(r, dict):
            return None                           # a poisoned row: skip the plan, never raise
    if not any(f"P_deferrable{k}" in rows[0] for k in sources.values()):
        return None                               # rows carry no thermal columns: same as no thermal
    try:
        t0 = _parse_ts(doc["t0"]).astimezone(timezone.utc)
        plan_ts = _parse_ts(doc["plan_ts"]).astimezone(timezone.utc)
        n = min(int(doc.get("n") or 0), len(rows))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None                               # malformed doc: skip, never raise
    i = int((now_utc - t0).total_seconds() // (STEP_MIN * 60))
    if i < 0 or i >= n:
        return None
    zones = th.get("zones")
    return {"plan_ts": doc["plan_ts"], "t0": doc["t0"], "n": n, "index": i, "row": rows[i],
            "next_row": rows[i + 1] if i + 1 < n else None, "rows": rows,
            "sources": dict(sources), "zones": dict(zones) if isinstance(zones, dict) else {},
            "stale": now_utc - plan_ts > timedelta(minutes=float(stale_min))}


def thermal_step_in_force(archive_dir: str, now: datetime, stale_min: float = FOLLOWER_STALE_MIN) -> dict | None:
    """The row at `now` and the row after it from the newest organic plan that
    carried the topology (doc["thermal"]["sent"], not stripped) and whose
    horizon covers `now`. None means the follower reports off and the legacy
    planner stays in charge. Index arithmetic through UTC.

    Newest first, the scan:
    - stops with None at a plan without a thermal record, or one not sent
      for any reason but a gap (disabled, an input error): heating_emhass
      off means off now, not a 3-hour-old trajectory;
    - looks past a stripped plan or a sensor or forecast gap
      (CONTINUITY_REASONS) to an older thermal plan;
    - skips a doc whose thermal record or rows are malformed;
    - opens at most one plan older than stale_min: a plan that old can only
      report stale, so nothing older is read (a flag-off or gappy archive
      costs a few documents per tick, not three days of them)."""
    now_utc = now.astimezone(timezone.utc)
    cutoff = now_utc - timedelta(minutes=float(stale_min))
    for doc in iter_organic_plans(archive_dir, since=now - ARCHIVE_REACH, until=now):
        th = doc.get("thermal")
        if th is None:
            return None                           # no thermal record: a plan without the topology
        if isinstance(th, dict):
            if th.get("sent") and th.get("fallback") != "stripped":
                step = _step_of(doc, th, now_utc, stale_min)
                if step is not None:
                    return step
            elif not _continuity(th):
                return None
        try:
            older = _parse_ts(doc["plan_ts"]).astimezone(timezone.utc) < cutoff
        except (KeyError, TypeError, ValueError, AttributeError):
            older = False
        if older:
            return None
    return None


def _on(w) -> bool:
    return w is not None and float(w) >= ON_W


def source_word(p_hp_w, p_pellet_w, next_hp_w, next_pellet_w, pellet_available: bool) -> str:
    """The step's source for the executor. A heat pump is on whenever the
    plan runs it this step (next_hp_w is not read): holding it through its
    last planned step is cheaper than an off/on cycle, and the LP already
    pays its starts. The hold rule from the writer applies to the pellet
    only (an expensive start and an executor minimum session): a pellet on
    this step and off the next is treated as off. A pellet column while the
    stove is unavailable is ignored."""
    hp = _on(p_hp_w)
    pel = pellet_available and _on(p_pellet_w) and (next_pellet_w is None or _on(next_pellet_w))
    if hp and pel:
        return "COMBINED"
    if pel:
        return "PELLET"
    if hp:
        return "AC"
    return "COAST"


def daikin_grid(value: float, correction: float) -> float:
    """A setpoint on the Daikin's 0,5 grid, clamped to its 10..30 range."""
    v = min(max(float(value) + float(correction), 10.0), 30.0)
    return math.floor(v * 2.0 + 0.5) / 2.0


def _col(row: dict, prefix: str, k) -> float | None:
    if row is None or k is None:
        return None
    try:
        v = row.get(f"{prefix}{k}")
        return None if v is None else float(v)
    except (TypeError, ValueError, AttributeError):
        return None


def _r0(v):
    """Whole watts for a horizon series; None for a missing or non-finite value."""
    return None if v is None or not math.isfinite(v) else int(round(v))


def _r2(v):
    """Two decimals for a temperature series; None for a missing or non-finite value."""
    return None if v is None or not math.isfinite(v) else round(v, 2)


def _block_end(rows: list, i: int, n: int, ks: tuple) -> int:
    """The last index of the contiguous run from step i on which any of the
    source columns ks is on."""
    j = i
    while j + 1 < n:
        nxt = rows[j + 1]
        if not any(_on(_col(nxt, "P_deferrable", k) or 0.0) for k in ks if k is not None):
            break
        j += 1
    return j


def _block_target(rows: list, i: int, n: int, ks: tuple, k_zone) -> float | None:
    """The setpoint for a zone whose source runs from step i: the highest
    predicted temperature from this step to the end of the block (row j + 1,
    the end of the last on-step j, when the horizon holds it). With the
    thermal inertia lag the next row alone sits at the room temperature and
    the unit idles."""
    j = _block_end(rows, i, n, ks)
    best = None
    for r in rows[i:min(j + 2, n)]:
        v = _col(r, "predicted_temp_heater", k_zone)
        if v is not None and math.isfinite(v) and (best is None or v > best):
            best = v
    return best


def follower_doc(step: dict | None, now: datetime, correction: float, pellet_available: bool,
                 upstairs_on: bool) -> dict:
    """What the follower publishes: source (AC, PELLET, COMBINED, COAST, stale,
    off), the setpoints on the Daikin grid (None when unknown) and the sensor
    attributes. A zone whose source runs this step targets the highest
    predicted temperature to the end of the source's block (_block_target);
    a zone whose source is off targets its comfort floor this step."""
    base = {"tick_ts": now.isoformat(timespec="seconds")}
    if step is None:
        return {"source": "off", "target_living": None, "target_upstairs": None,
                "attrs": dict(base, status="no plan", plan_ts=None)}
    s, z = step["sources"], step["zones"]
    row, nxt = step["row"], step["next_row"]
    k_hp, k_mu, k_pel = s.get("hp_living"), s.get("hp_multi"), s.get("pellet")
    k_liv, k_up = z.get("living"), z.get("upstairs")
    rows, i, n = step["rows"], step["index"], step["n"]
    # The pellet's hold: only next_row None (the last step) means no hold; a
    # missing column in an existing next row is 0 W.
    next_pel = None if nxt is None else (_col(nxt, "P_deferrable", k_pel) or 0.0)
    word = source_word(_col(row, "P_deferrable", k_hp) or 0.0, _col(row, "P_deferrable", k_pel) or 0.0,
                       None, next_pel, pellet_available)
    hp_multi_on = k_mu is not None and _on(_col(row, "P_deferrable", k_mu) or 0.0)
    zone_ok = upstairs_on and k_up is not None
    block = {"AC": (k_hp,), "PELLET": (k_pel,), "COMBINED": (k_hp, k_pel)}.get(word)
    if block is None:                             # COAST: the comfort floor this step
        t_liv_next = _col(row, "min_temp_heater", k_liv)
    else:
        t_liv_next = _block_target(rows, i, n, block, k_liv)
    if not zone_ok:
        t_up_next = None
    elif hp_multi_on:
        t_up_next = _block_target(rows, i, n, (k_mu,), k_up)
    else:
        t_up_next = _col(row, "min_temp_heater", k_up)
    times = [r.get("timestamp") if isinstance(r, dict) else None for r in step["rows"]]
    attrs = dict(base, plan_ts=step["plan_ts"], index=step["index"],
                 status="stale" if step["stale"] else "ok",
                 p_hp_living_w=_col(row, "P_deferrable", k_hp), p_hp_multi_w=_col(row, "P_deferrable", k_mu),
                 p_pellet_w=_col(row, "P_deferrable", k_pel), hp_multi_on=hp_multi_on,
                 t_living_pred=_col(row, "predicted_temp_heater", k_liv),
                 t_living_min=_col(row, "min_temp_heater", k_liv), t_living_max=_col(row, "max_temp_heater", k_liv),
                 t_upstairs_pred=_col(row, "predicted_temp_heater", k_up),
                 t_upstairs_min=_col(row, "min_temp_heater", k_up), t_upstairs_max=_col(row, "max_temp_heater", k_up),
                 q_living_w=_col(row, "heating_demand_heater", k_liv),
                 q_upstairs_w=_col(row, "heating_demand_heater", k_up),
                 times=times,
                 p_hp_living_w_series=[_r0(_col(r, "P_deferrable", k_hp)) for r in step["rows"]],
                 p_hp_multi_w_series=[_r0(_col(r, "P_deferrable", k_mu)) for r in step["rows"]],
                 p_pellet_w_series=[_r0(_col(r, "P_deferrable", k_pel)) for r in step["rows"]],
                 t_living_pred_series=[_r2(_col(r, "predicted_temp_heater", k_liv)) for r in step["rows"]],
                 t_living_min_series=[_r2(_col(r, "min_temp_heater", k_liv)) for r in step["rows"]],
                 t_living_max_series=[_r2(_col(r, "max_temp_heater", k_liv)) for r in step["rows"]],
                 t_upstairs_pred_series=[_r2(_col(r, "predicted_temp_heater", k_up)) for r in step["rows"]],
                 t_upstairs_min_series=[_r2(_col(r, "min_temp_heater", k_up)) for r in step["rows"]],
                 t_upstairs_max_series=[_r2(_col(r, "max_temp_heater", k_up)) for r in step["rows"]],
                 q_living_w_series=[_r0(_col(r, "heating_demand_heater", k_liv)) for r in step["rows"]],
                 q_upstairs_w_series=[_r0(_col(r, "heating_demand_heater", k_up)) for r in step["rows"]])
    if step["stale"]:
        return {"source": "stale", "target_living": None, "target_upstairs": None, "attrs": attrs}
    return {"source": word,
            "target_living": daikin_grid(t_liv_next, correction) if t_liv_next is not None else None,
            "target_upstairs": daikin_grid(t_up_next, 0.0) if t_up_next is not None else None,
            "attrs": attrs}
