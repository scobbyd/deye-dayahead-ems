"""The plan archive: write, list, the cached heads, the organic iterator, the day slice, the plan selectors
(each a different question), and what the organic chain says the planner's state is."""
from __future__ import annotations

import gzip
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .grid import expected_steps, _parse_ts, STEP_MIN
from .objective import REBALANCE_FULL_LEVEL


# Documents are gzipped on write (2026-09-07): 134 KB -> 17 KB per plan, which at
# the 30-minute cadence is 0,34 GB a year instead of 2,6, and every HA backup
# carries the archive. gzip's 32 KB window cannot see that consecutive plans
# repeat each other, so a day bundle under gzip gains nothing more; a long-window
# compressor (xz) on a finished day does (4x again) and is a separate step.
# Legacy .json documents stay readable; list_plans orders by the stem so the two
# suffixes interleave correctly.
ARCHIVE_SUFFIX = ".json.gz"


def _plan_stem(name: str) -> str:
    for suf in (".json.gz", ".json"):
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def write_plan_archive(archive_dir: str, plan_ts_local: datetime, doc: dict) -> str:
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, plan_ts_local.strftime("%Y%m%dT%H%M%S") + ARCHIVE_SUFFIX)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


# A plan's horizon reaches at most 24:00 of D+2, under 72 h past its plan_ts, so
# no plan older than this can hold a row for the window a caller is rebuilding.
# The bound is what keeps the archive scans flat as the archive grows: at the
# 30-minute cadence (2026-09-07) the archive takes ~54 documents a day, and the
# reconstruction used to parse EVERY document on every call.
ARCHIVE_REACH = timedelta(days=3)


def iter_organic_plans(archive_dir: str, since: datetime | None = None,
                       until: datetime | None = None):
    """Every Optimal plan the EMS ACTUALLY RAN, newest plan_ts first, opened one
    at a time so a caller that returns early never loads the rest. `since`
    bounds the scan: heads are sorted newest first, so the walk stops at the
    first plan_ts before it and nothing older is even opened; `until` skips the
    heads newer than a cutoff without opening them.

    Two things separate this from `reversed(list_plans(...))`, and both bite the
    reconstruction path (found live 2026-09-04, and visible on the board: the
    whole of yesterday's virtual lane was being served by a single plan stamped
    two days earlier, with all 21 of that day's hourly re-plans passed over):

      - ORDER. list_plans sorts by FILENAME, and replay_plan deliberately breaks
        the correspondence between filename and plan_ts: it archives a re-solve
        under today's name while keeping the source plan's own pre-midnight
        plan_ts, so plan_for_day picks it as the record. Any first-match-wins
        scan that reads filename order AS plan_ts order therefore hands a replay
        doc every step it happens to cover, before a newer genuine plan is ever
        reached.
      - REPLAYS. A replay is a counterfactual: what the current settings WOULD
        have done. It was never in force at any past wall-clock step, and its
        horizon can reach days the replay never even scored - the two live
        replay docs of 09-02 carry 96 steps of 09-04 with them. virtual_day and
        virtual_soc_at ask "what was running then", so they must not see one.

    Scoring is a different question with a different answer and keeps its own
    selectors: plan_for_day and newest_plan_for_day rank by plan_ts with the
    FILE breaking a tie, which is how a replay becomes the plan of record for
    its own day on purpose, without reaching into the day after it. Both orders
    come off the one sort in plan_heads; this view only drops the replays on top
    of it, so the two cannot drift apart."""
    for path, head in plan_heads(archive_dir):
        pts = datetime.fromisoformat(head["plan_ts"])
        if until is not None and pts > until:
            continue
        if since is not None and pts < since:
            break
        if not head["replay"] and head["optim_status"] == "Optimal":
            yield load_plan(path)


def organic_plans(archive_dir: str, since: datetime | None = None) -> list[dict]:
    """iter_organic_plans as a list, for callers that need every document."""
    return list(iter_organic_plans(archive_dir, since))


_HEAD_CACHE: dict = {}     # path -> (mtime, head); archived plans are immutable


def plan_heads(archive_dir: str) -> list[tuple[str, dict]]:
    """(path, head) for every archived plan, newest PLAN_TS first, the filename
    breaking a tie. `head` carries plan_ts, optim_status and replay only, which
    is everything a selector needs to ORDER and FILTER; the rows stay on disk so
    a caller loads just the candidates it actually tests.

    Ordering by plan_ts rather than filename is the point. list_plans sorts by
    FILENAME, and replay_plan deliberately breaks the correspondence: it
    archives a re-solve under today's name while keeping the source plan's own
    pre-midnight plan_ts, so the replay becomes the record for the day it
    replays. The tie-break keeps a replay ahead of the plan it re-solved, which
    is what the harness is for, while a genuinely newer plan still outranks it.

    Heads are cached on (path, mtime) because the scans repeat: days_since_full
    walks the archive thirty times inside a single solve, and every scan used to
    re-read and re-parse every plan document in full."""
    out = []
    for path in list_plans(archive_dir):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        hit = _HEAD_CACHE.get(path)
        if hit is None or hit[0] != mtime:
            doc = load_plan(path)
            hit = (mtime, {"plan_ts": doc.get("plan_ts"), "optim_status": doc.get("optim_status"),
                           "replay": bool(doc.get("replay"))})
            _HEAD_CACHE[path] = hit
        if hit[1]["plan_ts"]:
            out.append((path, hit[1]))
    out.sort(key=lambda ph: (datetime.fromisoformat(ph[1]["plan_ts"]), ph[0]), reverse=True)
    return out


