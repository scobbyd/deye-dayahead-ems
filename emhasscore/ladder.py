"""The ladder: what the controller did, what it could have done, and what
clairvoyance would add - three lanes walked day by day over the archive, each
chaining its OWN pack across midnight and scored on CASH ALONE.

Why (2026-09-09). The scoreboard's hindsight lane solves the plan of
record's two-day horizon with tomorrow's prices known at midnight, then the
day score cuts that trajectory at midnight and values what is left in the pack
at lambda. On 09-06 and 09-07 it held 7 to 14 kWh for the next evening's
higher peak, was credited a tenth of a euro per kWh for it, and came out BELOW
the plan that sold the same energy that night. A lane that is omniscient on
prices and scored on a cheap midnight cut is neither a ceiling nor a floor.

The three rungs, by what each knows about the days after D:

  actual      what the controller did: the plans in force through the day,
              settled through the virtual pack (the executed lane); forecast
              PV and load, tomorrow's prices predicted until 13:00
  hindsight   PV and load EXACT for D, the plan's own forecast for D+1, prices
              exactly as published: D+1 arrives at 13:00 and not before. Two
              solves, midnight and 13:00, chained through the pack. The best
              any controller on this site could reach with a perfect nowcast.
  omni2       PV, load and prices exact over the whole three-day box (D, D+1,
              D+2), all known at midnight. A pack that stores a day of
              arbitrage should not be able to use it - that is the hypothesis
              this rung tests. (omni1, exact to the end of D+1, was dropped
              2026-09-14: over 208 replayed days it never left omni2's side.)

No SOC term. Every rung starts day D where ITS OWN trajectory ended day D-1
(reset to the actual lane's midnight only when its chain is broken), and each
day is scored as the replay lane's cash plus the bridge/port loss over the
measured day. The gap over any window is then a plain sum of daily cash, exact
over the window and only approximate on a single day, which is the honest
shape of a problem where energy crosses midnight. ladder_summary prices each
rung's END-of-window pack against the actual lane at the last day's lambda so
the books can be closed when the reader wants them closed.

Solves are cheap because the inputs are exact: nothing changes between two
solves except price arrival, so every rung takes the same two solves a day
(midnight and 13:00). A year of the whole ladder is about 3.600 solves.
"""
from __future__ import annotations

import csv
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .archive import day_slice, iter_organic_plans, plan_for_day
from .deye import settle_slice
from .grid import STEP_MIN, _parse_ts, expected_steps, step_index, step_times
from .objective import CAPACITY_KWH, build_payload, rebalance_schedule, SOC_FINAL_TARGET, stress_costs
from .repair import effective_load, pv_potential
from .scoring import _hindsight_solve, lambda_for, replay_day
from .series import price_series, tariff
from .slices import compact_slice, virtual_day

# The three rungs the verdict is made of. A window is complete when all three
# have settled, and the gaps are computed between them.
CORE_RUNGS = ("actual", "hindsight", "omni2")
# Two DIAGNOSTIC rungs decompose the nowcast: each knows half of day D and
# forecasts the other half, so hindsight_pv - actual is what knowing the sun is
# worth and hindsight_load - actual what knowing the load is worth. They are
# walked and stored but never gate the headline: a diagnostic that fails must
# not empty the windows. The two do NOT add up to hindsight - PV and load
# errors interact through one battery trajectory - and the residual
# (hindsight - hindsight_pv - hindsight_load + actual) is the interaction term.
RUNGS = CORE_RUNGS + ("hindsight_pv", "hindsight_load")
# what each rung knows about day D: (exact days, which half of D is measured)
KNOWLEDGE = {"hindsight": (1, "both"), "hindsight_pv": (1, "pv"), "hindsight_load": (1, "load"),
             "omni2": (3, "both")}
RUNG_KEYS = ("eur", "cash", "loss", "soc_end_pct", "status", "solves")
LADDER_COLUMNS = (["date", "soc_start_pct", "lambda_eur_kwh"]
                  + [f"{r}_{k}" for r in RUNGS for k in RUNG_KEYS] + ["flags"])
_TEXT_COLUMNS = {"date", "flags"} | {f"{r}_status" for r in RUNGS}
PUBLISH_HOUR = 13        # the hour tomorrow's day-ahead prices reach a controller (Nord Pool NL ~12:45)
HORIZON_DAYS = 3         # the box every rung shares: D, D+1, D+2


def _n_steps(day: date, tz: str, days: int) -> int:
    return sum(expected_steps(day + timedelta(days=i), tz) for i in range(days))


# ---- the CSV ---------------------------------------------------------------------

def read_ladder(csv_path: str) -> list[dict]:
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, newline="") as f:
        raw = list(csv.DictReader(f))
    out = []
    for r in raw:
        d = {}
        for k in LADDER_COLUMNS:
            v = r.get(k, "")
            if k in _TEXT_COLUMNS:
                d[k] = v if v else ("" if k == "flags" else None)
            elif v in ("", "nan", "None"):
                d[k] = None
            else:
                d[k] = int(float(v)) if k.endswith("_solves") else float(v)
        out.append(d)
    return out


def upsert_ladder(csv_path: str, row: dict) -> list[dict]:
    rows = [r for r in read_ladder(csv_path) if r["date"] != row["date"]] + [dict(row)]
    rows.sort(key=lambda r: r["date"])
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LADDER_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in LADDER_COLUMNS})
    os.replace(tmp, csv_path)
    return rows


# ---- the hourly sidecar (2026-09-16) ---------------------------------------------
#
# The ladder's actual lane is one number a day in ladder.csv; the dashboard's
# period card wants the hours too (which hours earn), and a recorder statistic
# rather than an attribute array so a date picker can drive it. The replay
# already prices every step; ladder_hours.csv next to ladder.csv keeps the
# hour sums (solver sign, like the CSV) for every day the actual lane ran
# fresh. Days imported from the closed replay have no steps on this side, so
# the earned series below falls back to their daily value at 00:00.

