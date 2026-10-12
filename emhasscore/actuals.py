"""What the recorder measured, on the plan grid: a day's or a window's series from
the 5-minute statistics, the export switch per step, the curtailment fallback mask,
the load history the same-clock shape is built from, and DayActuals, the one
record a day's measurements travel in to the virtual day.

The wrapper fetches (statistics_during_period, state_changes_during_period) and
hands the rows here; it logs the returned notes, in order, as (level, text)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .grid import STEP_MIN
from .series import fifteen_min_series, MAX_PV_GAPS, window_series
from .repair import PV_CURTAIL_SOC_PCT


# The series a day or a window can carry, in the order they are read (and so
# the order their notes are logged in). imp and exp are the fiscal meter's two
# halves in kW; they leave the result as grid_w.
ACTUAL_SERIES = ("pv_w", "load_w", "imp", "exp", "batt_dc_w", "soc_pct", "curtailed", "micro_w", "batt_v")

# A window's series that may be missing: curtailed falls back to the mask,
# micro_w to repairing the whole array. Any other gappy series voids the window.
WINDOW_OPTIONAL = ("curtailed", "micro_w")


# run_plan's flat inp keys per DayActuals field, for today ("actual_") and
# yesterday ("prev_"). Today's curtailment, peak, must-take and anchor keys
# carry no prefix: that is how the inp dict has always named them.
_INP_KEYS = {
    "actual_": {"pv_w": "actual_pv_w", "load_w": "actual_load_w", "curtailed": "curtailed",
                "pv_peak_w": "pv_peak_w", "micro_w": "micro_w", "batt_w": "actual_batt_w",
                "grid_w": "actual_grid_w", "soc_pct": "actual_soc_pct", "soc_anchor": "soc_anchor"},
    "prev_": {"pv_w": "prev_pv_w", "load_w": "prev_load_w", "curtailed": "prev_curtailed",
              "pv_peak_w": "prev_pv_peak_w", "micro_w": "prev_micro_w", "batt_w": "prev_batt_w",
              "grid_w": "prev_grid_w", "soc_pct": "prev_soc_pct", "soc_anchor": "prev_soc_anchor"},
}
# The fields whose inp key the wrapper has always set, None or not; every
# other key is in inp only when its field is set.
_INP_ALWAYS = {"actual_": ("pv_w", "load_w", "curtailed", "pv_peak_w", "micro_w", "soc_anchor"),
               "prev_": ("pv_w", "load_w", "curtailed", "pv_peak_w", "micro_w")}


@dataclass(frozen=True)
class DayActuals:
    """One day's measurements on the plan grid, what virtual_day substitutes
    over the past: pv_w and load_w for the plan's forecast, curtailed,
    pv_peak_w and micro_w for the PV repair, and in live mode the real pack
    (batt_w DC + discharge, grid_w + import, soc_pct) with soc_anchor =
    (step, soc_pct), the step the walk is reset on the real pack. W per step;
    a None field is a series not measured."""
    pv_w: list | None = None
    load_w: list | None = None
    curtailed: list | None = None
    pv_peak_w: list | None = None
    micro_w: list | None = None
    batt_w: list | None = None
    grid_w: list | None = None
    soc_pct: list | None = None
    soc_anchor: tuple | None = None

    @classmethod
    def of(cls, act: dict | None, live: bool, soc_anchor: tuple | None = None) -> DayActuals:
        """From day_actuals' dict (batt_dc_w becomes batt_w). THE VIRTUAL PACK
        IS RETIRED IN LIVE MODE (2026-09-26): with `live` the past steps
        take the measured pack, meter and SOC, but only when all three series
        are there. The anchor needs `live` only: the writer's switch step does
        not depend on the recorder."""
        a = act or {}
        lanes = {}
        if live and all(a.get(k) is not None for k in ("batt_dc_w", "grid_w", "soc_pct")):
            lanes = {"batt_w": a["batt_dc_w"], "grid_w": a["grid_w"], "soc_pct": a["soc_pct"]}
        return cls(pv_w=a.get("pv_w"), load_w=a.get("load_w"), curtailed=a.get("curtailed"),
                   pv_peak_w=a.get("pv_peak_w"), micro_w=a.get("micro_w"),
                   soc_anchor=soc_anchor if live else None, **lanes)

    @classmethod
    def from_inp(cls, inp: dict, prefix: str) -> DayActuals:
        """From run_plan's flat inp keys, prefix "actual_" (today) or "prev_"."""
        return cls(**{f: inp.get(k) for f, k in _INP_KEYS[prefix].items()})

    def to_inp(self, prefix: str) -> dict:
        """The flat inp keys from_inp reads, with the key presence the wrapper
        has always given them."""
        always = _INP_ALWAYS[prefix]
        return {k: getattr(self, f) for f, k in _INP_KEYS[prefix].items()
                if f in always or getattr(self, f) is not None}

    @property
    def measured(self) -> bool:
        """The real pack settles the past: pack, meter and SOC all measured."""
        return self.batt_w is not None and self.grid_w is not None and self.soc_pct is not None


