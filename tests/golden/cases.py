"""What the golden pin freezes. Each case is a function of the core module that
returns a JSON-able value; build.py writes canonical(value) to expected/<name>.json
and test_golden.py asserts byte identity. A case that writes runs on a throwaway
copy of the archive; solves go through fixtures.StubSolver, never HTTP.

Adding a case: register it here, run build.py, commit the new expected file.
Changing behaviour on purpose: run build.py, read the diff of expected/, explain
every changed file in the commit that causes it.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone

from . import fixtures as F

CASES: dict = {}


def case(fn):
    CASES[fn.__name__] = fn
    return fn


def _sel(found):
    """A selector's (doc, slice) reduced to what identifies the pick."""
    if not found:
        return None
    doc, sl = found
    return {"plan_ts": doc["plan_ts"], "t0": doc["t0"], "n_doc": doc["n"], "replay": bool(doc.get("replay")),
            "slice_n": sl["n"], "slice_start": sl["slice_start"], "soc_start_pct": sl["soc_start_pct"]}


def _record(core, day: date, archive=F.PLANS):
    found = core.plan_for_day(archive, day, F.TZ)
    return core.compact_slice(found[1], found[0]) if found else None


DAYS = [date(2026, 9, d) for d in range(2, 10)]
NOW_0907 = F.local(2026, 9, 7, 14, 40, 12)


# ---- archive --------------------------------------------------------------------------

@case
def archive_heads(core):
    heads = [(os.path.basename(p), h) for p, h in core.plan_heads(F.PLANS)]
    return {"heads": heads, "list_plans": [os.path.basename(p) for p in core.list_plans(F.PLANS)]}


@case
def archive_selectors(core):
    out = {}
    for d in DAYS:
        out[d.isoformat()] = {
            "plan_for_day": _sel(core.plan_for_day(F.PLANS, d, F.TZ)),
            "newest_plan_for_day": _sel(core.newest_plan_for_day(F.PLANS, d, F.TZ)),
            "original_plan_for_day": (lambda doc: {"plan_ts": doc["plan_ts"], "t0": doc["t0"]} if doc else None)(
                core.original_plan_for_day(F.PLANS, d, F.TZ)),
        }
    return out


@case
def organic_windows(core):
    m6, m7 = F.local(2026, 9, 6), F.local(2026, 9, 7)
    return {"since_0906_until_0907": [d["plan_ts"] for d in core.iter_organic_plans(F.PLANS, since=m6, until=m7)],
            "since_0907": [d["plan_ts"] for d in core.organic_plans(F.PLANS, since=m7)],
            "unbounded_count": len(core.organic_plans(F.PLANS))}


@case
def compact_slices(core):
    return {d.isoformat(): _record(core, d) for d in DAYS}


@case
def virtual_soc_at(core):
    pts = [F.local(2026, 9, d) for d in range(3, 9)] + [F.local(2026, 9, 7, 14, 45), F.local(2026, 9, 5, 12, 0),
                                                        F.local(2026, 9, 5, 12, 7)]
    return {t.isoformat(): list(core.virtual_soc_at(F.PLANS, t)) for t in pts}


@case
def days_since_full(core):
    out = {}
    for t in (NOW_0907, F.local(2026, 9, 6, 0, 5), F.local(2026, 9, 5, 12, 5)):
        out[t.isoformat()] = {"default": core.days_since_full(F.PLANS, t, F.TZ),
                              "level_0.9": core.days_since_full(F.PLANS, t, F.TZ, level=0.9),
                              "lookback_1": core.days_since_full(F.PLANS, t, F.TZ, lookback=1)}
    return out


@case
def aux_cut_state(core):
    return {t.isoformat(): core.aux_cut_state(F.PLANS, t, F.TZ)
            for t in (NOW_0907, F.local(2026, 9, 7, 0, 5), F.local(2026, 9, 6, 18, 0))}


# ---- reconstruction ---------------------------------------------------------------------

def _vd(core, day, now, act=None, archive=F.PLANS):
    act = act or {}
    return core.virtual_day(archive, day, F.TZ, now, act.get("pv_w"), act.get("load_w"), act.get("curtailed"),
                            act.get("pv_peak_w"), act.get("micro_w"))


@case
def virtual_day_0905_complete(core):
    return _vd(core, date(2026, 9, 5), F.local(2026, 9, 6), F.actuals_for_day(core, date(2026, 9, 5)))


@case
def virtual_day_0905_no_actuals(core):
    return _vd(core, date(2026, 9, 5), F.local(2026, 9, 6))


@case
def virtual_day_0905_no_micro(core):
    act = dict(F.actuals_for_day(core, date(2026, 9, 5)))
    act.pop("micro_w", None)
    act.pop("curtailed", None)
    return _vd(core, date(2026, 9, 5), F.local(2026, 9, 6), act)


@case
def virtual_day_0906_midday(core):
    now = F.local(2026, 9, 6, 12, 7)
    return _vd(core, date(2026, 9, 6), now, F.actuals_for_day(core, date(2026, 9, 6), core._slot(now)))


@case
def virtual_day_0906_complete(core):
    return _vd(core, date(2026, 9, 6), F.local(2026, 9, 7), F.actuals_for_day(core, date(2026, 9, 6)))


@case
def virtual_day_0907_1440(core):
    return _vd(core, date(2026, 9, 7), NOW_0907, F.actuals_for_day(core, date(2026, 9, 7), core._slot(NOW_0907)))


@case
def virtual_day_0903_pre_profile(core):
    return _vd(core, date(2026, 9, 3), F.local(2026, 9, 4), F.actuals_for_day(core, date(2026, 9, 3)))


def _rolled(core, now, archive=F.PLANS):
    today = now.date()
    act = F.actuals_for_day(core, today, core._slot(now))
    prev = F.actuals_for_day(core, today - timedelta(days=1))
    return core.rolled_slices(archive, now, F.TZ, act.get("pv_w"), act.get("load_w"), prev.get("pv_w"),
                              prev.get("load_w"), act.get("curtailed"), prev.get("curtailed"),
                              act.get("pv_peak_w"), prev.get("pv_peak_w"), act.get("micro_w"), prev.get("micro_w"))


@case
def rolled_slices_0907(core):
    return _rolled(core, NOW_0907)


@case
def rolled_slices_0906_noon(core):
    return _rolled(core, F.local(2026, 9, 6, 12, 7))


@case
def rolled_slices_0903_chain_broken(core):
    return _rolled(core, F.local(2026, 9, 3, 9, 0))


