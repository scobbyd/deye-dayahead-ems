"""Price inputs for the closed replay: the published auction and the fill
beyond it, in the shapes emhasscore.series.price_series reads live.

Live, every solve gets Nord Pool rows for today (and for tomorrow from 13:00
local) plus sensor.epex_predictor_nl's 120 h forecast list; price_series takes
the rows first and the list for every other step, counting those as
predicted. No forecast as issued exists before 2026-09-02 (the as-of
capture of the predictor started then), so the replay brackets the tail
instead:

  foresight    the settled auction for every quarter. Upper bound: the planner
               knows D+1 before 13:00 and D+2 always.
  persistence  quarters beyond the published frontier take the same clock
               quarter of the frontier day (a 24 h shift, repeated for D+2).
               Lower bound: a naive forecast, worse than the predictor.
  asof         the capture file, the newest issue at or before the tick. Valid
               from 2026-09-02 only; the seam check against live plans.

The frontier rule is the walk's: tomorrow is published from 13:00 local.
Ticks in July-September 2026 see no DST change, so the 24 h shift keeps the
local clock; a walk across a DST day should shift by local days instead.
"""
from __future__ import annotations

import bisect
import csv
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))          # gitignored
DA_CSV = os.path.join(DATA, "da_prices_q15.csv")                    # ts,eur_mwh per quarter (the energy-charts layout)
ASOF_CSV = os.path.join(DATA, "epex_asof.csv")                      # ts_utc,issued_utc,eur_kwh,predicted
STEP = timedelta(minutes=15)
PUBLISH_HOUR = 13
MODES = ("foresight", "persistence", "asof")


def _utc(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _floor(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)


def load_da(path: str = DA_CSV) -> dict:
    """{utc quarter start: EUR/kWh} from da_prices_q15.csv (ts, eur_mwh)."""
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        r = csv.reader(fh)
        next(r)
        for ts, mwh in r:
            if mwh != "":
                out[_utc(ts)] = float(mwh) / 1000.0
    return out


def load_asof(path: str = ASOF_CSV) -> dict:
    """{utc quarter: ([issued...], [value...]) sorted by issue} from epex_asof.csv."""
    acc = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["predicted"] != "1":
                continue
            acc.setdefault(_utc(r["ts_utc"]), []).append((_utc(r["issued_utc"]), float(r["eur_kwh"])))
    return {k: tuple(zip(*sorted(v))) for k, v in acc.items()}


def frontier(tick: datetime, tz: str) -> datetime:
    """Exclusive end of the published auction at the tick: local midnight
    after today before 13:00 local, after tomorrow from 13:00."""
    z = ZoneInfo(tz)
    local = tick.astimezone(z)
    day = local.date() + timedelta(days=1 if local.hour >= PUBLISH_HOUR else 0)
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=z).astimezone(timezone.utc)


def nordpool_rows(day, da: dict, tz: str) -> list:
    """{start, end, price EUR/MWh} for every quarter of the local day that the
    auction file holds, the live Nord Pool sensor's row shape."""
    z = ZoneInfo(tz)
    t = datetime.combine(day, datetime.min.time(), tzinfo=z)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=z)
    rows = []
    while t < end:
        u = t.astimezone(timezone.utc)
        if u in da:
            rows.append({"start": u.isoformat(), "end": (u + STEP).isoformat(),
                         "price": round(da[u] * 1000.0, 2)})
        t += STEP
    return rows


def epex_forecast(tick: datetime, mode: str, da: dict, tz: str, hours: int = 120,
                  asof: dict | None = None) -> list:
    """[{datetime, value EUR/kWh}] from the tick's quarter for `hours`, the
    live sensor's forecast shape. Quarters the mode cannot price are left out,
    which price_series then holds at the previous value (and counts)."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    start, fr = _floor(tick), frontier(tick, tz)
    out = []
    for k in range(hours * 4):
        ts = start + k * STEP
        v = None
        if mode == "foresight":
            v = da.get(ts)
        elif mode == "persistence":
            src = ts
            while src >= fr:
                src -= timedelta(days=1)
            v = da.get(src)
        else:
            hist = (asof or {}).get(ts)
            if hist:
                i = bisect.bisect_right(hist[0], tick.astimezone(timezone.utc))
                v = hist[1][i - 1] if i else None
        if v is not None:
            out.append({"datetime": ts.isoformat().replace("+00:00", "Z"), "value": round(v, 5)})
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="print the fill for one tick")
    ap.add_argument("--tick", required=True, help="ISO, e.g. 2026-07-24T10:13:00+02:00")
    ap.add_argument("--mode", default="persistence", choices=MODES)
    ap.add_argument("--tz", default="Europe/Amsterdam")
    a = ap.parse_args(argv)
    tick = datetime.fromisoformat(a.tick)
    da = load_da()
    fc = epex_forecast(tick, a.mode, da, a.tz, asof=load_asof() if a.mode == "asof" else None)
    fr = frontier(tick, a.tz)
    beyond = [p for p in fc if _utc(p["datetime"]) >= fr]
    print(f"tick {tick.isoformat()} frontier {fr.isoformat()} mode {a.mode}: "
          f"{len(fc)} quarters, {len(beyond)} beyond the frontier")
    for p in beyond[:8]:
        print(" ", p["datetime"], p["value"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
