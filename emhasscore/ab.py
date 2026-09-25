"""The A/B tester: a day's own sequence of solves re-run under other knobs, each settled against the real day
through the virtual Deye. Never writes to the plan archive or to scores.csv."""
from __future__ import annotations

import gzip
import json
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .grid import expected_steps, local_midnight, _parse_ts, _slot, STEP_H, STEP_MIN, step_index, step_times
from .series import pv_mix
from .objective import (
    aux_cut_decision,
    CAPACITY_KWH,
    cut_payload,
    cut_thresholds,
    knobs,
    LIVE_KNOBS,
    SOC_MAX,
    rows_to_gross,
    SOC_MIN,
    soc_target_timestep,
    step_cost,
    SURPLUS_BASE,
)
from .deye import clamp_write, DEYE_CLAMP_DEADBAND_A, SETTLE_MARGIN, integrate_soc, settle_step, soc_dwell_h, wanted_clamp
from .addon import solve
from .archive import iter_organic_plans
from .slices import virtual_day
from .scoring import lambda_for, soc_term


#
# "How well would a knob change have done yesterday?" cannot be answered by a
# single re-solve (replay_plan) or by hindsight (which knows the day). It takes
# the day's own sequence of solves, re-run under the change, each starting from
# the SOC the settled walk had reached, each step settled against the real day
# through the virtual Deye - what the throwaway harness did on 2026-09-07 to
# rank the chain fix, the mix, the dwell penalty, the stress cost, the cadence
# and the actuator rule against each other on 09-05. This is that harness, as a
# service. It never writes to the plan archive or to scores.csv.

AB_RUNTIME_KEYS = {
    "battery_stress_cost", "inverter_stress_cost",
    "battery_soc_surplus_threshold", "battery_soc_surplus_cost",
    "battery_soc_deficit_threshold", "battery_soc_deficit_cost",
    "soc_final", "weight_battery_discharge", "weight_battery_charge",
    "battery_charge_power_max", "battery_discharge_power_max",
    "battery_charge_efficiency", "battery_discharge_efficiency",
    "battery_minimum_state_of_charge", "battery_maximum_state_of_charge",
    "soc_target",
}


# `margin` (2026-09-08): settle the walk under the writer's clamp margin
# (deye_command margin=True). Actuator policy, never a helper. Since
# 2026-09-12 the live settlement carries it (deye.SETTLE_MARGIN), so the
# override's default follows that and `margin: false` is the experiment.
# `clamp_deadband_a` (same day): the writer's deadband on the charge clamp for
# the walk's settlement; 0 compiles a fresh clamp every step.
AB_OWN_KEYS = set(LIVE_KNOBS) | {"load", "margin", "clamp_deadband_a"}


AB_LIST_KEYS = ("pv_power_forecast", "load_cost_forecast", "prod_price_forecast", "load_power_forecast")


def ab_validate(overrides: dict) -> list[str]:
    """Unknown override keys; empty when every key is one the walk understands."""
    return sorted(k for k in (overrides or {}) if k not in AB_RUNTIME_KEYS and k not in AB_OWN_KEYS)