def hours_path(csv_path: str) -> str:
    base, ext = os.path.splitext(csv_path)
    return f"{base}_hours{ext or '.csv'}"


def tariff_path(csv_path: str) -> str:
    """tariff_hours.csv next to ladder.csv: the buy and sell tariff per hour
    for the deep past (generated from the day-ahead archive by
    the tariff-hours helper); the ladder's own rows carry the tariff for
    every replayed day and win where both exist."""
    return os.path.join(os.path.dirname(csv_path) or ".", "tariff_hours.csv")


# The shadow lanes kept per hour beside the cash (2026-09-17): means over the
# replay's 15-minute steps, split so a day or month mean of each stays a
# magnitude (a mean of the signed battery flow over a day is its net, and a
# pack's net is nil). kW, %, ct/kWh.
LANE_COLUMNS = ("charge_kw", "discharge_kw", "import_kw", "export_kw", "soc_pct", "pv_kw", "load_kw", "buy_ct", "sell_ct")
HOURS_COLUMNS = ("hour", "eur") + LANE_COLUMNS


def _hour_keys(n: int, day: date, tz: str) -> list[str]:
    z = ZoneInfo(tz)
    t0 = datetime.combine(day, time(0), tzinfo=z).astimezone(ZoneInfo("UTC"))
    return [(t0 + timedelta(minutes=STEP_MIN * i)).astimezone(z).replace(minute=0, second=0, microsecond=0).isoformat()
            for i in range(n)]


def hour_lanes(ex: dict, day: date, tz: str) -> list[tuple[str, dict]]:
    """[(hour start ISO, {lane: mean})] for one replayed day (`ex` = the
    virtual_day dict the actual rung settles). A lane whose array is missing
    or short is left out of that day (None in the sidecar)."""
    n = expected_steps(day, tz)
    keys = _hour_keys(n, day, tz)
    arr = lambda k: ex.get(k) if isinstance(ex.get(k), list) and len(ex.get(k)) == n else None
    pb, pg, soc, pv, cut, load, buy, sell = (arr(k) for k in ("p_batt_w", "p_grid_w", "soc_pct", "p_pv_w", "pv_curtail_w",
                                                            "p_load_w", "buy", "sell"))
    f = lambda v: None if v is None else float(v)
    per_step = {
        "charge_kw": None if pb is None else [max(0.0, -f(v)) / 1000.0 if v is not None else None for v in pb],
        "discharge_kw": None if pb is None else [max(0.0, f(v)) / 1000.0 if v is not None else None for v in pb],
        "import_kw": None if pg is None else [max(0.0, f(v)) / 1000.0 if v is not None else None for v in pg],
        "export_kw": None if pg is None else [max(0.0, -f(v)) / 1000.0 if v is not None else None for v in pg],
        "soc_pct": None if soc is None else [f(v) for v in soc],
        "pv_kw": None if pv is None else [(f(pv[i]) - (f(cut[i]) if cut and cut[i] is not None else 0.0)) / 1000.0
                                          if pv[i] is not None else None for i in range(n)],
        "load_kw": None if load is None else [f(v) / 1000.0 if v is not None else None for v in load],
        "buy_ct": None if buy is None else [f(v) * 100.0 if v is not None else None for v in buy],
        "sell_ct": None if sell is None else [f(v) * 100.0 if v is not None else None for v in sell],
    }
    out, order = {}, []
    for i, k in enumerate(keys):
        if k not in out:
            out[k] = {c: [] for c in LANE_COLUMNS}
            order.append(k)
        for c in LANE_COLUMNS:
            v = per_step[c]
            if v is not None and v[i] is not None:
                out[k][c].append(v[i])
    return [(k, {c: (round(sum(out[k][c]) / len(out[k][c]), 4) if out[k][c] else None) for c in LANE_COLUMNS})
            for k in order]


def hour_sums(step_eur: list, day: date, tz: str) -> list[tuple[str, float]]:
    """[(hour start ISO in tz, EUR)] for one day's per-step lane. Steps are
    walked in UTC so a DST day buckets to 23 or 25 hours, not 24 with a gap."""
    n = expected_steps(day, tz)
    if len(step_eur) != n:
        raise ValueError(f"{day}: {len(step_eur)} steps, expected {n}")
    z = ZoneInfo(tz)
    t0 = datetime.combine(day, time(0), tzinfo=z).astimezone(ZoneInfo("UTC"))
    out, order = {}, []
    for i, v in enumerate(step_eur):
        k = (t0 + timedelta(minutes=STEP_MIN * i)).astimezone(z).replace(minute=0, second=0, microsecond=0).isoformat()
        if k not in out:
            out[k] = 0.0
            order.append(k)
        out[k] += float(v)
    return [(k, round(out[k], 6)) for k in order]


def today_hours(settled: dict, day: date, tz: str) -> tuple[list, list]:
    """The running day's hours from the live plan run's settled slice
    (virtual_day at `now`): cash from cost_eur (no bridge loss: that is a
    day-level term the ladder adds when it settles the day) and the lanes,
    both over the hours that have at least one settled step (i < n_past).
    The ladder's nightly row replaces these rows the next morning."""
    n = expected_steps(day, tz)
    np_ = int(settled.get("n_past") or 0)
    keys = _hour_keys(n, day, tz)
    keep = set(keys[:np_])
    cost = settled.get("cost_eur") or []
    steps = [(float(cost[i]) if i < np_ and i < len(cost) and cost[i] is not None else 0.0) for i in range(n)]
    hours = [(k, v) for k, v in hour_sums(steps, day, tz) if k in keep]
    masked = dict(settled)
    for key in ("p_batt_w", "p_grid_w", "soc_pct", "p_pv_w", "pv_curtail_w", "p_load_w", "buy", "sell"):
        arr = settled.get(key)
        if isinstance(arr, list) and len(arr) == n:
            masked[key] = [arr[i] if i < np_ else None for i in range(n)]
    lanes = [(k, v) for k, v in hour_lanes(masked, day, tz) if k in keep]
    return hours, lanes


