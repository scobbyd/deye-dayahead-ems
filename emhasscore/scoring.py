"""The scoreboard's lanes: planned, replayed (the plan's decisions on the real day), realised, hindsight
(the 20/20 ceiling), the replay of a past day under current settings, and score_day that writes the row."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .grid import expected_steps, _parse_ts, STEP_H
from .series import load_series, price_series, tariff
from .objective import (
    apply_knobs,
    build_payload,
    knobs,
    ETA_BRIDGE,
    GRID_CAP_W,
    METER_DRIFT_EUR,
    Q_BRIDGE,
    plan_cost,
    Q_PORT,
    rebalance_schedule,
    restamp,
    rows_to_gross,
    SOC_FINAL_TARGET,
    SOC_MAX,
    SOC_MIN,
    step_cost,
    stress_costs,
)
from .deye import settle_slice, soc_dwell_h
from .addon import solve
from .archive import day_slice, days_since_full, original_plan_for_day, plan_for_day, write_plan_archive
from .repair import effective_load, pv_potential
from .scoreboard import read_scores, rolling, SCORE_COLUMNS, upsert_score
from .slices import compact_slice


def lambda_for(sell_values, frac: float) -> float:
    """Value of a kWh left in the pack at midnight: a fraction of the day's mean sell price."""
    vals = list(sell_values or [])
    return float(frac) * (sum(vals) / len(vals)) if vals else 0.0


def soc_term(start_pct: float, end_pct: float, capacity_kwh: float, lam: float) -> float:
    """Positive when the pack ends lower than it started (energy consumed from storage)."""
    return (float(start_pct) - float(end_pct)) / 100.0 * capacity_kwh * lam


def planned_cost(compact: dict, capacity_kwh: float, lam: float) -> dict:
    cash = round(sum(compact["cost_eur"]), 4)
    st = round(soc_term(compact["soc_start_pct"], compact["soc_pct"][-1], capacity_kwh, lam), 4)
    loss = round(float(compact.get("loss_eur") or 0.0), 4)
    return {"cash": cash, "soc_term": st, "loss": loss, "total": round(cash + st + loss, 4)}


def cash_in_frame(g_w, buy, sell) -> float:
    """Settle a measured grid trace (W, + = import) in a given tariff frame."""
    return round(sum(step_cost(float(g), buy[i], sell[i]) for i, g in enumerate(g_w)), 4)


