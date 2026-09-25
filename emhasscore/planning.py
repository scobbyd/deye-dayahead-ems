"""The plan run: build the runtime lists, solve (twice when the Growatt cut fires), verify, archive, roll."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .grid import horizon, _parse_ts, STEP_H, STEP_MIN, step_times
from .series import load_series, MAX_PV_GAPS, price_series, pv_mix, pv_series, pv_split, tariff
from .objective import (
    apply_knobs,
    aux_cut_decision,
    build_payload,
    cut_payload,
    cut_thresholds,
    knobs,
    plan_cost,
    rebalance_schedule,
    rows_to_gross,
    SOC_MAX,
    SOC_MIN,
    stress_costs,
)
from .addon import solve
from .archive import aux_cut_state, days_since_full, write_plan_archive
from . import rebalance
from .slices import rolled_slices


def load_exact_key(t: datetime) -> str:
    """The key of one step in load_exact_w: the UTC instant as YYYY-MM-DDTHH:MMZ."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _plan_series(inp: dict, t0: datetime, n: int, tz: str, kn: dict, out: dict) -> dict | None:
    """The sun and the load shape on the grid, with their summary keys on out.

    Returns a dict: p50_w, p10_w (the raw Solcast percentiles), mix,
    pv_w (the mix, what the planner is told), gaps, load_w, load_info, split,
    share, main_w, micro_w (the Growatt split). None when the forecast has too
    many gaps to solve; out then carries the message.
    """
    sc = (inp.get("solcast_today"), inp.get("solcast_tomorrow"), inp.get("solcast_day3") or [])
    p50_w, gaps = pv_series(t0, n, *sc)
    p10_w, _ = pv_series(t0, n, *sc, field="pv_estimate10")
    mix = min(max(float(kn["pv_p10_mix"]), 0.0), 1.0)
    pv_w = pv_mix(p50_w, p10_w, mix)           # what the PLANNER is told; P50 stays archived as the ceiling
    out.update(pv_gap_steps=gaps, pv_p10_mix=mix,
               pv_p50_kwh=round(sum(p50_w) * STEP_H / 1000.0, 3),
               pv_mixed_kwh=round(sum(pv_w) * STEP_H / 1000.0, 3))
    limit = inp.get("max_pv_gaps", MAX_PV_GAPS)
    if gaps > limit:
        out["message"] = f"solcast: {gaps} steps without a forecast (limit {limit}); not solved"
        return None
    # Same-clock load shape, or nothing when the caller passed no history (the
    # ML switch is on, or too few days) and EMHASS keeps its own method.
    load_w, load_info = load_series(t0, n, tz, inp.get("load_days"), inp.get("load_now_w"))
    out.update(load_source="profile" if load_w else "emhass",
               load_ref_days=load_info["ref_days"])
    # Exact load on named instants (the ladder's omniscient rungs replayed as a
    # rolling controller, backtest/): {utc key: W} overrides the
    # profile step by step. Live never passes it. The profile stays the base so
    # the Growatt split below still has a load list to net against.
    exact = inp.get("load_exact_w")
    if load_w and exact:
        hits = 0
        for k, t in enumerate(step_times(t0, n)):
            v = exact.get(load_exact_key(t))
            if v is not None:
                load_w[k], hits = round(max(float(v), 0.0), 1), hits + 1
        out["load_exact_steps"] = hits
    # The must-take half leaves pv_power_forecast and arrives as NEGATIVE LOAD,
    # the textbook treatment of an uncontrollable behind-the-meter generator and
    # the only way the solver stops believing it can throttle the Growatt.
    # EMHASS passes a negative load_power_forecast straight through - probed on
    # the live add-on 2026-09-06: four steps sent at -1.500 W came back at
    # -1.500 W with P_grid at -1.500, so set_zero_min does not touch a list
    # supplied as a runtime parameter.
    #
    # It stands down without an ESE series, or without our own load list: when
    # EMHASS runs its own load method there is nowhere to put the Growatt, and
    # sending main-only PV alone would silently delete it from the plan.
    # The ESE site takes the same mix, so the split's invariant (main + micro =
    # the planner's total) holds on the mixed series too.
    ese_sc = (inp.get("solcast_ese_today"), inp.get("solcast_ese_tomorrow"), inp.get("solcast_ese_day3") or [])
    ese50_w, ese_gaps = pv_series(t0, n, *ese_sc)
    ese10_w, _ = pv_series(t0, n, *ese_sc, field="pv_estimate10")
    ese_w = pv_mix(ese50_w, ese10_w, mix)
    share = float(kn["growatt_share"])
    split = bool(load_w) and ese_gaps <= limit and any(ese_w)
    main_w, micro_w = pv_split(pv_w, ese_w, share) if split else (list(pv_w), [0.0] * n)
    out.update(pv_split=split, pv_ese_gap_steps=ese_gaps,
               pv_micro_kwh=round(sum(micro_w) * STEP_H / 1000.0, 3))
    return {"p50_w": p50_w, "p10_w": p10_w, "mix": mix, "pv_w": pv_w, "gaps": gaps,
            "load_w": load_w, "load_info": load_info, "split": split, "share": share,
            "main_w": main_w, "micro_w": micro_w}