def read_ladder_hour_rows(path: str) -> list[dict]:
    """Every row of the hourly sidecar as {hour, eur, <lanes>} (a lane column
    the file predates reads None), sorted by hour."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("hour"):
                continue
            d = {"hour": r["hour"], "eur": float(r["eur"])}
            for c in LANE_COLUMNS:
                v = r.get(c)
                d[c] = None if v in (None, "", "None") else float(v)
            out.append(d)
    out.sort(key=lambda d: d["hour"])
    return out


def read_ladder_hours(path: str) -> list[tuple[str, float]]:
    return [(d["hour"], d["eur"]) for d in read_ladder_hour_rows(path)]


def upsert_ladder_hours(path: str, day_iso: str, hours: list, lanes: list | None = None) -> list[tuple[str, float]]:
    """Replace one day's hour rows (matched on the ISO date prefix) and rewrite
    the file sorted. `hours` = [(hour, eur)], `lanes` = hour_lanes() output for
    the same day (optional; a day written without keeps None lanes)."""
    by_hour = {k: dict(v) for k, v in (lanes or [])}
    rows = [d for d in read_ladder_hour_rows(path) if not d["hour"].startswith(day_iso)]
    for k, v in hours:
        d = {"hour": k, "eur": float(v)}
        d.update({c: by_hour.get(k, {}).get(c) for c in LANE_COLUMNS})
        rows.append(d)
    rows.sort(key=lambda d: d["hour"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HOURS_COLUMNS)
        w.writeheader()
        for d in rows:
            w.writerow({c: ("" if d.get(c) is None else d.get(c)) for c in HOURS_COLUMNS})
    os.replace(tmp, path)
    return [(d["hour"], d["eur"]) for d in rows]


def read_tariff_hours(path: str) -> list[tuple[str, float, float]]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = [(r["hour"], float(r["buy_ct"]), float(r["sell_ct"])) for r in csv.DictReader(f) if r.get("hour")]
    rows.sort()
    return rows


def upsert_tariff_hours(path: str, day_iso: str, rows: list) -> list[tuple[str, float, float]]:
    """Replace one day's rows of the tariff sidecar; `rows` = [(hour, buy_ct, sell_ct)]."""
    keep = [r for r in read_tariff_hours(path) if not r[0].startswith(day_iso)] + [(k, float(b), float(s)) for k, b, s in rows]
    keep.sort()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hour", "buy_ct", "sell_ct"])
        for k, b, s in keep:
            w.writerow([k, b, s])
    os.replace(tmp, path)
    return keep


def ladder_shadow_series(csv_path: str) -> dict[str, list[tuple[str, float]]]:
    """{lane: [(hour ISO, mean)]} for the recorder: the shadow lanes of every
    replayed day from the hourly sidecar, and the tariff pair extended into
    the deep past from tariff_hours.csv where the ladder has no row."""
    out = {c: [] for c in LANE_COLUMNS}
    have = set()
    for d in read_ladder_hour_rows(hours_path(csv_path)):
        for c in LANE_COLUMNS:
            if d.get(c) is not None:
                out[c].append((d["hour"], d[c]))
                if c == "buy_ct":
                    have.add(d["hour"])
    for k, b, sell in read_tariff_hours(tariff_path(csv_path)):
        if k in have:
            continue
        out["buy_ct"].append((k, b))
        out["sell_ct"].append((k, sell))
    for c in LANE_COLUMNS:
        out[c].sort()
    return out


def ladder_earned_series(csv_path: str, tz: str = "Europe/Amsterdam") -> list[tuple[str, float]]:
    """The actual lane as EUR EARNED (solver sign negated once, here) for the
    recorder: the sidecar's hour rows where a day has them, the day's value at
    00:00 where it does not. Seed rows (the replay import's chain anchor) and
    days without a settled actual are left out.

    The row in ladder.csv is the day's number of record; a backfilled replay
    can land cents away from it (actuals at the recorder's retention edge, a
    mask fallback), so the hours are reconciled to the row: the residual is
    spread over the day's hours in proportion to their size, which keeps the
    shape and makes every period total equal the ladder's own sum."""
    hours = read_ladder_hours(hours_path(csv_path))
    by_day = {}
    for k, v in hours:
        by_day.setdefault(k[:10], []).append((k, v))
    out = []
    rows = read_ladder(csv_path)
    dated = {r["date"] for r in rows}
    # the running day (today_hours) has hour rows and no ladder row yet: as-is
    for d, hs in by_day.items():
        if d not in dated:
            out.extend((k, round(-v, 6)) for k, v in hs)
    for r in rows:
        if "seed" in (r.get("flags") or "").split(";"):
            continue
        if r.get("actual_status") != "ok" or r.get("actual_eur") is None:
            continue
        d, day_eur = r["date"], float(r["actual_eur"])
        if d in by_day:
            hs = by_day[d]
            resid = day_eur - sum(v for _, v in hs)
            if abs(resid) > 5e-4:
                tot = sum(abs(v) for _, v in hs)
                w = [abs(v) / tot for _, v in hs] if tot > 0 else [1.0 / len(hs)] * len(hs)
                hs = [(k, v + resid * wi) for (k, v), wi in zip(hs, w)]
            out.extend((k, round(-v, 6)) for k, v in hs)
        else:
            out.append((_midnight(date.fromisoformat(d), tz).isoformat(), round(-day_eur, 6)))
    out.sort()
    return out


