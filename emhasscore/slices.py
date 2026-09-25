"""The display slices: a compact day slice of one plan, the virtual day rebuilt from whichever plan was in
force at each step, the three rolled slices, and the post-restart rehydrate."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .grid import expected_steps, local_midnight, _parse_ts, STEP_MIN, step_times
from .objective import CAPACITY_KWH, loss_adjustment, step_cost
from .deye import clamp_write, DEYE_CLAMP_DEADBAND_A, SETTLE_MARGIN, integrate_soc, settle_step, soc_dwell_h, wanted_clamp
from .archive import ARCHIVE_REACH, iter_organic_plans, load_plan, newest_plan_for_day, plan_for_day, plan_heads
from .repair import pv_potential
from .scoreboard import read_scores, rolling


def compact_slice(sl: dict, doc: dict) -> dict:
    """Attribute payload for sensor.emhass_plan_today / _next_day (about 5,5 kB for 96 steps)."""
    rows = sl["rows"]
    buy = [round(float(r["unit_load_cost"]), 5) for r in rows]
    sell = [round(float(r["unit_prod_price"]), 5) for r in rows]
    pg = [round(float(r["P_grid"]), 1) for r in rows]
    # The raw P50 per slice row, from the doc's positional series; a pre-mix doc
    # has none and the planner's own P_PV (which then WAS the P50) stands in.
    p50_by_ts = {r["timestamp"]: v for r, v in zip(doc.get("rows") or [], doc.get("pv_p50_w") or [])}
    p50 = [round(float(p50_by_ts.get(r["timestamp"], r["P_PV"])), 1) for r in rows]
    # The plan's own must-take share per slice row, so the scorer's settlement
    # runs the base on the plan's inputs (a zero here double-counted the gen-port
    # spill on a full pack: base with no Growatt held the meter at zero, the
    # actual run spilled, and the difference was booked as extra export).
    mic_by_ts = {r["timestamp"]: v for r, v in zip(doc.get("rows") or [], doc.get("pv_micro_w") or [])}
    micro = [round(float(mic_by_ts.get(r["timestamp"], 0.0)), 1) for r in rows]
    # The Growatt the plan SWITCHED OFF, in watts of forecast, per slice row.
    cut_by_ts = {r["timestamp"]: bool(c) for r, c in zip(doc.get("rows") or [], doc.get("micro_cut") or [])}
    micro_cut_w = [round(float(mic_by_ts.get(r["timestamp"], 0.0)), 1) if cut_by_ts.get(r["timestamp"]) else 0.0
                   for r in rows]
    return {"date": sl["date"], "slice_start": sl["slice_start"], "step_min": STEP_MIN, "n": sl["n"],
            "full_day": sl["n"] == expected_steps(date.fromisoformat(sl["date"]), doc["tz"]),
            "plan_ts": doc["plan_ts"], "t0": doc["t0"], "n_predicted_steps": doc.get("n_predicted_steps", 0),
            "pv_gap_steps": doc.get("pv_gap_steps", 0), "optim_status": doc.get("optim_status"),
            "soc_source": doc.get("soc_source", "real"),
            "soc_start_pct": sl["soc_start_pct"],
            "loss_eur": loss_adjustment(rows),
            "p_batt_w": [round(float(r["P_batt"]), 1) for r in rows], "p_grid_w": pg,
            "soc_pct": [round(float(r["SOC_opt"]) * 100, 2) for r in rows],
            "p_pv_w": [round(float(r["P_PV"]), 1) for r in rows],
            "pv_p50_w": p50, "pv_micro_w": micro, "micro_cut_w": micro_cut_w,
            # P_PV_curtailment is a decision variable of the solve, not a
            # residual: p_pv_w is the sun AVAILABLE and the plan declines part of
            # it when the sell price turns negative. Without this column the
            # chart shows a ceiling and lets it be read as harvest, and the flows
            # across the three charts do not close (2026-09-05: at noon,
            # 7586 available, 2305 declined, 5282 taken, load 279, charge 5000,
            # grid 0 - a 3,1 W balance).
            "pv_curtail_w": [round(float(r.get("P_PV_curtailment") or 0.0), 1) for r in rows],
            "p_load_w": [round(float(r["P_Load"]), 1) for r in rows],
            "buy": buy, "sell": sell,
            "cost_eur": [round(step_cost(pg[i], buy[i], sell[i]), 5) for i in range(len(rows))]}


def _plans_in_force(archive_dir: str, midnight: datetime, now: datetime, key0: float) -> tuple[list, list]:
    """The plans that can be in force on the day, and each plan's rows indexed
    by timestamp. Returns (docs, plans): docs newest first as the archive
    yields them, plans as (plan_ts, plan_ts_str, by_ts, doc) in the same order,
    where by_ts maps a row's epoch timestamp to (row, micro_w, p50_w, cut)."""
    # THE PLANS THAT CAN BE IN FORCE ON `day` (2026-09-07): every organic plan
    # made during the day up to `now`, plus the single newest plan from before
    # its midnight that has a row there. The cutoff rule below (newest plan
    # with plan_ts <= min(step_start, now)) can never pick anything else, so
    # nothing else is opened: at the 15-minute cadence that is ~97 documents
    # for a day instead of the whole archive, and the cost of a reconstruction
    # stops growing with the archive.
    day_end = midnight + timedelta(days=1)
    plans = []                                     # newest plan_ts first, replays out
    docs = list(iter_organic_plans(archive_dir, since=midnight, until=min(now, day_end)))
    for doc in iter_organic_plans(archive_dir, since=midnight - ARCHIVE_REACH,
                                  until=midnight - timedelta(seconds=1)):
        if any(_parse_ts(r["timestamp"]).timestamp() == key0 for r in doc["rows"]):
            docs.append(doc)
            break
    for doc in docs:
        plan_ts = datetime.fromisoformat(doc["plan_ts"])
        # Each row carries its own plan's must-take share, because the plan in
        # force changes step to step and a pre-split plan has none at all.
        mic = doc.get("pv_micro_w") or []
        p50s = doc.get("pv_p50_w") or []
        cuts = doc.get("micro_cut") or []
        by_ts = {_parse_ts(r["timestamp"]).timestamp(): (r, float(mic[k]) if k < len(mic) else 0.0,
                                                         float(p50s[k]) if k < len(p50s) else float(r["P_PV"]),
                                                         bool(cuts[k]) if k < len(cuts) else False)
                 for k, r in enumerate(doc["rows"])}
        plans.append((plan_ts, doc["plan_ts"], by_ts, doc))
    return docs, plans


