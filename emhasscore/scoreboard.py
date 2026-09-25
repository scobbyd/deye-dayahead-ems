"""scores.csv and the rolling window: the columns, read, upsert, and the sums the dashboard shows."""
from __future__ import annotations

import csv
import os


# gap_eur is realised MINUS REPLAYED (2026-09-04): the plan lane priced on its
# own forecasts was never a fair counterparty for a measured day. planned_eur
# stays as the plan's self-reported P&L and forecast_gap_eur (replayed minus
# planned) is what the forecasts flattered it by. realised_cash_eur is settled
# in the plan's tariff frame; realised_cash_meter_eur keeps the utility meter's
# own figure as a cross-check and a helper-drift alarm.
SCORE_COLUMNS = ["date", "plan_ts", "optim_status", "n_predicted_steps",
                 "planned_cash_eur", "planned_soc_term_eur", "planned_loss_eur", "planned_eur",
                 "replayed_cash_eur", "replayed_loss_eur", "replayed_eur", "replay_status",
                 "realised_cash_eur", "realised_cash_meter_eur", "realised_soc_term_eur",
                 "realised_eur", "gap_eur", "forecast_gap_eur",
                 "hindsight_eur", "hindsight_status", "lambda_eur_kwh",
                 "soc_start_plan_pct", "soc_end_plan_pct", "soc_start_real_pct", "soc_end_real_pct",
                 "pv_planned_kwh", "pv_actual_kwh", "load_planned_kwh", "load_actual_kwh",
                 "pv_scale", "pv_curtailed_frac", "pv_reconstructed_kwh", "dwell_95_h", "flags"]


_TEXT_COLUMNS = {"date", "plan_ts", "optim_status", "hindsight_status", "replay_status", "flags"}


def read_scores(csv_path: str) -> list[dict]:
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, newline="") as f:
        raw = list(csv.DictReader(f))
    out = []
    for r in raw:
        d = {}
        for k in SCORE_COLUMNS:
            v = r.get(k, "")
            if k in _TEXT_COLUMNS:
                d[k] = v if v else ("" if k == "flags" else None)
            elif v in ("", "nan", "None"):
                d[k] = None
            else:
                d[k] = int(float(v)) if k == "n_predicted_steps" else float(v)
        out.append(d)
    return out


def upsert_score(csv_path: str, row: dict) -> list[dict]:
    rows = [r for r in read_scores(csv_path) if r["date"] != row["date"]] + [dict(row)]
    rows.sort(key=lambda r: r["date"])
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCORE_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in SCORE_COLUMNS})
    os.replace(tmp, csv_path)
    return rows


def _r2(v):
    return None if v is None else round(float(v), 2)


def scoreboard_row(r: dict) -> bool:
    """A row counts on the scoreboard only when the verdict gap exists: the
    plan's decisions on the real day (replayed = ACTUAL) against perfect
    foresight (hindsight = MAX). Rows missing either lane - too old for the
    recorder's actuals, or a failed hindsight solve - drop out of the sums
    until the day is re-scored rather than being averaged in on a stale
    definition (before 2026-09-05 the gap was realised minus replayed)."""
    return r.get("replayed_eur") is not None and r.get("hindsight_eur") is not None


def rolling(rows: list[dict], days: int = 30) -> dict:
    last = rows[-days:]
    scored = [r for r in last if scoreboard_row(r)]

    # actual, max, gap: the plan on the real day, the theoretical best, and the
    # shortfall between them. realised stays out of the verdict table (drift
    # alarm only); planned rides the race chart as the EXPECTED lane.
    def compact(r, with_flags):
        c = [r["date"], _r2(r.get("replayed_eur")), _r2(r.get("hindsight_eur")), _r2(r.get("gap_eur"))]
        return c + [r.get("flags") or ""] if with_flags else c

    def sums(rs):
        sc = [r for r in rs if scoreboard_row(r)]
        return {"n": len(sc), "gap": round(sum(r["gap_eur"] for r in sc), 2),
                "planned": round(sum(r["planned_eur"] for r in sc), 2),
                "replayed": round(sum(r["replayed_eur"] for r in sc), 2),
                "hindsight": round(sum(r["hindsight_eur"] for r in sc), 2),
                "forecast_gap": round(sum(r.get("forecast_gap_eur") or 0.0 for r in sc), 2),
                "realised": round(sum(r["realised_eur"] for r in sc if r.get("realised_eur") is not None), 2)}

    # Capture = the share of the theoretical maximum the plan actually banked:
    # replayed / hindsight over the scoreboard days, meaningful only when the
    # window is a net-earning one (hindsight below -0,50 EUR), so the ratio of
    # two negative earnings reads as "we captured N % of the best possible".
    sb = [r for r in rows if scoreboard_row(r)]
    rep_sum = sum(r["replayed_eur"] for r in sb)
    max_sum = sum(r["hindsight_eur"] for r in sb)
    capture = {"n_days": len(sb), "captured_eur": round(rep_sum, 2), "max_eur": round(max_sum, 2),
               "pct": round(100.0 * rep_sum / max_sum, 1) if max_sum < -0.5 else None}

    return {"gap_30d": round(sum(r["gap_eur"] for r in scored), 2), "n_days": len(scored),
            "planned_30d": round(sum(r["planned_eur"] for r in scored), 2),
            "replayed_30d": round(sum(r["replayed_eur"] for r in scored), 2),
            "hindsight_30d": round(sum(r["hindsight_eur"] for r in scored), 2),
            "n_flagged": sum(1 for r in last if r.get("flags")), "window_days": days,
            "windows": {"d1": sums(rows[-1:]), "d7": sums(rows[-7:]),
                        "d30": sums(last), "all": sums(rows)},
            "capture": capture,
            "recent": [compact(r, True) for r in rows[-7:]], "days": [compact(r, False) for r in last]}