def replay_day(compact: dict, g_meas_w, b_meas_dc_w, pv_actual_w,
               capacity_kwh: float, lam: float, eta_bridge: float = ETA_BRIDGE,
               pv_pot_w=None) -> dict:
    """The counterfactual lane: the plan's battery decisions on the real day.

    Written as a DIFFERENCE from the measured grid trace, never as an absolute
    balance. The site does not balance - about 4,2 kWh/day of inverter standby,
    conversion loss and unmetered circuits sits between (load - PV - battery)
    and the meter - and an absolute reconstruction hands that residual to
    whichever lane is rebuilt, worth up to 0,70 EUR/day, which is larger than
    the defect being fixed. In the difference form every unmodelled term appears
    in both halves and cancels, and eta_bridge is the only coefficient needed:

        B_plan(t)    = P_Load(t) - (P_PV(t) - P_PV_curtailment(t)) - P_grid(t)   the plan's battery at the AC node,
                                                          read off the row rather than P_batt so
                                                          the steps where the 12 kW bridge cap
                                                          binds stay honest (up to 133 W apart);
                                                          a SETTLED slice takes p_batt_w instead,
                                                          because its rows still hold the forecast
                                                          PV and load (see below)
        B_meas_ac(t) = eta x B_dc when discharging, B_dc / eta when charging
        G_replay(t)  = G_meas(t) - B_plan(t) + B_meas_ac(t) - (PV_pot(t) - PV_meas(t))

    The last term is the curtailment repair. Where the real array was held back
    because the REAL pack was full, the counterfactual house - whose pack had
    room - would have had that energy, and it leaves through the meter. Without
    `pv_pot_w` the term is zero and the lane runs on measured PV alone.

    Same house, same PV, same load, same standby and conversion losses on the
    common path; EMHASS's battery instead of the incumbent EMS's. Settled in the plan's OWN
    archived tariff, never the live helpers.

    status: 'ok', 'no_actuals' (a series missing or the wrong length), or 'caps'
    when the replayed trace asks more of the connection than GRID_CAP_W in any
    step - scored anyway, but flagged rather than silently believed.
    """
    n = compact["n"]
    out = {"status": "no_actuals", "cash": None, "soc_term": None, "loss": None, "eur": None,
           "peak_import_kw": None, "peak_export_kw": None, "cap_steps": 0}
    if any(s is None or len(s) != n for s in (g_meas_w, b_meas_dc_w, pv_actual_w)):
        return out
    buy, sell = compact["buy"], compact["sell"]
    pl, pv, pg = compact["p_load_w"], compact["p_pv_w"], compact["p_grid_w"]
    # p_pv_w is the sun AVAILABLE; what entered the balance is the harvest, so a
    # step the plan curtailed needs the declined watts taken back off it or
    # B_plan overstates the commanded charge by exactly that amount (2026-09-05:
    # -6904 W read against a commanded -5101). Nothing scored so far is affected
    # - the plans of record for 09-03 and 09-04 both declined 0,00 kWh - but the
    # plan of record for 09-05 declines 10,39.
    pc = compact.get("pv_curtail_w") or [0.0] * n
    pot = pv_pot_w if (pv_pot_w and len(pv_pot_w) == n) else pv_actual_w
    # A SETTLED slice carries the settlement's battery in p_batt_w and its grid
    # in p_grid_w, but p_pv_w and p_load_w are still the FORECAST, so the row
    # balance no longer describes the trajectory that integrated soc_pct. Found
    # on 2026-09-04: 11:15 self-balanced to +165 W on the real deficit, the
    # balance still read -3.658 W, and the lane was billed 3,7 kW of import at
    # 0,114 for a charge that never happened - 1,7 EUR of the day's 1,98 gap.
    settled = bool(compact.get("settled"))
    pb = compact.get("p_batt_w") or [0.0] * n
    eta = float(eta_bridge)
    cash, loss, g_hi, g_lo, caps = 0.0, 0.0, 0.0, 0.0, 0
    # step_eur: the same cash and loss, kept per step for the ladder's hourly
    # sidecar (2026-09-16); sums to cash + loss, never to eur (no SOC term).
    step_eur = []
    for i in range(n):
        if settled:
            b_plan = float(pb[i])
        else:
            b_plan = float(pl[i]) - (float(pv[i]) - float(pc[i])) - float(pg[i])
        b_dc = float(b_meas_dc_w[i])
        b_ac = b_dc * eta if b_dc >= 0 else b_dc / eta
        repair = float(pot[i]) - float(pv_actual_w[i])
        g = float(g_meas_w[i]) - b_plan + b_ac - repair
        c = step_cost(g, buy[i], sell[i])
        cash += c
        # loss_adjustment's two stages on the REPLAYED flows: the port carries the
        # plan's battery, the bridge carries the real PV plus that battery (a
        # charging b_plan is negative, so PV going DC-coupled into the pack never
        # crosses the bridge - the same asymmetry Q_PORT/Q_BRIDGE exists to price).
        p_b = b_plan / 1000.0
        p_hyb = (float(pot[i]) + b_plan) / 1000.0
        price = (float(buy[i]) + float(sell[i])) / 2.0
        l = (Q_PORT * p_b * p_b + Q_BRIDGE * p_hyb * p_hyb) * STEP_H * price
        loss += l
        step_eur.append(round(c + l, 6))
        g_hi, g_lo = max(g_hi, g), min(g_lo, g)
        if abs(g) > GRID_CAP_W:
            caps += 1
    # The battery trajectory is the plan's, unchanged, so the SOC carry-over term
    # is the plan's too - only the cash and the bridge loss move.
    st = soc_term(compact["soc_start_pct"], compact["soc_pct"][-1], capacity_kwh, lam)
    out.update(status="caps" if caps else "ok", cash=round(cash, 4), soc_term=round(st, 4),
               loss=round(loss, 4), eur=round(cash + st + loss, 4), cap_steps=caps, step_eur=step_eur,
               peak_import_kw=round(g_hi / 1000.0, 2), peak_export_kw=round(-g_lo / 1000.0, 2))
    return out