# ---- horizons --------------------------------------------------------------------

def _midnight(day: date, tz: str) -> datetime:
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(tz))


def _doc_end(doc: dict) -> datetime:
    return _parse_ts(doc["t0"]) + timedelta(minutes=STEP_MIN * int(doc["n"]))


def ladder_windows(archive_dir: str, day_iso: str, tz: str) -> dict | None:
    """The two windows measured from the day's local midnight: n1 over D and
    D+1 (kept for callers that still hand a win1), n2 over the whole three-day
    box that omni2 needs. None when the day has no plan of record."""
    day = date.fromisoformat(day_iso)
    found = plan_for_day(archive_dir, day, tz)
    if not found:
        return None
    t0 = _midnight(day, tz)
    n1 = _n_steps(day, tz, 2)                       # D and D+1 measured
    n2 = _n_steps(day, tz, HORIZON_DAYS)            # omni2: the whole box measured
    return {"t0": t0.isoformat(), "n1": n1, "n2": n2}


# ---- inputs ----------------------------------------------------------------------

def _doc_view(doc: dict, t_from: datetime, n: int) -> dict | None:
    """What the plan of record believed, per step from t_from: gross PV and
    load, its own buy/sell (real for the published day, predicted beyond), the
    must-take Growatt and the cut flags. None if the doc does not cover it."""
    rows, mic = doc["rows"], doc.get("pv_micro_w") or [0.0] * int(doc["n"])
    cut = doc.get("micro_cut") or [False] * int(doc["n"])
    shift = step_index(t_from, _parse_ts(doc["t0"]))
    if shift < 0 or shift + n > len(rows):
        return None
    sl = slice(shift, shift + n)
    return {"pv": [float(r["P_PV"]) for r in rows[sl]], "load": [float(r["P_Load"]) for r in rows[sl]],
            "buy": [float(r["unit_load_cost"]) for r in rows[sl]],
            "sell": [float(r["unit_prod_price"]) for r in rows[sl]],
            "mic": [float(m) for m in mic[sl]], "cut": [bool(c) for c in cut[sl]], "shift": shift}


def _exact(win: dict, doc: dict, t0: datetime, n: int) -> dict:
    """The measured window as LP inputs: PV repaired where the real pack held the
    array back (raw Solcast from the plan where it reaches, the measurement
    beyond), the effective load, and the must-take half."""
    mic = list(win.get("micro_w") or []) if len(win.get("micro_w") or []) == n else [0.0] * n
    view = _doc_view(doc, t0, n)
    p50 = doc.get("pv_p50_w") or []
    if view is not None and len(p50) >= view["shift"] + n:
        plan_pv = [float(v) for v in p50[view["shift"]:view["shift"] + n]]
    elif view is not None:
        plan_pv = view["pv"]
    else:
        m = _doc_view(doc, t0, max(0, step_index(_doc_end(doc), t0)))
        plan_pv = (m["pv"] if m else []) + list(win["pv_w"])
        plan_pv = plan_pv[:n]
    pv_pot, info = pv_potential(win["pv_w"], plan_pv, win.get("curtailed") or [0.0] * n,
                                win.get("pv_peak_w"), micro_meas_w=mic)
    load = effective_load(win["grid_w"], win["batt_dc_w"], win["pv_w"])
    return {"pv": pv_pot, "load": load, "mic": mic, "cut": [False] * n, "pv_info": info}


def _tail_view(archive_dir: str, t_from: datetime, n: int, t0: datetime) -> dict | None:
    """The forecast past the plan of record's end, from the EARLIEST solve of day
    D that reaches it. iter_organic_plans yields newest first, so the last hit is
    the earliest plan - the 00:05 solve, whose horizon is the first of the day to
    open to the end of D+2."""
    best = None
    for cand in iter_organic_plans(archive_dir, since=t0, until=t0 + timedelta(hours=6)):
        v = _doc_view(cand, t_from, n)
        if v is not None:
            best = v
    return best


def _box_view(archive_dir: str, doc: dict, t0: datetime, n: int) -> dict | None:
    """The plan's own forecast over the whole box: the plan of record where it
    reaches, the day's first solve beyond it.

    The plan of record is a pre-midnight solve and stops at the end of D+1, but
    the live controller ran to the end of D+2 from 00:15 onward (n 287,
    n_predicted_steps 192 at midnight, 96 after 13:00). A rung confined to the
    plan of record's two days would be boxed SMALLER than the lane it is meant
    to bound, which is not a ceiling.
    """
    v = _doc_view(doc, t0, n)
    if v is not None:
        return v
    reach = step_index(_doc_end(doc), t0)
    head = _doc_view(doc, t0, reach) if 0 < reach < n else None
    if head is None:
        return None
    tail = _tail_view(archive_dir, _doc_end(doc), n - reach, t0)
    if tail is None:
        return None
    out = {k: list(head[k]) + list(tail[k]) for k in ("pv", "load", "buy", "sell", "mic", "cut")}
    out["shift"] = head["shift"]
    return out


def _real_prices(doc: dict, t0: datetime, n: int, np_rows) -> tuple | None:
    """The published day-ahead for every step, in the plan's tariff frame, or
    None when any step would have to be held or predicted."""
    try:
        da, n_pred = price_series(t0, n, np_rows, None, None)
    except ValueError:
        return None
    if n_pred:
        return None
    tf = doc["tariff"]
    return tariff(da, tf["energy_tax"], tf["supplier_fee"], tf["btw_pct"], tf["feedin_fee"])