def _grid_w(imp: list, exp: list) -> list[float]:
    """The P1 reader reports kW; everything else in the pipeline is W, + = import."""
    return [round((imp[i] - exp[i]) * 1000.0, 1) for i in range(len(imp))]


def day_actuals(day: date, tz: str, stats: dict | None, entities: dict,
                gap_upto: int | None = None) -> tuple[dict, list]:
    """The day's measured series as W per 15-minute step, from the recorder's
    5-minute means: {pv_w, load_w} feed the virtual day and the display slices,
    {pv_w, grid_w, batt_dc_w} the ladder's replay. `entities` maps a key of
    ACTUAL_SERIES to its entity id; `stats` is what statistics_during_period
    returned for them ({entity id: rows}, mean and max).

    Any series with more than MAX_PV_GAPS held steps is left out of the dict
    rather than returned (a lane built on gap-holds is worse than no lane, and
    each lane then fails on its own), with a warning note. pv_peak_w is the
    5-minute PEAK of the array inside each step, the only thing that separates
    a throttled array from a cloudy one (see pv_potential).

    curtailed is left out when its sensor is gappy: the fallback mask needs the
    export switch's history, which only the caller reads (export_off,
    fallback_mask). Returns (series, notes)."""
    st = stats or {}
    got, notes = {}, []
    for key in [k for k in ACTUAL_SERIES if k in entities]:
        eid = entities[key]
        vals, gaps = fifteen_min_series(day, tz, st.get(eid), gap_upto)
        if gaps > MAX_PV_GAPS:
            notes.append(("warning", f"emhass: actuals too gappy for {day}: {eid} {gaps} steps"))
        else:
            got[key] = vals
    pk, pk_gaps = fifteen_min_series(day, tz, st.get(entities["pv_w"]), gap_upto, "max", "max")
    if pk_gaps <= MAX_PV_GAPS:
        got["pv_peak_w"] = pk
    imp, exp = got.pop("imp", None), got.pop("exp", None)
    if imp is not None and exp is not None:
        got["grid_w"] = _grid_w(imp, exp)
    return got, notes


def window_actuals(t0: datetime, n: int, stats: dict | None, entities: dict) -> tuple[dict | None, list]:
    """{pv_w, grid_w, batt_dc_w, soc_pct, curtailed, micro_w, pv_peak_w} as W per
    step over n steps from t0, crossing whatever calendar days the plan's
    horizon spans. No load series: the 20/20 lane runs on effective_load, which
    is built from these and so never touches the contested load entity.

    A gappy curtailed or micro_w comes back as None (the caller fills curtailed
    with the fallback mask); any other gappy series makes the window None, with
    a warning note. Returns (series or None, notes)."""
    st = stats or {}
    got = {}
    for key in [k for k in ACTUAL_SERIES if k in entities]:
        eid = entities[key]
        vals, gaps = window_series(t0, n, st.get(eid))
        if gaps > MAX_PV_GAPS:
            if key in WINDOW_OPTIONAL:
                got[key] = None
                continue
            return None, [("warning", f"emhass: hindsight window too gappy: {eid} {gaps} of {n} steps")]
        got[key] = vals
    pk, pk_gaps = window_series(t0, n, st.get(entities["pv_w"]), None, "max", "max")
    if pk_gaps <= MAX_PV_GAPS:
        got["pv_peak_w"] = pk
    got["grid_w"] = _grid_w(got.pop("imp"), got.pop("exp"))
    return got, []