def _planned_lane(row: dict, plan: dict, capacity_kwh: float, lambda_frac: float, flags: list) -> float:
    """The plan's own self-cost in its own world; returns the day's lambda."""
    lam = lambda_for(plan["sell"], lambda_frac)
    pc = planned_cost(plan, capacity_kwh, lam)
    row.update(plan_ts=plan["plan_ts"], optim_status=plan["optim_status"],
               n_predicted_steps=int(plan.get("n_predicted_steps") or 0),
               planned_cash_eur=pc["cash"], planned_soc_term_eur=pc["soc_term"],
               planned_loss_eur=pc["loss"], planned_eur=pc["total"],
               lambda_eur_kwh=round(lam, 5), soc_start_plan_pct=plan["soc_start_pct"],
               soc_end_plan_pct=plan["soc_pct"][-1],
               pv_planned_kwh=round(sum(plan["p_pv_w"]) * STEP_H / 1000.0, 2),
               load_planned_kwh=round(sum(plan["p_load_w"]) * STEP_H / 1000.0, 2))
    if row["n_predicted_steps"]:
        flags.append("predicted_prices")
    if plan["optim_status"] != "Optimal":
        flags.append("not_optimal")
    if plan.get("pv_gap_steps"):
        flags.append("pv_gaps")
    return lam


def _replayed_lane(row: dict, plan: dict, actuals: dict | None, capacity_kwh: float, lam: float,
                   flags: list) -> None:
    """PV repair, the closed-loop settlement and the plan's decisions on the real day."""
    act = actuals or {}
    pv_pot, pv_info = None, None
    if act.get("pv_w") and len(act["pv_w"]) == plan["n"]:
        pv_pot, pv_info = pv_potential(act["pv_w"], plan.get("pv_p50_w") or plan["p_pv_w"],
                                       act.get("curtailed") or [0.0] * plan["n"],
                                       act.get("pv_peak_w"),
                                       micro_meas_w=act.get("micro_w"))
        row.update(pv_scale=pv_info["scale"], pv_reconstructed_kwh=pv_info["reconstructed_kwh"],
                   pv_curtailed_frac=round(pv_info["curtailed_steps"] / float(plan["n"]), 4))
    # CLOSED-LOOP BASIS (2026-09-06). plan_for_day stays the scoring selector:
    # the plan of record still has to be frozen before the day starts. What
    # moves is the settlement underneath it. Scored open-loop, a plan that
    # self-balances got billed at the meter for every watt of sun that did
    # not show up - 7,69 kWh of import on 09-05 that no solve ever chose.
    # planned_* above stays on the RAW plan, because that is the plan's own
    # self-cost in its own world and is not about the real day at all.
    scored = settle_slice(plan, pv_pot or act.get("pv_w"), act.get("load_w"),
                          act.get("micro_w"), capacity_kwh) or plan
    if scored is not plan:
        flags.append("settled")
    if scored.get("soc_pct"):
        row["dwell_95_h"] = soc_dwell_h(scored["soc_pct"])
    rep = replay_day(scored, act.get("grid_w"), act.get("batt_dc_w"), act.get("pv_w"),
                     capacity_kwh, lam, pv_pot_w=pv_pot)
    row.update(replay_status=rep["status"], replayed_cash_eur=rep["cash"],
               replayed_loss_eur=rep["loss"], replayed_eur=rep["eur"])
    if pv_info and pv_info["repaired_steps"]:
        flags.append("pv_repaired")
    if rep["status"] == "caps":
        flags.append("replay_caps")


def _realised_lane(row: dict, plan, realised: dict, actuals: dict | None, capacity_kwh: float,
                   flags: list) -> None:
    """The measured day in the plan's own tariff, with the utility meter alongside."""
    # The measured flows re-settled in the plan's OWN tariff, so a helper edited
    # mid-day cannot price the two lanes differently (0,75 EUR of the headline on
    # 2026-09-03). The utility meter stays alongside as the cross-check.
    meter = realised.get("cash_eur")
    cash = meter
    g_meas = (actuals or {}).get("grid_w")
    if plan is not None and g_meas is not None and len(g_meas) == plan["n"]:
        cash = cash_in_frame(g_meas, plan["buy"], plan["sell"])
        if meter is not None and abs(cash - meter) > METER_DRIFT_EUR:
            flags.append("meter_drift")
    row.update(realised_cash_eur=cash, realised_cash_meter_eur=meter,
               soc_start_real_pct=realised.get("soc_start_pct"),
               soc_end_real_pct=realised.get("soc_end_pct"), pv_actual_kwh=realised.get("pv_kwh"),
               load_actual_kwh=realised.get("load_kwh"))
    if cash is None:
        flags.append("no_meter")
    have_soc = realised.get("soc_start_pct") is not None and realised.get("soc_end_pct") is not None
    if plan is not None and cash is not None:
        if have_soc:
            rst = round(soc_term(realised["soc_start_pct"], realised["soc_end_pct"], capacity_kwh,
                                 row["lambda_eur_kwh"]), 4)
            row["realised_soc_term_eur"] = rst
            row["realised_eur"] = round(float(cash) + rst, 4)
        else:
            flags.append("no_soc")