def _prices_to(doc: dict, t0: datetime, n: int, np_rows, cutoff: datetime, view: dict) -> tuple | None:
    """Published day-ahead up to `cutoff`, the plan's own predicted prices after
    it. A controller knows the auction only as far as it has cleared: at midnight
    on D that is the end of D, from 12:45 the end of D+1. Only the omniscient
    rungs push the cutoff to the end of the box, and that is what makes them
    omniscient."""
    i = min(n, max(0, step_index(cutoff, t0)))
    if i:
        real = _real_prices(doc, t0, i, np_rows)
        if real is None:
            return None
        return list(real[0]) + list(view["buy"][i:]), list(real[1]) + list(view["sell"][i:])
    return list(view["buy"]), list(view["sell"])


def _payload(doc: dict, t0: datetime, n: int, soc_init: float, pv, load, mic, cut, buy, sell) -> dict:
    """The plan of record's OWN objective on the given inputs: its stress costs
    and SOC knobs, its terminal target, main-only PV and a load net of the
    must-take half exactly as run_plan posts them."""
    soc_final = float(doc.get("soc_final") or SOC_FINAL_TARGET)
    pv_net = [round(max(0.0, float(p) - float(m)), 1) for p, m in zip(pv, mic)]
    pay = build_payload(t0, n, soc_init, soc_final, pv_net, list(buy), list(sell))
    pay["load_power_forecast"] = [round(float(l) - (0.0 if c else float(m)), 1)
                                  for l, m, c in zip(load, mic, cut)]
    plan_payload = doc.get("payload") or {}
    u_batt, u_inv = stress_costs(buy, sell)
    pay["battery_stress_cost"] = plan_payload.get("battery_stress_cost", u_batt)
    pay["inverter_stress_cost"] = plan_payload.get("inverter_stress_cost", u_inv)
    knobs = ("battery_soc_surplus_cost", "battery_soc_deficit_threshold", "battery_soc_deficit_cost")
    if all(k in plan_payload for k in knobs):
        pay.update({k: plan_payload[k] for k in knobs})
    else:
        pay.update(rebalance_schedule(None))
    return pay


# ---- scoring one rung's day ------------------------------------------------------

def _score(rows_day: list, doc: dict, day: date, tz: str, soc_start: float, mic_day: list, act: dict,
           capacity_kwh: float, lam: float, source: str) -> dict:
    """Cut the day out of a rung's rows and settle it through the replay lane
    against the measured day, on the same footing as the actual lane."""
    sl = day_slice(rows_day, day, tz, soc_start)
    if not sl or sl["n"] != expected_steps(day, tz):
        return {"status": "short_slice"}
    hs = compact_slice(sl, dict(doc, n_predicted_steps=0, pv_gap_steps=0, rows=rows_day, optim_status="Optimal",
                                soc_source=source, pv_micro_w=list(mic_day), micro_cut=None, pv_p50_w=None))
    rep = replay_day(hs, act["grid_w"], act["batt_dc_w"], act["pv_w"], capacity_kwh, lam, pv_pot_w=hs["p_pv_w"])
    if rep["status"] == "no_actuals":
        return {"status": "no_actuals"}
    return {"status": "ok", "cash": rep["cash"], "loss": rep["loss"], "eur": round(rep["cash"] + rep["loss"], 4),
            "soc_end_pct": hs["soc_pct"][-1], "caps": rep["cap_steps"]}


def _reprice(ex: dict, day: date, tz: str, np_rows, tf: dict | None) -> dict:
    """Re-price a settled day at a GIVEN tariff instead of the prices the plans
    in force happened to carry (2026-09-24: the ledger is a shadow EMS, so
    it should read at the settings we believe are correct, not at whatever was
    live when each quarter hour was solved).

    virtual_day takes buy/sell per step from the archived plan that was in
    force at that step, so a day is priced in as many tariff frames as it had
    re-plans. Re-solving the plan of record cannot reach those. This rebuilds
    both lanes from the PUBLISHED day-ahead for the day and one tariff frame.
    Returns `ex` unchanged when the auction cannot be served for every step
    (a held or predicted price), so a day is never half re-priced."""
    if not tf:
        return ex
    n = len(ex.get("p_grid_w") or [])
    if not n:
        return ex
    try:
        da, n_pred = price_series(_midnight(day, tz), n, np_rows, None, None)
    except ValueError:
        return ex
    if n_pred:
        return ex
    buy, sell = tariff(da, tf["energy_tax"], tf["supplier_fee"], tf["btw_pct"], tf["feedin_fee"])
    return dict(ex, buy=list(buy), sell=list(sell))


def _run_actual(archive_dir: str, day: date, tz: str, act: dict, capacity_kwh: float, lam: float,
                soc0: float | None = None, np_rows=None, reprice: dict | None = None) -> dict:
    """What the controller actually did: the plans in force through the day,
    settled step by step through the virtual pack (the A/B tester's executed
    lane). NOT the plan of record replayed over the whole day: that lane sells
    the evening on the midnight plan and then starts the next day from the live
    pack, which the 13:13 re-plan had kept full for the morning peak - 14 kWh
    counted twice at the 09-07 seam, the whole of the 'actual beats every
    ceiling' anomaly of the first run."""
    t0 = _midnight(day, tz)
    ex = virtual_day(archive_dir, day, tz, t0 + timedelta(days=1), act["pv_w"], act.get("load_w"),
                     act.get("curtailed"), act.get("pv_peak_w"), act.get("micro_w"), capacity_kwh,
                     soc_start_pct=soc0)
    if ex is None:
        return {"status": "no_chain", "solves": 0}
    ex = _reprice(ex, day, tz, np_rows, reprice)
    for k in ("p_batt_w", "p_grid_w", "p_pv_w", "buy", "sell", "soc_pct"):
        if any(v is None for v in ex.get(k) or [None]):
            return {"status": "no_chain", "solves": 0}
    rep = replay_day(dict(ex, settled=True), act["grid_w"], act["batt_dc_w"], act["pv_w"], capacity_kwh, lam,
                     pv_pot_w=ex["p_pv_w"])
    if rep["status"] == "no_actuals":
        return {"status": "no_actuals", "solves": 0}
    return {"status": "ok", "cash": rep["cash"], "loss": rep["loss"], "eur": round(rep["cash"] + rep["loss"], 4),
            "soc_end_pct": ex["soc_pct"][-1], "soc_start_pct": ex["soc_start_pct"], "caps": rep["cap_steps"],
            "solves": 0, "step_eur": rep.get("step_eur"), "ex": ex}