@case
def rehydrate_0907(core):
    now = NOW_0907
    act = F.actuals_for_day(core, now.date(), core._slot(now))
    prev = F.actuals_for_day(core, now.date() - timedelta(days=1))
    out = core.rehydrate(F.PLANS, F.TZ, now.isoformat(), 26.0, act.get("pv_w"), act.get("load_w"),
                         prev.get("pv_w"), prev.get("load_w"), act.get("curtailed"), prev.get("curtailed"),
                         act.get("pv_peak_w"), prev.get("pv_peak_w"), act.get("micro_w"), prev.get("micro_w"))
    # the today slice after a restart is the same slice the plan path serves
    out["same_as_rolled"] = out["today"] == _rolled(core, now)["today"] and out["yesterday"] == _rolled(core, now)["yesterday"]
    out["stale"] = core.rehydrate(F.PLANS, F.TZ, F.local(2026, 9, 9).isoformat(), 26.0)["plan_fresh"]
    return out


# ---- pv repair and settlement -----------------------------------------------------------

@case
def pv_potential_0905(core):
    act = F.actuals_for_day(core, date(2026, 9, 5))
    plan = _record(core, date(2026, 9, 5))
    fc = plan.get("pv_p50_w") or plan["p_pv_w"]
    out = {}
    for name, kw in (("with_micro", {"micro_meas_w": act.get("micro_w")}),
                     ("plain", {}),
                     # fit_scale / peak_w are retired no-ops (2026-09-07); this pins the signature, not a guard
                     ("no_fit", {"fit_scale": False, "micro_meas_w": act.get("micro_w")}),
                     ("micro_fc", {"micro_meas_w": act.get("micro_w"), "micro_fc_w": plan["pv_micro_w"]})):
        pot, info = core.pv_potential(act["pv_w"], fc, act["curtailed"], act.get("pv_peak_w"), **kw)
        out[name] = {"pot": pot, "info": info}
    out["length_mismatch"] = list(core.pv_potential(act["pv_w"][:10], fc, act["curtailed"]))
    out["effective_load"] = core.effective_load(act["grid_w"], act["batt_dc_w"], act["pv_w"])
    return out


@case
def settle_slice_0905(core):
    plan = _record(core, date(2026, 9, 5))
    act = F.actuals_for_day(core, date(2026, 9, 5))
    pot, _ = core.pv_potential(act["pv_w"], plan.get("pv_p50_w") or plan["p_pv_w"], act["curtailed"],
                               act.get("pv_peak_w"), micro_meas_w=act.get("micro_w"))
    return {"potential": core.settle_slice(plan, pot, act["load_w"], act.get("micro_w")),
            "measured_no_micro": core.settle_slice(plan, act["pv_w"], act["load_w"]),
            "short": core.settle_slice(plan, act["pv_w"][:5], act["load_w"])}


@case
def replay_day_0905(core):
    plan = _record(core, date(2026, 9, 5))
    act = F.actuals_for_day(core, date(2026, 9, 5))
    lam = core.lambda_for(plan["sell"], 0.9)
    pot, _ = core.pv_potential(act["pv_w"], plan.get("pv_p50_w") or plan["p_pv_w"], act["curtailed"],
                               micro_meas_w=act.get("micro_w"))
    return {"lambda": lam,
            "raw_plan": core.replay_day(plan, act["grid_w"], act["batt_dc_w"], act["pv_w"], 48.2, lam),
            "raw_plan_pot": core.replay_day(plan, act["grid_w"], act["batt_dc_w"], act["pv_w"], 48.2, lam, pv_pot_w=pot),
            "no_actuals": core.replay_day(plan, None, act["batt_dc_w"], act["pv_w"], 48.2, lam),
            "cash_in_frame": core.cash_in_frame(act["grid_w"], plan["buy"], plan["sell"])}


def _plan_inp(core, now, archive, dry_run=True, days_ahead=2, ese=True, load=True, **extra):
    """run_plan's input dict for `now`, the sun taken from the newest archived
    document's own P50/P10 series, prices from the fixture Nord Pool rows."""
    today = now.date()
    # the newest document made before `now`: its own P50/P10 series start at its
    # t0, which is the ceil of its plan_ts, so a solve at `now` sees a full sun
    doc = next(core.load_plan(p) for p, h in core.plan_heads(archive)
               if not h["replay"] and datetime.fromisoformat(h["plan_ts"]) <= now)
    inp = {"now": now.isoformat(), "tz": F.TZ, "base_url": "http://stub", "archive_dir": archive,
           "dry_run": dry_run, "np_today": F.nordpool(today), "np_tomorrow": F.nordpool(today + timedelta(days=1)),
           "epex": [], "solcast_today": F.solcast_from_doc(doc, today),
           "solcast_tomorrow": F.solcast_from_doc(doc, today + timedelta(days=1)),
           "solcast_day3": F.solcast_from_doc(doc, today + timedelta(days=2)) if days_ahead == 2 else [],
           "soc_pct": 68.3, "soc_source": "settled",
           "tariff": {"energy_tax": 0.0, "supplier_fee": 0.019, "btw_pct": 21.0, "feedin_fee": 0.019},
           "knobs": {k: None for k in core.LIVE_KNOBS}}
    if ese:
        share = float(doc.get("growatt_share") or 0.288)
        # the ESE site as a fixed 0,6 of the array, so micro = 0,6 x share x total
        inp["solcast_ese_today"] = (F.solcast_from_doc(doc, today, scale=0.6)
                                    + F.solcast_from_doc(doc, today + timedelta(days=1), scale=0.6)
                                    + F.solcast_from_doc(doc, today + timedelta(days=2), scale=0.6))
    if load:
        inp["load_days"] = F.load_days(core, today)
        inp["load_now_w"] = 512.0
    act = F.actuals_for_day(core, today, core._slot(now))
    prev = F.actuals_for_day(core, today - timedelta(days=1))
    inp.update(actual_pv_w=act.get("pv_w"), actual_load_w=act.get("load_w"), curtailed=act.get("curtailed"),
               pv_peak_w=act.get("pv_peak_w"), micro_w=act.get("micro_w"),
               prev_pv_w=prev.get("pv_w"), prev_load_w=prev.get("load_w"), prev_curtailed=prev.get("curtailed"),
               prev_pv_peak_w=prev.get("pv_peak_w"), prev_micro_w=prev.get("micro_w"))
    inp.update(extra)
    return inp


def _run(core, inp, now, days_ahead=2, **stub_kw):
    t0, _n = core.horizon(now, F.TZ, days_ahead=days_ahead)
    stub = F.StubSolver(t0, **stub_kw)
    with F.patched_solve(core.run_plan, stub):
        out = core.run_plan(inp)
    res = {"out": F.strip_paths(out, (inp["archive_dir"],)), "posts": [
        {k: v for k, v in p.items() if not isinstance(v, list)} | {"lists": {k: len(v) for k, v in p.items() if isinstance(v, list)}}
        for p in stub.calls]}
    if out.get("archive_path"):
        doc = core.load_plan(out["archive_path"])
        res["doc"] = doc
    return res