def _hindsight_lane(row: dict, hindsight: dict | None, flags: list) -> None:
    """The 20/20 ceiling as the caller computed it (see hindsight_day)."""
    if hindsight is not None:
        row["hindsight_status"] = hindsight.get("status") or None
        if hindsight.get("status") == "ok":
            row["hindsight_eur"] = hindsight.get("eur")
        elif hindsight.get("status") == "pending_horizon":
            # Not a failure: the matched horizon reaches past the scored day, so
            # the lane fills in once the plan's whole window is in the recorder.
            flags.append("hindsight_pending")
        else:
            flags.append("no_hindsight")


def score_row(day: date, plan, realised: dict, capacity_kwh: float, lambda_frac: float,
              hindsight: dict | None = None, actuals: dict | None = None) -> dict:
    row = {c: None for c in SCORE_COLUMNS}
    row["date"] = day.isoformat()
    flags = []
    if plan is None:
        flags.append("no_plan")
    else:
        lam = _planned_lane(row, plan, capacity_kwh, lambda_frac, flags)
        _replayed_lane(row, plan, actuals, capacity_kwh, lam, flags)
    _realised_lane(row, plan, realised, actuals, capacity_kwh, flags)
    if plan is not None and row["replayed_eur"] is None:
        flags.append("no_replay")
    _hindsight_lane(row, hindsight, flags)
    # The verdict (2026-09-05): expected vs theoretical max vs actual.
    #   planned_eur    what the plan EXPECTED to earn, on its own forecasts
    #   replayed_eur   that plan's decisions run against the real day (ACTUAL)
    #   hindsight_eur  the best any plan could do with perfect foresight (MAX)
    # gap = actual - max: how far short of perfect foresight the plan fell
    # (>= 0, lower is better). forecast_gap = actual - expected: how much the
    # forecasts flattered the plan. realised (the incumbent EMS's steering priced in OUR
    # frame, with none of its aFRR revenue) is deliberately NOT a verdict lane -
    # it is kept only as the meter drift cross-check; the real comparison with the incumbent
    # comes later from their settled data plus operational metrics.
    if row["replayed_eur"] is not None and row["hindsight_eur"] is not None:
        row["gap_eur"] = round(row["replayed_eur"] - row["hindsight_eur"], 4)
    if row["replayed_eur"] is not None and row["planned_eur"] is not None:
        row["forecast_gap_eur"] = round(row["replayed_eur"] - row["planned_eur"], 4)
    row["flags"] = ";".join(flags)
    return row


def score_day(archive_dir: str, csv_path: str, day_iso: str, tz: str, realised: dict,
              capacity_kwh: float, lambda_frac: float, hindsight: dict | None = None,
              actuals: dict | None = None) -> dict:
    """Score one calendar day from the archive; re-scoring keeps the realised
    (and hindsight) columns already captured for that day when the caller
    passes None, and keeps an old ok hindsight over a newly failed one.

    `actuals` carries the day's measured 15-minute series in W - pv_w, grid_w
    (+ = import) and batt_dc_w (+ = discharge) - which build the replayed lane
    and re-settle the realised one. Without them the row keeps its planned and
    realised columns but has no gap, and is flagged no_replay."""
    day = date.fromisoformat(day_iso)
    realised = dict(realised)
    old = next((r for r in read_scores(csv_path) if r["date"] == day_iso), None)
    if old:
        for k_new, k_old in (("soc_start_pct", "soc_start_real_pct"),
                             ("soc_end_pct", "soc_end_real_pct"), ("pv_kwh", "pv_actual_kwh"),
                             ("load_kwh", "load_actual_kwh")):
            if realised.get(k_new) is None:
                realised[k_new] = old.get(k_old)
        # cash_eur is the utility meter's own figure. Rows written before the
        # replay lane landed kept it in realised_cash_eur, which now holds the
        # frame-settled value instead, so fall back to the old column.
        if realised.get("cash_eur") is None:
            realised["cash_eur"] = (old.get("realised_cash_meter_eur")
                                    if old.get("realised_cash_meter_eur") is not None
                                    else old.get("realised_cash_eur"))
    # "pending_horizon" is monotone: a plan's window closes once and never
    # reopens, so a pending day carrying a hindsight value can only be holding
    # one computed under an older definition of the lane. Preserving it would
    # score a new replayed lane against an old ceiling, so it is dropped and the
    # day settles for real when the window closes.
    pending = (hindsight or {}).get("status") == "pending_horizon"
    if old and not pending and (hindsight is None or hindsight.get("status") != "ok"):
        if old.get("hindsight_eur") is not None:
            hindsight = {"status": old.get("hindsight_status") or "ok", "eur": old["hindsight_eur"]}
        elif hindsight is None and old.get("hindsight_status"):
            hindsight = {"status": old["hindsight_status"], "eur": None}
    found = plan_for_day(archive_dir, day, tz)
    plan = compact_slice(found[1], found[0]) if found else None
    row = score_row(day, plan, realised, capacity_kwh, lambda_frac, hindsight, actuals)
    rows = upsert_score(csv_path, row)
    return {"row": row, "rows": rows, "rolling": rolling(rows)}