def list_plans(archive_dir: str) -> list[str]:
    """Every archived document, oldest first by filename stem (.json and .json.gz alike)."""
    if not os.path.isdir(archive_dir):
        return []
    names = [f for f in os.listdir(archive_dir) if f.endswith(".json") or f.endswith(".json.gz")]
    return [os.path.join(archive_dir, f) for f in sorted(names, key=_plan_stem)]


def load_plan(path: str) -> dict:
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path) as f:
        return json.load(f)


def day_slice(rows: list[dict], day: date, tz: str, soc_init: float) -> dict | None:
    """Rows whose local date is `day`, plus the SOC the pack enters the day with:
    EMHASS's SOC_opt is the state AFTER a step, so the day starts at the SOC of
    the last step before it, or at soc_init when the horizon starts inside the day."""
    z = ZoneInfo(tz)
    sel, prev_soc = [], None
    for r in rows:
        t = _parse_ts(r["timestamp"]).astimezone(z)
        if t.date() < day:
            prev_soc = float(r["SOC_opt"])
        elif t.date() == day:
            sel.append((t, r))
    if not sel:
        return None
    soc_start = prev_soc if prev_soc is not None else float(soc_init)
    return {"date": day.isoformat(), "slice_start": sel[0][0].isoformat(), "n": len(sel),
            "soc_start_pct": round(soc_start * 100, 2), "rows": [r for _, r in sel]}


def plan_for_day(archive_dir: str, day: date, tz: str):
    """Newest archived Optimal plan made before 00:00 local of `day` that covers
    the whole of `day`, ranked by PLAN_TS and not by filename. Returns
    (doc, slice) or None. Bounded by ARCHIVE_REACH like every other archive
    walk (2026-09-07): a day nothing covers used to open every older document.

    The ordering is the whole point (found live 2026-09-05, and visible on the
    board: 09-04 was scored against a plan solved 25 h before the day began,
    with load_planned_kwh 8,97 on both 09-03 and 09-04 because it was one
    document sliced twice). A replay doc carries the source plan's pre-midnight
    plan_ts under a much later filename, AND its 195-step horizon reaches a full
    day past the one it replays, so in filename order it was reached first and
    handed the following day as well - two days of stale forecasts, with the
    horizon-end SOC pin landing inside the scored day instead of beyond it.
    plan_ts order keeps a replay as the record for its OWN day (same plan_ts,
    later file) and stops it outranking the genuine day-ahead plan for the next
    one. See plans_by_plan_ts and organic_plans for the sibling case."""
    midnight = datetime.combine(day, time(0), tzinfo=ZoneInfo(tz))
    want = expected_steps(day, tz)
    for path, head in plan_heads(archive_dir):
        if head["optim_status"] != "Optimal":
            continue
        pts = datetime.fromisoformat(head["plan_ts"])
        if pts >= midnight:
            continue
        if pts < midnight - ARCHIVE_REACH:            # nothing older can hold a row on `day`
            break
        doc = load_plan(path)
        sl = day_slice(doc["rows"], day, tz, doc["soc_init"])
        if sl and sl["n"] == want:
            return doc, sl
    return None


def newest_plan_for_day(archive_dir: str, day: date, tz: str):
    """Newest archived Optimal plan whose day_slice covers the whole of `day`,
    with no restriction on when it was made. plan_for_day freezes the plan of
    record before the day starts, which is right for scoring but wrong for a
    display that wants the latest re-plan; this is that display's lookup for
    the NEXT day, where a plan made today can legitimately already cover the
    whole of tomorrow. Ranked by plan_ts like plan_for_day, so a replay doc's
    old plan_ts cannot pass for the newest re-plan. Returns (doc, slice) or None.

    Not scoring-only: rolled_slices takes the next_day display slice from here,
    which is the forward half of the Shadow EMS plan charts. Bounded by
    ARCHIVE_REACH from the END of `day` (2026-09-07)."""
    want = expected_steps(day, tz)
    day_end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=ZoneInfo(tz))
    for path, head in plan_heads(archive_dir):
        if head["optim_status"] != "Optimal":
            continue
        if datetime.fromisoformat(head["plan_ts"]) < day_end - ARCHIVE_REACH:   # nothing older reaches `day`
            break
        doc = load_plan(path)
        sl = day_slice(doc["rows"], day, tz, doc["soc_init"])
        if sl and sl["n"] == want:
            return doc, sl
    return None