@case
def run_plan_0907_dry(core):
    now = NOW_0907
    return _run(core, _plan_inp(core, now, F.PLANS), now)


@case
def run_plan_0907_full(core):
    now = NOW_0907
    with F.tmp_archive() as arch:
        return _run(core, _plan_inp(core, now, arch, dry_run=False), now)


@case
def run_plan_0907_cut(core):
    """A punitive feed-in fee makes every sell price negative, the stub then
    curtails every surplus watt, and the Growatt cut fires on the first solve
    and holds on the next (state carried through the archive)."""
    now = F.local(2026, 9, 7, 10, 7)
    with F.tmp_archive() as arch:
        inp = _plan_inp(core, now, arch, dry_run=False, soc_pct=97.0,
                        tariff={"energy_tax": 0.0, "supplier_fee": 0.019, "btw_pct": 21.0, "feedin_fee": 0.5})
        first = _run(core, inp, now)
        now2 = F.local(2026, 9, 7, 10, 22)        # before the archive's real 10:35 plan, so the carry reads OUR cut plan
        inp2 = _plan_inp(core, now2, arch, dry_run=False, soc_pct=98.0,
                         tariff={"energy_tax": 0.0, "supplier_fee": 0.019, "btw_pct": 21.0, "feedin_fee": 0.5})
        second = _run(core, inp2, now2)
        # the cut solve fails: the uncut plan stands and the state does not advance
        now3 = F.local(2026, 9, 7, 10, 27)
        inp3 = _plan_inp(core, now3, arch, dry_run=True, soc_pct=98.0,
                         tariff={"energy_tax": 0.0, "supplier_fee": 0.019, "btw_pct": 21.0, "feedin_fee": 0.5})
        t0, _ = core.horizon(now3, F.TZ, days_ahead=2)
        good, bad = F.StubSolver(t0), F.StubSolver(t0, status="Infeasible")
        calls = {"n": 0}

        def flaky(base_url, payload, timeout=180):
            calls["n"] += 1
            return (good if calls["n"] == 1 else bad)(base_url, payload, timeout)
        with F.patched_solve(core.run_plan, flaky):
            third = core.run_plan(inp3)
    return {"first": first, "second": second, "third_cut_solve_fails": F.strip_paths(third, (arch,))}


@case
def run_plan_0907_variants(core):
    now = NOW_0907
    out = {}
    out["no_ese"] = _run(core, _plan_inp(core, now, F.PLANS, ese=False), now)
    out["no_load"] = _run(core, _plan_inp(core, now, F.PLANS, load=False), now)
    out["one_day"] = _run(core, _plan_inp(core, now, F.PLANS, days_ahead=1), now, days_ahead=1)
    # the pre-knob single keys are ignored since 2026-09-07: same result as the dry run
    out["legacy_keys_ignored"] = _run(core, _plan_inp(core, now, F.PLANS, pv_p10_mix=0.5, growatt_share=0.4), now)
    kn = {k: None for k in core.LIVE_KNOBS}
    kn.update(stress_scale=2.0, soc_final=0.6, soc_target=0.85, soc_target_at="17:00", batt_power_max_w=8000.0,
              surplus_base=0.01, deficit_threshold=0.25, deficit_cost=0.02, soc_min=0.15, weight_battery_discharge=0.02,
              aux_cut_on_ratio=1.1, pv_p10_mix=0.3, growatt_share=0.3)
    out["knobs"] = _run(core, _plan_inp(core, now, F.PLANS, knobs=kn), now)
    out["not_optimal"] = _run(core, _plan_inp(core, now, F.PLANS), now, status="Infeasible")
    out["pv_gaps"] = _run(core, _plan_inp(core, now, F.PLANS, solcast_tomorrow=[], solcast_day3=[]), now)
    out["no_prices"] = _run(core, _plan_inp(core, now, F.PLANS, np_today=[], np_tomorrow=[]), now)
    ep = [{"datetime": t.isoformat(), "value": 0.05 + 0.001 * k}
          for k, t in enumerate(core.step_times(F.local(2026, 9, 7), 96 * 3))]
    out["epex_fill"] = _run(core, _plan_inp(core, now, F.PLANS, np_tomorrow=[], epex=ep), now)
    misaligned = F.StubSolver(F.local(2026, 9, 7, 15, 0))
    with F.patched_solve(core.run_plan, misaligned):
        out["misaligned"] = core.run_plan(_plan_inp(core, now, F.PLANS))
    # the solver failing outright (HTTP or last-run refused), and a non-Optimal
    # plan on a FULL run: archived, not rolled or published
    with F.patched_solve(core.run_plan, lambda base_url, payload, timeout=180: {"ok": False, "message": "naive-mpc-optim HTTP 0: stub down", "seconds": 0.0}):
        out["solve_failed"] = core.run_plan(_plan_inp(core, now, F.PLANS))
    with F.tmp_archive() as arch:
        out["not_optimal_full"] = _run(core, _plan_inp(core, now, arch, dry_run=False), now, status="Infeasible")
        out["not_optimal_full"]["heads_after"] = [(os.path.basename(p), h) for p, h in core.plan_heads(arch)][:2]
    return out


# ---- hindsight, replay ---------------------------------------------------------------------

@case
def hindsight_day_0905(core):
    day = date(2026, 9, 5)
    found = core.plan_for_day(F.PLANS, day, F.TZ)
    doc = found[0]
    t0, n = datetime.fromisoformat(doc["t0"]).astimezone(F.Z), int(doc["n"])
    win = F.window_actuals(core, t0, n)
    act = F.actuals_for_day(core, day)
    np_rows = []
    d = t0.date()
    while d <= (t0 + timedelta(minutes=15 * n)).date():
        np_rows += F.nordpool(d)
        d += timedelta(days=1)
    stub = F.StubSolver(F.local(2026, 9, 7, 14, 45))
    with F.patched_solve(core.hindsight_day, stub):
        ok = core.hindsight_day(F.PLANS, "http://stub", "2026-09-05", F.TZ, np_rows, win, act, 48.2, 0.9)
        no_prices = core.hindsight_day(F.PLANS, "http://stub", "2026-09-05", F.TZ, F.nordpool(day), win, act, 48.2, 0.9)
        no_win = core.hindsight_day(F.PLANS, "http://stub", "2026-09-05", F.TZ, np_rows, {}, act, 48.2, 0.9)
        no_plan = core.hindsight_day(F.PLANS, "http://stub", "2026-09-10", F.TZ, np_rows, win, act, 48.2, 0.9)
    with F.patched_solve(core.hindsight_day, F.StubSolver(F.local(2026, 9, 7, 14, 45), status="Infeasible")):
        bad = core.hindsight_day(F.PLANS, "http://stub", "2026-09-05", F.TZ, np_rows, win, act, 48.2, 0.9)
    posted = [{k: v for k, v in p.items() if not isinstance(v, list)} | {"lists": {k: len(v) for k, v in p.items() if isinstance(v, list)}}
              for p in stub.calls]
    return {"ok": ok, "no_prices": no_prices, "no_window": no_win, "no_plan": no_plan, "not_optimal": bad,
            "posted": posted, "window_lengths": {k: (len(v) if v else None) for k, v in win.items()}}