def _run_rung(base_url: str, archive_dir: str, doc: dict, day: date, tz: str, soc0: float, exact_days: int,
              win: dict | None, act: dict, mic_day: list, np_rows, capacity_kwh: float, lam: float,
              timeout: int, source: str, exact_of: str = "both") -> dict:
    """One rung, two solves, in the box every rung shares.

    Midnight and 13:00, the same cadence for all three, because a rung that
    re-solves at the publication hour gets a second chance to correct a plan
    built on predicted prices and a rung that does not is being handicapped
    rather than informed. Only two things separate the rungs: how many days of
    PV and load are exact (`exact_days`), and how far the auction has cleared,
    which is the end of the same number of days at midnight and one day further
    from 12:45 for the rung that only knew D.
    """
    t0 = _midnight(day, tz)
    n_day = expected_steps(day, tz)
    n = _n_steps(day, tz, HORIZON_DAYS)
    n_exact = _n_steps(day, tz, exact_days)
    wall = t0 + timedelta(days=HORIZON_DAYS)
    if win is None or any(len(win.get(k) or []) != n_exact for k in ("pv_w", "grid_w", "batt_dc_w")):
        return {"status": "pending", "solves": 0}
    view = _box_view(archive_dir, doc, t0, n)
    if view is None or n < n_day:
        return {"status": "no_plan", "solves": 0}
    ex = _exact(win, doc, t0, n_exact)
    # `exact_of` keeps one half of the measured day and hands the other back to
    # the plan's forecast. The must-take Growatt follows the SUN: its output is
    # production, and a measured must-take under a forecast array would net a
    # negative curtailable half.
    if exact_of in ("both", "pv"):
        ex_pv, ex_mic = list(ex["pv"]), list(ex["mic"])
    else:
        ex_pv, ex_mic = list(view["pv"][:n_exact]), list(view["mic"][:n_exact])
    ex_load = list(ex["load"]) if exact_of in ("both", "load") else list(view["load"][:n_exact])
    ex = {"pv": ex_pv, "load": ex_load, "mic": ex_mic}
    pv = list(ex["pv"]) + view["pv"][n_exact:]
    load = list(ex["load"]) + view["load"][n_exact:]
    mic = list(ex["mic"]) + view["mic"][n_exact:]
    cut = [False] * n_exact + view["cut"][n_exact:]
    prices = _prices_to(doc, t0, n, np_rows, _midnight(day + timedelta(days=exact_days), tz), view)
    if prices is None:
        return {"status": "no_prices", "solves": 0}
    pay = _payload(doc, t0, n, soc0, pv, load, mic, cut, prices[0], prices[1])
    out = {"solves": 0}
    rows_a = _hindsight_solve(base_url, pay, timeout, t0, n, mic, out)
    out["solves"] += 1
    if rows_a is None:
        return {"status": out.get("status", "solve_failed"), "solves": out["solves"]}
    # 13:00: the newest plan the EMS actually ran before the publication hour
    # carries the forecast a controller had then; the plan of record if none
    t13 = datetime.combine(day, time(PUBLISH_HOUR), tzinfo=ZoneInfo(tz))
    i13 = step_index(t13, t0)
    doc_b = doc
    for cand in iter_organic_plans(archive_dir, since=t0, until=t13):
        if _parse_ts(cand["t0"]) <= t13 and _doc_end(cand) >= wall:
            doc_b = cand
            break
    n2 = n - i13
    view_b = _box_view(archive_dir, doc_b, t13, n2)
    if view_b is None:
        return {"status": "no_plan", "solves": out["solves"]}
    k_ex, k_day = n_exact - i13, n_day - i13
    pv_b = list(ex["pv"][i13:]) + view_b["pv"][k_ex:]
    load_b = list(ex["load"][i13:]) + view_b["load"][k_ex:]
    mic_b = list(ex["mic"][i13:]) + view_b["mic"][k_ex:]
    cut_b = [False] * k_ex + view_b["cut"][k_ex:]
    cleared = _midnight(day + timedelta(days=max(exact_days, 2)), tz)
    prices_b = _prices_to(doc_b, t13, n2, np_rows, cleared, view_b)
    if prices_b is None:
        return {"status": "no_prices", "solves": out["solves"]}
    soc13 = float(rows_a[i13 - 1]["SOC_opt"])
    pay_b = _payload(doc_b, t13, n2, soc13, pv_b, load_b, mic_b, cut_b, prices_b[0], prices_b[1])
    rows_b = _hindsight_solve(base_url, pay_b, timeout, t13, n2, mic_b, out)
    out["solves"] += 1
    if rows_b is None:
        return {"status": out.get("status", "solve_failed"), "solves": out["solves"]}
    res = _score(rows_a[:i13] + rows_b[:k_day], doc, day, tz, soc0, mic_day, act, capacity_kwh, lam, source)
    res["solves"] = out["solves"]
    return res