def _rows_in_force(plans: list, starts: list, now: datetime) -> tuple[list, list, list, list, set]:
    """The row in force per step: the newest plan with plan_ts <= min(step_start,
    now) that has a row stamped exactly at step_start. Returns (rows, micro_w,
    p50_w, cut, used_ts); a step nothing covers is None / 0.0 / None / False."""
    n = len(starts)
    rows_in_force = [None] * n
    micro_in_force = [0.0] * n
    p50_in_force = [None] * n                     # what Solcast SAID, the repair's ceiling (not the mix)
    cut_in_force = [False] * n                    # the Growatt switched off by the plan in force
    used_ts = set()
    for i, step_start in enumerate(starts):
        cutoff = min(step_start, now)
        key = step_start.timestamp()
        for plan_ts, plan_ts_str, by_ts, _doc in plans:
            if plan_ts <= cutoff and key in by_ts:
                rows_in_force[i], micro_in_force[i], p50_in_force[i], cut_in_force[i] = by_ts[key]
                used_ts.add(plan_ts_str)
                break
    return rows_in_force, micro_in_force, p50_in_force, cut_in_force, used_ts


def _anchor_soc(docs: list, starts: list, key0: float) -> float | None:
    """The SOC fraction the virtual pack starts the day from, or None when no
    plan in `docs` can say (the chain is broken)."""
    # The midnight anchor comes from the same set, newest first, by virtual_soc_at's
    # rule: a plan whose horizon starts at midnight anchors on its soc_init, else the
    # SOC_opt of the step ending at midnight. No second archive scan.
    soc = None
    key_prev = (starts[0].astimezone(timezone.utc) - timedelta(minutes=STEP_MIN)).timestamp()
    for doc in docs:
        rows = doc["rows"]
        if rows and _parse_ts(rows[0]["timestamp"]).timestamp() == key0:
            soc = float(doc["soc_init"])
            break
        hit = next((r for r in rows if _parse_ts(r["timestamp"]).timestamp() == key_prev), None)
        if hit is not None:
            soc = float(hit["SOC_opt"])
            break
    return soc