@case
def hindsight_day_0906_window_open(core):
    day = date(2026, 9, 6)
    doc = core.plan_for_day(F.PLANS, day, F.TZ)[0]
    t0, n = datetime.fromisoformat(doc["t0"]).astimezone(F.Z), int(doc["n"])
    win = F.window_actuals(core, t0, n)
    return {"window": None if win is None else {k: len(v) for k, v in win.items()}, "t0": t0, "n": n}


def _pay_summary(p):
    return {k: v for k, v in p.items() if not isinstance(v, list)} | {"lists": {k: (len(v), round(sum(float(x) for x in v), 3)) for k, v in p.items() if isinstance(v, list)}}


@case
def ab_solves(core):
    return {str(c): [(t.isoformat(), d["plan_ts"], s) for t, d, s in core._ab_solves(F.PLANS, date(2026, 9, 6), F.TZ, c)]
            for c in (60, 30, 15)}


@case
def ab_payload(core):
    doc = core.load_plan(os.path.join(F.PLANS, "20260907T130211.json.gz"))
    old = core.load_plan(os.path.join(F.PLANS, "20260905T130500.json.gz"))
    act = F.actuals_for_day(core, date(2026, 9, 7), 60)
    out = {}
    for name, d, shift, ov in (("plain", doc, 0, {}), ("shift2", doc, 2, {}),
                               ("share", doc, 0, {"growatt_share": 0.4}),
                               ("mix", doc, 0, {"pv_p10_mix": 0.6}),
                               ("knobs", doc, 1, {"stress_scale": 3.0, "surplus_base": 0.02, "deficit_threshold": 0.3,
                                                  "deficit_cost": 0.05, "soc_final": 0.7, "weight_battery_discharge": 0.05,
                                                  "soc_min": 0.2, "soc_max": 0.9, "batt_power_max_w": 6000.0}),
                               ("target", doc, 0, {"soc_target": 0.9, "soc_target_at": "16:00"}),
                               ("target_off", doc, 0, {"soc_target": 0.0}),
                               ("raw", doc, 0, {"battery_stress_cost": 0.5, "soc_target": 0.8}),
                               ("load_actual", doc, 0, {"load": "actual"}),
                               ("pre_split_mix", old, 0, {"pv_p10_mix": 0.6, "growatt_share": 0.5})):
        pay, micro, ready, notes, times, kn = core._ab_payload(d, shift, ov, date(2026, 9, 7), F.TZ, act.get("load_w"))
        out[name] = {"payload": _pay_summary(pay), "micro": micro, "cut_ready": ready, "notes": notes,
                     "times": [times[0], times[-1], len(times)], "knobs": kn}
    return out


def _walk(core, day, overrides, cadence=30, settle="closed", act=None):
    act = act if act is not None else F.actuals_for_day(core, day)
    stub = F.StubSolver(F.local(2026, 9, 7, 14, 45))
    with F.patched_solve(core.ab_walk, stub):
        out = core.ab_walk(F.PLANS, "http://stub", day.isoformat(), F.TZ, overrides, act, cadence, settle, 48.2)
    out["stub_calls"] = len(stub.calls)
    return out


@case
def ab_walk_0906_base(core):
    return _walk(core, date(2026, 9, 6), {})


@case
def ab_walk_0906_variant(core):
    return _walk(core, date(2026, 9, 6), {"pv_p10_mix": 0.5, "stress_scale": 2.0, "soc_target": 0.8,
                                          "growatt_share": 0.35, "surplus_base": 0.01, "load": "actual",
                                          "aux_cut_on_ratio": 0.5, "aux_cut_on_min_kwh": 0.1})


@case
def ab_walk_0906_open_15(core):
    return _walk(core, date(2026, 9, 6), {"soc_final": 0.7}, cadence=15, settle="open")


@case
def ab_walk_0905_hourly(core):
    return _walk(core, date(2026, 9, 5), {"batt_power_max_w": 6000.0}, cadence=60)


@case
def ab_walk_failures(core):
    day = date(2026, 9, 6)
    act = F.actuals_for_day(core, day)
    out = {"unknown": _walk(core, day, {"nonsense": 1}),
           "no_actuals": _walk(core, day, {}, act={"pv_w": act["pv_w"]}),
           "no_chain": _walk(core, date(2026, 9, 2), {}, act=F.actuals_for_day(core, date(2026, 9, 2))),
           "partial_day_no_actuals": _walk(core, date(2026, 9, 7), {}, act=F.actuals_for_day(core, date(2026, 9, 7)))}
    bad = F.StubSolver(F.local(2026, 9, 7, 14, 45), status="Infeasible")
    with F.patched_solve(core.ab_walk, bad):
        out["not_optimal"] = core.ab_walk(F.PLANS, "http://stub", day.isoformat(), F.TZ, {}, act, 30, "closed", 48.2)

    def foreign(base_url, payload, timeout=180):
        r = F.StubSolver(F.local(2026, 9, 7, 14, 45))(base_url, payload, timeout)
        r["rows"] = r["rows"][:-1]
        return r
    with F.patched_solve(core.ab_walk, foreign):
        out["foreign_solve"] = core.ab_walk(F.PLANS, "http://stub", day.isoformat(), F.TZ, {}, act, 30, "closed", 48.2)
    return out


@case
def ab_misc(core):
    d = tempfile.mkdtemp(prefix="emhass-golden-")
    try:
        p = core.ab_store(d, "golden", {"a": 1, "b": [1.5, None]})
        loaded = core.ab_load(d, "golden")
        missing = core.ab_load(d, "nope")
        name = os.path.basename(p)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"validate": {k: core.ab_validate(v) for k, v in {"ok": {"stress_scale": 1, "soc_target": 0.5, "load": "actual"},
                                                            "bad": {"x": 1, "stress_scale": 1}, "none": None}.items()},
            "apply": [list(core.ab_apply_plan(o)) for o in ({"stress_scale": 2.0, "soc_target_at": "16:00", "battery_stress_cost": 1,
                                                             "load": "actual", "zzz": 0}, {}, None)],
            "runtime_keys": sorted(core.AB_RUNTIME_KEYS), "own_keys": sorted(core.AB_OWN_KEYS), "list_keys": list(core.AB_LIST_KEYS),
            "store": {"name": name, "loaded": loaded, "missing": missing}}