def _ab_solves(archive_dir: str, day: date, tz: str, cadence_min: int) -> list[tuple[datetime, dict, int]]:
    """(solve time, source document, shift in steps) in walk order. 60 and 30
    take the first archived solve per hour / half hour; 15 adds a re-solve of
    the same document a quarter later, shifted, until the next document."""
    z = ZoneInfo(tz)
    midnight = datetime.combine(day, time(0), tzinfo=z)
    docs = sorted(iter_organic_plans(archive_dir, since=midnight, until=midnight + timedelta(days=1)),
                  key=lambda d: d["plan_ts"])
    picked, seen = [], set()
    for d in docs:
        pts = datetime.fromisoformat(d["plan_ts"]).astimezone(z)
        slot = (pts.hour, pts.minute // (60 if cadence_min >= 60 else 30))
        if slot in seen:
            continue
        seen.add(slot)
        picked.append((_parse_ts(d["t0"]).astimezone(z), d, 0))
    if cadence_min <= 15:
        extra = []
        for j, (t0, d, _) in enumerate(picked):
            nxt = picked[j + 1][0] if j + 1 < len(picked) else midnight + timedelta(days=1)
            k = 1
            while t0 + timedelta(minutes=STEP_MIN * k) < nxt and k < int(d["n"]) - 1:
                extra.append((t0 + timedelta(minutes=STEP_MIN * k), d, k))
                k += 1
        picked = sorted(picked + extra, key=lambda x: x[0])
    return picked


def _ab_payload(doc: dict, shift: int, overrides: dict, day: date, tz: str, load_actual_w: list | None):
    """The archived payload, shifted, with the overrides applied. Returns
    (payload, micro_w, cut_ready, notes): micro_w is the must-take lane on the
    same shifted grid, cut_ready says whether the cut rule can run (a split is
    on), notes lists approximations taken."""
    pay = dict(doc.get("payload") or {})
    n = int(doc["n"]) - shift
    for k in AB_LIST_KEYS:
        if k in pay:
            pay[k] = list(pay[k])[shift:]
    pay["prediction_horizon"] = n
    # An archived intermediate target is an index on the archived grid; a shifted
    # re-solve starts `shift` steps later, so the index moves with it or, once
    # it is at or before the new t0, leaves (2026-09-07; until then a cadence-15
    # re-solve of a plan with a target aimed it `shift` steps late).
    if shift and pay.get("soc_target_timestep") is not None:
        k = int(pay["soc_target_timestep"]) - shift
        if k > 0:
            pay["soc_target_timestep"] = k
        else:
            pay.pop("soc_target", None)
            pay.pop("soc_target_timestep", None)
    micro = [float(v) for v in (doc.get("pv_micro_w") or [0.0] * int(doc["n"]))][shift:]
    cuts = [bool(v) for v in (doc.get("micro_cut") or [False] * int(doc["n"]))][shift:]
    notes = []
    z = ZoneInfo(tz)
    t0 = _parse_ts(doc["t0"]).astimezone(z) + timedelta(minutes=STEP_MIN * shift)
    times = step_times(t0, n)
    # base load without the must-take lane, whatever the archived cut did
    load_w = None
    if pay.get("load_power_forecast"):
        load_w = [float(l) + (0.0 if c else m) for l, m, c in zip(pay["load_power_forecast"], micro, cuts)]
    # own knobs that rebuild the lists. The TOTAL the planner saw is fixed first
    # (archived main + archived lane); the lane and the main are then re-derived
    # from it, so a share change moves watts between the two halves and a mix
    # change moves the total, never both at once by accident.
    total = [float(v) + m for v, m in zip(pay.get("pv_power_forecast") or [0.0] * n, micro)]
    if "growatt_share" in overrides and doc.get("growatt_share"):
        f = float(overrides["growatt_share"]) / float(doc["growatt_share"])
        micro = [min(m * f, t) for m, t in zip(micro, total)]
        notes.append("growatt_share rescales the archived must-take lane")
    if "pv_p10_mix" in overrides:
        p50, p10 = doc.get("pv_p50_w"), doc.get("pv_p10_w")
        if p50 and p10 and len(p50) == int(doc["n"]):
            mixed = pv_mix(p50[shift:], p10[shift:], float(overrides["pv_p10_mix"]))
            micro = [m * (mx / t) if t > 0 else m for m, mx, t in zip(micro, mixed, total)]
            total = mixed
            notes.append("pv_p10_mix rebuilt from the archived percentiles; the must-take lane scaled with the total")
        else:
            notes.append("pv_p10_mix unavailable: the document has no archived percentiles")
    pay["pv_power_forecast"] = [round(max(0.0, t - m), 1) for t, m in zip(total, micro)]
    if overrides.get("load") == "actual" and load_actual_w is not None and load_w is not None:
        for k, t in enumerate(times):
            if t.date() == day:
                i = _slot(t)
                if i < len(load_actual_w) and load_actual_w[i] is not None:
                    load_w[k] = float(load_actual_w[i])
        notes.append("load: measured load in place of the profile over the day")
    if load_w is not None:
        pay["load_power_forecast"] = [round(l - m, 1) for l, m in zip(load_w, micro)]
    # The knob layer, on top of what the archived solve ran with: knobs not
    # overridden keep the ARCHIVED values (the document carries them from
    # 2026-09-07 on; older documents imply the constants of their day).
    archived = dict(doc.get("knobs") or {})
    kn = knobs(archived)
    for k in LIVE_KNOBS:
        if k in overrides:
            kn[k] = overrides[k]
    if "stress_scale" in overrides:
        f = float(overrides["stress_scale"]) / float(archived.get("stress_scale", 1.0) or 1.0)
        pay["battery_stress_cost"] = round(float(pay.get("battery_stress_cost", 0.0)) * f, 5)
        pay["inverter_stress_cost"] = round(float(pay.get("inverter_stress_cost", 0.0)) * f, 5)
    if "surplus_base" in overrides:
        base = float(archived.get("surplus_base", SURPLUS_BASE) or SURPLUS_BASE)
        pay["battery_soc_surplus_cost"] = round(float(pay.get("battery_soc_surplus_cost", 0.0))
                                                * float(overrides["surplus_base"]) / base, 5)
        notes.append("surplus_base scales the archived dwell cost (the rebalance fade of that day is kept)")
    if "deficit_threshold" in overrides:
        pay["battery_soc_deficit_threshold"] = float(overrides["deficit_threshold"])
    if "deficit_cost" in overrides:
        pay["battery_soc_deficit_cost"] = float(overrides["deficit_cost"])
    if "soc_final" in overrides:
        pay["soc_final"] = round(float(overrides["soc_final"]), 4)
    for k, key in (("weight_battery_discharge", "weight_battery_discharge"),
                   ("soc_min", "battery_minimum_state_of_charge"), ("soc_max", "battery_maximum_state_of_charge")):
        if k in overrides:
            pay[key] = float(overrides[k])
    if "batt_power_max_w" in overrides:
        pay["battery_charge_power_max"] = pay["battery_discharge_power_max"] = float(overrides["batt_power_max_w"])
    for k in AB_RUNTIME_KEYS:                      # raw runtime keys, for diagnostics; they win
        if k in overrides:
            pay[k] = overrides[k]
    if "soc_target" in overrides or "soc_target_at" in overrides:
        pay.pop("soc_target", None)
        pay.pop("soc_target_timestep", None)
        level = float(overrides.get("soc_target", kn["soc_target"]) or 0.0)
        if level > 0:
            k = soc_target_timestep(t0, n, day, kn["soc_target_at"])
            if k is not None:
                pay["soc_target"], pay["soc_target_timestep"] = round(level, 4), k
    return pay, micro, (load_w is not None and any(micro)), notes, times, kn


def _ab_inputs(archive_dir: str, day: date, tz: str, overrides: dict, actuals: dict, cadence_min: int,
               n_day: int, midnight: datetime) -> tuple[dict | None, list, tuple[str, str] | None]:
    """The walk's inputs in check order: the overrides validated, the actuals
    complete, the executed lane settled, the solves picked. Returns (executed,
    solves, error) with error = (status, message) on the first failed check."""
    bad = ab_validate(overrides)
    if bad:
        return None, [], ("unknown_override", f"unknown override keys: {', '.join(bad)}")
    for k in ("pv_w", "load_w"):
        if not actuals.get(k) or len(actuals[k]) != n_day:
            return None, [], ("no_actuals", f"{k} missing or not {n_day} steps")
    executed = virtual_day(archive_dir, day, tz, midnight + timedelta(days=1), actuals["pv_w"], actuals["load_w"],
                           actuals.get("curtailed"), None, actuals.get("micro_w"))
    if executed is None or executed["soc_pct"][0] is None:
        return None, [], ("no_chain", "no settled chain covers the day")
    solves = _ab_solves(archive_dir, day, tz, cadence_min)
    if not solves:
        return executed, [], ("no_plans", "no organic plan made on the day")
    return executed, solves, None


def _ab_prefix(executed: dict, first_solve: datetime, midnight: datetime, n_day: int) -> tuple[dict, float]:
    """The empty lanes with the steps before the first solve copied from the
    executed lane. Returns (lanes, soc) with soc the fraction the walk starts from."""
    # step 0 is taken from the executed lane; the walk starts at the first solve
    lanes = {k: [None] * n_day for k in ("soc_pct", "p_grid_w", "p_batt_w", "pv_curtail_w", "micro_cut_w",
                                          "p_pv_w", "p_load_w", "believed_soc_pct", "buy", "sell", "clamp_a")}
    first_idx = step_index(first_solve, midnight)
    for i in range(max(0, first_idx)):
        for k in ("soc_pct", "p_grid_w", "p_batt_w", "pv_curtail_w", "p_pv_w", "p_load_w", "buy", "sell"):
            lanes[k][i] = executed[k][i]
        lanes["micro_cut_w"][i] = (executed.get("micro_cut_w") or [0.0] * n_day)[i]
        lanes["believed_soc_pct"][i] = executed["soc_pct"][i]
    # The step the walk starts from must be a settled one: a gap-held step (no
    # plan in force, None in the executed lane) right before the first solve
    # used to raise TypeError here and kill the run without a status.
    prev = executed["soc_pct"][first_idx - 1] if first_idx > 0 else executed["soc_start_pct"]
    return lanes, (prev / 100.0 if prev is not None else None)


def _ab_solve_once(base_url: str, pay: dict, micro: list, cut_ready: bool, today_idx: list, kn: dict,
                   cut_active: bool, timeout: int, out: dict, t_solve: datetime, shift: int):
    """One solve of the walk: the add-on, the row-count check, the Growatt cut
    second pass when the rule fires, then back to gross. Counts solves and
    seconds into `out`. Returns (rows, micro_cut, cut_active, log_row), or None
    after setting out's status when the solve failed."""
    n = int(pay["prediction_horizon"])
    res = solve(base_url, pay, timeout)
    out["solves"] += 1
    out["seconds"] = round(out["seconds"] + res.get("seconds", 0.0), 1)
    rows = res.get("rows") or []
    # EXACTLY this solve's horizon, not "at least": naive-mpc returns exactly
    # prediction_horizon rows, so any other count means the add-on's latest
    # result belongs to a solve that landed in between (a production plan),
    # and truncating it would use another plan's rows without a trace.
    if not res["ok"] or len(rows) != n or str(rows[0].get("optim_status", "")) != "Optimal":
        out.update(status="solve_failed",
                   message=f"solve at {t_solve.isoformat()}: " + (res.get("message") or
                           (f"{len(rows)} rows for a {n}-step horizon: a foreign solve landed in between"
                            if res["ok"] and len(rows) != n else "not Optimal")))
        return None
    micro_cut = [False] * n
    today_set = set(today_idx)
    dec = None
    if cut_ready and today_idx:
        dec = aux_cut_decision(sum(float(rows[k].get("P_PV_curtailment") or 0.0) for k in today_idx) * STEP_H / 1000.0,
                               sum(micro[k] for k in today_idx) * STEP_H / 1000.0, cut_active, **cut_thresholds(kn))
        if dec["active"]:
            base_load = [float(l) + m for l, m in zip(pay["load_power_forecast"], micro)]
            pay2 = cut_payload(pay, base_load, micro, today_set)
            res2 = solve(base_url, pay2, timeout)
            out["solves"] += 1
            out["seconds"] = round(out["seconds"] + res2.get("seconds", 0.0), 1)
            rows2 = (res2.get("rows") or [])[:n]
            if res2["ok"] and len(rows2) == n and str(rows2[0].get("optim_status", "")) == "Optimal":
                rows = rows2
                micro_cut = [k in today_set for k in range(n)]
            else:
                dec["active"] = False
        cut_active = dec["active"]
    # gross, as run_plan archives it
    rows_to_gross(rows, micro, micro_cut)
    log_row = {"t": t_solve.isoformat(), "shift": shift, "soc_init": pay["soc_init"],
               "cut": bool(dec and dec["active"]), "ratio": (dec or {}).get("ratio"),
               "soc_target_timestep": pay.get("soc_target_timestep")}
    return rows, micro_cut, cut_active, log_row


def _ab_settle(rows: list, micro: list, micro_cut: list, times: list, t_solve: datetime, t_next: datetime,
               day: date, midnight: datetime, pot: list, load_a: list, mic_a: list, soc: float, settle: str,
               capacity_kwh: float, eta_c: float, eta_d: float, lanes: dict, margin: bool = False,
               deadband_a: float = DEYE_CLAMP_DEADBAND_A, clamp: dict | None = None) -> float:
    """Settle the steps this solve is in force for against the real day and
    write them into the lanes. Returns the SOC fraction the next solve starts
    from. `clamp` carries the writer's standing charge clamp and write count
    across solves ({"a": amps or None, "writes": n})."""
    clamp = clamp if clamp is not None else {"a": None, "writes": 0}
    for k, t in enumerate(times):
        if t < t_solve or t >= t_next or t.date() != day:
            continue
        i = step_index(t, midnight)
        r = rows[k]
        pg, pb, ppv, pl = float(r["P_grid"]), float(r["P_batt"]), float(r["P_PV"]), float(r["P_Load"])
        pc = float(r.get("P_PV_curtailment") or 0.0)
        if settle == "open":
            g = pg + (load_a[i] - pl) - (pot[i] - ppv)
            b, c = pb, pc
        else:
            wanted, lift = wanted_clamp(pg, pb, micro_cut=micro_cut[k], pv_curtail_w=pc,
                                        sell=float(r["unit_prod_price"]), margin=margin)
            clamp["a"], wrote = clamp_write(clamp["a"], wanted, deadband_a, lift=lift)
            clamp["writes"] += wrote
            g, b, c = settle_step(pg, pb, ppv, pl, pc, pot[i], load_a[i], micro[k], mic_a[i], soc,
                                  capacity_kwh, micro_cut=micro_cut[k], sell=float(r["unit_prod_price"]),
                                  eta_c=eta_c, eta_d=eta_d, margin=margin, clamp_a=clamp["a"])
            lanes["clamp_a"][i] = clamp["a"]
        soc, _clamped = integrate_soc(soc, b, eta_c, eta_d, capacity_kwh)
        lanes["soc_pct"][i] = round(soc * 100, 2)
        lanes["p_grid_w"][i], lanes["p_batt_w"][i], lanes["pv_curtail_w"][i] = round(g, 1), round(b, 1), round(c, 1)
        lanes["micro_cut_w"][i] = round(micro[k], 1) if micro_cut[k] else 0.0
        lanes["p_pv_w"][i], lanes["p_load_w"][i] = round(pot[i], 1), round(load_a[i], 1)
        lanes["believed_soc_pct"][i] = round(float(r["SOC_opt"]) * 100, 2)
        lanes["buy"][i], lanes["sell"][i] = float(r["unit_load_cost"]), float(r["unit_prod_price"])
    return soc


def _ab_summaries(lanes: dict, executed: dict, n_day: int, capacity_kwh: float,
                  day: date, tz: str) -> tuple[dict, dict]:
    """The variant's summary and the executed day's, in the same frame."""
    summary = ab_summary(lanes, n_day, capacity_kwh, day, tz)
    executed_summary = ab_summary({k: executed[k] for k in ("soc_pct", "p_grid_w", "pv_curtail_w", "buy", "sell")}
                                  | {"micro_cut_w": executed.get("micro_cut_w") or [0.0] * n_day}, n_day, capacity_kwh,
                                  day, tz)
    return summary, executed_summary


def ab_walk(archive_dir: str, base_url: str, day_iso: str, tz: str, overrides: dict, actuals: dict,
            cadence_min: int = 30, settle: str = "closed", capacity_kwh: float = CAPACITY_KWH,
            eta_c: float = 0.961, eta_d: float = 0.957, timeout: int = 180) -> dict:
    """One day, one variant. `actuals`: pv_w (measured), load_w, micro_w,
    curtailed (mask), all per 15-minute step of the day. Returns the summary,
    the settled lanes and the solve log; status != ok explains why not."""
    day = date.fromisoformat(day_iso)
    midnight = local_midnight(day, tz)
    n_day = expected_steps(day, tz)
    out = {"status": "", "day": day_iso, "overrides": dict(overrides or {}), "cadence_min": cadence_min,
           "settle": settle, "solves": 0, "seconds": 0.0, "notes": []}
    executed, solves, error = _ab_inputs(archive_dir, day, tz, overrides, actuals, cadence_min, n_day, midnight)
    if error:
        out.update(status=error[0], message=error[1])
        return out
    pot = [float(v) if v is not None else float(actuals["pv_w"][i]) for i, v in enumerate(executed["p_pv_w"])]
    mic_a = [float(v) for v in (actuals.get("micro_w") or [0.0] * n_day)]
    load_a = [float(v) for v in actuals["load_w"]]
    lanes, soc = _ab_prefix(executed, solves[0][0], midnight, n_day)
    if soc is None:
        out.update(status="no_chain", message="no settled step before the first solve")
        return out
    cut_active, log_rows = False, []
    clamp = {"a": None, "writes": 0}
    ov = overrides or {}
    deadband_a = float(ov["clamp_deadband_a"]) if ov.get("clamp_deadband_a") is not None else DEYE_CLAMP_DEADBAND_A
    for j, (t_solve, doc, shift) in enumerate(solves):
        t_next = solves[j + 1][0] if j + 1 < len(solves) else midnight + timedelta(days=1)
        pay, micro, cut_ready, notes, times, kn = _ab_payload(doc, shift, overrides, day, tz, load_a)
        for nt in notes:
            if nt not in out["notes"]:
                out["notes"].append(nt)
        pay["soc_init"] = round(min(max(soc, SOC_MIN), SOC_MAX), 4)
        today_idx = [k for k, t in enumerate(times) if t.date() == day]
        solved = _ab_solve_once(base_url, pay, micro, cut_ready, today_idx, kn, cut_active, timeout, out, t_solve, shift)
        if solved is None:
            return out
        rows, micro_cut, cut_active, log_row = solved
        log_rows.append(log_row)
        # settle the steps this solve is in force for
        soc = _ab_settle(rows, micro, micro_cut, times, t_solve, t_next, day, midnight, pot, load_a, mic_a, soc,
                         settle, capacity_kwh, eta_c, eta_d, lanes, margin=bool(ov.get("margin", SETTLE_MARGIN)),
                         deadband_a=deadband_a, clamp=clamp)
    summary, executed_summary = _ab_summaries(lanes, executed, n_day, capacity_kwh, day, tz)
    summary["clamp_writes"] = clamp["writes"]
    executed_summary["clamp_writes"] = executed.get("clamp_writes")
    out.update(status="ok", lanes=lanes, solve_log=log_rows, summary=summary, executed=executed_summary)
    return out


def ab_store(ab_dir: str, label: str, payload: dict) -> str:
    """Write an A/B run, gzipped, atomically. Native so the wrapper can run it
    off the event loop (a gzip.open inside pyscript is a blocking call HA warns about)."""
    os.makedirs(ab_dir, exist_ok=True)
    path = os.path.join(ab_dir, f"{label}.json.gz")
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def ab_load(ab_dir: str, label: str) -> dict | None:
    path = os.path.join(ab_dir, f"{label}.json.gz")
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def ab_apply_plan(overrides: dict) -> tuple[dict, dict]:
    """Which helpers a variant's overrides write, and which keys cannot go live.
    Returns (applied {entity_id: value}, skipped {key: reason}). The wrapper does
    the writes; this stays pure and testable."""
    applied, skipped = {}, {}
    for k, v in (overrides or {}).items():
        if k in LIVE_KNOBS:
            applied[LIVE_KNOBS[k][1]] = v
        elif k in AB_RUNTIME_KEYS:
            skipped[k] = "raw EMHASS runtime key: use the knob form or change config.json"
        elif k == "load":
            skipped[k] = "a diagnostic, not a setting"
        elif k == "margin":
            skipped[k] = "actuator policy (the writer's clamp margin), not a helper"
        elif k == "clamp_deadband_a":
            skipped[k] = "actuator policy (the writer's clamp deadband), not a helper"
        else:
            skipped[k] = "unknown"
    return applied, skipped


def ab_summary(lanes: dict, n_day: int, capacity_kwh: float = CAPACITY_KWH,
               day: date | None = None, tz: str | None = None) -> dict:
    """The figures a knob is judged on: SOC at 17:00 (the start of the evening
    block), the peak and when, sun declined 10:00-17:00, import by day and by
    daytime, export, and the settled cash in the plan's own prices.

    The clock marks are found on the day's OWN grid when `day` and `tz` are
    given (2026-09-07): as fixed indices (68 for 17:00) every figure after the
    fold was an hour off on the two DST days and the peak was stamped with the
    wrong hour. Without a day the 96-step arithmetic stands."""
    soc = lanes["soc_pct"]; g = lanes["p_grid_w"]; c = lanes["pv_curtail_w"]
    if day is not None and tz is not None:
        starts = step_times(local_midnight(day, tz), n_day)
        at = {hh: next((i for i, t in enumerate(starts) if t.hour == hh and t.minute == 0), None)
              for hh in (6, 10, 17, 18)}
        fmt = lambda i: starts[i].strftime("%H:%M") if i is not None else None
    else:
        at = {6: 24, 10: 40, 17: 68, 18: 72}
        fmt = lambda i: f"{i // 4:02d}:{(i % 4) * 15:02d}" if i is not None else None
    def kwh(vals, lo=0, hi=None, sign=1):
        hi = n_day if hi is None else hi
        if lo is None or hi is None:
            return None
        return round(sum(max(sign * float(v), 0.0) for v in vals[lo:hi] if v is not None) * STEP_H / 1000.0, 3)
    known = [(i, v) for i, v in enumerate(soc) if v is not None]
    peak_i, peak = max(known, key=lambda x: x[1]) if known else (None, None)
    full = next((i for i, v in known if v >= 99.9), None)
    cash = round(sum(step_cost(float(g[i]), float(lanes["buy"][i]), float(lanes["sell"][i]))
                     for i in range(n_day) if g[i] is not None and lanes["buy"][i] is not None), 4)
    # The SOC carry term in the scoreboard's own frame (lambda = 0,9 x the day's
    # mean sell price), so a variant that ends the day emptier is not read as
    # cheaper: on 09-05 the executed lane out-cashed the fixed chain by 0,14 EUR
    # purely by ending 9 points lower.
    sells = [float(v) for v in lanes["sell"] if v is not None]
    lam = lambda_for(sells, 0.9)
    s0 = next((v for v in soc if v is not None), None)
    st = round(soc_term(s0, soc[-1], capacity_kwh, lam), 4) if s0 is not None and soc[-1] is not None else None
    return {"soc_17h_pct": soc[at[17]] if at[17] is not None and n_day > at[17] else None, "soc_end_pct": soc[-1],
            "peak_soc_pct": peak, "peak_at": fmt(peak_i), "full_at": fmt(full), "dwell_95_h": soc_dwell_h(soc),
            "declined_10_17_kwh": kwh(c, at[10], at[17]), "declined_day_kwh": kwh(c),
            "growatt_cut_kwh": kwh(lanes.get("micro_cut_w") or [0.0] * n_day),
            "import_day_kwh": kwh(g), "import_06_18_kwh": kwh(g, at[6], at[18]),
            "export_day_kwh": kwh(g, sign=-1), "cash_eur": cash,
            "soc_term_eur": st, "total_eur": (round(cash + st, 4) if st is not None else None),
            "lambda_eur_kwh": round(lam, 5)}