def _repair_past_pv(starts: list, now: datetime, rows_in_force: list, micro_in_force: list,
                    p50_in_force: list, actual_pv_w: list | None, curtailed: list | None,
                    pv_peak_w: list | None, actual_micro_w: list | None
                    ) -> tuple[list, list, dict | None, list]:
    """The measured past on the potential basis. Returns (pv_meas_w, pv_pot_w,
    pv_repair, micro_at), each list per step and None where there is no
    measurement or the step is not in the past."""
    n = len(starts)
    # Potential PV over the past, from each step's own plan-in-force forecast
    # (P_PV is the raw Solcast input echoed back; EMHASS reports whatever it
    # chose to throw away separately, as P_PV_curtailment). Fitted only on a day
    # that has finished - see pv_potential's fit_scale.
    pv_meas_w = [None] * n
    pv_pot_w = [None] * n                        # local: folded into p_pv_w by _integrate_day
    pv_repair = None
    if actual_pv_w is not None:
        idx = [i for i in range(n)
               if starts[i] < now and rows_in_force[i] is not None
               and i < len(actual_pv_w) and actual_pv_w[i] is not None]
        if idx:
            meas = [float(actual_pv_w[i]) for i in idx]
            fc = [float(p50_in_force[i]) for i in idx]
            curt = ([float(curtailed[i]) for i in idx]
                    if curtailed is not None and len(curtailed) >= n else [0.0] * len(idx))
            peak = ([float(pv_peak_w[i]) for i in idx]
                    if pv_peak_w is not None and len(pv_peak_w) >= n else None)
            whole = len(idx) == n and starts[-1] + timedelta(minutes=STEP_MIN) <= now
            # REPAIR THE CURTAILABLE HALF ONLY (2026-09-06). The Growatt is never
            # throttled, so on a curtailed step it is already making everything it
            # can; scaling it up with the strings credits the shadow EMS with sun
            # that was never withheld and inflates pv_reconstructed_kwh and the
            # fitted pv_scale together. Subtract the must-take half from BOTH the
            # measurement and the forecast, repair what is left, add the measured
            # half back untouched. Zero micro everywhere - a pre-split plan, or no
            # micro measurement - collapses to exactly the previous behaviour.
            mic_m = ([float(actual_micro_w[i]) for i in idx]
                     if actual_micro_w is not None and len(actual_micro_w) >= n else [0.0] * len(idx))
            # A PRE-SPLIT plan carries no must-take share, and passing its zeros
            # would fit the scale on main measured against a GROSS forecast -
            # a mixed basis, and a different one from score_row's, which leaves
            # the charts and the scoreboard disagreeing on a curtailed day.
            # None instead makes pv_potential default the forecast side to the
            # measurement, exactly as score_row does.
            mic_f = [micro_in_force[i] for i in idx]
            pot, pv_repair = pv_potential(meas, fc, curt, peak, fit_scale=whole,
                                          micro_meas_w=mic_m,
                                          micro_fc_w=mic_f if any(mic_f) else None)
            for k, i in enumerate(idx):
                pv_meas_w[i] = round(meas[k], 1)
                pv_pot_w[i] = pot[k]

    micro_at = [None] * n
    if actual_micro_w is not None and len(actual_micro_w) >= n:
        for i in range(n):
            if starts[i] < now and actual_micro_w[i] is not None:
                micro_at[i] = float(actual_micro_w[i])
    return pv_meas_w, pv_pot_w, pv_repair, micro_at