def _plan_payload(inp: dict, t0: datetime, n: int, now: datetime, tz: str, soc: float, kn: dict,
                  s: dict, buy, sell, out: dict) -> dict:
    """The solve payload: plant and prices, the net load, stress costs, the
    rebalance schedule and the knobs. days_since_full and knobs land on out."""
    payload = build_payload(t0, n, soc, float(kn["soc_final"]), s["main_w"], buy, sell)
    if s["load_w"]:
        payload["load_power_forecast"] = [round(l - m, 1) for l, m in zip(s["load_w"], s["micro_w"])]
    payload["battery_stress_cost"], payload["inverter_stress_cost"] = stress_costs(buy, sell)
    # The settled clock when the caller keeps one (inp["rebalance"], see
    # rebalance.py); the plan-chain scan only for a caller that does not.
    if "rebalance" in inp:
        dsf = rebalance.days_since(inp.get("rebalance"), now)
    else:
        dsf = days_since_full(inp["archive_dir"], now, tz)
    payload.update(rebalance_schedule(dsf, kn["surplus_base"], kn["deficit_threshold"], kn["deficit_cost"]))
    out["days_since_full"] = dsf
    apply_knobs(payload, kn, t0, n, now.astimezone(ZoneInfo(tz)).date())
    out["knobs"] = kn
    return payload