@case
def ab_summary_table(core):
    vd = _vd(core, date(2026, 9, 5), F.local(2026, 9, 6), F.actuals_for_day(core, date(2026, 9, 5)))
    lanes = {k: vd[k] for k in ("soc_pct", "p_grid_w", "pv_curtail_w", "buy", "sell")} | {"micro_cut_w": vd["micro_cut_w"]}
    full = core.ab_summary(lanes, 96, 48.2)
    holes = {k: [None if i % 7 == 0 else v for i, v in enumerate(vs)] for k, vs in lanes.items()}
    return {"full": full, "holes": core.ab_summary(holes, 96), "no_micro": core.ab_summary({k: v for k, v in lanes.items() if k != "micro_cut_w"}, 96),
            "short_day": core.ab_summary({k: v[:60] for k, v in lanes.items()}, 60)}


# ---- pure tables ---------------------------------------------------------------------------------

@case
def deye_table(core):
    out = {"amps": [core.deye_amps(w, v) for w in (0, 25, 26, 51, 800, 12500, 20000, -5) for v in (51.2, 48.0, 0)],
           "constants": {"tier": core.DEYE_TIER, "baseline": core.DEYE_BASELINE, "max_a": core.DEYE_CURRENT_MAX_A,
                         "step_a": core.DEYE_CURRENT_STEP_A, "grid_db": core.DEYE_GRID_DEADBAND_W,
                         "batt_db": core.DEYE_BATT_DEADBAND_W, "pack_v": core.DEYE_PACK_V},
           "commands": [], "responses": [], "settle": []}
    grid = [(g, b, c, mc, ts) for g in (-3000.0, -50.0, 0.0, 80.0, 2500.0) for b in (-6000.0, -90.0, 0.0, 120.0, 4000.0)
            for c in (0.0, 1500.0) for mc in (False, True) for ts in (None, 0.4)]
    for g, b, c, mc, ts in grid:
        cmd = core.deye_command(g, b, micro_cut=mc, target_soc=ts, pv_curtail_w=c)
        out["commands"].append({"in": [g, b, c, mc, ts], "cmd": {k: v for k, v in cmd.items() if k != "tier"}})
        for pv, mic, load, soc in ((6000.0, 800.0, 400.0, 0.5), (200.0, 0.0, 1500.0, 0.12), (9000.0, 2500.0, 300.0, 0.995)):
            out["responses"].append({"in": [g, b, c, mc, ts, pv, mic, load, soc],
                                     "resp": core.deye_response(cmd, pv, mic, load, soc)})
    for plan in ((0.0, -5000.0, 7000.0, 400.0, 0.0), (0.0, -5000.0, 7000.0, 400.0, 2000.0), (2000.0, -3000.0, 1000.0, 400.0, 0.0),
                 (-4000.0, 3000.0, 1500.0, 400.0, 0.0), (300.0, 0.0, 100.0, 400.0, 0.0), (-2000.0, 0.0, 3000.0, 400.0, 0.0)):
        for act in ((5000.0, 700.0, 0.0, 0.0), (8000.0, 300.0, 600.0, 900.0), (0.0, 1200.0, 600.0, 0.0)):
            for soc in (0.5, 0.99):
                for mc in (False, True):
                    out["settle"].append({"plan": list(plan), "act": list(act), "soc": soc, "cut": mc,
                                          "res": list(core.settle_step(*plan, act[0], act[1], act[2], act[3], soc, 48.2, micro_cut=mc))})
    return out


@case
def writer_table(core):
    """The writer's pure functions over the command grid: the trade floor, the
    diff against four standing states, the write order, and a tick in each
    mode."""
    now = F.local(2026, 9, 27, 10, 30, 20)
    out = {"constants": {"fields": list(core.WRITER_FIELDS), "restore": list(core.RESTORE_ORDER),
                         "never": list(core.WRITER_NEVER), "entity": core.WRITER_ENTITY,
                         "stale_min": core.WRITER_STALE_MIN},
           "held": [], "diffs": [], "ticks": []}
    grid = [(g, b, c, mc, s) for g in (-3000.0, 0.0, 2500.0) for b in (-6000.0, 0.0, 4000.0)
            for c in (0.0, 1500.0) for mc in (False, True) for s in (-0.01, 0.08)]
    cmds = [core.deye_command(g, b, 51.2, micro_cut=mc, pv_curtail_w=c, sell=s, margin=True) for g, b, c, mc, s in grid]
    # the trade floor (2026-10-03, in place of the hold rule): each command
    # against a standing command picked across the grid
    for i, cur in enumerate(cmds):
        prev = cmds[(i * 7 + 3) % len(cmds)]
        st = {f: prev[f] for f in core.WRITER_FIELDS}
        rec, held = core.trade_floor(cur, st)
        out["held"].append({"cur": cur["intent"], "standing": prev["intent"], "held": held,
                            "record": core.off_baseline(rec)})
    base = {f: core.DEYE_BASELINE[f] for f in core.WRITER_FIELDS}
    standings = {"baseline": base,
                 "export": {f: cmds[grid.index((-3000.0, 4000.0, 0.0, False, 0.08))][f] for f in core.WRITER_FIELDS},
                 "grid_charge": {f: cmds[grid.index((2500.0, -6000.0, 0.0, False, 0.08))][f] for f in core.WRITER_FIELDS},
                 "clamped": dict(base, battery_max_charging_current=118.0, export_surplus=False),
                 "strings": dict(base, export_surplus="on", battery_grid_charging="off", battery_max_charging_current="240.0")}
    for name, st in standings.items():
        for cmd in cmds:
            d = core.writer_diff(st, cmd)
            out["diffs"].append({"standing": name, "intent": cmd["intent"], "diff": d, "writes": core.order_writes(d)})

    def row(g, b, c=0.0, s=0.08):
        return {"timestamp": "2026-09-27T08:30:00.000Z", "P_grid": g, "P_batt": b, "P_PV_curtailment": c, "unit_prod_price": s}
    steps = {"export_held": {"row": row(-9000.0, 8000.0), "next_row": row(0.0, -5000.0), "micro_cut": False, "next_micro_cut": False, "stale": False},
             "export": {"row": row(-9000.0, 8000.0), "next_row": row(-6000.0, 5000.0), "micro_cut": False, "next_micro_cut": False, "stale": False},
             "grid_charge": {"row": row(2000.0, -6000.0), "next_row": row(2000.0, -6000.0), "micro_cut": False, "next_micro_cut": False, "stale": False},
             "cut": {"row": row(0.0, -5000.0, 800.0, -0.01), "next_row": row(0.0, -5000.0, 800.0, -0.01), "micro_cut": True, "next_micro_cut": True, "stale": False},
             "stale": {"row": row(-9000.0, 8000.0), "next_row": row(-9000.0, 8000.0), "micro_cut": False, "next_micro_cut": False, "stale": True},
             "none": None}
    for name, step in steps.items():
        if step:
            step = dict(step, plan_ts="2026-09-27T10:13:00+02:00", t0="2026-09-27T10:15:00+02:00", n=8, index=0)
        for mode in ("off", "dry", "live"):
            for sname, st in standings.items():
                for pv in (51.2, None):
                    doc = core.writer_tick(mode, st, step, pv, now, {"work_mode": 1})
                    out["ticks"].append({"step": name, "mode": mode, "standing": sname, "pack_v": pv, "doc": doc})
    export_step = dict(steps["export"], plan_ts="2026-09-27T10:13:00+02:00", t0="2026-09-27T10:15:00+02:00", n=8, index=0)
    out["fold"] = core.fold_writes(core.writer_tick("live", base, export_step, 51.2, now),
                                   [("battery_max_discharging_current", 156.0, 4.2), ("work_mode", "Export First", None)])
    return out


