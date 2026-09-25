"""viewer: three linked subplots with the shadow-ems series."""
import os
from datetime import date

import pytest

pytest.importorskip("plotly")

from backtest import viewer
from test_backtest_replay_ladder import settled_days


def test_figure_structure():
    fig = viewer.figure(settled_days(), "test")
    names = [t.name for t in fig.data]
    for want in ("Charge", "Discharge", "Bank SOC", "Import", "Export", "Buy price", "Sell price",
                 "PV, shadow regime", "PV, metered", "PV forecast", "Consumption", "Consumption forecast",
                 "PV ceiling, declined"):
        assert want in names, want
    rows = {t.yaxis for t in fig.data}
    assert {"y", "y2", "y3", "y4", "y5"} <= rows            # 3 rows, two with a right axis
    assert fig.layout.xaxis3.rangeslider.visible is True


def test_write_html(tmp_path):
    p = viewer.write_html(settled_days(), str(tmp_path / "v.html"), "test")
    assert os.path.exists(p) and os.path.getsize(p) > 1_000_000        # plotly.js embedded
    html = open(p, encoding="utf-8").read()
    assert "Bank SOC" in html and "<script" in html


def test_harvest_floor_and_soc_ticks():
    df = settled_days()
    df.loc[df.index[:4], "pv_curtail_w"] = 5000.0          # declined more than the sun that came
    fig = viewer.figure(df, "test")
    harvest = next(t for t in fig.data if t.name == "PV, shadow regime")
    assert min(harvest.y) == 0.0
    assert fig.layout.yaxis2.dtick == 20 and fig.layout.yaxis2.tick0 == 0


def test_grid_ranges_share_one_zero():
    import pandas as pd
    from backtest.viewer import _grid_ranges
    kw, ct = _grid_ranges(pd.Series([-10.0, 3.5]), pd.Series([12.0, 41.0, -3.0]), pd.Series([9.0, 38.0, -6.0]))
    # the same negative fraction on both axes puts 0 ct on the 0 kW rule
    assert kw[0] / kw[1] == ct[0] / ct[1]
    assert kw == (-10, 5) and ct[1] == 45.0 and ct[0] == -90.0
    kw, ct = _grid_ranges(pd.Series([-12.0, 12.4]), pd.Series([12.0, 41.0]), pd.Series([9.0, 38.0]))
    assert kw == (-12, 13) and ct == (-12 * 45.0 / 13, 45.0)