def original_plan_for_day(archive_dir: str, day: date, tz: str):
    """Like plan_for_day but skips replay docs: the newest ORGANIC Optimal plan
    made before day's midnight that covers the whole day. This is the source
    'shoes' a replay re-solves from, so re-running a day always feeds on the
    genuine original plan, never on a previous replay of itself (idempotent)."""
    z = ZoneInfo(tz)
    midnight = datetime.combine(day, time(0), tzinfo=z)
    want = expected_steps(day, tz)
    for path, head in plan_heads(archive_dir):
        if head["replay"] or head["optim_status"] != "Optimal":
            continue
        pts = datetime.fromisoformat(head["plan_ts"])
        if pts >= midnight:
            continue
        if pts < midnight - ARCHIVE_REACH:
            break
        doc = load_plan(path)
        sl = day_slice(doc["rows"], day, tz, doc["soc_init"])
        if sl and sl["n"] == want:
            return doc
    return None


def virtual_soc_at(archive_dir: str, t0: datetime):
    """SOC of the free-running virtual pack at t0 (a grid-aligned instant):
    the newest archived Optimal plan covering t0, evaluated there. SOC_opt is
    the state AFTER a step, so the value at t0 is SOC_opt of the step that
    ends at t0, or the plan's own soc_init when t0 is its first step.
    Returns (fraction, source plan_ts) or (None, None) when nothing covers t0,
    which means the chain is broken and the caller re-anchors on the real pack."""
    target = t0.astimezone(timezone.utc)
    prev_start = target - timedelta(minutes=STEP_MIN)
    # A plan made after t0 cannot hold a row at t0 (its first row is at the ceil
    # of its own plan_ts), so nothing newer is opened.
    for doc in iter_organic_plans(archive_dir, since=target - ARCHIVE_REACH, until=target):
        rows = doc["rows"]
        if rows and _parse_ts(rows[0]["timestamp"]) == target:
            return float(doc["soc_init"]), doc["plan_ts"]
        for r in rows:
            if _parse_ts(r["timestamp"]) == prev_start:
                return float(r["SOC_opt"]), doc["plan_ts"]
    return None, None


def aux_cut_state(archive_dir: str, now: datetime, tz: str) -> bool:
    """Whether the newest organic plan made TODAY has the Growatt cut. Nothing
    made today (first solve after midnight) means not cut."""
    z = ZoneInfo(tz)
    midnight = datetime.combine(now.astimezone(z).date(), time(0), tzinfo=z)
    for doc in iter_organic_plans(archive_dir, since=midnight, until=now):
        return bool((doc.get("aux_cut") or {}).get("active"))
    return False


def days_since_full(archive_dir: str, now: datetime, tz: str,
                    lookback: int = 30, level: float = REBALANCE_FULL_LEVEL):
    """Days since the virtual pack last reached `level`, per the plan-of-record
    chain (today: the newest Optimal plan covering the past part of today;
    earlier days: plan_for_day). None when nothing in the window qualifies."""
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    # The ORGANIC chain, newest plan_ts first (2026-09-05): this asks what the
    # pack actually did, so a replay - a counterfactual that was never in force,
    # archived under a much later filename than its plan_ts - must not be the
    # plan that decides today, and a filename-order scan handed it exactly that.
    # Same defect as plan_for_day's, but it fails quietly into the rebalance
    # schedule instead of onto a chart, and a wrong answer here has a physical
    # consequence for the pack rather than a cosmetic one.
    # Heads once, documents on demand (2026-09-07): the walk used to parse every
    # archived plan up front, thirty days of them, on every solve. A day is
    # decided by the newest covering plan, which is nearly always the first
    # candidate opened, so loading lazily through the cached heads makes the
    # walk cost a handful of reads however large the archive grows.
    heads = [(path, datetime.fromisoformat(h["plan_ts"]))
             for path, h in plan_heads(archive_dir)
             if not h["replay"] and h["optim_status"] == "Optimal"]
    loaded: dict[str, dict] = {}

    def doc_at(path):
        if path not in loaded:
            loaded[path] = load_plan(path)
        return loaded[path]

    for path, pts in heads:
        if pts > now:
            continue
        if pts < now - ARCHIVE_REACH:
            break
        doc = doc_at(path)
        sl = day_slice(doc["rows"], local.date(), tz, doc["soc_init"])
        if sl:
            past = [r for r in sl["rows"] if _parse_ts(r["timestamp"]) <= now]
            if any(float(r["SOC_opt"]) >= level for r in past):
                return 0
            break                                        # newest covering plan decides today
    for d in range(1, lookback + 1):
        day = local.date() - timedelta(days=d)
        midnight = datetime.combine(day, time(0), tzinfo=z)
        want = expected_steps(day, tz)
        for path, pts in heads:                          # plan_for_day's rule, organic chain only
            if pts >= midnight:
                continue
            if pts < midnight - ARCHIVE_REACH:
                break
            doc = doc_at(path)
            sl = day_slice(doc["rows"], day, tz, doc["soc_init"])
            if sl and sl["n"] == want:
                if any(float(r["SOC_opt"]) >= level for r in sl["rows"]):
                    return d
                break
    return None