def _integrate_day(starts: list, now: datetime, rows_in_force: list, micro_in_force: list,
                   cut_in_force: list, pv_pot_w: list, micro_at: list, actual_load_w: list | None,
                   soc: float, capacity_kwh: float, eta_c: float, eta_d: float,
                   margin: bool = SETTLE_MARGIN, deadband_a: float = DEYE_CLAMP_DEADBAND_A) -> dict:
    """Walk the day through the virtual Deye from `soc`, one settled step at a
    time. Returns the per-step lanes under their output names (p_batt_w,
    soc_pct, p_grid_w, p_pv_w, p_load_w, pv_fc_w, load_fc_w, buy, sell,
    cost_eur, pv_curtail_w; None on a step nothing covers) plus n_past and
    clamped_steps."""
    p_batt, soc_pct, p_grid, p_pv, p_load, buy, sell, cost = [], [], [], [], [], [], [], []
    pv_fc, load_fc, pv_curtail, clamp_a = [], [], [], []
    n_past, clamped_steps = 0, 0
    standing, clamp_writes = None, 0
    for i, step_start in enumerate(starts):
        past = step_start < now
        if past:
            n_past += 1
        r = rows_in_force[i]
        if r is None:
            for lst in (p_batt, soc_pct, p_grid, p_pv, p_load, buy, sell, cost, pv_fc, load_fc,
                        pv_curtail, clamp_a):
                lst.append(None)
            continue
        pb = float(r["P_batt"])
        soc_before = soc

        if pv_pot_w[i] is not None:
            pv_v = float(pv_pot_w[i])
        else:
            pv_v = float(r["P_PV"])
        if past and actual_load_w is not None and i < len(actual_load_w) and actual_load_w[i] is not None:
            load_v = float(actual_load_w[i])
        else:
            load_v = float(r["P_Load"])
        # CLOSED LOOP (2026-09-06). Still the plan's own P_grid moved by a DELTA,
        # never a from-scratch balance: the site does not balance, and the plan's
        # P_grid carries EMHASS's conversion loss that an absolute sum would
        # throw away. What changed is how the delta is found. It used to be
        # assumed to land entirely on the meter, which on 09-05 booked 7,69 kWh
        # of import that no solve ever chose - every contemporaneous plan had
        # P_grid at exactly 0 across that window.
        #
        # Now the same command is run through the virtual inverter twice, once
        # on the plan's own inputs and once on what actually happened, and the
        # difference is the delta. Where the plan is self-balancing the pack
        # absorbs it and the meter does not move, which is what zero-export does
        # in hardware. Where the plan is trading with the grid on purpose the
        # meter takes it, which is also correct. This subsumes the curtailment
        # special case that used to be the only closed-loop branch here.
        mic_p = micro_in_force[i]
        mic_a = micro_at[i] if micro_at[i] is not None else mic_p
        # The standing charge clamp: the writer's register, rewritten only past
        # the deadband, drives the actual run of every step (settle_step).
        wanted, lift = wanted_clamp(float(r["P_grid"]), pb, micro_cut=cut_in_force[i],
                                    pv_curtail_w=float(r.get("P_PV_curtailment") or 0.0),
                                    sell=float(r["unit_prod_price"]), margin=margin)
        standing, wrote = clamp_write(standing, wanted, deadband_a, lift=lift)
        clamp_writes += wrote
        grid_v, pb, cur_v = settle_step(
            float(r["P_grid"]), pb, float(r["P_PV"]), float(r["P_Load"]),
            float(r.get("P_PV_curtailment") or 0.0), pv_v, load_v,
            mic_p, mic_a, soc_before, capacity_kwh, micro_cut=cut_in_force[i],
            sell=float(r["unit_prod_price"]), margin=margin, eta_c=eta_c, eta_d=eta_d,
            clamp_a=standing)

        soc, clamped = integrate_soc(soc, pb, eta_c, eta_d, capacity_kwh)
        if clamped:
            clamped_steps += 1

        b = round(float(r["unit_load_cost"]), 5)
        s = round(float(r["unit_prod_price"]), 5)
        p_batt.append(round(pb, 1))
        soc_pct.append(round(soc * 100, 2))
        p_grid.append(round(grid_v, 1))
        p_pv.append(round(pv_v, 1))
        p_load.append(round(load_v, 1))
        buy.append(b)
        sell.append(s)
        cost.append(round(step_cost(grid_v, b, s), 5))
        # The plan's OWN forecast, kept beside the substituted lane. p_pv_w and
        # p_load_w carry the measurement over the past (that is what makes the
        # virtual pack real), which leaves the forecast-against-actual chart
        # with nothing to compare against - both its lanes would be the same
        # measurement. These two are what EMHASS thought would happen.
        pv_fc.append(round(float(r["P_PV"]), 1))
        load_fc.append(round(float(r["P_Load"]), 1))
        pv_curtail.append(round(cur_v, 1))
        clamp_a.append(standing)
    return {"p_batt_w": p_batt, "soc_pct": soc_pct, "p_grid_w": p_grid, "p_pv_w": p_pv, "p_load_w": p_load,
            "pv_fc_w": pv_fc, "load_fc_w": load_fc, "buy": buy, "sell": sell, "cost_eur": cost,
            "pv_curtail_w": pv_curtail, "n_past": n_past, "clamped_steps": clamped_steps,
            "clamp_a": clamp_a, "clamp_writes": clamp_writes}