def _solve_with_cut(inp: dict, t0: datetime, n: int, now: datetime, tz: str, kn: dict,
                    s: dict, payload: dict, out: dict) -> dict | None:
    """Solve, verify the horizon, decide and apply the Growatt cut (a second
    solve), bring the rows back to gross. seconds and the aux_cut_* keys land
    on out. Returns rows, payload (the one actually solved), aux, micro_cut and
    last_run, or None when the solve failed or misaligned; out then carries
    the message.
    """
    micro_w, split = s["micro_w"], s["split"]
    res = solve(inp["base_url"], payload, inp.get("timeout", 180))
    out["seconds"] = res.get("seconds", 0.0)
    if not res["ok"]:
        out["message"] = res["message"]
        return None
    rows = res["rows"]
    first = _parse_ts(rows[0]["timestamp"])
    # EXACTLY this solve's horizon, not "at least" (2026-09-07, the guard ab_walk
    # learned live): naive-mpc-optim returns exactly prediction_horizon rows, so
    # any other count means the add-on's latest result belongs to a solve that
    # landed in between (a hindsight or replay re-solve anchored in the same
    # 15-minute slot passes the t0 check), and truncating it would archive
    # another solve's rows as the plan of record.
    if first != t0 or len(rows) != n:
        out["message"] = (f"horizon misaligned: EMHASS starts {first.isoformat()} with {len(rows)} rows, "
                          f"expected {t0.isoformat()} x {n}")
        return None
    # THE GROWATT CUT, two-pass (see aux_cut_decision). Decided on this UNCUT
    # solve's remaining-today curtailment (main-only by construction) against the
    # remaining-today must-take forecast, with the state carried from today's
    # newest plan. When it fires, the Growatt leaves today's remaining load
    # entries (tomorrow keeps it) and the plan is solved again on that footing.
    times = step_times(t0, n)
    today_idx = [k for k, t in enumerate(times) if t.date() == now.astimezone(ZoneInfo(tz)).date()]
    today_set = set(today_idx)
    aux = aux_cut_decision(sum(float(rows[k].get("P_PV_curtailment") or 0.0) for k in today_idx) * STEP_H / 1000.0,
                           sum(micro_w[k] for k in today_idx) * STEP_H / 1000.0,
                           aux_cut_state(inp["archive_dir"], now, tz), **cut_thresholds(kn))
    aux.update(cut_from=None, cut_until=None, n_cut_steps=0, second_solve=None,
               uncut_cost_eur=plan_cost(rows))
    micro_cut = [False] * n
    if aux["active"] and split and today_idx:
        payload2 = cut_payload(payload, s["load_w"], micro_w, today_set)
        res2 = solve(inp["base_url"], payload2, inp.get("timeout", 180))
        out["seconds"] = round(out["seconds"] + res2.get("seconds", 0.0), 1)
        rows2 = res2.get("rows") or []
        if (res2["ok"] and len(rows2) == n and _parse_ts(rows2[0]["timestamp"]) == t0
                and str(rows2[0].get("optim_status", "")) == "Optimal"):
            rows, payload = rows2, payload2
            micro_cut = [k in today_set for k in range(n)]
            aux.update(second_solve="ok", n_cut_steps=len(today_idx),
                       cut_from=times[today_idx[0]].isoformat(),
                       cut_until=(times[today_idx[-1]] + timedelta(minutes=STEP_MIN)).isoformat())
        else:
            # The uncut plan stands, and the state does not advance on a failed solve.
            aux.update(active=False, second_solve=res2.get("message") or "not Optimal")
    out.update(aux_cut_active=aux["active"], aux_cut_ratio=aux["ratio"],
               aux_cut_curtail_kwh=aux["curtail_kwh"], aux_cut_aux_kwh=aux["aux_kwh"],
               aux_cut_steps=aux["n_cut_steps"])
    # Only a split plan has anything to put back; an unsplit one keeps EMHASS's
    # rows untouched (micro_w is all zero there, but the gross helper would still
    # re-round every P_PV and P_Load).
    if split:
        rows_to_gross(rows, micro_w, micro_cut)
    return {"rows": rows, "payload": payload, "aux": aux, "micro_cut": micro_cut, "last_run": res["last_run"]}


def _archive_doc(t0: datetime, n: int, tz: str, soc: float, kn: dict, tf: dict,
                 s: dict, sol: dict, out: dict) -> dict:
    """The archived plan document, from the series, the solve and the result so far."""
    return {"plan_ts": out["plan_ts"], "t0": t0.isoformat(), "n": n, "tz": tz, "soc_init": soc,
            "soc_final": float(kn["soc_final"]),
            "tariff": tf, "n_predicted_steps": out["n_predicted_steps"], "pv_gap_steps": s["gaps"],
            "growatt_share": s["share"] if s["split"] else None, "pv_micro_w": s["micro_w"],
            # The raw Solcast percentiles, gross, positional over the horizon. rows[i].P_PV
            # is what the planner was FED (the mix, gross); these are what Solcast SAID.
            "pv_p50_w": s["p50_w"], "pv_p10_w": s["p10_w"], "pv_p10_mix": s["mix"],
            "knobs": kn,
            "aux_cut": sol["aux"], "micro_cut": sol["micro_cut"],
            "prices_predicted": out["prices_predicted"], "optim_status": out["optim_status"],
            "cost_eur": out["cost_eur"],
            "seconds": out["seconds"], "payload": sol["payload"], "rows": sol["rows"], "last_run": sol["last_run"],
            "soc_source": out["soc_source"], "load_source": out["load_source"],
            "load_ref_days": out["load_ref_days"]}


