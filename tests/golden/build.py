#!/usr/bin/env python3
"""Build or refresh the golden pin.

  build.py fixtures <vm_pull_dir>   one-off: select the archived plans, pack the
                                    5-minute statistics, Nord Pool rows and scores.csv
                                    from a directory holding the VM's plans/, a
                                    stats5.json (recorder/statistics_during_period
                                    output) and nordpool.json ({date: rows})
  build.py synthetic                write the synthetic archive (DST day, a
                                    non-Optimal solve, a cut plan, a legacy .json)
  build.py expected [case ...]      run the cases and (re)write expected/*.json

The fixtures and the synthetic archive are committed and normally never rebuilt;
`expected` is what a deliberate behaviour change re-runs, and its diff is the
receipt the commit has to explain.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))            # tools/emhass
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "..", "pyscript_helpers"))
sys.path.insert(0, os.path.dirname(HERE))                             # tools/emhass/tests

from golden import fixtures as F                                      # noqa: E402

# ---- fixture selection ---------------------------------------------------------------------

# The plans that make up the real fixture archive. Whole chains for the three days
# the reconstruction cases walk, plus the plan of record for each scored day and
# the one replay document beside its source.
SELECT = {
    # records, and the replay pair for 09-03
    "20260902T230500", "20260904T190509", "20260903T230500", "20260904T230500", "20260905T230500", "20260906T230500",
    # 09-06: pre-split -> split at 16:24, soc_source virtual -> settled
    "20260906T000500", "20260906T060500", "20260906T090500", "20260906T120500", "20260906T130159", "20260906T140500",
    "20260906T150500", "20260906T160500", "20260906T162431", "20260906T162947", "20260906T170500", "20260906T173747",
    "20260906T180500", "20260906T200500", "20260906T220500",
    # 09-07: the P50 archive from 10:05, the knob layer from 13:02, the 30-minute cadence
    "20260907T000500", "20260907T001003", "20260907T060500", "20260907T090500", "20260907T093500", "20260907T100500",
    "20260907T103500", "20260907T110500", "20260907T113500", "20260907T120500", "20260907T123500", "20260907T130211",
    "20260907T133500", "20260907T140500", "20260907T141321", "20260907T143500",
}
SELECT_DAYS = {"20260905"}          # every organic document of the curtailment day


def build_fixtures(src: str):
    os.makedirs(F.PLANS, exist_ok=True)
    n = 0
    for p in sorted(glob.glob(os.path.join(src, "plans", "*.json*"))):
        stem = os.path.basename(p).split(".")[0]
        if stem in SELECT or stem[:8] in SELECT_DAYS:
            shutil.copy2(p, F.PLANS)
            n += 1
    print(f"plans: {n} documents, {sum(os.path.getsize(f) for f in glob.glob(os.path.join(F.PLANS, '*'))) // 1024} KB")
    with open(os.path.join(src, "stats5.json")) as f:
        stats = json.load(f)
    packed = {eid: [[r["start"], r.get("mean"), r.get("max")] for r in rows] for eid, rows in stats.items()}
    with gzip.open(F.ACTUALS_5MIN, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(packed, f, separators=(",", ":"))
    shutil.copy(os.path.join(src, "nordpool.json"), F.NORDPOOL)
    shutil.copy(os.path.join(src, "scores.csv"), F.SCORES)
    print("actuals, nordpool, scores written")


# ---- the synthetic archive --------------------------------------------------------------------

HOURLY_DA = [0.09] * 6 + [0.12] * 3 + [0.05] * 2 + [-0.02] * 4 + [0.06] * 2 + [0.18] * 4 + [0.10] * 3


def _price_at(t):
    return HOURLY_DA[t.hour]


def _pv_at(t):
    import math
    h = t.hour + t.minute / 60.0
    return round(7000.0 * math.sin(math.pi * (h - 7.0) / 11.0), 1) if 7.0 < h < 18.0 else 0.0


def _load_at(t):
    return 400.0 + (1200.0 if 18 <= t.hour < 21 else 0.0) + (300.0 if 7 <= t.hour < 9 else 0.0)


def synth_doc(core, now, days_ahead=1, soc_init=0.5, status="Optimal", split=True, cut=False,
              knobs=True, p50=True, legacy=False, replay=False):
    t0, n = core.horizon(now, F.TZ, days_ahead=days_ahead)
    times = core.step_times(t0, n)
    da = [_price_at(t) for t in times]
    tf = {"energy_tax": 0.0, "supplier_fee": 0.019, "btw_pct": 21.0, "feedin_fee": 0.019}
    buy, sell = core.tariff(da, tf["energy_tax"], tf["supplier_fee"], tf["btw_pct"], tf["feedin_fee"])
    p50_w = [_pv_at(t) for t in times]
    p10_w = [round(v * 0.8, 1) for v in p50_w]
    p90_w = [round(v * 1.15, 1) for v in p50_w]
    pv_w = core.pv_mix(p50_w, p10_w, 0.2) if p50 else list(p50_w)
    micro = [round(v * 0.25, 1) for v in pv_w] if split else [0.0] * n
    main = [round(v - m, 1) for v, m in zip(pv_w, micro)]
    load = [_load_at(t) for t in times]
    today = now.date()
    today_set = {k for k, t in enumerate(times) if t.date() == today}
    micro_cut = [cut and k in today_set for k in range(n)]
    payload = core.build_payload(t0, n, soc_init, 0.5, main, buy, sell)
    payload["load_power_forecast"] = [round(l - (0.0 if micro_cut[k] else m), 1) for k, (l, m) in enumerate(zip(load, micro))]
    payload["battery_stress_cost"], payload["inverter_stress_cost"] = core.stress_costs(buy, sell)
    payload.update(core.rebalance_schedule(3))
    kn = core.knobs(None)
    core.apply_knobs(payload, kn, t0, n, today)
    rows = F.StubSolver(t0, status=status)("stub", payload)["rows"]
    for k, (r, m) in enumerate(zip(rows, micro)):
        r["P_PV"] = round(float(r["P_PV"]) + m, 1)
        if micro_cut[k]:
            r["P_PV_curtailment"] = round(float(r.get("P_PV_curtailment") or 0.0) + m, 1)
        else:
            r["P_Load"] = round(float(r["P_Load"]) + m, 1)
    cost = round(sum(core.step_cost(float(r["P_grid"]), float(r["unit_load_cost"]), float(r["unit_prod_price"])) for r in rows), 4)
    doc = {"plan_ts": now.isoformat(timespec="seconds"), "t0": t0.isoformat(), "n": n, "tz": F.TZ, "soc_init": soc_init,
           "soc_final": 0.5, "tariff": tf, "n_predicted_steps": 0, "pv_gap_steps": 0,
           "prices_predicted": False, "optim_status": status, "cost_eur": cost, "seconds": 0.0,
           "payload": payload, "rows": rows, "last_run": dict(F.LAST_RUN), "soc_source": "virtual"}
    if legacy:
        return doc
    doc.update(growatt_share=0.288 if split else None, pv_micro_w=micro, load_source="profile", load_ref_days=7)
    if p50:
        doc.update(pv_p50_w=p50_w, pv_p10_w=p10_w, pv_p90_w=p90_w, pv_p10_mix=0.2)
    if knobs:
        doc["knobs"] = kn
    if split:
        aux = core.aux_cut_decision(sum(float(rows[k].get("P_PV_curtailment") or 0.0) for k in today_set) * 0.25 / 1000.0,
                                    sum(micro[k] for k in today_set) * 0.25 / 1000.0, False)
        aux.update(cut_from=None, cut_until=None, n_cut_steps=0, second_solve=None, uncut_cost_eur=cost, active=bool(cut))
        doc.update(aux_cut=aux, micro_cut=micro_cut)
    if replay:
        doc.update(replay=True, source_plan_ts=doc["plan_ts"], soc_source="replay")
    return doc


def build_synthetic():
    import emhass_core as core
    shutil.rmtree(os.path.dirname(F.SYN_PLANS), ignore_errors=True)
    os.makedirs(F.SYN_PLANS, exist_ok=True)
    L = F.local
    docs = [
        (L(2026, 10, 23, 23, 5), dict(days_ahead=1, soc_init=0.4)),                           # record for 10-24
        (L(2026, 10, 24, 23, 5), dict(days_ahead=2, soc_init=0.55)),                          # record for 10-25, DST day mid-horizon
        (L(2026, 10, 25, 6, 5), dict(days_ahead=1, soc_init=0.5)),
        (L(2026, 10, 25, 12, 5), dict(days_ahead=1, soc_init=0.9, status="Infeasible")),      # a failed solve
        (L(2026, 10, 25, 13, 5), dict(days_ahead=1, soc_init=0.9, cut=True)),                 # the Growatt cut firing
        (L(2026, 10, 25, 18, 5), dict(days_ahead=1, soc_init=0.7)),
    ]
    for now, kw in docs:
        core.write_plan_archive(F.SYN_PLANS, now, synth_doc(core, now, **kw))
    # a legacy uncompressed, pre-split, pre-profile document, as the first days of the archive were
    leg = synth_doc(core, L(2026, 10, 24, 22, 5), days_ahead=1, soc_init=0.6, split=False, knobs=False, p50=False, legacy=True)
    with open(os.path.join(F.SYN_PLANS, "20261024T220500.json"), "w") as f:
        json.dump(leg, f, separators=(",", ":"))
    # a replay of the 10-24 record, archived a day later under its source's plan_ts
    rep = synth_doc(core, L(2026, 10, 23, 23, 5), days_ahead=1, soc_init=0.4, replay=True)
    core.write_plan_archive(F.SYN_PLANS, L(2026, 10, 25, 19, 30), rep)
    # measured 10-25 (100 steps), derived from the record's own rows so the day is self-consistent
    rec = core.load_plan(os.path.join(F.SYN_PLANS, "20261024T230500.json.gz"))
    sl = core.day_slice(rec["rows"], date(2026, 10, 25), F.TZ, rec["soc_init"])
    mic_by_ts = {r["timestamp"]: m for r, m in zip(rec["rows"], rec["pv_micro_w"])}
    rows = sl["rows"]
    act = {"pv_w": [round(float(r["P_PV"]) * 0.9, 1) for r in rows],
           "load_w": [round(float(r["P_Load"]) * 1.1, 1) for r in rows],
           "grid_w": [round(float(r["P_grid"]) + 100.0, 1) for r in rows],
           "batt_dc_w": [round(float(r["P_batt"]) * 0.98, 1) for r in rows],
           "soc_pct": [round(float(r["SOC_opt"]) * 100, 2) for r in rows],
           "micro_w": [round(mic_by_ts[r["timestamp"]] * 0.95, 1) for r in rows],
           "curtailed": [1.0 if float(r["SOC_opt"]) > 0.95 else 0.0 for r in rows],
           "pv_peak_w": [round(float(r["P_PV"]) * 0.95, 1) for r in rows]}
    with open(F.SYN_ACTUALS, "w") as f:
        json.dump(act, f, separators=(",", ":"))
    print(f"synthetic: {len(os.listdir(F.SYN_PLANS))} documents, actuals {len(rows)} steps")


# ---- expected -----------------------------------------------------------------------------------

def build_expected(names):
    import emhass_core as core
    from golden.cases import CASES
    os.makedirs(F.EXPECTED, exist_ok=True)
    for name, fn in CASES.items():
        if names and name not in names:
            continue
        text = F.canonical(fn(core))
        path = os.path.join(F.EXPECTED, name + ".json")
        old = open(path).read() if os.path.exists(path) else None
        with open(path, "w") as f:
            f.write(text)
        print(f"{'same   ' if old == text else 'new    ' if old is None else 'CHANGED'} {name} ({len(text) // 1024} KB)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "expected"
    if cmd == "fixtures":
        build_fixtures(sys.argv[2])
    elif cmd == "synthetic":
        build_synthetic()
    elif cmd == "expected":
        build_expected(set(sys.argv[2:]))
    else:
        sys.exit(__doc__)