def virtual_day(archive_dir: str, day: date, tz: str, now: datetime,
                actual_pv_w: list | None = None, actual_load_w: list | None = None,
                curtailed: list | None = None, pv_peak_w: list | None = None,
                actual_micro_w: list | None = None,
                capacity_kwh: float = CAPACITY_KWH,
                eta_c: float = 0.961, eta_d: float = 0.957, margin: bool = SETTLE_MARGIN,
                soc_start_pct: float | None = None) -> dict | None:
    """Reconstruct what the shadow EMS actually ran (the past part of `day`)
    and currently intends (the rest of it), one step at a time, instead of
    plan_for_day's single plan frozen before midnight. rolled_slices freezes
    today's chart on last night's solve and ignores every hourly re-plan;
    this rebuilds each step from whichever archived plan was actually in
    force at that step's wall-clock time.

    Plan in force, per step: cutoff = min(step_start, now). At wall-clock
    time T the EMS is executing the newest plan it had by T, so a step still
    ahead of now takes the newest plan available right now, not the newest
    plan chronologically nearest to that future step. The chosen plan is the
    newest archived Optimal one with plan_ts <= cutoff that also has a row
    stamped exactly at step_start; a step nothing covers comes back None in
    every per-step list - a gap in the archive, not an error.

    SOC is integrated here rather than read off SOC_opt, so a step pulling
    P_batt from a different plan than its neighbour's never makes the virtual
    SOC jump: start from virtual_soc_at's reading at local midnight and walk
    the SAME P_batt chain the caller sees through eta_c (charge) / eta_d
    (discharge), clamping into [SOC_MIN, SOC_MAX] like the real pack's BMS
    would - the only place the virtual pack is allowed to deviate from the
    commanded flow, and every clamped step is counted. Returns None when
    virtual_soc_at can't find where the chain starts (the chain is broken);
    the caller then re-anchors on the real pack, same as virtual_soc_at's
    other callers.

    actual_pv_w / actual_load_w, when given, replace the plan's own PV and
    load forecast for steps already in the past (step_start < now); future
    steps always use the plan's forecast, even when actuals are supplied.

    PV IS SUBSTITUTED ON THE POTENTIAL BASIS, not the measured one: measured PV
    over a step the real pack curtailed is not the sun the shadow EMS would have
    had, and score_day's replay lane has settled on potential PV since the
    repair shipped. Leaving the display on measured PV made the charts and the
    scoreboard disagree by exactly the repair term on any curtailed day (found
    on 2026-09-05, the same day the repair landed). So p_pv_w - and through
    it p_grid_w, the whole virtual meter - now runs on pv_potential. p_pv_w is
    therefore the sun AVAILABLE at every step: repaired measurement over the
    past, forecast over the future. What the shadow EMS actually harvests is
    p_pv_w - pv_curtail_w, and that is the lane that balances against p_load_w,
    p_batt_w and p_grid_w. pv_meas_w rides along as the metered truth, None
    outside the past.

    Grid is the plan's own P_grid plus ONLY the forecast errors, never a
    from-scratch load - pv - P_batt balance: the plan's P_grid already carries
    EMHASS's own conversion and port losses for the commanded P_batt, and
    those depend on more than P_batt alone (hybrid-inverter throughput too),
    so re-deriving them from the raw balance is systematically wrong by
    exactly that loss (confirmed against live archived plans: tens of W at
    large flows, and not a function of P_batt alone - two steps at the same
    P_batt showed different residuals). The battery command is unchanged, so
    those losses are unchanged and carry over untouched; only the load and
    PV forecast errors move, and the grid is assumed to absorb them one for
    one:
        p_grid = P_grid_plan + (load_used - P_Load_plan) - (pv_used - P_PV_plan)
    For a future step both deltas are zero and this collapses to P_grid_plan
    exactly, as it must.

    `margin` replays the writer's insurance clamp (settle_step); since
    2026-09-12 every settled lane carries it (deye.SETTLE_MARGIN).
    """
    midnight = local_midnight(day, tz)
    n = expected_steps(day, tz)
    starts = step_times(midnight, n)
    key0 = starts[0].timestamp()

    docs, plans = _plans_in_force(archive_dir, midnight, now, key0)
    rows_in_force, micro_in_force, p50_in_force, cut_in_force, used_ts = _rows_in_force(plans, starts, now)

    # `soc_start_pct` overrides the archive's anchor: the ladder chains the
    # executed lane through its own settled pack instead of the live one, which
    # rehydrates re-anchor at every restart (seams of up to 8 % on 09-03..09-07).
    soc = soc_start_pct / 100.0 if soc_start_pct is not None else _anchor_soc(docs, starts, key0)
    if soc is None:
        return None
    soc_start_pct = round(soc * 100, 2)

    pv_meas_w, pv_pot_w, pv_repair, micro_at = _repair_past_pv(
        starts, now, rows_in_force, micro_in_force, p50_in_force,
        actual_pv_w, curtailed, pv_peak_w, actual_micro_w)

    lanes = _integrate_day(starts, now, rows_in_force, micro_in_force, cut_in_force, pv_pot_w, micro_at,
                           actual_load_w, soc, capacity_kwh, eta_c, eta_d, margin=margin)
    soc_pct, n_past = lanes["soc_pct"], lanes["n_past"]

    sources = sorted(used_ts, key=lambda t: datetime.fromisoformat(t), reverse=True)
    # Drop-in for compact_slice: the cards read plan_ts, optim_status and
    # n_predicted_steps off these sensors, so carry the newest contributing
    # plan's own values rather than leaving the keys missing. Every source is
    # Optimal by construction (the selection loop skips anything else).
    newest = next((d for ts, tss, by, d in plans if tss == sources[0]), {}) if sources else {}
    return {"date": day.isoformat(), "slice_start": starts[0].isoformat(), "step_min": STEP_MIN, "n": n,
            "full_day": True, "soc_start_pct": soc_start_pct,
            "plan_ts": sources[0] if sources else None, "t0": newest.get("t0"),
            "optim_status": "Optimal" if sources else None,
            "n_predicted_steps": newest.get("n_predicted_steps", 0),
            "pv_gap_steps": newest.get("pv_gap_steps", 0), "soc_source": "virtual",
            # The SETTLED SOC as of `now`: the last past step's integrated value,
            # which is what the pack would actually be holding rather than what
            # the plan predicted. run_plan chains the next solve's soc_init off
            # this, which is the difference between an hourly re-solve and an
            # hourly re-solve THAT KNOWS SOMETHING WENT WRONG.
            "soc_now_pct": next((soc_pct[i] for i in range(min(n_past, len(soc_pct)) - 1, -1, -1)
                                 if soc_pct[i] is not None), None),
            "loss_eur": loss_adjustment([r for r in rows_in_force if r is not None]),
            "p_batt_w": lanes["p_batt_w"], "soc_pct": soc_pct, "p_grid_w": lanes["p_grid_w"],
            "p_pv_w": lanes["p_pv_w"], "p_load_w": lanes["p_load_w"],
            "pv_fc_w": lanes["pv_fc_w"], "load_fc_w": lanes["load_fc_w"], "buy": lanes["buy"],
            "sell": lanes["sell"], "cost_eur": lanes["cost_eur"],
            "pv_curtail_w": lanes["pv_curtail_w"], "pv_meas_w": pv_meas_w,
            "micro_cut_w": [(round(micro_in_force[i], 1) if cut_in_force[i] else 0.0) if rows_in_force[i] is not None else None
                            for i in range(n)],
            "pv_scale": (pv_repair or {}).get("scale"),
            "pv_freed_steps": (pv_repair or {}).get("freed_steps", 0),
            "pv_repaired_steps": (pv_repair or {}).get("repaired_steps", 0),
            "pv_reconstructed_kwh": (pv_repair or {}).get("reconstructed_kwh", 0.0),
            "n_past": n_past, "clamped_steps": lanes["clamped_steps"], "sources": sources,
            # hours the virtual pack sat at or above 95 % (the past half; future steps are the plan's own SOC)
            "dwell_95_h": soc_dwell_h(soc_pct[:n_past]),
            # the writer's standing charge clamp per step, and how many times it was rewritten
            "clamp_a": lanes["clamp_a"], "clamp_writes": lanes["clamp_writes"],
            "actuals_used": actual_pv_w is not None or actual_load_w is not None}