@case
def writer_day_0905(core):
    """96 live-mode ticks over the 09-05 archive with the standing state
    following the writes: the intent per step, the holds, the writes and the
    counters. This is what the dry day on the VM is compared against."""
    standing = {f: core.DEYE_BASELINE[f] for f in core.WRITER_FIELDS}
    counts, ticks = {}, []
    for q in range(96):
        now = F.local(2026, 9, 5, 0, 0, 20) + timedelta(minutes=15 * q)
        # the 09-05 archive is hourly, so the staleness threshold is 75 min here
        doc = core.writer_tick("live", standing, core.step_in_force(F.PLANS, now, stale_min=75.0), 51.2, now, counts)
        doc = core.fold_writes(doc, [(f, v, 1.0) for f, v in doc["writes"]])
        for f, v in doc["writes"]:
            standing[f] = v
        counts = doc["write_counts"]
        ticks.append({k: doc[k] for k in ("tick_ts", "plan_ts", "intent", "next_intent", "held", "record", "writes", "status")})
    return {"ticks": ticks, "write_counts": counts, "standing_at_end": standing}


@case
def inputs_table(core):
    doc = core.load_plan(os.path.join(F.PLANS, "20260907T143500.json.gz"))
    t0, n = core.horizon(NOW_0907, F.TZ, days_ahead=2)
    ep = [{"datetime": t.isoformat(), "value": 0.05 + 0.001 * k} for k, t in enumerate(core.step_times(F.local(2026, 9, 7), 96 * 3))]
    da, npred = core.price_series(t0, n, F.nordpool(date(2026, 9, 7)), F.nordpool(date(2026, 9, 8)), ep)
    da2, npred2 = core.price_series(t0, n, F.nordpool(date(2026, 9, 7)), None, None)
    sc = [F.solcast_from_doc(doc, date(2026, 9, 7) + timedelta(days=k)) for k in range(3)]
    p50, gaps = core.pv_series(t0, n, *sc)
    p10, _ = core.pv_series(t0, n, *sc, field="pv_estimate10")
    p90, _ = core.pv_series(t0, n, *sc, field="pv_estimate90")
    ese = [F.solcast_from_doc(doc, date(2026, 9, 7) + timedelta(days=k), scale=0.6) for k in range(3)]
    ese50, ese_gaps = core.pv_series(t0, n, *ese)
    mixed = core.pv_mix(p50, p10, 0.2)
    main, micro = core.pv_split(mixed, core.pv_mix(ese50, ese50, 0.0), 0.288)
    ld = F.load_days(core, date(2026, 9, 7))
    prof = core.load_profile(ld, F.TZ)
    load, info = core.load_series(t0, n, F.TZ, ld, 512.0)
    buy, sell = core.tariff(da, 0.0, 0.019, 21.0, 0.019)
    pay = core.build_payload(t0, n, 0.683, 0.5, main, buy, sell)
    rec = core.plan_for_day(F.PLANS, date(2026, 9, 5), F.TZ)[0]
    out = {
        "horizon": {t.isoformat(): list(core.horizon(t, F.TZ, d)) for t in (NOW_0907, F.local(2026, 10, 24, 14, 0, 4), F.local(2026, 3, 28, 23, 59, 59),
                                                                            F.local(2026, 10, 25, 2, 30)) for d in (1, 2)},
        "expected_steps": {d.isoformat(): core.expected_steps(d, F.TZ) for d in (date(2026, 9, 7), date(2026, 10, 25), date(2026, 3, 29))},
        "ceil": [core.ceil_step(t).isoformat() for t in (NOW_0907, F.local(2026, 9, 7, 14, 45), F.local(2026, 9, 7, 14, 45, 1), F.local(2026, 9, 7, 23, 59, 59))],
        "step_times_dst": [t.isoformat() for t in core.step_times(F.local(2026, 10, 25, 1, 0), 12)],
        "slot": [core._slot(t) for t in (NOW_0907, F.local(2026, 9, 7), F.local(2026, 9, 7, 23, 59))],
        "prices": {"da": da, "n_predicted": npred, "da_np_only": da2, "n_predicted_np_only": npred2},
        "tariff": {"buy": buy, "sell": sell, "other": list(core.tariff(da[:4], 0.11, 0.02, 21.0, 0.0))},
        "pv": {"p50": p50, "p10": p10, "p90": p90, "gaps": gaps, "ese50": ese50, "ese_gaps": ese_gaps, "mixed": mixed, "main": main, "micro": micro,
               "mix_clamped": core.pv_mix(p50[:3], p10[:3], 1.7), "split_clamped": list(core.pv_split(mixed[:3], ese50[:3], -1.0))},
        "load": {"profile": prof, "series": load, "info": info, "median": core._median([3, 1, 2, 5]), "median_even": core._median([4, 1, 3, 2]),
                 "too_few": list(core.load_series(t0, n, F.TZ, {date(2026, 9, 6): ld.get(date(2026, 9, 6))}, None))},
        "payload": _pay_summary(pay),
        "stress": {"costs": list(core.stress_costs(buy, sell)), "empty": list(core.stress_costs([], [])),
                   "loss_record_0905": core.loss_adjustment(rec["rows"]),
                   "loss_no_hybrid": core.loss_adjustment([{k: v for k, v in r.items() if k != "P_hybrid_inverter"} for r in rec["rows"][:20]])},
        "rebalance": {str(d): core.rebalance_schedule(d) for d in (None, 0, 3.5, 7, 10.5, 14, 30)}
                     | {"custom": core.rebalance_schedule(2, 0.01, 0.3, 0.02)},
        "knobs": {"defaults": core.knobs(None), "overlay": core.knobs({"stress_scale": 2, "soc_target": None, "unknown": 9}),
                  "cut": core.cut_thresholds(core.knobs({"aux_cut_on_ratio": 1.1})),
                  "target_ts": [core.soc_target_timestep(t0, n, date(2026, 9, d), at) for d in (7, 8, 9) for at in ("17:00", "14:45", "00:00", "x", "23:59:59")],
                  "apply": core.apply_knobs(dict(pay, battery_stress_cost=0.01, inverter_stress_cost=0.002),
                                            core.knobs({"stress_scale": 1.5, "soc_target": 0.8, "soc_target_at": "17:00"}), t0, n, date(2026, 9, 7))
                  | {"lists": None}},
        "aux_cut": [core.aux_cut_decision(c, a, act) for c in (0.0, 1.9, 2.0, 3.0, 12.0) for a in (0.0, 1.0, 2.0, 8.0) for act in (False, True)],
        "cost": [core.step_cost(g, 0.3, 0.1) for g in (1000.0, -1000.0, 0.0)],
        "score_terms": {"lambda": core.lambda_for([0.1, 0.2, 0.3], 0.9), "lambda_empty": core.lambda_for([], 0.9),
                        "soc_term": core.soc_term(60.0, 40.0, 48.2, 0.1)},
        "holds": [core.addon_holds_plan(a, b) for a, b in ((F.LAST_RUN, F.LAST_RUN), (F.LAST_RUN, dict(F.LAST_RUN, timestamp="2026-09-07T12:00:00+00:00")),
                                                            (F.LAST_RUN, dict(F.LAST_RUN, timestamp="2026-09-07T12:00:01Z")), (None, F.LAST_RUN),
                                                            (F.LAST_RUN, dict(F.LAST_RUN, action="publish-data")), ({"timestamp": "x"}, F.LAST_RUN))],
        "series": {d.isoformat(): F.actuals_for_day(core, d) for d in (date(2026, 9, 5), date(2026, 9, 6))}
                  | {"0907_partial": F.actuals_for_day(core, date(2026, 9, 7), core._slot(NOW_0907)),
                     "0907_whole": {k: len(v) for k, v in F.actuals_for_day(core, date(2026, 9, 7)).items()}},
        "window": F.window_actuals(core, F.local(2026, 9, 4, 23, 15), 195),
        "window_gaps": core.window_series(F.local(2026, 9, 7, 14, 0), 8, F.raw_stats()[F.ENT["pv_w"]], 4)[1],
        "constants": {k: getattr(core, k) for k in ("STEP_MIN", "STEP_H", "CAPACITY_KWH", "MAX_PV_GAPS", "ETA_BRIDGE", "GRID_CAP_W",
                                                    "SOC_MIN", "SOC_MAX", "SOC_FINAL_TARGET", "GROWATT_SHARE", "PV_P10_MIX", "AUX_CUT_ON_RATIO",
                                                    "AUX_CUT_ON_MIN_KWH", "AUX_CUT_OFF_RATIO", "AUX_CUT_OFF_MIN_KWH", "LOAD_REF_DAYS",
                                                    "LOAD_MIN_REF_DAYS", "LOAD_MIX_ALPHA", "Q_PORT", "Q_BRIDGE", "P_NOM_BATT_KW", "P_NOM_INV_KW",
                                                    "SURPLUS_BASE", "DEFICIT_BASE", "REBALANCE_SURPLUS_OFF_DAY", "REBALANCE_SOC_FINAL_DAY", "REBALANCE_PULL_DAY",
                                                    "REBALANCE_PULL_PER_DAY", "REBALANCE_PULL", "REBALANCE_FULL_LEVEL", "REBALANCE_TOP_V",
                                                    "REBALANCE_DWELL_H", "REBALANCE_BUDGET_H", "REBALANCE_LOOKBACK_D",
                                                    "ARCHIVE_SUFFIX", "PV_CURTAIL_SOC_PCT", "PV_DAYLIGHT_W", "PUBLISH_MAP",
                                                    "OMITTED_CONFIG_KEYS", "LIVE_KNOBS")}
                     | {"ARCHIVE_REACH_s": core.ARCHIVE_REACH.total_seconds()},
    }
    return out