# ---- the walk ------------------------------------------------------------------

def _chain(prev: dict | None, rung: str) -> float | None:
    if prev and prev.get(f"{rung}_status") == "ok" and prev.get(f"{rung}_soc_end_pct") is not None:
        return float(prev[f"{rung}_soc_end_pct"])
    return None


def ladder_day(archive_dir: str, base_url: str, day_iso: str, tz: str, np_rows, inputs: dict, prev: dict | None,
               rungs=RUNGS, capacity_kwh: float = CAPACITY_KWH, lambda_frac: float = 0.9,
               timeout: int = 180, old: dict | None = None, reprice: dict | None = None) -> dict:
    """One day of the ladder. `inputs` = {"day": the day's actuals, "win1":
    window actuals to the plan's end or None, "win2": to the end of D+2 or None};
    `prev` is the previous day's row (the chain), `old` this day's existing row
    (rungs already ok are kept, not re-solved)."""
    day = date.fromisoformat(day_iso)
    row = {c: None for c in LADDER_COLUMNS}
    row.update(date=day_iso, flags="")
    if old:
        for r in RUNGS:
            for k in RUNG_KEYS:
                row[f"{r}_{k}"] = old.get(f"{r}_{k}")
        row["soc_start_pct"], row["lambda_eur_kwh"] = old.get("soc_start_pct"), old.get("lambda_eur_kwh")
    flags = []
    found = plan_for_day(archive_dir, day, tz)
    if not found:
        for r in rungs:
            row[f"{r}_status"] = "no_plan"
        row["flags"] = "no_plan"
        row["posted"] = False
        return row
    doc, sl = found
    plan = compact_slice(sl, doc)
    lam = lambda_for(plan["sell"], lambda_frac)
    soc_start = float(plan["soc_start_pct"])
    row.update(lambda_eur_kwh=round(lam, 5))
    act = (inputs or {}).get("day") or {}
    n_day = expected_steps(day, tz)
    have_day = all(len(act.get(k) or []) == n_day for k in ("pv_w", "load_w", "grid_w", "batt_dc_w"))
    mic_day = list(act.get("micro_w") or []) if len(act.get("micro_w") or []) == n_day else [0.0] * n_day
    day_in = _exact(act, doc, _midnight(day, tz), n_day) if have_day else None
    posted = False
    # the actual lane goes first: its midnight is the pack every other rung resets to
    order = [r for r in rungs if r == "actual"] + [r for r in rungs if r != "actual"]
    for r in order:
        if old and old.get(f"{r}_status") == "ok":
            if r == "actual" and old.get("soc_start_pct") is not None:
                soc_start = float(old["soc_start_pct"])
            continue                                   # settled once, chained forever
        if not have_day:
            res = {"status": "no_actuals", "solves": 0}
        elif r == "actual":
            # chained through its own settled pack; the live anchor only when the chain is broken
            p_end = _chain(prev, "actual")
            res = _run_actual(archive_dir, day, tz, act, capacity_kwh, lam, soc0=p_end,
                              np_rows=np_rows, reprice=reprice)
            if p_end is None:
                flags.append("actual_reset")
            if res.get("soc_start_pct") is not None:
                soc_start = float(res["soc_start_pct"])
                if abs(soc_start - float(plan["soc_start_pct"])) > 1.0:
                    flags.append("actual_seam")        # the live pack did not enter the day where the lane left it
            if res["status"] == "ok" and res.get("step_eur"):
                row["_hours"] = hour_sums(res["step_eur"], day, tz)    # ladder_run moves both to the sidecar
                row["_lanes"] = hour_lanes(res.get("ex") or {}, day, tz)
        else:
            soc0 = _chain(prev, r)
            if soc0 is None:
                soc0, _ = soc_start, flags.append(f"{r}_reset")
            exact_days, exact_of = KNOWLEDGE[r]
            win = act if exact_days == 1 else inputs.get("win1" if exact_days == 2 else "win2")
            res = _run_rung(base_url, archive_dir, doc, day, tz, soc0 / 100.0, exact_days, win, act,
                            mic_day, np_rows, capacity_kwh, lam, timeout, r, exact_of)
            posted = posted or bool(res.get("solves"))
        row[f"{r}_status"] = res["status"]
        row[f"{r}_solves"] = int(res.get("solves") or 0)
        for k in ("eur", "cash", "loss", "soc_end_pct"):
            row[f"{r}_{k}"] = res.get(k)
        if res.get("caps"):
            flags.append(f"{r}_caps")
    row["soc_start_pct"] = soc_start
    row["flags"] = ";".join(flags)
    row["posted"] = posted
    return row


def ladder_run(archive_dir: str, csv_path: str, base_url: str, days: list[str], tz: str, np_rows, inputs: dict,
               rungs=RUNGS, capacity_kwh: float = CAPACITY_KWH, lambda_frac: float = 0.9,
               timeout: int = 180, reprice: dict | None = None) -> dict:
    """Walk `days` oldest first, each rung chaining from its own previous day,
    and upsert the CSV as it goes. Returns the rows, whether the add-on was
    posted to (the caller re-plans if so), the solve count and the summary."""
    rows = read_ladder(csv_path)
    by_date = {r["date"]: r for r in rows}
    posted, solves, done = False, 0, []
    for d in sorted(days):
        prev_day = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        row = ladder_day(archive_dir, base_url, d, tz, np_rows, (inputs or {}).get(d) or {}, by_date.get(prev_day),
                         rungs, capacity_kwh, lambda_frac, timeout, old=by_date.get(d), reprice=reprice)
        posted = posted or row.pop("posted", False)
        solves += sum(int(row.get(f"{r}_solves") or 0) for r in rungs if not (by_date.get(d) or {}).get(f"{r}_status") == "ok")
        hours, lanes = row.pop("_hours", None), row.pop("_lanes", None)
        if hours:
            upsert_ladder_hours(hours_path(csv_path), d, hours, lanes)
        rows = upsert_ladder(csv_path, row)
        by_date = {r["date"]: r for r in rows}
        done.append(row)
    return {"rows": done, "all_rows": rows, "posted": posted, "solves": solves,
            "summary": ladder_summary(rows, capacity_kwh)}