def rolled_slices(archive_dir: str, now: datetime, tz: str,
                  actual_pv_w: list | None = None, actual_load_w: list | None = None,
                  prev_pv_w: list | None = None, prev_load_w: list | None = None,
                  curtailed: list | None = None, prev_curtailed: list | None = None,
                  pv_peak_w: list | None = None, prev_pv_peak_w: list | None = None,
                  micro_w: list | None = None, prev_micro_w: list | None = None) -> dict:
    """The three display slices. NOT plan_for_day, which is the scoring
    selector: it takes only a plan made before the day's midnight covering the
    whole day, so today's chart froze on last night's solve and every hourly
    re-plan was thrown away (found live 2026-09-04, sixteen hours stale). A
    mid-day plan can never satisfy it either, since it only covers the rest of
    the day. So today comes from virtual_day - each step from the plan that was
    actually in force then, measured PV and load substituted over the past -
    and tomorrow from newest_plan_for_day, where a plan made today does cover
    the whole day. Scoring keeps plan_for_day untouched; the plan of record
    still has to be frozen before the day starts.

    YESTERDAY is the same virtual_day construction one day back, every step of
    it in the past, so it is entirely the realised virtual trajectory. The
    Shadow EMS charts run a fixed 24 h back / 36 h forward window; without it
    every lane began at today's midnight and the first hours of that window
    were blank canvas (2026-09-04). It carries no scoring weight - the
    scorer settles against plan_for_day, untouched - and a broken chain simply
    leaves it None, exactly as today does.

    DAY AFTER (2026-09-12): the fourth slice, D+2, from the same
    newest_plan_for_day lookup as tomorrow. The solve already reaches 24:00 of
    D+2 whenever Solcast day-3 is in (grid.horizon days_ahead=2), so the
    archive holds a full-day slice for it from the first solve of the day; the
    forecast-against-actual chart's 36 h forward window reaches into D+2 every
    afternoon and drew it blank. A gross slice through compact_slice,
    like tomorrow's, is what the chart's spec history asked for when the
    published emhass_da_* lanes were retired from that chart (they hold the
    curtailable share only). None when no Optimal plan covers the whole day,
    which is every solve made without a day-3 Solcast list."""
    today = now.astimezone(ZoneInfo(tz)).date()
    vd = virtual_day(archive_dir, today, tz, now, actual_pv_w, actual_load_w, curtailed, pv_peak_w,
                     micro_w)
    if vd is None:                                   # chain broken: last night's plan is better than nothing
        found = plan_for_day(archive_dir, today, tz)
        vd = compact_slice(found[1], found[0]) if found else None
    yday = today - timedelta(days=1)
    prev = virtual_day(archive_dir, yday, tz, now, prev_pv_w, prev_load_w, prev_curtailed,
                       prev_pv_peak_w, prev_micro_w)
    if prev is None:
        found = plan_for_day(archive_dir, yday, tz)
        prev = compact_slice(found[1], found[0]) if found else None
    nxt = newest_plan_for_day(archive_dir, today + timedelta(days=1), tz)
    aft = newest_plan_for_day(archive_dir, today + timedelta(days=2), tz)
    return {"yesterday": prev, "today": vd,
            "next_day": compact_slice(nxt[1], nxt[0]) if nxt else None,
            "day_after": compact_slice(aft[1], aft[0]) if aft else None}