# ---- the synthetic archive -------------------------------------------------------------------

def _syn_actuals():
    import json
    with open(F.SYN_ACTUALS) as f:
        return json.load(f)


@case
def syn_archive(core):
    day = date(2026, 10, 25)
    out = {"heads": [(os.path.basename(p), h) for p, h in core.plan_heads(F.SYN_PLANS)],
           "list_plans": [os.path.basename(p) for p in core.list_plans(F.SYN_PLANS)],
           "expected_steps": core.expected_steps(day, F.TZ)}
    for d in (date(2026, 10, 24), day, date(2026, 10, 26)):
        out[d.isoformat()] = {"plan_for_day": _sel(core.plan_for_day(F.SYN_PLANS, d, F.TZ)),
                              "newest": _sel(core.newest_plan_for_day(F.SYN_PLANS, d, F.TZ)),
                              "compact": _record(core, d, F.SYN_PLANS)}
    out["aux_cut_state"] = {t.isoformat(): core.aux_cut_state(F.SYN_PLANS, t, F.TZ)
                            for t in (F.local(2026, 10, 25, 14, 0), F.local(2026, 10, 25, 12, 30), F.local(2026, 10, 26, 0, 5))}
    out["days_since_full"] = core.days_since_full(F.SYN_PLANS, F.local(2026, 10, 25, 14, 0), F.TZ)
    out["organic"] = [d["plan_ts"] for d in core.organic_plans(F.SYN_PLANS)]
    out["virtual_soc"] = {t.isoformat(): list(core.virtual_soc_at(F.SYN_PLANS, t)) for t in (F.local(2026, 10, 25), F.local(2026, 10, 25, 2, 30), F.local(2026, 10, 26))}
    return out


@case
def syn_virtual_day_dst(core):
    act = _syn_actuals()
    day = date(2026, 10, 25)
    return {"complete": _vd(core, day, F.local(2026, 10, 26), act, F.SYN_PLANS),
            "midday": _vd(core, day, F.local(2026, 10, 25, 13, 20), act, F.SYN_PLANS),
            "no_actuals": _vd(core, day, F.local(2026, 10, 26), None, F.SYN_PLANS),
            "rolled": _rolled_syn(core, F.local(2026, 10, 25, 13, 20), act)}


def _rolled_syn(core, now, act):
    return core.rolled_slices(F.SYN_PLANS, now, F.TZ, act["pv_w"], act["load_w"], None, None, act["curtailed"], None,
                              act["pv_peak_w"], None, act["micro_w"], None)