def _hindsight_window_ok(window: dict | None, day_actuals: dict | None, n: int) -> bool:
    """The plan's whole horizon has to be in the recorder, and the scored day too."""
    for k in ("grid_w", "batt_dc_w", "pv_w"):
        w = (window or {}).get(k)
        if not w or len(w) != n:
            return False
        if not (day_actuals or {}).get(k):
            return False
    return True


def _hindsight_measured(doc: dict, window: dict, n: int) -> tuple:
    """Two of the three replaced inputs: the effective load and the repaired PV
    over the plan's horizon. Returns (load_window_w, pv_window_w, pv_info)."""
    # effective_load keeps MEASURED PV: it is the site's real consumption, which
    # curtailment does not change. Only the LP's PV input is repaired, otherwise
    # the load would be inflated by the curtailed energy and counted twice.
    load_window_w = effective_load(window["grid_w"], window["batt_dc_w"], window["pv_w"])
    p50s = doc.get("pv_p50_w") or []
    plan_pv = ([float(v) for v in p50s[:n]] if len(p50s) >= n
               else [float(r["P_PV"]) for r in doc["rows"][:n]])
    pv_window_w, pv_info = pv_potential(window["pv_w"], plan_pv,
                                        window.get("curtailed") or [0.0] * n,
                                        window.get("pv_peak_w"),
                                        micro_meas_w=window.get("micro_w"))
    return load_window_w, pv_window_w, pv_info


def _hindsight_prices(doc: dict, t0, n: int, np_rows):
    """The third replaced input: real day-ahead prices for every step, in the
    plan's own tariff frame. None when any step is held or predicted."""
    try:
        da, n_pred = price_series(t0, n, np_rows, None, None)
    except ValueError:
        n_pred = 1
    if n_pred:                       # a held or predicted step is not hindsight
        return None
    tf = doc["tariff"]
    return tariff(da, tf["energy_tax"], tf["supplier_fee"], tf["btw_pct"], tf["feedin_fee"])


def _hindsight_payload(doc: dict, t0, n: int, pv_window_w, load_window_w, mic, buy, sell) -> dict:
    """The replaced inputs on the plan of record's OWN objective."""
    soc_final = float(doc.get("soc_final") or SOC_FINAL_TARGET)
    payload = build_payload(t0, n, float(doc["soc_init"]), soc_final,
                            [round(max(0.0, float(p) - float(m)), 1) for p, m in zip(pv_window_w, mic)], buy, sell)
    payload["load_power_forecast"] = [round(float(v) - float(m), 1) for v, m in zip(load_window_w, mic)]
    # The plan's own objective, so the only difference between the lanes is the
    # forecasts: its stress costs and SOC knobs, not ones recomputed here.
    plan_payload = doc.get("payload") or {}
    u_batt, u_inv = stress_costs(buy, sell)
    payload["battery_stress_cost"] = plan_payload.get("battery_stress_cost", u_batt)
    payload["inverter_stress_cost"] = plan_payload.get("inverter_stress_cost", u_inv)
    knobs = ("battery_soc_surplus_cost", "battery_soc_deficit_threshold", "battery_soc_deficit_cost")
    if all(k in plan_payload for k in knobs):
        payload.update({k: plan_payload[k] for k in knobs})
    else:
        payload.update(rebalance_schedule(None))
    return payload