def run_plan(inp: dict) -> dict:
    """Build the runtime lists, solve, verify, archive, derive the rolled slices.

    inp keys: now (ISO), tz, base_url, archive_dir, dry_run, np_today, np_tomorrow
    (Nord Pool rows), epex (forecast list), solcast_today, solcast_tomorrow,
    solcast_day3 (detailedForecast lists; day3 extends the horizon a day),
    solcast_ese_today, solcast_ese_tomorrow, solcast_ese_day3 (the ESE site,
    the Growatt split), soc_pct, soc_source, tariff {energy_tax, supplier_fee,
    btw_pct, feedin_fee}, knobs (the live knob layer, see LIVE_KNOBS);
    optional load_days, load_now_w (the same-clock load shape; absent when
    EMHASS keeps its own method), load_exact_w ({load_exact_key: W}, exact load
    on named steps over the profile; the replay's knowledge walks), actual_pv_w, actual_load_w, curtailed,
    pv_peak_w, micro_w and the prev_* forms of those five (measured today and
    yesterday, for the rolled slices), rebalance (the settled clock's state,
    rebalance.py; when the key is present, even as None, the plan-chain scan is
    not used), max_pv_gaps, timeout.
    Result keys: ok, message, t0, n, seconds, n_predicted_steps, prices_predicted,
    pv_gap_steps, pv_p10_mix, pv_p50_kwh, pv_mixed_kwh, load_source,
    load_ref_days, load_exact_steps (only with load_exact_w), pv_split, pv_ese_gap_steps, pv_micro_kwh, days_since_full,
    knobs, aux_cut_active, aux_cut_ratio, aux_cut_curtail_kwh, aux_cut_aux_kwh,
    aux_cut_steps, optim_status, cost_eur, plan_ts, soc_source, archive_path,
    yesterday, today, next_day (compact slices; only when ok and not dry_run).
    Each early return carries the keys set up to that point.
    """
    tz = inp["tz"]
    now = datetime.fromisoformat(inp["now"]).astimezone(ZoneInfo(tz))
    # Look one day further when Solcast day-3 PV is available: the scored day
    # is then fully mid-horizon and no terminal artifact touches it. Prices
    # for the extra day come from the epex fill and count as predicted.
    day3 = inp.get("solcast_day3") or []
    t0, n = horizon(now, tz, days_ahead=2 if day3 else 1)
    out = {"ok": False, "message": "", "t0": t0.isoformat(), "n": n, "seconds": 0.0}
    try:
        da, n_pred = price_series(t0, n, inp.get("np_today"), inp.get("np_tomorrow"), inp.get("epex"))
    except ValueError as e:
        out["message"] = f"prices: {e}"
        return out
    tf = inp["tariff"]
    buy, sell = tariff(da, tf["energy_tax"], tf["supplier_fee"], tf["btw_pct"], tf["feedin_fee"])
    kn = knobs(inp.get("knobs"))
    out.update(n_predicted_steps=n_pred,
               prices_predicted=not (inp.get("np_today") or inp.get("np_tomorrow")))
    s = _plan_series(inp, t0, n, tz, kn, out)
    if s is None:
        return out
    soc = min(max(float(inp["soc_pct"]) / 100.0, SOC_MIN), SOC_MAX)
    payload = _plan_payload(inp, t0, n, now, tz, soc, kn, s, buy, sell, out)
    sol = _solve_with_cut(inp, t0, n, now, tz, kn, s, payload, out)
    if sol is None:
        return out
    rows = sol["rows"]
    status = str(rows[0].get("optim_status", ""))
    plan_ts = now.isoformat(timespec="seconds")
    out.update(optim_status=status, cost_eur=plan_cost(rows), plan_ts=plan_ts)
    out["soc_source"] = inp.get("soc_source", "real")
    if inp.get("dry_run"):
        out["ok"] = status == "Optimal"
        out["message"] = "dry run" if out["ok"] else f"optim_status {status}"
        return out
    doc = _archive_doc(t0, n, tz, soc, kn, tf, s, sol, out)
    out["archive_path"] = write_plan_archive(inp["archive_dir"], now, doc)
    if status != "Optimal":
        out["message"] = f"optim_status {status}; archived, not rolled or published"
        return out
    sl = rolled_slices(inp["archive_dir"], now, tz,
                       inp.get("actual_pv_w"), inp.get("actual_load_w"),
                       inp.get("prev_pv_w"), inp.get("prev_load_w"),
                       inp.get("curtailed"), inp.get("prev_curtailed"),
                       inp.get("pv_peak_w"), inp.get("prev_pv_peak_w"),
                       inp.get("micro_w"), inp.get("prev_micro_w"))
    out.update(yesterday=sl["yesterday"], today=sl["today"], next_day=sl["next_day"],
               day_after=sl["day_after"], ok=True, message="ok")
    return out
