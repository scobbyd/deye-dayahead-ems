"""The closed replay walk: the live shadow EMS re-run offline, tick by tick.

An offline twin of ha/pyscript/emhass_shadow.py::_plan_day. Every tick builds the
same `inp` dict the wrapper builds from HA (replay_inputs, pricefill) and
calls the unmodified emhasscore.planning.run_plan with the library solver
patched in. run_plan archives its plan doc into this run's plans/ dir, so
days_since_full, the aux-cut hysteresis, virtual_day and rolled_slices read
the archive they read live.

The virtual pack is emhasscore.slices.virtual_day over that archive, as live:
its soc_now_pct at the tick is the next solve's soc_init. Two rules are
stricter than live, on the closed-simulation ruling:
  - the SOC is seeded ONCE, from the frame at the first tick; a later tick
    without a settled SOC aborts the walk instead of re-anchoring on the meter;
  - the day boundary chains settled to settled: virtual_day gets soc_start_pct
    = the previous local day's settled end SOC (state.json), the way the
    ladder's actual rung chains, instead of the archive's midnight anchor
    (the last plan's predicted SOC at 00:00).

Per local day, after its last tick, the day is settled once more against the
full frame day (now = the next midnight) and stored as one parquet file: the
compact slice plus pv_fc_w, load_fc_w, pv_meas_w. That is the "realised" lane
for replay_ladder and the viewer.

Resumable: a tick whose plan doc exists is skipped, a day whose parquet exists
is not re-settled, and state.json carries the chain.

Two rules make a broken chain LOUD instead of silently re-anchoring:
  - the seed is keyed on state.json's own seed_tick, not on whether the
    archive directory happens to be empty. A failed first solve (nothing
    archived yet) would otherwise look "not seeded" to the next tick too,
    re-seeding a second time from the frame at a different instant;
  - once a day has a settled predecessor (any day after `start`), a tick or a
    day-end settle with no recorded soc_end_pct for the day before it raises
    instead of letting virtual_day fall back to the archive's own midnight
    anchor - a real chain break should stop the walk, not be quietly patched
    over with a different (and less trustworthy) SOC source.

Knobs are frozen at the run's first start (meta.json), not re-read on every
resume: ri.live_knobs() reads the live mirror, which can drift between a
walk's first run and a later resume of the same run_dir, and a walk must
solve every tick of a run under the one knob set it started with.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from backtest import hardware as hwm
from backtest import pricefill as pf
from backtest import replay_inputs as ri
from emhasscore.archive import ARCHIVE_SUFFIX
from emhasscore.grid import _slot

SLICE_KEYS = ["p_batt_w", "p_grid_w", "soc_pct", "p_pv_w", "pv_curtail_w", "p_load_w", "pv_fc_w", "load_fc_w",
              "pv_meas_w", "pv_micro_w", "micro_cut_w", "buy", "sell", "cost_eur"]
DAY_KEYS = ["soc_start_pct", "soc_now_pct", "clamp_writes", "dwell_95_h", "n_past"]
BASE_URL = "http://library"
MAX_CONSECUTIVE_FAILURES = 10


def plan_path(archive: str, tick: datetime) -> str:
    """Where write_plan_archive puts the doc for a solve at `tick`."""
    return os.path.join(archive, tick.strftime("%Y%m%dT%H%M%S") + ARCHIVE_SUFFIX)


def slice_frame(day: dict) -> pd.DataFrame:
    """A settled virtual day as a long frame: one row per quarter."""
    n = int(day["n"])
    start = datetime.fromisoformat(day["slice_start"]).astimezone(timezone.utc)
    ts = [start + timedelta(minutes=int(day.get("step_min", 15)) * i) for i in range(n)]
    cols = {"date": [day["date"]] * n, "ts_utc": pd.to_datetime(ts, utc=True)}
    for k in SLICE_KEYS:
        v = day.get(k) or [None] * n
        cols[k] = [None if x is None else float(x) for x in v]
    for k in DAY_KEYS:
        cols[k] = [day.get(k)] * n
    return pd.DataFrame(cols)


def load_slices(run_dir: str) -> pd.DataFrame:
    """Every settled day in the run, oldest first."""
    d = os.path.join(run_dir, "slices")
    files = sorted(f for f in os.listdir(d) if f.endswith(".parquet")) if os.path.isdir(d) else []
    if not files:
        return pd.DataFrame(columns=["date", "ts_utc"] + SLICE_KEYS + DAY_KEYS)
    return pd.concat([pd.read_parquet(os.path.join(d, f)) for f in files], ignore_index=True)


class Walk:
    def __init__(self, mode: str, start: date, end: date, run_dir: str | None = None, solver=None,
                 tz: str = ri.TZ, frame: pd.DataFrame | None = None, frac: dict | None = None,
                 knobs: dict | None = None, da: dict | None = None, knowledge: str = "actual",
                 tariff: dict | None = None, pv_scale: float | None = None, hardware: dict | None = None,
                 frame_csv: str | None = None, tick_min: int | None = None):
        if mode not in pf.MODES:
            raise ValueError(f"mode {mode!r} not in {pf.MODES}")
        if knowledge not in ri.KNOWLEDGE:
            raise ValueError(f"knowledge {knowledge!r} not in {tuple(ri.KNOWLEDGE)}")
        self.mode, self.start, self.end, self.tz = mode, start, end, tz
        self.knowledge = knowledge
        self.z = ZoneInfo(tz)
        self.run_dir = run_dir or os.path.join(ri.REPLAY_DIR, mode if knowledge == "actual" else f"{mode}_{knowledge}")
        self.archive = os.path.join(self.run_dir, "plans")
        self.slices_dir = os.path.join(self.run_dir, "slices")
        for d in (self.archive, self.slices_dir):
            os.makedirs(d, exist_ok=True)
        self.solver = solver
        # the plant's size is frozen with the knobs and the tariff (1.0 = as built)
        self.pv_scale = float(pv_scale) if pv_scale is not None else self._resume_pv_scale()
        # the frame file is frozen too: a pretend world (a modelled frame) resumes on its own file
        self.frame_csv = frame_csv if frame_csv is not None else self._resume_frame()
        self.df = ri.scale_pv(frame if frame is not None else ri.load_frame(ri.frame_path(self.frame_csv)), self.pv_scale)
        self.frac = frac if frac is not None else ri.ese_fraction()
        # the storage hardware (pack kWh, inverter kW) is frozen with the knobs; see backtest/hardware.py
        self.hw = hwm.normalize(hardware) if hardware is not None else self._resume_hardware()
        hwm.apply(self.hw)
        self.knobs = hwm.knobs_for(knobs if knobs is not None else self._resume_knobs(), self.hw)
        # the tariff is frozen with the knobs: a resume prices every tick as the run began
        self.tariff = tariff if tariff is not None else self._resume_tariff()
        # the solve cadence is frozen too: a resume must walk the same schedule
        self.tick_min = int(tick_min) if tick_min is not None else self._resume_tick_min()
        self.da = da if da is not None else pf.load_da()
        self.asof = pf.load_asof() if mode == "asof" else None
        self.state = self._load_state()
        self.failures = 0
        self.log = open(os.path.join(self.run_dir, "walk.log"), "a", buffering=1)

    def close(self) -> None:
        """Release the log handle. Idempotent."""
        if not self.log.closed:
            self.log.close()

    # ---- state ----------------------------------------------------------------------

    def _load_state(self) -> dict:
        p = os.path.join(self.run_dir, "state.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return {"soc_end_pct": {}, "seed_soc_pct": None, "seed_tick": None, "rebalance": None}

    def _save_state(self) -> None:
        p = os.path.join(self.run_dir, "state.json")
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, p)

    def _read_meta(self) -> dict | None:
        p = os.path.join(self.run_dir, "meta.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return None

    def _resume_knobs(self) -> dict:
        """The knobs frozen in this run's meta.json, if this run_dir already
        has one; else the live mirror's current knobs. A resumed walk solves
        every tick under the knob set it started with, not whatever the live
        mirror holds by the time it is resumed."""
        meta = self._read_meta()
        if meta and meta.get("knobs"):
            return meta["knobs"]
        return ri.live_knobs()

    def _resume_hardware(self) -> dict:
        meta = self._read_meta()
        return hwm.normalize(meta.get("hardware") if meta else None)

    def _resume_frame(self) -> str | None:
        meta = self._read_meta()
        return meta.get("frame") if meta else None

    def _resume_pv_scale(self) -> float:
        meta = self._read_meta()
        return float(meta.get("pv_scale", 1.0)) if meta else 1.0

    def _resume_tick_min(self) -> int:
        meta = self._read_meta()
        return int(meta.get("tick_min", 30)) if meta else 30

    def _resume_tariff(self) -> dict:
        meta = self._read_meta()
        if meta and meta.get("tariff"):
            return dict(meta["tariff"])
        return dict(ri.TARIFF)

    def _write_meta(self) -> None:
        """Write meta.json once, on this run_dir's first start; a resume only
        appends to resumed_utc. knobs/tariff/started_utc are the frozen record
        of what the run began with and must never be overwritten in place."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = self._read_meta()
        if existing is not None:
            existing.setdefault("resumed_utc", []).append(now_iso)
            meta = existing
        else:
            try:
                import importlib.metadata as md
                ver = md.version("emhass")
            except Exception:
                ver = None
            meta = {"mode": self.mode, "knowledge": self.knowledge, "start": self.start.isoformat(), "end": self.end.isoformat(), "tz": self.tz,
                    "tariff": dict(self.tariff), "pv_scale": self.pv_scale, "hardware": dict(self.hw), "frame": self.frame_csv,
                    "knobs": self.knobs, "tick_min": self.tick_min, "emhass_version": ver,
                    "started_utc": now_iso, "resumed_utc": []}
        p = os.path.join(self.run_dir, "meta.json")
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f, indent=1)
        os.replace(tmp, p)

    def _log(self, msg: str) -> None:
        self.log.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")

    # ---- one tick -------------------------------------------------------------------

    def tick_inputs(self, tick: datetime, soc_pct: float, soc_source: str) -> dict:
        """The wrapper's inp dict for one tick, from the frame."""
        local = tick.astimezone(self.z)
        today = local.date()
        slot = _slot(local)
        act = ri.actuals(self.df, today, self.tz, gap_upto=slot)
        prev = ri.actuals(self.df, today - timedelta(days=1), self.tz)
        inp = {"now": tick.isoformat(), "tz": self.tz, "base_url": BASE_URL, "archive_dir": self.archive,
               "dry_run": False,
               "np_today": pf.nordpool_rows(today, self.da, self.tz),
               "np_tomorrow": pf.nordpool_rows(today + timedelta(days=1), self.da, self.tz)
               if local.hour >= pf.PUBLISH_HOUR else None,
               "epex": pf.epex_forecast(tick, self.mode, self.da, self.tz, asof=self.asof),
               "soc_pct": float(soc_pct), "soc_source": soc_source, "tariff": dict(self.tariff),
               "knobs": dict(self.knobs)}
        kn = ri.KNOWLEDGE[self.knowledge]
        inp.update(ri.solcast_inputs(self.df, self.frac, today, float(self.knobs["growatt_share"]), self.tz,
                                     exact_days=kn["pv_exact_days"]))
        inp["actual_pv_w"], inp["actual_load_w"] = act["pv_w"], act["load_w"]
        inp["curtailed"], inp["pv_peak_w"], inp["micro_w"] = act["curtailed"], act["pv_peak_w"], act["micro_w"]
        inp["prev_pv_w"], inp["prev_load_w"] = prev["pv_w"], prev["load_w"]
        inp["prev_curtailed"], inp["prev_pv_peak_w"], inp["prev_micro_w"] = prev["curtailed"], prev["pv_peak_w"], prev["micro_w"]
        # exact load = today's own 96 quarters as the reference: the same-clock
        # median of identical days is that day. Two keys, because load_series
        # wants LOAD_MIN_REF_DAYS (2) days or it hands the load to EMHASS's own
        # method, which needs HA.
        if kn["load_exact"]:
            vals = [round(float(v), 1) for v in ri.day_frame(self.df, today, self.tz)["load_w"]]
            inp["load_days"] = {today: vals, today - timedelta(days=1): list(vals)}
        else:
            inp["load_days"] = ri.load_days(self.df, today, self.tz)
        if kn["load_exact_days"]:
            inp["load_exact_w"] = ri.load_exact_w(self.df, today, kn["load_exact_days"], self.tz)
        inp["load_now_w"] = ri.load_now_w(self.df, tick)
        return inp

    def _settled_soc(self, tick: datetime) -> float | None:
        """virtual_day's soc_now_pct at the tick, chained from yesterday's settled end.

        Once the walk is past its first day, the previous day MUST already be
        settled (run() settles every day it crosses at midnight); a missing
        link there is a broken chain, not a case for virtual_day's own
        archive-midnight fallback to quietly paper over."""
        from emhasscore.slices import virtual_day
        local = tick.astimezone(self.z)
        today = local.date()
        act = ri.actuals(self.df, today, self.tz, gap_upto=_slot(local))
        prev_end = self.state["soc_end_pct"].get((today - timedelta(days=1)).isoformat())
        if prev_end is None and today > self.start:
            raise RuntimeError(f"no settled SOC for {today - timedelta(days=1)}: "
                                f"chain broken before {tick.isoformat()}")
        vd = virtual_day(self.archive, today, self.tz, tick, act["pv_w"], act["load_w"], act["curtailed"],
                         act["pv_peak_w"], act["micro_w"], capacity_kwh=self.hw["capacity_kwh"], soc_start_pct=prev_end)
        if not vd:
            return None
        self._fold_rebalance(vd)
        return vd.get("soc_now_pct")

    def _fold_rebalance(self, vd: dict) -> None:
        """The settled rebalancing clock (emhasscore.rebalance), as the wrapper
        keeps it in /config/emhass/rebalance.json: every settled slice is folded
        in, the state travels in state.json, and run_plan prices the SOC knobs
        off it. A walk starts with nothing on record, which reads as overdue."""
        from emhasscore import rebalance
        if vd.get("soc_pct") is None or not vd.get("slice_start"):
            return
        dwell = self.knobs.get("rebalance_dwell_h")
        self.state["rebalance"] = rebalance.update(self.state.get("rebalance"), vd["slice_start"], vd["soc_pct"],
                                                   int(vd.get("n_past") or 0),
                                                   dwell_h=float(dwell) if dwell is not None else rebalance.REBALANCE_DWELL_H)

    def run_tick(self, tick: datetime) -> dict:
        """One solve. Returns run_plan's result plus soc_pct / soc_source /
        skipped / tick."""
        from backtest.solver import patch_solve
        from emhasscore import planning
        if os.path.exists(plan_path(self.archive, tick)):
            return {"tick": tick.isoformat(), "skipped": True, "ok": True}
        first = self.state.get("seed_tick") is None
        if first:
            soc, src = ri.soc_at(self.df, tick), "real"
            self.state["seed_soc_pct"], self.state["seed_tick"] = soc, tick.isoformat()
            self._save_state()
        else:
            soc, src = self._settled_soc(tick), "settled"
            if soc is None:
                self._log(f"ABORT {tick.isoformat()} chain broken: no settled SOC")
                raise RuntimeError(f"virtual chain broken at {tick.isoformat()}")
        inp = self.tick_inputs(tick, soc, src)
        inp["rebalance"] = self.state.get("rebalance")
        t = time.monotonic()
        if self.solver is not None:
            self.solver.now = tick
        with patch_solve(planning.run_plan, self.solver):
            res = planning.run_plan(inp)
        res.update(tick=tick.isoformat(), skipped=False, soc_pct=float(soc), soc_source=src)
        self.failures = 0 if res.get("ok") else self.failures + 1
        self._log(f"{tick.isoformat()} src={src} soc={soc:.2f} status={res.get('optim_status')} "
                  f"pred={res.get('n_predicted_steps')} cost={res.get('cost_eur')} "
                  f"aux_cut={res.get('aux_cut_active')} s={time.monotonic() - t:.1f} ok={res.get('ok')} "
                  f"{'' if res.get('ok') else res.get('message')}")
        if self.failures >= MAX_CONSECUTIVE_FAILURES:
            raise RuntimeError(f"{self.failures} consecutive failed solves, last at {tick.isoformat()}")
        return res

    # ---- one day --------------------------------------------------------------------

    def settle_day(self, day: date) -> dict:
        """The finished local day through virtual_day against the full frame
        day; state saved before the parquet is written, so a crash between
        the two leaves the link intact and only the parquet to re-derive
        (run()'s _ensure_day_settled re-settles when the parquet is missing).

        Same chain-break rule as _settled_soc: past the walk's first day, the
        previous day must already have a settled end."""
        from emhasscore.slices import virtual_day
        act = ri.actuals(self.df, day, self.tz)
        midnight_next = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=self.z)
        prev_end = self.state["soc_end_pct"].get((day - timedelta(days=1)).isoformat())
        if prev_end is None and day > self.start:
            raise RuntimeError(f"no settled SOC for {day - timedelta(days=1)}: cannot settle {day}")
        vd = virtual_day(self.archive, day, self.tz, midnight_next, act["pv_w"], act["load_w"], act["curtailed"],
                         act["pv_peak_w"], act["micro_w"], capacity_kwh=self.hw["capacity_kwh"], soc_start_pct=prev_end)
        if not vd or vd.get("soc_now_pct") is None:
            raise RuntimeError(f"cannot settle {day}: virtual chain broken")
        self.state["soc_end_pct"][day.isoformat()] = float(vd["soc_now_pct"])
        self._fold_rebalance(vd)                      # the day's last quarters, which no tick saw
        self._save_state()
        path = os.path.join(self.slices_dir, f"{day.isoformat()}.parquet")
        tmp = path + ".tmp"
        slice_frame(vd).to_parquet(tmp, index=False)
        os.replace(tmp, path)
        self._log(f"SETTLED {day} soc_start={vd['soc_start_pct']} soc_end={vd['soc_now_pct']} "
                  f"clamp_writes={vd.get('clamp_writes')} dwell95={vd.get('dwell_95_h')}")
        return vd

    def _ensure_day_settled(self, day: date) -> None:
        """settle_day(day), unless its parquet already exists. A parquet with
        no matching state link (an interrupted older run, or a state.json
        rebuilt from scratch) is backfilled from the parquet's own
        soc_now_pct column rather than silently left unlinked or re-derived."""
        path = os.path.join(self.slices_dir, f"{day.isoformat()}.parquet")
        if not os.path.exists(path):
            self.settle_day(day)
            return
        if day.isoformat() not in self.state["soc_end_pct"]:
            soc_now = pd.read_parquet(path, columns=["soc_now_pct"])["soc_now_pct"].iloc[0]
            self.state["soc_end_pct"][day.isoformat()] = float(soc_now)
            self._save_state()

    # ---- the walk -------------------------------------------------------------------

    def run(self, max_ticks: int | None = None) -> dict:
        if self.solver is None:
            from backtest.solver import LibrarySolver
            self.solver = LibrarySolver(data_dir=os.path.join(self.run_dir, "emhass_data"))
        hwm.patch_solver(self.solver, self.hw)
        self._write_meta()
        schedule = ri.ticks(self.start, self.end, self.tz, self.tick_min)
        done, solved, last_day = 0, 0, None
        for tick in schedule:
            local_day = tick.astimezone(self.z).date()
            # crossing midnight: settle the day that just finished
            if last_day is not None and local_day > last_day and last_day >= self.start:
                self._ensure_day_settled(last_day)
            last_day = local_day
            res = self.run_tick(tick)
            done += 1
            solved += 0 if res.get("skipped") else 1
            if max_ticks is not None and solved >= max_ticks:
                break
        else:
            # the schedule's last tick is 00:13 of end + 1, so `end` is finished
            self._ensure_day_settled(self.end)
            load_slices(self.run_dir).to_parquet(os.path.join(self.run_dir, "slices.parquet"), index=False)
        self._log(f"DONE ticks={done} solved={solved}")
        return {"ticks": done, "solved": solved, "run_dir": self.run_dir}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the closed replay walk")
    ap.add_argument("--mode", required=True, choices=pf.MODES)
    ap.add_argument("--start", required=True, help="first local day whose settled slice is kept (ISO)")
    ap.add_argument("--end", required=True, help="last local day walked (inclusive)")
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--knowledge", default="actual", choices=tuple(ri.KNOWLEDGE),
                    help="what the walk knows exactly: actual (forecasts), hindsight, hindsight_pv, hindsight_load, omni2")
    ap.add_argument("--max-ticks", type=int, default=None, help="stop after N solves (smoke runs)")
    ap.add_argument("--pv-scale", type=float, default=None,
                    help="scale every PV column of the frame (0 = no plant, 0.3 = a third); default 1")
    ap.add_argument("--capacity-kwh", type=float, default=None, help="pack nominal capacity (default 48,2)")
    ap.add_argument("--inverter-kw", type=float, default=None, help="hybrid inverter AC nominal (default 12)")
    ap.add_argument("--energy-tax", type=float, default=None,
                    help="energy tax EUR/kWh excl. BTW on every imported kWh (the post-salderen buy price); "
                         "default the live helper's value in replay_inputs.TARIFF")
    ap.add_argument("--supplier-fee", type=float, default=None,
                    help="supplier fee EUR/kWh excl. BTW added to every imported kWh; default the live "
                         "helper's value in replay_inputs.TARIFF (0,019)")
    ap.add_argument("--feedin-fee", type=float, default=None,
                    help="feed-in fee EUR/kWh excl. BTW taken off every exported kWh; NEGATIVE pays a premium "
                         "instead (a supplier paying market price + 0,02 is --feedin-fee -0.02)")
    ap.add_argument("--tick-min", type=int, default=None, choices=(30, 60),
                    help="minutes between solves: 30 is the live cadence and the default, 60 halves the "
                         "solve count by keeping only the :43 solve of each hour (13:00:04 is kept either "
                         "way). A 60 walk is not comparable with a 30 walk; frozen in meta.json")
    ap.add_argument("--frame", default=None,
                    help="frame file instead of data/frame.csv (a name in the data dir, e.g. frame_winter.csv)")
    a = ap.parse_args(argv)
    over = {k: v for k, v in (("energy_tax", a.energy_tax), ("feedin_fee", a.feedin_fee),
                              ("supplier_fee", a.supplier_fee)) if v is not None}
    tariff = dict(ri.TARIFF, **over) if over else None
    w = Walk(a.mode, date.fromisoformat(a.start), date.fromisoformat(a.end), run_dir=a.run_dir, knowledge=a.knowledge,
             tariff=tariff, pv_scale=a.pv_scale, frame_csv=a.frame, tick_min=a.tick_min,
             hardware=({"capacity_kwh": a.capacity_kwh, "inverter_kw": a.inverter_kw}
                       if a.capacity_kwh is not None or a.inverter_kw is not None else None))
    try:
        out = w.run(max_ticks=a.max_ticks)
    finally:
        w.close()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