def export_off(t0: datetime, n: int, points) -> list[float] | None:
    """1,0 per step where switch.inverter_export_surplus was OFF at the step start,
    from its recorded states as (timestamp, state) points in any order. The
    switch keeps no statistics, only states. Steps before the first recorded
    point read that point's state. None when no on/off state is recorded for
    the window, which is the caller's signal to drop back to the SOC half alone."""
    pts = sorted([(ts, s) for ts, s in (points or []) if s in ("on", "off")])
    if not pts:
        return None
    a = t0.astimezone(timezone.utc)
    out, j, cur = [], 0, pts[0][1]
    for i in range(n):
        ts = (a + timedelta(minutes=STEP_MIN * i)).timestamp()
        while j < len(pts) and pts[j][0] <= ts:
            cur = pts[j][1]
            j += 1
        out.append(1.0 if cur == "off" else 0.0)
    return out


def fallback_mask(soc_pct: list, off: list | None, n: int) -> list[float]:
    """Curtailment mask for a window that predates sensor.pv_curtailment_active
    (which only started recording at 14:00 on 2026-09-05).

    BOTH halves of the site's rule, not just SOC: the pack above PV_CURTAIL_SOC_PCT
    AND the export switch off (`off`, from export_off). The SOC-only version
    was loose by design, on the argument that pv_potential's max(measured,
    scaled) guard would neutralise a mask that over-fired. 09-05 disproved
    that: SOC sat above 95 % from the small hours, the fallback flagged the
    whole day, and with Solcast running high the guard never bit - five steps
    were credited with sun while the array was demonstrably free, peaking at
    1,4x the ceiling it was supposedly held back from. Only a window with no
    recorded switch state at all (off None) falls back to SOC alone."""
    m = min(n, len(soc_pct))
    if off is None:
        return [1.0 if float(soc_pct[i]) > PV_CURTAIL_SOC_PCT else 0.0 for i in range(m)]
    return [1.0 if (float(soc_pct[i]) > PV_CURTAIL_SOC_PCT and off[i] >= 0.5) else 0.0
            for i in range(m)]


def add_day_series(a: dict, b: dict) -> dict:
    """{date: [W]} + {date: [W]} elementwise; a day in one only is copied,
    a shorter list is padded with zeros."""
    out = {}
    for d in set(a or {}) | set(b or {}):
        x, y = list((a or {}).get(d) or []), list((b or {}).get(d) or [])
        m = max(len(x), len(y))
        x += [0.0] * (m - len(x))
        y += [0.0] * (m - len(y))
        out[d] = [round(float(p) + float(q), 1) for p, q in zip(x, y)]
    return out


def load_history_days(today: date, n_days: int, tz: str, rows) -> tuple[dict, list]:
    """{date: W per 15-minute step} for the last n COMPLETE days before `today`,
    from the load entity's 5-minute statistics rows. A day with more than
    MAX_PV_GAPS held steps is skipped, with a debug note. Returns (days, notes)."""
    first = today - timedelta(days=n_days)
    out, notes = {}, []
    for k in range(n_days):
        day = first + timedelta(days=k)
        vals, gaps = fifteen_min_series(day, tz, rows)
        if gaps <= MAX_PV_GAPS:
            out[day] = vals
        else:
            notes.append(("debug", f"emhass: load history skips {day}: {gaps} gap steps"))
    return out, notes