def _hindsight_solve(base_url: str, payload: dict, timeout: int, t0, n: int, mic, out: dict, tries: int = 2):
    """Solve, verify, restamp onto the plan's grid and go back to gross. Returns
    the rows, or None with out carrying the status (and message).

    Retried ONCE by default. A walk posts dozens of solves in sequence and reads
    each back off the add-on's opt_res_latest, so a neighbouring solve landing in
    between is read as the wrong row count and the foreign-solve guard rejects
    it. Measured on the real archive: two rungs lost out of 36 on one six-day
    walk, and a rung that loses a solve also loses its CHAIN, so the next day
    resets and the window's comparison is broken. A re-post costs a few seconds.
    A genuine infeasibility is not retried - `not_optimal` is an answer.
    """
    out["posted"] = True
    for attempt in range(max(1, tries)):
        res = solve(base_url, payload, timeout)
        out["seconds"] = res.get("seconds", 0.0)
        if not res["ok"]:
            out["status"], out["message"] = "solve_failed", res["message"]
            continue
        rows = res["rows"]
        if len(rows) != n:             # exactly this solve's horizon, or a foreign solve landed in between
            out["status"] = "solve_failed"
            out["message"] = f"{len(rows)} rows for a {n}-step horizon: not this solve"
            continue
        if str(rows[0].get("optim_status", "")) != "Optimal":
            out["status"] = "not_optimal"
            return None
        out.pop("status", None)
        out.pop("message", None)
        out["retries"] = attempt
        return rows_to_gross(restamp(rows, t0, n), [float(m) for m in mic])
    return None


def _hindsight_settle(rows, doc: dict, day: date, tz: str, mic, day_actuals: dict,
                      capacity_kwh: float, lambda_frac: float) -> tuple:
    """Cut the scored day out of the horizon and settle it through replay_day.
    Returns (status, hs, rep); hs and rep are None unless status is 'ok'."""
    sl = day_slice(rows, day, tz, doc["soc_init"])
    if not sl or sl["n"] != expected_steps(day, tz):
        return "short_slice", None, None
    # rows are already gross, so the slice's own P_PV is the potential and the doc's
    # micro list is the measured Growatt (never cut in the ceiling lane)
    hs = compact_slice(sl, dict(doc, n_predicted_steps=0, pv_gap_steps=0, rows=rows,
                                optim_status="Optimal", soc_source="hindsight",
                                pv_micro_w=[float(m) for m in mic], micro_cut=None, pv_p50_w=None))
    lam = lambda_for(hs["sell"], lambda_frac)
    # hs["p_pv_w"] is the repaired series we posted, sliced to the day, so it is
    # the counterfactual's own PV and settles on the same footing as the plan lane.
    rep = replay_day(hs, day_actuals["grid_w"], day_actuals["batt_dc_w"], day_actuals["pv_w"],
                     capacity_kwh, lam, pv_pot_w=hs["p_pv_w"])
    if rep["status"] == "no_actuals":
        return "no_actuals", None, None
    return "ok", hs, rep
