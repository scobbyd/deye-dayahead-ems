"""The plan run: build the runtime lists, solve (twice when the Growatt cut fires), verify, archive, roll."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .grid import horizon, _parse_ts, STEP_H, STEP_MIN, step_times
from .series import (load_series, MAX_PV_GAPS, MICRO_IN_LOAD, price_series, pv_mix, pv_series, pv_split, solver_buy,
                     tariff_frame)
from .objective import (
    apply_knobs,
    aux_cut_decision,
    batt_power_limits,
    build_payload,
    cut_payload,
    lp_lists,
    cut_thresholds,
    is_v2,
    knobs,
    plan_cost,
    rebalance_schedule,
    rebalance_soc_final,
    rows_to_gross,
    SOC_MAX,
    SOC_MIN,
    STANDBY_LOAD_W,
    stress_costs,
)
from .addon import solve
from .archive import aux_cut_state, days_since_full, list_plans, virtual_soc_at, write_plan_archive
from . import rebalance
from .actuals import DayActuals, add_day_series
from .slices import rolled_slices
from .thermal import _not_sent, apply_thermal, heat_topology, relax_topology, strip_topology


REFRESH_PERIOD_MIN = 15      # the plan refresh: :13/:28/:43/:58 (emhass_automations.yaml, emhass_plan_refresh)
REFRESH_OFFSET_MIN = 13
REFRESH_GUARD_MIN = 2        # an ad-hoc solve stays this many minutes clear of a refresh, either side


def refresh_wait_s(minute: int) -> int:
    """Seconds an ad-hoc solve (the A/B) waits so it does not start inside a
    refresh window: from REFRESH_GUARD_MIN minutes before a refresh minute to
    as many after it, the wait runs to the minute after the window; 0 outside.
    Until 2026-10-05 the wrapper stepped off :13 and :43 only, the refresh
    minutes of before 2026-09-27."""
    k = (int(minute) - (REFRESH_OFFSET_MIN - REFRESH_GUARD_MIN)) % REFRESH_PERIOD_MIN
    width = 2 * REFRESH_GUARD_MIN + 1
    return 60 * (width - k) if k < width else 0


def load_exact_key(t: datetime) -> str:
    """The key of one step in load_exact_w: the UTC instant as YYYY-MM-DDTHH:MMZ."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _ac_exclusion(inp: dict, tinfo: dict) -> tuple[dict | None, float | None]:
    """The AC circuits the solve models itself, to take out of the load
    history: (summed same-clock history, summed live W) over the zones the
    topology actually sent (inp["ac_days_by_zone"], inp["ac_now_w_by_zone"],
    keyed living / upstairs), or (None, None) when it is not sent."""
    if not tinfo.get("sent"):
        return None, None
    days_by, now_by = inp.get("ac_days_by_zone") or {}, inp.get("ac_now_w_by_zone") or {}
    days, now_w = {}, 0.0
    for zone in tinfo.get("zones") or {}:
        days = add_day_series(days, days_by.get(zone) or {})
        now_w += float(now_by.get(zone) or 0.0)
    return days or None, now_w