def rehydrate(archive_dir: str, csv_path: str, tz: str, now_iso: str, stale_hours: float,
              actual_pv_w: list | None = None, actual_load_w: list | None = None,
              prev_pv_w: list | None = None, prev_load_w: list | None = None,
              curtailed: list | None = None, prev_curtailed: list | None = None,
              pv_peak_w: list | None = None, prev_pv_peak_w: list | None = None,
              micro_w: list | None = None, prev_micro_w: list | None = None) -> dict:
    """Everything the wrapper needs to rebuild its states after an HA restart.

    The must-take series ride along since 2026-09-07: without them the today
    slice rebuilt after a restart repaired the WHOLE array on a curtailed step
    (the Growatt included, which is never throttled) until the next plan
    replaced it, the exact inconsistency the 09-06 repair-the-curtailable-half
    fix removed from the plan path."""
    now = datetime.fromisoformat(now_iso).astimezone(ZoneInfo(tz))
    out = rolled_slices(archive_dir, now, tz, actual_pv_w, actual_load_w, prev_pv_w, prev_load_w,
                        curtailed, prev_curtailed, pv_peak_w, prev_pv_peak_w, micro_w, prev_micro_w)
    rows = read_scores(csv_path)
    out["rolling"] = rolling(rows) if rows else None
    out["last_row"] = rows[-1] if rows else None
    # Freshness is "how old is the newest plan the EMS actually ran", so it reads
    # the same organic list as the reconstruction. The old form took the newest
    # FILE, which after a replay run is a doc carrying its source's pre-midnight
    # plan_ts: a restart in that window would have measured the age as ~44 h
    # against a 26 h limit, called the plan stale and skipped republishing it,
    # leaving every future lane on the Shadow EMS charts blank until the next
    # hourly run. emhass_replay_range writes one such doc per day replayed.
    path, newest = next(((p, h["plan_ts"]) for p, h in plan_heads(archive_dir)
                         if not h["replay"] and h["optim_status"] == "Optimal"), (None, None))
    age_h = ((now.timestamp() - datetime.fromisoformat(newest).timestamp()) / 3600
             if newest else None)
    out["plan_fresh"] = age_h is not None and age_h < float(stale_hours)
    # The solve record of that plan, for addon_holds_plan: fresh says the plan
    # is worth serving, this says whether the add-on still holds it.
    out["newest_last_run"] = (load_plan(path) or {}).get("last_run") if path else None
    return out