def hindsight_day(archive_dir: str, base_url: str, day_iso: str, tz: str, np_rows,
                  window: dict, day_actuals: dict,
                  capacity_kwh: float, lambda_frac: float, timeout: int = 180) -> dict:
    """The 20/20 lane: the plan of record's OWN solve, re-run with the forecasts
    replaced by what actually happened.

    Matched horizon (2026-09-05). This used to solve 00:00 to 24:00 of the
    scored day with soc_final pinned at SOC_FINAL_TARGET, while the plan lane
    ran a 48-72 h horizon with the scored day sitting MID-horizon, so the plan's
    terminal artifact landed on the following day and hindsight's landed inside
    the day being scored. On 2026-09-03 that forced hindsight to hold 14,5 kWh
    more at midnight than the plan did, credited at lambda against evening
    prices up to 0,337, which handicapped the very lane that is supposed to be
    the ceiling. Giving hindsight the plan's horizon is not extra information
    about the scored day - it already has perfect information there - it only
    stops the pack's closing value being priced differently for the two lanes.
    The stopping point is the PLAN's horizon and no further: beyond it we would
    be measuring the value of a longer forecast, which no real controller can
    have (the day-ahead market publishes about 13 to 37 h out), rather than the
    controller's own shortfall.

    So every input is the plan of record's, taken from the archived doc: t0, n,
    soc_init, soc_final, tariff frame, SOC knobs and stress costs. Only three
    things change, and all three are the definition of hindsight: measured PV,
    measured load, and real day-ahead prices for every step including the ones
    the plan had to predict.

    Settlement is the replay lane's, not the LP's own P_grid. An LP on measured
    PV and measured load produces the ABSOLUTE trace `load - PV - battery`,
    which silently gives hindsight the site's ~4,2 kWh/day of standby,
    conversion loss and unmetered circuits for free - worth up to 0,70 EUR/day
    against a replayed lane that is written as a difference and does not get it.
    Running the hindsight trajectory through replay_day puts both lanes on one
    footing, so gap_eur compares two battery plans over one measured day and
    nothing else.

    Latency is the price: the horizon reaches 24 to 48 h past the scored day, so
    a day can only be settled once its plan's whole window is in the recorder.
    The caller decides when, and gets "pending_horizon" until then.
    """
    day = date.fromisoformat(day_iso)
    out = {"status": "", "eur": None, "seconds": 0.0, "posted": False}
    found = plan_for_day(archive_dir, day, tz)
    if not found:
        out["status"] = "no_plan"
        return out
    doc = found[0]
    t0, n = _parse_ts(doc["t0"]).astimezone(ZoneInfo(tz)), int(doc["n"])
    if not _hindsight_window_ok(window, day_actuals, n):
        out["status"] = "no_actuals"
        return out
    load_window_w, pv_window_w, pv_info = _hindsight_measured(doc, window, n)
    prices = _hindsight_prices(doc, t0, n, np_rows)
    if prices is None:
        out["status"] = "no_prices"
        return out
    buy, sell = prices
    # THE GROWATT IS MUST-TAKE HERE TOO (2026-09-07). The plan lane hands the LP
    # main-only PV and the Growatt as negative load; the ceiling used to get gross
    # PV and so believed it could curtail the gen port, a lever no controller has.
    # Measured Growatt output leaves the PV list and the effective load alike, and
    # the rows go back to gross before slicing, exactly as run_plan does.
    mic = list(window.get("micro_w") or [])
    if len(mic) != n:
        mic = [0.0] * n
    payload = _hindsight_payload(doc, t0, n, pv_window_w, load_window_w, mic, buy, sell)
    rows = _hindsight_solve(base_url, payload, timeout, t0, n, mic, out)
    if rows is None:
        return out
    status, hs, rep = _hindsight_settle(rows, doc, day, tz, mic, day_actuals, capacity_kwh, lambda_frac)
    if status != "ok":
        out["status"] = status
        return out
    out.update(status="ok", cash_eur=rep["cash"], soc_term_eur=rep["soc_term"],
               loss_eur=rep["loss"], eur=rep["eur"], horizon_steps=n,
               horizon_start=t0.isoformat(), soc_start_pct=hs["soc_start_pct"],
               soc_end_pct=hs["soc_pct"][-1], caps=rep["cap_steps"],
               load_eff_kwh=round(sum(load_window_w) * STEP_H / 1000.0, 2), pv=pv_info)
    return out