def ladder_hours_fill(archive_dir: str, csv_path: str, days: list[str], tz: str, inputs: dict,
                      capacity_kwh: float = CAPACITY_KWH, lambda_frac: float = 0.9) -> dict:
    """Backfill the hourly sidecar for days whose actual lane already settled:
    the same replay, chained from the previous row's pack exactly as the walk
    did, written to ladder_hours.csv only. ladder.csv is not touched. Returns
    {day: {"hours": n, "eur": sum, "csv": the row's value}} so a caller can see
    the two agree; days without a row, an ok actual or actuals are skipped."""
    rows = read_ladder(csv_path)
    by_date = {r["date"]: r for r in rows}
    out = {}
    for d in sorted(days):
        row = by_date.get(d)
        if not row or row.get("actual_status") != "ok":
            out[d] = {"status": "no_row"}
            continue
        day = date.fromisoformat(d)
        act = ((inputs or {}).get(d) or {}).get("day") or {}
        n_day = expected_steps(day, tz)
        if not all(len(act.get(k) or []) == n_day for k in ("pv_w", "load_w", "grid_w", "batt_dc_w")):
            out[d] = {"status": "no_actuals"}
            continue
        found = plan_for_day(archive_dir, day, tz)
        if not found:
            out[d] = {"status": "no_plan"}
            continue
        lam = row.get("lambda_eur_kwh")
        if lam is None:
            lam = lambda_for(compact_slice(found[1], found[0])["sell"], lambda_frac)
        prev = by_date.get((day - timedelta(days=1)).isoformat())
        res = _run_actual(archive_dir, day, tz, act, capacity_kwh, float(lam), soc0=_chain(prev, "actual"))
        if res.get("status") != "ok" or not res.get("step_eur"):
            out[d] = {"status": res.get("status")}
            continue
        hours = hour_sums(res["step_eur"], day, tz)
        upsert_ladder_hours(hours_path(csv_path), d, hours, hour_lanes(res.get("ex") or {}, day, tz))
        out[d] = {"status": "ok", "hours": len(hours), "eur": round(sum(v for _, v in hours), 4),
                  "csv": row.get("actual_eur")}
    return out


# Flags that make a day's cash fiction rather than a result, so the day leaves
# every window sum. Only a CAP qualifies: a replay clipped at the grid limit did
# not run. A SEAM does NOT, though it did until 2026-09-11: it measures the
# virtual actual lane drifting from the live pack, which is the normal condition
# of a chained lane and the reason the actual rung chains its own pack at all.
# On a clean single walk it fires on four days in six. Excluding those days
# while the CHAIN still runs through them is worse than useless: 09-03's
# hindsight drained the pack and banked 2,5 EUR, 09-04 paid it back, and
# dropping only the first charges the payback to the survivor. A RESET stays
# too: every rung starts a reset day from the same midnight.
DISQUALIFYING = tuple(f"{r}_caps" for r in RUNGS)


def usable(row: dict) -> bool:
    return not (set(DISQUALIFYING) & set((row.get("flags") or "").split(";")))


def ladder_summary(rows: list[dict], capacity_kwh: float = CAPACITY_KWH) -> dict:
    """Window sums over the days EVERY rung has AND no flag disqualifies, the
    marginal value of each rung of knowledge, and the end-of-window pack of each
    rung priced against the actual lane at the last usable day's lambda.

    A window holds n USABLE days, not the last n calendar rows: the walk's tail
    is ragged (hindsight settles on D+1, omni2 on D+3), and
    anchoring at today would shorten every sum by the latency of its slowest
    rung. The frontier moves back instead, and the caller shows the date range.
    """
    def complete(r):
        return all(r.get(f"{x}_status") == "ok" and r.get(f"{x}_eur") is not None for x in CORE_RUNGS)

    def sums(sc):
        out = {"n": len(sc)}
        for x in RUNGS:
            vals = [r[f"{x}_eur"] for r in sc if r.get(f"{x}_eur") is not None]
            out[x] = round(sum(vals), 2) if sc and len(vals) == len(sc) else None
        def gap(a, b):
            return None if out.get(a) is None or out.get(b) is None else round(out[a] - out[b], 2)
        out["gap_hindsight"] = gap("actual", "hindsight")                      # the controller's shortfall
        out["gap_omni2"] = gap("hindsight", "omni2")                           # the two days after D exact, a day early
        return out

    ok = [r for r in rows if complete(r) and usable(r)]
    last = ok[-1] if ok else (rows[-1] if rows else {})
    lam = float(last.get("lambda_eur_kwh") or 0.0)
    a_end = last.get("actual_soc_end_pct")
    end = {}
    for x in RUNGS:
        e = last.get(f"{x}_soc_end_pct")
        end[f"{x}_soc_end_pct"] = e
        end[f"{x}_pack_value_eur"] = (round((float(e) - float(a_end)) / 100.0 * capacity_kwh * lam, 4)
                                      if e is not None and a_end is not None else None)
    return {"windows": {"d7": sums(ok[-7:]), "d30": sums(ok[-30:]), "all": sums(ok)},
            "end": end, "last_date": last.get("date"),
            "excluded": sum(1 for r in rows if complete(r) and not usable(r))}
