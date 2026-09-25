"""One standalone HTML page with the three shadow-ems charts over a whole
replay run: battery, grid, forecast against actual. Same series as
www/dos/dos_charts.js emhass-plan-battery / emhass-plan-grid /
emhass-forecast-vs-actual, on the settled virtual days (replay.load_slices).
Shared x axis, range slider, wheel zoom, unified hover, European decimals.
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest import replay
from backtest import replay_inputs as ri

# DOS day tokens (themes/dos.yaml), the mapping of dos_charts.js
BLUE, RED, INK, GREEN = "#5d8499", "#b5654d", "#0d1b2a", "#6b7a52"     # series1, series2, ink, series4
BUY, SELL, MUTED = "#8a3f33", "#3e5567", "#8a8a8a"                      # accent2, accent, quiet
BLUE_FILL, RED_FILL = "rgba(93,132,153,0.40)", "rgba(181,101,77,0.45)"
TZ = ri.TZ


def _local(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["ts_utc"], utc=True).dt.tz_convert(TZ)


def _kw(s) -> pd.Series:
    return pd.Series(s, dtype=float) / 1000.0


def _grid_ranges(pg_kw: pd.Series, buy_ct: pd.Series, sell_ct: pd.Series) -> tuple[tuple[float, float], tuple[float, float]]:
    """[kW range], [ct range] with the same zero. The kW range holds the flows
    in whole kW; the import side is given at least half the export side so the
    prices, which live above zero, keep a third of the frame on a summer day
    of pure export. The ct scale is the highest price over the import side
    (rounded up to 5 ct), and the export side grows if a negative price needs
    more room below the shared rule."""
    import math
    lo_kw = max(1.0, math.ceil(float((-pg_kw).max()) if len(pg_kw) else 1.0))
    hi_kw = max(1.0, math.ceil(float(pg_kw.max()) if len(pg_kw) else 1.0), math.ceil(lo_kw / 2.0))
    p_hi = max(float(buy_ct.max()), float(sell_ct.max()), 1.0)
    p_lo = max(0.0, -min(float(buy_ct.min()), float(sell_ct.min())))
    hi_ct = math.ceil(p_hi / 5.0) * 5.0
    scale = hi_ct / hi_kw                                      # ct per kW
    lo_kw = max(lo_kw, math.ceil(p_lo / scale))
    return (-lo_kw, hi_kw), (-lo_kw * scale, hi_ct)


def figure(df: pd.DataFrame, title: str) -> go.Figure:
    d = df.sort_values("ts_utc").reset_index(drop=True)
    x = _local(d)
    x_end = x + pd.Timedelta(minutes=15)                       # SOC is stamped at the end of the step
    pb, pg = _kw(d["p_batt_w"]), _kw(d["p_grid_w"])
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{"secondary_y": False}]],
                        subplot_titles=("Battery", "Grid", "Forecast against actual"))
    area = dict(mode="lines", line=dict(width=0.8), fill="tozeroy", hovertemplate="%{y:,.2f} kW")
    # 1. battery: charge positive (plan sign flipped, as the DOS chart does)
    fig.add_trace(go.Scatter(x=x, y=(-pb).clip(lower=0), name="Charge", line_color=BLUE, fillcolor=BLUE_FILL, **area), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=(-pb).clip(upper=0), name="Discharge", line_color=RED, fillcolor=RED_FILL, **area), 1, 1)
    fig.add_trace(go.Scatter(x=x_end, y=d["soc_pct"], name="Bank SOC", line=dict(color=INK, width=1.4),
                             hovertemplate="%{y:,.1f} %"), 1, 1, secondary_y=True)
    # 2. grid: import positive
    fig.add_trace(go.Scatter(x=x, y=pg.clip(lower=0), name="Import", line_color=RED, fillcolor=RED_FILL, **area), 2, 1)
    fig.add_trace(go.Scatter(x=x, y=pg.clip(upper=0), name="Export", line_color=BLUE, fillcolor=BLUE_FILL, **area), 2, 1)
    fig.add_trace(go.Scatter(x=x, y=d["buy"] * 100, name="Buy price", line=dict(color=BUY, width=1.2, shape="hv"),
                             hovertemplate="%{y:,.1f} ct/kWh"), 2, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=x, y=d["sell"] * 100, name="Sell price", line=dict(color=SELL, width=1.2, shape="hv"),
                             hovertemplate="%{y:,.1f} ct/kWh"), 2, 1, secondary_y=True)
    # 3. forecast vs actual
    # p_pv_w is the sun available (measured on past steps); pv_curtail_w is what
    # the plan in force declined, sized on its forecast. A dull day the plan
    # expected to be bright reads negative without the floor.
    harvest = (_kw(d["p_pv_w"]) - _kw(d["pv_curtail_w"])).clip(lower=0)
    fig.add_trace(go.Scatter(x=x, y=harvest, name="PV, shadow regime", line_color=BLUE, fillcolor=BLUE_FILL, **area), 3, 1)
    fig.add_trace(go.Scatter(x=x, y=_kw(d["p_load_w"]), name="Consumption", line_color=RED, fillcolor=RED_FILL, **area), 3, 1)
    fig.add_trace(go.Scatter(x=x, y=_kw(d["pv_meas_w"]), name="PV, metered", line=dict(color=GREEN, width=1.4), hovertemplate="%{y:,.2f} kW"), 3, 1)
    fig.add_trace(go.Scatter(x=x, y=_kw(d["pv_fc_w"]), name="PV forecast", line=dict(color=MUTED, width=1, dash="dot"), hovertemplate="%{y:,.2f} kW"), 3, 1)
    fig.add_trace(go.Scatter(x=x, y=_kw(d["load_fc_w"]), name="Consumption forecast", line=dict(color=MUTED, width=1, dash="dash"), hovertemplate="%{y:,.2f} kW"), 3, 1)
    ceiling = _kw(d["p_pv_w"]).where(pd.Series(d["pv_curtail_w"], dtype=float) > 5.0)
    fig.add_trace(go.Scatter(x=x, y=ceiling, name="PV ceiling, declined", line=dict(color=MUTED, width=1, dash="dot"),
                             connectgaps=False, hovertemplate="%{y:,.2f} kW"), 3, 1)
    fig.update_yaxes(title_text="kW", row=1, col=1, secondary_y=False, zeroline=True)
    fig.update_yaxes(title_text="%", row=1, col=1, secondary_y=True, range=[0, 100], showgrid=False,
                     tickmode="linear", tick0=0, dtick=20)
    # One zero for the grid panel (ruling 2026-09-15): the price axis takes the
    # kW axis's negative fraction, so 0 ct sits on the 0 kW rule and a negative
    # price dips below it like an export. Both ranges are fixed from the data
    # (rounded outward to whole kW and to 5 ct) so a zoom in x keeps the pin.
    lo, hi = _grid_ranges(pg, d["buy"] * 100, d["sell"] * 100)
    fig.update_yaxes(title_text="kW", row=2, col=1, secondary_y=False, zeroline=True, range=list(lo))
    fig.update_yaxes(title_text="ct/kWh", row=2, col=1, secondary_y=True, showgrid=False, range=list(hi), zeroline=False)
    fig.update_yaxes(title_text="kW", row=3, col=1, rangemode="tozero")
    fig.update_xaxes(row=3, col=1, rangeslider=dict(visible=True, thickness=0.05))
    # 13 legend entries wrap to two lines at this width. Anchor title and
    # legend to the container (not paper/domain) so their stacking order is
    # explicit: title, then the two-line legend, then a clear gap before the
    # "Battery" row-title annotation. The brief's y=1.03, t=80 put the legend
    # right where that annotation sits ("Battery" read as "ImpBattery").
    fig.update_layout(height=1150, hovermode="x unified", dragmode="pan",
                      title=dict(text=title, x=0.01, xanchor="left", y=0.995, yanchor="top", yref="container"),
                      legend=dict(orientation="h", x=0, xanchor="left", y=0.93, yanchor="top", yref="container"),
                      separators=",.", template="plotly_white", margin=dict(l=60, r=60, t=210, b=40))
    return fig


def write_html(df: pd.DataFrame, path: str, title: str) -> str:
    fig = figure(df, title)
    fig.write_html(path, include_plotlyjs=True, full_html=True,
                   config={"scrollZoom": True, "displaylogo": False, "responsive": True})
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="render the three-chart viewer for a replay run")
    ap.add_argument("--mode", required=True)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    run_dir = a.run_dir or os.path.join(ri.REPLAY_DIR, a.mode)
    out = a.out or os.path.join(ri.REPLAY_DIR, f"replay_viewer_{a.mode}.html")
    df = replay.load_slices(run_dir)
    if df.empty:
        print(f"no settled slices in {run_dir}", file=sys.stderr)
        return 1
    p = write_html(df, out, f"EMHASS closed replay, {a.mode} fill, {df['date'].min()} .. {df['date'].max()}")
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