def replay_plan(archive_dir: str, base_url: str, day_iso: str, tz: str, new_tariff: dict,
                load_days: dict, capacity_kwh: float, lambda_frac: float, timeout: int = 180,
                live_knobs: dict | None = None) -> dict:
    """Re-solve a past day's plan of record under CURRENT logic, on the same
    forecast shoes it originally saw, and insert the result as the new plan of
    record so the day rescores to current settings.

    Preserved from the source plan (the shoes): the archived PV forecast and the
    raw day-ahead price series (re-derived by inverting the source plan's own
    tariff, so any predicted D+1/D+2 prices it saw are kept), plus the horizon
    (t0, n) and soc_init. Swapped to current: the tariff frame, the price-scaled
    stress, the rebalance schedule, and the live knob layer (`live_knobs`, what
    the wrapper reads off the helpers: the SOC band and terminal, the stress
    scale, the dwell costs, the SOC target, the discharge weight, the power cap;
    None means the constants, which is what every replay before 2026-09-07
    silently ran on while claiming current settings). The two knobs that shape
    the sun (pv_p10_mix, growatt_share) are NOT applied: the shoes are the
    archived forecast, and re-mixing it would replay a different day. REBUILT: the load
    forecast, from load_days via load_series - never EMHASS's naive method, which
    on a >1-day horizon reintroduces the phase shift the profile exists to remove
    (so replaying an old payload with no load_power_forecast key would otherwise
    measure the old defect, not current logic).

    The MPC anchors its grid at now, so the solved rows come back stamped with
    today's clock; they are re-stamped onto the source horizon before archiving
    so the plan slices back onto the replayed day. The new doc is marked
    replay=True and carries the source plan's own pre-midnight plan_ts, so
    plan_for_day (which prefers the newest file) picks it as the record while
    original_plan_for_day still finds the untouched original.

    status: ok | no_plan (no organic plan of record for the day) | bad_source |
    insufficient_load (load_series had too few reference days - not solved) |
    solve_failed | not_optimal. posted=True once a solve reached the add-on, so
    the caller re-plans live afterward.
    """
    day = date.fromisoformat(day_iso)
    out = {"status": "", "day": day_iso, "eur": None, "n": 0, "seconds": 0.0,
           "archive_path": None, "load_ref_days": 0, "posted": False}
    src = original_plan_for_day(archive_dir, day, tz)
    if src is None:
        out["status"] = "no_plan"
        return out
    t0 = _parse_ts(src["t0"])
    n = int(src["n"])
    pay = src.get("payload") or {}
    pv_w, old_buy = pay.get("pv_power_forecast"), pay.get("load_cost_forecast")
    if not pv_w or not old_buy or len(pv_w) != n or len(old_buy) != n:
        out["status"] = "bad_source"
        return out
    ot = src["tariff"]
    fo = 1.0 + float(ot["btw_pct"]) / 100.0
    da = [b / fo - ot["energy_tax"] - ot["supplier_fee"] for b in old_buy]
    buy, sell = tariff(da, new_tariff["energy_tax"], new_tariff["supplier_fee"],
                       new_tariff["btw_pct"], new_tariff["feedin_fee"])
    load_w, load_info = load_series(t0, n, tz, load_days)
    out["load_ref_days"] = load_info["ref_days"]
    if load_w is None:
        out["status"] = "insufficient_load"          # peer trap: never fall back to naive
        return out
    kn = knobs(live_knobs)
    soc = min(max(float(src["soc_init"]), SOC_MIN), SOC_MAX)
    payload = build_payload(t0, n, soc, float(kn["soc_final"]), pv_w, buy, sell)
    payload["load_power_forecast"] = load_w
    payload["battery_stress_cost"], payload["inverter_stress_cost"] = stress_costs(buy, sell)
    payload.update(rebalance_schedule(days_since_full(archive_dir, t0, tz), kn["surplus_base"],
                                      kn["deficit_threshold"], kn["deficit_cost"]))
    apply_knobs(payload, kn, t0, n, day)
    out["posted"] = True
    res = solve(base_url, payload, timeout)
    out["seconds"] = res.get("seconds", 0.0)
    if not res["ok"]:
        out["status"], out["message"] = "solve_failed", res["message"]
        return out
    rows = res["rows"]
    if len(rows) != n:                 # exactly this solve's horizon, or a foreign solve landed in between
        out["status"], out["message"] = "solve_failed", f"{len(rows)} rows for a {n}-step horizon: not this solve"
        return out
    if str(rows[0].get("optim_status", "")) != "Optimal":
        out["status"] = "not_optimal"
        return out
    restamp(rows, t0, n)                             # re-stamp the now-anchored grid onto the source horizon
    cost = plan_cost(rows)
    doc = {"plan_ts": src["plan_ts"], "t0": src["t0"], "n": n, "tz": tz, "soc_init": soc,
           "soc_final": float(kn["soc_final"]), "tariff": dict(new_tariff), "knobs": kn,
           "n_predicted_steps": src.get("n_predicted_steps", 0),
           "pv_gap_steps": src.get("pv_gap_steps", 0),
           "prices_predicted": src.get("prices_predicted", False),
           "optim_status": "Optimal", "cost_eur": cost, "seconds": out["seconds"],
           "payload": payload, "rows": rows, "last_run": res.get("last_run"),
           "soc_source": "replay", "load_source": "profile", "load_ref_days": load_info["ref_days"],
           "replay": True, "source_plan_ts": src["plan_ts"]}
    out["archive_path"] = write_plan_archive(archive_dir, datetime.now(ZoneInfo(tz)), doc)
    out.update(status="ok", eur=cost, n=n)
    return out