def _plan_series(inp: dict, t0: datetime, n: int, tz: str, kn: dict, out: dict,
                 exclude: tuple = (None, None)) -> dict | None:
    """The sun and the load shape on the grid, with their summary keys on out.

    Returns a dict: p50_w, p10_w (the raw Solcast percentiles), mix,
    pv_w (the mix, what the planner is told), gaps, load_w, load_info, split,
    share, micro_w (the Growatt's part of pv_w, see pv_split), in_load (how the
    Growatt reaches the LP: the plant's convention, or inp["micro_in_load"]
    when set).
    None when the forecast has too many gaps to solve; out then carries the
    message.
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
    load_w, load_info = load_series(t0, n, tz, inp.get("load_days"), inp.get("load_now_w"), *exclude)
    out.update(load_source="profile" if load_w else "emhass",
               load_ref_days=load_info["ref_days"])
    if "thermal" in inp:
        out["load_excluded_w"] = load_info.get("excluded_mean_w", 0.0)
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
    if load_w and is_v2(kn):
        # The Deye's own draw is not in the UPS-load register (loss map v2).
        load_w = [round(l + STANDBY_LOAD_W, 1) for l in load_w]
    # THE GROWATT SPLIT. The ESE site decides which part of the planner's total
    # is the Growatt (micro_w = ESE x share, see pv_split); the ESE site takes
    # the same mix, so the split's invariant (main + micro = the planner's
    # total) holds on the mixed series too. What the LP is then handed is the
    # plant's convention (lp_lists, MICRO_IN_LOAD):
    #
    #   gross (default since 2026-10-12): the whole sun and the whole load; the
    #   Growatt series serves the binary aux cut only.
    #   legacy: the must-take half leaves pv_power_forecast and arrives as
    #   NEGATIVE LOAD, the textbook treatment of an uncontrollable behind-the-
    #   meter generator and the only way an add-on up to 0.18.1 stops believing
    #   it can throttle the Growatt (probed live 2026-09-06: four steps sent at
    #   -1.500 W came back at -1.500 W). EMHASS 0.18.5 clips that load to 0 W
    #   before the solve (upstream #1135), which deleted the sun above house
    #   load from every sunny plan 2026-10-10..12: hence the default.
    #
    # The split stands down without an ESE series, or without our own load
    # list: when EMHASS runs its own load method there is nowhere to net the
    # Growatt, and a cut has nothing to take out. The sun is then sent whole.
    ese_sc = (inp.get("solcast_ese_today"), inp.get("solcast_ese_tomorrow"), inp.get("solcast_ese_day3") or [])
    ese50_w, ese_gaps = pv_series(t0, n, *ese_sc)
    ese10_w, _ = pv_series(t0, n, *ese_sc, field="pv_estimate10")
    ese_w = pv_mix(ese50_w, ese10_w, mix)
    share = float(kn["growatt_share"])
    split = bool(load_w) and ese_gaps <= limit and any(ese_w)
    micro_w = pv_split(pv_w, ese_w, share)[1] if split else [0.0] * n
    out.update(pv_split=split, pv_ese_gap_steps=ese_gaps,
               pv_micro_kwh=round(sum(micro_w) * STEP_H / 1000.0, 3))
    in_load = inp.get("micro_in_load")
    in_load = MICRO_IN_LOAD if in_load is None else bool(in_load)
    return {"p50_w": p50_w, "p10_w": p10_w, "mix": mix, "pv_w": pv_w, "gaps": gaps,
            "load_w": load_w, "load_info": load_info, "split": split, "share": share,
            "micro_w": micro_w, "in_load": in_load}


def _plan_payload(inp: dict, t0: datetime, n: int, now: datetime, tz: str, soc: float, kn: dict,
                  s: dict, buy, sell, out: dict) -> dict:
    """The solve payload: plant and prices, the load (gross, or net of the
    Growatt under the legacy convention), stress costs, the rebalance schedule
    and the knobs. days_since_full and knobs land on out."""
    # The LP sees buy raised to sell (solver_buy); the archived rows get the
    # true buy back in run_plan, and the stress costs price at the true buy.
    pv_lp, load_lp = lp_lists(s["pv_w"], s["load_w"] or None, s["micro_w"], s["in_load"])
    payload = build_payload(t0, n, soc, float(kn["soc_final"]), pv_lp, solver_buy(buy, sell), sell)
    if load_lp:
        payload["load_power_forecast"] = load_lp
    ch_max, dis_max = batt_power_limits(kn["batt_power_max_w"], v2=is_v2(kn))
    payload["battery_stress_cost"], payload["inverter_stress_cost"] = \
        stress_costs(buy, sell, p_nom_batt_kw=max(ch_max, dis_max) / 1000.0)
    # The settled clock when the caller keeps one (inp["rebalance"], see
    # rebalance.py); the plan-chain scan only for a caller that does not.
    if "rebalance" in inp:
        dsf = rebalance.days_since(inp.get("rebalance"), now)
    else:
        dsf = days_since_full(inp["archive_dir"], now, tz)
    payload.update(rebalance_schedule(dsf, kn["surplus_base"], kn["deficit_threshold"], kn["deficit_cost"]))
    payload["battery_soc_surplus_threshold"] = float(kn["surplus_threshold"])
    out["days_since_full"] = dsf
    apply_knobs(payload, kn, t0, n, now.astimezone(ZoneInfo(tz)).date())
    # after the knobs: apply_knobs writes the soc_final knob, the overdue
    # rebalance replaces it with full (rebalance_soc_final, 2026-10-02)
    payload["soc_final"] = rebalance_soc_final(dsf, payload["soc_final"], kn["soc_max"])
    out["soc_final"] = payload["soc_final"]
    apply_thermal(payload, out.get("_topo"), out.get("thermal") or {})
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
    out["_solve_status"] = res.get("status")     # the HTTP status of a failed POST (400: infeasible, 0: no answer)
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
    # solve's remaining-today curtailment against the remaining-today Growatt
    # forecast, with the state carried from today's newest plan. When it fires,
    # the Growatt leaves today's remaining entries (the PV list on gross lists,
    # the load list under the legacy netting; tomorrow keeps it) and the plan is
    # solved again on that footing. The thresholds were calibrated on main-only
    # curtailment (2026-09-07); on gross lists the uncut solve's figure can hold
    # the Growatt's own share too, so the cut comes a little earlier on a day
    # the LP dumps everything.
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
        payload2 = cut_payload(payload, s["load_w"], micro_w, today_set, s["in_load"])
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
        rows_to_gross(rows, micro_w, micro_cut, s["in_load"])
    return {"rows": rows, "payload": payload, "aux": aux, "micro_cut": micro_cut, "last_run": res["last_run"]}


def _archive_doc(t0: datetime, n: int, tz: str, soc: float, kn: dict, tf: dict,
                 s: dict, sol: dict, out: dict) -> dict:
    """The archived plan document, from the series, the solve and the result so far."""
    return {"plan_ts": out["plan_ts"], "t0": t0.isoformat(), "n": n, "tz": tz, "soc_init": soc,
            "soc_final": float(kn["soc_final"]),
            "tariff": tf, "n_predicted_steps": out["n_predicted_steps"], "pv_gap_steps": s["gaps"],
            "growatt_share": s["share"] if s["split"] else None, "pv_micro_w": s["micro_w"],
            # the LP's lists: a document without the key was archived under the
            # legacy netting (every plan before 2026-10-12), so only gross says so
            **({} if s["in_load"] else {"micro_in_load": False}),
            # The raw Solcast percentiles, gross, positional over the horizon. rows[i].P_PV
            # is what the planner was FED (the mix, gross); these are what Solcast SAID.
            "pv_p50_w": s["p50_w"], "pv_p10_w": s["p10_w"], "pv_p10_mix": s["mix"],
            "knobs": kn,
            # the standby the load carries (v2), so an A/B flip back to v1
            # takes out what this solve added (2026-10-09: 90 -> 72 W)
            "standby_load_w": STANDBY_LOAD_W if is_v2(kn) else None,
            "aux_cut": sol["aux"], "micro_cut": sol["micro_cut"],
            "prices_predicted": out["prices_predicted"], "optim_status": out["optim_status"],
            "cost_eur": out["cost_eur"],
            "seconds": out["seconds"], "payload": sol["payload"], "rows": sol["rows"], "last_run": sol["last_run"],
            "soc_source": out["soc_source"], "load_source": out["load_source"],
            "load_ref_days": out["load_ref_days"],
            **({"thermal": out["thermal"], "load_excluded_w": out.get("load_excluded_w", 0.0)}
               if "thermal" in out else {})}


def plan_soc(archive_dir: str, t0: datetime, settled_pct: float | None, real_pct: float | None,
             live: bool, soc_entity: str) -> dict:
    """The SOC a solve starts from, and its source.

    CLOSING THE CHAIN (2026-09-06): the next solve starts from the SETTLED SOC
    (settled_pct, virtual_day's soc_now_pct), what the pack would actually be
    holding after the plan's own commands met the real sun and load, and not
    from the previous plan's SOC_opt, which is what it PREDICTED. Chaining off
    the prediction is why an hourly re-solve never noticed the pack falling
    behind: it was re-solving a fiction, correctly. Without a settled SOC the
    free-running virtual pack at t0 stands in; without that the chain is
    broken and the real pack (real_pct, None when unreadable) re-anchors it.

    LIVE MODE PLANS FROM THE REAL PACK (spec 2026-09-26, section 3.6). The
    writer executes this plan on the inverter, so the solve must start from
    what the pack actually holds, not from the settled virtual chain (which
    stood at 100 % while the real pack read 82 % on go-live day). The virtual
    chain keeps settling for the display slices either way; in live mode its
    drift is the writer's own execution error.

    The archive is read only on the path that needs it. Returns {soc_pct,
    soc_source, notes, error}: notes are (level, text) to log in order; error
    is the message when the chain is broken and the real pack is unreadable
    (soc_pct and soc_source are None then)."""
    notes = []
    v_soc = None if settled_pct is not None else virtual_soc_at(archive_dir, t0)[0]
    if settled_pct is not None:
        soc, source = float(settled_pct), "settled"
    elif v_soc is not None:
        soc, source = v_soc * 100.0, "virtual"
    else:
        soc = real_pct
        if soc is None:
            return {"soc_pct": None, "soc_source": None, "notes": [],
                    "error": f"chain broken and {soc_entity} unavailable"}
        had_plans = bool(list_plans(archive_dir))
        source = "reanchored" if had_plans else "real"
        if had_plans:
            notes.append(("warning", "emhass: virtual SOC chain broken (no archived plan covers %s); "
                                     "reanchored on the real pack" % t0.isoformat()))
    if live:
        if real_pct is not None:
            soc, source = float(real_pct), "real"
        else:
            notes.append(("warning", "emhass: writer live but %s unavailable; planning from the %s SOC"
                          % (soc_entity, source)))
    return {"soc_pct": soc, "soc_source": source, "notes": notes, "error": None}


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
    on named steps over the profile; the replay's knowledge walks), micro_in_load (True or False pins
    how the Growatt reaches the LP, see lp_lists; absent: the plant's convention), actual_pv_w, actual_load_w, curtailed,
    pv_peak_w, micro_w and the prev_* forms of those five (measured today and
    yesterday, for the rolled slices), thermal (the heat topology's inputs,
    thermal.heat_topology), ac_days_by_zone and ac_now_w_by_zone (the AC
    circuit history and live W per zone, living / upstairs; subtracted from
    the load only for the zones the topology sends), rebalance (the settled clock's state,
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
    buy, sell = tariff_frame(da, tf, t0, n)
    kn = knobs(inp.get("knobs"))
    out.update(n_predicted_steps=n_pred,
               prices_predicted=not (inp.get("np_today") or inp.get("np_tomorrow")))
    # The heat topology (thermal.py): decided before the series, because the
    # load forecast drops the AC circuit of each zone the topology sends.
    # A heating input error must never cost the battery its plan: any
    # exception here is the topology not sent, and the solve runs as without it.
    try:
        topo, tinfo = heat_topology(inp.get("thermal"), t0, n, tz, sum(buy) / max(len(buy), 1))
    except Exception as e:
        topo, tinfo = _not_sent(f"error: {e!r}")
    out["_topo"] = topo
    if "thermal" in inp:
        out["thermal"] = dict(tinfo, fallback=None)
    s = _plan_series(inp, t0, n, tz, kn, out, exclude=_ac_exclusion(inp, tinfo))
    if s is None:
        out.pop("_topo", None)
        return out
    soc = min(max(float(inp["soc_pct"]) / 100.0, SOC_MIN), SOC_MAX)
    payload = _plan_payload(inp, t0, n, now, tz, soc, kn, s, buy, sell, out)
    sol = _solve_with_cut(inp, t0, n, now, tz, kn, s, payload, out)
    if tinfo.get("sent"):
        # A floor the sources cannot hold must not cost the battery its plan.
        # On an infeasible solve (a non-Optimal status, or HTTP 400, which is
        # how the add-on answers an infeasible model) and on any other HTTP
        # error the add-on answers with (a 500 is an exception inside EMHASS
        # on a model it accepted): release the first steps' bounds, then drop
        # the topology. A transport failure (no answer, a timeout: status 0
        # or None) or a misaligned horizon ends the quarter: two more solves
        # would not fit the 15-minute refresh. The executor runs legacy for
        # the stale window.
        for label, fn in (("relaxed", relax_topology), ("stripped", strip_topology)):
            if sol is not None and str(sol["rows"][0].get("optim_status", "")) == "Optimal":
                break
            if sol is None and out.get("_solve_status") in (0, None):
                break
            payload = fn(payload)
            out["thermal"]["fallback"] = label
            if label == "stripped":
                # no heat pumps are modelled any more: the AC circuits go back into the load
                s = _plan_series(inp, t0, n, tz, kn, out)
                if s["load_w"]:
                    payload["load_power_forecast"] = [round(l - m, 1) for l, m in zip(s["load_w"], s["micro_w"])]
            sol = _solve_with_cut(inp, t0, n, now, tz, kn, s, payload, out)
        if out["thermal"]["fallback"] == "stripped":
            out["thermal"]["sent"] = False
    out.pop("_topo", None)
    out.pop("_solve_status", None)
    if sol is None:
        return out
    rows = sol["rows"]
    for k, r in enumerate(rows):          # the true buy, not the solver's (solver_buy)
        r["unit_load_cost"] = buy[k]
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
    sl = rolled_slices(inp["archive_dir"], now, tz, DayActuals.from_inp(inp, "actual_"),
                       DayActuals.from_inp(inp, "prev_"))
    out.update(yesterday=sl["yesterday"], today=sl["today"], next_day=sl["next_day"],
               day_after=sl["day_after"], ok=True, message="ok")
    return out
