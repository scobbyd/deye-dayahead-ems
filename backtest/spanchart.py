"""One standalone HTML page over the replay metrics sidecar: the money of the
lanes over the span, cumulative and per day, next to a supplier's same days.

Top: cumulative cash (ladder sign flipped so up = earned) for every lane in
replay_metrics.csv, the supplier invoice, the supplier's flows at day-ahead (the
control), the no-battery house (load minus available PV at the same prices,
the baseline the pack's gain is measured against) and the running lead of the
first lane over the invoice. Middle: daily cash per lane and the invoice as
bars: the other lanes and the invoice, not the first lane (its rec days are
the row below as the lead over the invoice), plus its live days after the
seam. Bottom: the first lane's lead over the invoice per day. Same DOS tokens
and conventions as viewer.py. The span starts where the walk is warm (the
first days after the seed carry the seed SOC) and ends on the last day both
sides have.

--live (the default): after the first lane's last day the `live` rows
(source `live`, the shadow EMS in Home Assistant) continue that lane's
series in the same colour, dashed and lighter, from a seam marked "live from
<date>". The other lanes end at the seam. The invoice, its day-ahead control
and the lead over the invoice run as far as the supplier's file goes; the
no-battery baseline continues over the live days. The span then ends on the
last live day.

--chain WINTER:LANE prepends another run's rows to a lane: the rows of
WINTER dated before --warm (LANE's first warm day) are shown as LANE (the pretend-plant
winter, a walk on a modelled frame, in front of the live-plant walk), with a
seam marked "pretend plant to <date>". The supplier columns of those prepended
days are dropped (their invoice there is another plant), so the invoice, its
control and the lead start where the live-plant lane does.

    python -m backtest.spanchart [--lanes foresight foresight_omni2] [--first 2026-02-16] [--no-live] [--out ...]
                                 [--chain foresight_winter:foresight foresight_omni2_winter:foresight_omni2]
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest import metrics as mx
from backtest import replay_inputs as ri
from backtest.viewer import BLUE, BUY, GREEN, INK, MUTED, RED, SELL

LANE_COLORS = [BLUE, RED, GREEN, BUY, SELL]
LIVE_LANE = "live"
OUT = os.path.join(ri.REPLAY_DIR, "replay_span.html")


def seam_day(df: pd.DataFrame, lane: str, live_lane: str = LIVE_LANE) -> str | None:
    """The first live day after `lane`'s last scored day, or None when the
    live rows do not continue the lane (none, or none after it)."""
    rec = df[(df["lane"] == lane) & df["cash_eur"].notna()]["date"]
    live = df[(df["lane"] == live_lane) & df["cash_eur"].notna()]["date"]
    if not len(rec) or not len(live):
        return None
    after = live[live > rec.max()]
    return after.min() if len(after) else None


def continued(df: pd.DataFrame, lane: str, seam: str | None, live_lane: str = LIVE_LANE) -> pd.DataFrame:
    """`lane`'s rows, then the live rows from the seam on, one frame indexed by
    date; a `live` column says which half a day belongs to."""
    rec = df[df["lane"] == lane].set_index("date").sort_index()
    rec = rec.assign(live=False)
    if seam is None:
        return rec
    live = df[(df["lane"] == live_lane) & (df["date"] >= seam)].set_index("date").sort_index().assign(live=True)
    return pd.concat([rec[rec.index < seam], live]).sort_index()


def _until(series: pd.Series, x, last) -> pd.Series:
    """The cumulative sum, cut (NaN) after `last` so a line stops where its data does."""
    out = series.fillna(0).cumsum()
    if last is not None:
        out = out.where(x <= pd.Timestamp(last))
    return out


def chain(raw: pd.DataFrame, pairs: list[str], warm: str | None = None) -> tuple[pd.DataFrame, str | None]:
    """The sidecar with each WINTER:LANE pair's rows before `warm` (default
    LANE's first scored day) relabelled as LANE, supplier columns emptied on
    those rows; LANE's own rows before `warm` are dropped (its cold seed days).
    Returns (frame, the last prepended day of the first pair or None)."""
    out, join = raw.copy(), None
    for pair in pairs:
        front, lane = pair.split(":", 1)
        first = warm or out[(out["lane"] == lane) & out["cash_eur"].notna()]["date"].min()
        out = out[~((out["lane"] == lane) & (out["date"] < first))]
        take = (out["lane"] == front) & (out["date"] < first)
        out.loc[take, "lane"] = lane
        out.loc[take, [c for c in out.columns if c.startswith("supplier_")]] = float("nan")
        if join is None and take.any():
            join = out.loc[take, "date"].max()
    return out, join


def _seam_marks(fig: go.Figure, day: str, text: str, before: bool) -> None:
    """A dotted vertical through the three rows half a day before (or after)
    `day`, with a small label at the foot of the top row."""
    at = pd.Timestamp(day) + pd.Timedelta(hours=-12 if before else 12)
    for row in (1, 2, 3):
        fig.add_shape(type="line", x0=at, x1=at, y0=0, y1=1, xref="x" if row == 1 else f"x{row}",
                      yref=f"{'y' if row == 1 else f'y{row}'} domain", line=dict(color=MUTED, width=1, dash="dot"))
    fig.add_annotation(x=at, y=0.02, xref="x", yref="y domain", text=text, showarrow=False,
                       xanchor="left" if before else "right", yanchor="bottom", xshift=4 if before else -4,
                       font=dict(color=MUTED, size=12))


def figure(df: pd.DataFrame, lanes: list[str], title: str, seam: str | None = None, join: str | None = None) -> go.Figure:
    base = continued(df, lanes[0], seam)
    x = pd.to_datetime(base.index)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.5, 0.25, 0.25],
                        subplot_titles=("Cumulative, EUR earned",
                                        f"Per day, EUR earned: {', '.join(lanes[1:])}, the supplier invoice"
                                        + (f", {lanes[0]} live" if seam else ""),
                                        f"{lanes[0]} minus supplier invoice, per day, EUR"))
    hov = "%{y:,.2f} EUR"
    is_live = base["live"].to_numpy()
    for k, lane in enumerate(lanes):
        d = base if k == 0 else df[df["lane"] == lane].set_index("date").sort_index().reindex(base.index)
        earned = -d["cash_eur"].astype(float)
        c = LANE_COLORS[k % len(LANE_COLORS)]
        cum = earned.cumsum()
        if k == 0 and seam is not None:
            # the rec half solid, the live half dashed; the last rec day is on both so the line joins
            rec_x = x < pd.Timestamp(seam)
            last_rec = x[rec_x].max()
            fig.add_trace(go.Scatter(x=x, y=cum.where(rec_x), name=f"{lane} (cash)", line=dict(color=c, width=2),
                                     hovertemplate=hov), 1, 1)
            fig.add_trace(go.Scatter(x=x, y=cum.where(x >= last_rec), name=f"{lane}, live (cash)",
                                     line=dict(color=c, width=2, dash="dash"), opacity=0.75, hovertemplate=hov), 1, 1)
            # per day the first lane shows only its live half: its rec half is the lead bars of row 3
            fig.add_trace(go.Bar(x=x, y=earned.where(is_live), name=f"{lane}, live, day", marker_color=c, opacity=0.5,
                                 hovertemplate=hov, showlegend=False), 2, 1)
            continue
        fig.add_trace(go.Scatter(x=x, y=cum, name=f"{lane} (cash)", line=dict(color=c, width=2),
                                 hovertemplate=hov), 1, 1)
        fig.add_trace(go.Bar(x=x, y=earned, name=f"{lane}, day", marker_color=c, opacity=0.8, hovertemplate=hov,
                             showlegend=False), 2, 1)
    bal = -base["supplier_net_eur"].astype(float)
    last_bal = bal.dropna().index.max() if bal.notna().any() else None
    lead = -base["cash_eur"].astype(float) - bal
    fig.add_trace(go.Scatter(x=x, y=_until(lead, x, last_bal), name=f"{lanes[0]} minus supplier invoice, cumulative",
                             line=dict(color=GREEN, width=2.4), hovertemplate=hov), 1, 1)
    fig.add_trace(go.Bar(x=x, y=lead, name=f"{lanes[0]} minus supplier invoice, day", marker_color=GREEN,
                         hovertemplate=hov, showlegend=False), 3, 1)
    fig.add_trace(go.Scatter(x=x, y=_until(bal, x, last_bal), name="supplier invoice", line=dict(color=INK, width=2),
                             hovertemplate=hov), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=_until(-base["supplier_da_eur"].astype(float), x, last_bal),
                             name="supplier flows at day-ahead (control)", line=dict(color=INK, width=1, dash="dot"),
                             hovertemplate=hov), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=(-base["nobatt_cash_eur"].astype(float)).cumsum(), name="No battery",
                             line=dict(color=MUTED, width=1, dash="dash"), hovertemplate=hov), 1, 1)
    fig.add_trace(go.Bar(x=x, y=bal, name="supplier invoice, day", marker_color=INK, opacity=0.7, hovertemplate=hov,
                         showlegend=False), 2, 1)
    if seam is not None:
        # the seam sits between the last rec day and the first live day
        _seam_marks(fig, seam, f"live from {seam}", before=True)
    if join is not None:
        _seam_marks(fig, join, f"pretend plant to {join}", before=False)
    fig.update_yaxes(title_text="EUR", row=1, col=1)
    fig.update_yaxes(title_text="EUR", row=2, col=1, zeroline=True)
    fig.update_yaxes(title_text="EUR", row=3, col=1, zeroline=True)
    fig.update_xaxes(row=3, col=1, rangeslider=dict(visible=True, thickness=0.05))
    fig.update_layout(height=1000, hovermode="x unified", dragmode="pan", barmode="group", bargap=0.15,
                      title=dict(text=title, x=0.01, xanchor="left", y=0.995, yanchor="top", yref="container"),
                      legend=dict(orientation="h", x=0, xanchor="left", y=0.95, yanchor="top", yref="container"),
                      separators=",.", template="plotly_white", margin=dict(l=60, r=40, t=130, b=40))
    return fig


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="render the span chart over the metrics sidecar")
    ap.add_argument("--lanes", nargs="+", default=["foresight", "foresight_omni2"])
    ap.add_argument("--csv", default=mx.ALL_CSV)
    ap.add_argument("--first", default="2026-02-16", help="first day shown (the walk is warm from here)")
    ap.add_argument("--live", action=argparse.BooleanOptionalAction, default=True,
                    help="continue the first lane with the live rows after its last day")
    ap.add_argument("--chain", nargs="*", default=[], metavar="FRONT:LANE",
                    help="prepend FRONT's earlier rows to LANE (the pretend-plant winter in front of the live-plant walk)")
    ap.add_argument("--warm", default="2026-02-16",
                    help="with --chain: the live-plant lane is shown from here, the front lane up to the day before")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    raw = pd.read_csv(a.csv, dtype={"date": str})
    raw, join = chain(raw, a.chain, a.warm) if a.chain else (raw, None)
    df = raw[raw["lane"].isin(a.lanes) & (raw["date"] >= a.first)]
    # the last day both sides have: every lane scored and the supplier invoice present
    have = df.dropna(subset=["cash_eur", "supplier_net_eur"]).groupby("date")["lane"].nunique()   # winter days lack the invoice by design; they precede the summer ones
    last = have[have == len(a.lanes)].index.max()
    df = df[df["date"] <= last]
    first, last = df["date"].min(), df["date"].max()
    title = f"Closed replay {first} to {last}: {', '.join(a.lanes)} against the supplier invoice"
    seam = None
    if a.live:
        live = raw[(raw["lane"] == LIVE_LANE) & (raw["date"] > last)]
        seam = seam_day(pd.concat([df, live], ignore_index=True), a.lanes[0])
        if seam is not None:
            df = pd.concat([df, live], ignore_index=True)
            title += f"; live from {seam} to {live['date'].max()}"
    if join is not None:
        title += f"; pretend plant to {join}"
    fig = figure(df, a.lanes, title, seam, join)
    fig.write_html(a.out, include_plotlyjs=True, full_html=True,
                   config={"scrollZoom": True, "displaylogo": False, "responsive": True})
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