@case
def syn_score_dst(core):
    act = _syn_actuals()
    day = date(2026, 10, 25)
    # The score_row lane of this case retired with the scoreboard (2026-09-27).
    cut = core.compact_slice(*reversed(core.newest_plan_for_day(F.SYN_PLANS, day, F.TZ)))
    return {"cut_slice": cut, "settled_cut": core.settle_slice(cut, act["pv_w"], act["load_w"], act["micro_w"]),
            "ab_solves": {str(c): [(t.isoformat(), d["plan_ts"], s) for t, d, s in core._ab_solves(F.SYN_PLANS, day, F.TZ, c)] for c in (60, 15)}}


@case
def foreign_solve_refused(core):
    """A solve that comes back with one row more than its horizon is another
    solve's result (2026-09-07): every production path refuses it instead of
    truncating it into a plan of record."""
    def longer(t0):
        base = F.StubSolver(t0)

        def solve(base_url, payload, timeout=180):
            r = base(base_url, payload, timeout)
            r["rows"] = r["rows"] + [dict(r["rows"][-1])]
            return r
        return solve
    now = NOW_0907
    t0, _ = core.horizon(now, F.TZ, days_ahead=2)
    out = {}
    with F.patched_solve(core.run_plan, longer(t0)):
        out["run_plan"] = core.run_plan(_plan_inp(core, now, F.PLANS))
    day = date(2026, 9, 5)
    doc = core.plan_for_day(F.PLANS, day, F.TZ)[0]
    ht0, hn = datetime.fromisoformat(doc["t0"]).astimezone(F.Z), int(doc["n"])
    win = F.window_actuals(core, ht0, hn)
    np_rows = sum((F.nordpool(ht0.date() + timedelta(days=k)) for k in range(4)), [])
    with F.patched_solve(core.hindsight_day, longer(F.local(2026, 9, 7, 14, 45))):
        out["hindsight"] = core.hindsight_day(F.PLANS, "http://stub", "2026-09-05", F.TZ, np_rows, win,
                                              F.actuals_for_day(core, day), 48.2, 0.9)
    return out


@case
def virtual_day_unreached(core):
    """A day the archive does not reach: the chain is broken and virtual_day says so."""
    return {"0910": _vd(core, date(2026, 9, 10), F.local(2026, 9, 11)),
            "0901": _vd(core, date(2026, 9, 1), F.local(2026, 9, 2), F.actuals_for_day(core, date(2026, 9, 2)))}


@case
def syn_ab_walk_no_plans(core):
    """A day the chain reaches (the 10-25 18:05 plan covers all of 10-26) but on
    which no organic plan was made: ab_walk has nothing to re-run."""
    doc = core.load_plan(os.path.join(F.SYN_PLANS, "20261025T180500.json.gz"))
    sl = core.day_slice(doc["rows"], date(2026, 10, 26), F.TZ, doc["soc_init"])
    mic = {r["timestamp"]: m for r, m in zip(doc["rows"], doc["pv_micro_w"])}
    rows = sl["rows"]
    act = {"pv_w": [round(float(r["P_PV"]) * 0.9, 1) for r in rows], "load_w": [round(float(r["P_Load"]) * 1.1, 1) for r in rows],
           "micro_w": [round(mic[r["timestamp"]] * 0.95, 1) for r in rows], "curtailed": [0.0] * len(rows)}
    stub = F.StubSolver(F.local(2026, 10, 27, 10, 0))
    with F.patched_solve(core.ab_walk, stub):
        out = core.ab_walk(F.SYN_PLANS, "http://stub", "2026-10-26", F.TZ, {}, act, 60, "closed", 48.2)
    out["stub_calls"] = len(stub.calls)
    return out


@case
def syn_ab_walk_cut(core):
    """The Growatt cut inside the A/B walk: a low soc_max leaves the stub no
    headroom, so it curtails through the negative block, and the thresholds
    lowered to almost nothing make the rule fire, re-solve and carry."""
    act = _syn_actuals()
    stub = F.StubSolver(F.local(2026, 10, 26, 10, 0))
    with F.patched_solve(core.ab_walk, stub):
        out = core.ab_walk(F.SYN_PLANS, "http://stub", "2026-10-25", F.TZ,
                           {"soc_max": 0.3, "aux_cut_on_ratio": 0.5, "aux_cut_on_min_kwh": 0.1, "aux_cut_off_min_kwh": 0.05},
                           act, 60, "closed", 48.2)
    out["stub_calls"] = len(stub.calls)
    return out


@case
def addon_health(core):
    """health() against canned /healthz and /get-config answers: the drift rule with
    the two keys /get-config never echoes, and the failure shapes. The age field
    reads the wall clock and is masked."""
    import json as _json
    import emhasscore.addon as addon
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(F.ROOT)), "config.json")     # ha/config.json
    with open(cfg_path) as f:
        cfg = _json.load(f)
    answers = {}

    def fake_get(base_url, path, timeout=30):
        for key, val in answers.items():
            if path.startswith(key):
                return val
        return 404, "nope"
    saved = addon.emhass_get
    addon.emhass_get = fake_get
    out = {}
    try:
        live = {k: v for k, v in cfg.items() if k not in core.OMITTED_CONFIG_KEYS}
        for name, hz, gc in (("ok", (200, {"status": "ok", "last_run_ts": "2026-09-07T12:00:00Z"}), (200, live)),
                             ("degraded", (503, {"status": "degraded"}), (200, live)),
                             ("drift", (200, {"status": "ok"}), (200, dict(live, optimization_time_step=30, extra_key=1))),
                             ("omitted_echoed", (200, {"status": "ok"}), (200, dict(live, data_path="/x"))),
                             ("no_config", (200, {"status": "ok"}), (0, "URLError: down")),
                             ("bad_body", (200, "not json"), (200, live))):
            answers = {"/healthz": hz, "/get-config": gc}
            r = core.health("http://stub", cfg_path)
            r["last_run_age_h"] = "<wall clock>" if r.get("last_run_age_h") is not None else None
            out[name] = r
        answers = {"/healthz": (200, {"status": "ok"}), "/get-config": (200, live)}
        out["unreadable_repo"] = core.health("http://stub", "/nonexistent/config.json")
    finally:
        addon.emhass_get = saved
    return out


@case
def syn_ab_walk_dst(core):
    act = _syn_actuals()
    stub = F.StubSolver(F.local(2026, 10, 26, 10, 0))
    with F.patched_solve(core.ab_walk, stub):
        out = core.ab_walk(F.SYN_PLANS, "http://stub", "2026-10-25", F.TZ, {"soc_target": 0.9}, act, 60, "closed", 48.2)
    out["stub_calls"] = len(stub.calls)
    return out
