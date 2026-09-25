"""pricefill: the frontier rule, the three fills, and that price_series eats them."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backtest import pricefill as pf
from emhasscore.grid import horizon
from emhasscore.series import price_series

TZ = "Europe/Amsterdam"
AMS = ZoneInfo(TZ)
UTC = timezone.utc


def synthetic_da(first=date(2026, 7, 22), days=6):
    """Distinct price per (day, local quarter): 0.1 x day-index + quarter / 10000."""
    da, t = {}, datetime.combine(first, datetime.min.time(), tzinfo=AMS)
    end = t + timedelta(days=days)
    while t < end:
        d = (t.date() - first).days
        q = t.hour * 4 + t.minute // 15
        da[t.astimezone(UTC)] = round(0.1 * d + q / 10000.0, 5)
        t += timedelta(minutes=15)
    return da


def local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=AMS)


def test_frontier_flips_at_13_local():
    assert pf.frontier(local(2026, 7, 24, 12, 59), TZ) == local(2026, 7, 25, 0).astimezone(UTC)
    assert pf.frontier(local(2026, 7, 24, 13, 0), TZ) == local(2026, 7, 26, 0).astimezone(UTC)


def test_nordpool_rows_shape():
    da = synthetic_da()
    rows = pf.nordpool_rows(date(2026, 7, 24), da, TZ)
    assert len(rows) == 96
    assert rows[0]["start"] == local(2026, 7, 24, 0).astimezone(UTC).isoformat()
    assert rows[0]["price"] == pytest.approx(da[local(2026, 7, 24, 0).astimezone(UTC)] * 1000)
    assert pf.nordpool_rows(date(2030, 1, 1), da, TZ) == []


def test_foresight_is_the_auction():
    da = synthetic_da()
    tick = local(2026, 7, 24, 10, 13)
    fc = pf.epex_forecast(tick, "foresight", da, TZ)
    assert fc[0]["datetime"] == "2026-07-24T08:00:00Z"
    for p in fc:
        assert p["value"] == pytest.approx(da[pf._utc(p["datetime"])])


def test_persistence_repeats_the_frontier_day():
    da = synthetic_da()
    tick = local(2026, 7, 24, 10, 13)             # frontier: end of 07-24
    fc = {pf._utc(p["datetime"]): p["value"] for p in pf.epex_forecast(tick, "persistence", da, TZ)}
    q = local(2026, 7, 24, 18, 30).astimezone(UTC)
    assert fc[q] == pytest.approx(da[q])                                      # published, untouched
    for days in (1, 2, 3):
        assert fc[q + timedelta(days=days)] == pytest.approx(da[q])          # every later day = 07-24's 18:30
    tick = local(2026, 7, 24, 13, 0)              # frontier: end of 07-25
    fc = {pf._utc(p["datetime"]): p["value"] for p in pf.epex_forecast(tick, "persistence", da, TZ)}
    q1 = q + timedelta(days=1)
    assert fc[q1] == pytest.approx(da[q1])
    assert fc[q1 + timedelta(days=1)] == pytest.approx(da[q1])


def test_asof_takes_the_newest_issue_before_the_tick(tmp_path):
    p = tmp_path / "asof.csv"
    p.write_text("issued_utc,ts_utc,eur_kwh,predicted,source\n"
                 "2026-09-05T08:13:00Z,2026-09-06T10:00:00Z,0.10000,1,plan\n"
                 "2026-09-05T11:13:00Z,2026-09-06T10:00:00Z,0.20000,1,plan\n"
                 "2026-09-05T14:13:00Z,2026-09-06T10:00:00Z,0.30000,1,plan\n"
                 "2026-09-05T11:13:00Z,2026-09-06T10:15:00Z,0.99000,0,plan\n")
    asof = pf.load_asof(str(p))
    tick = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    fc = pf.epex_forecast(tick, "asof", {}, TZ, asof=asof)
    assert fc == [{"datetime": "2026-09-06T10:00:00Z", "value": 0.2}]      # 11:13 issue; 14:13 is after the tick


@pytest.mark.parametrize("mode", ["foresight", "persistence"])
@pytest.mark.parametrize("hh", [0, 10, 13, 23])
def test_price_series_eats_the_fill(mode, hh):
    da = synthetic_da()
    tick = local(2026, 7, 24, hh, 13)
    t0, n = horizon(tick, TZ, days_ahead=2)
    np_today = pf.nordpool_rows(date(2026, 7, 24), da, TZ)
    np_tomorrow = pf.nordpool_rows(date(2026, 7, 25), da, TZ) if hh >= 13 else None
    prices, n_pred = price_series(t0, n, np_today, np_tomorrow, pf.epex_forecast(tick, mode, da, TZ))
    assert len(prices) == n
    published = 96 * (2 if hh >= 13 else 1) - (t0 - local(2026, 7, 24, 0)).seconds // 900
    assert n_pred == n - published
    if mode == "foresight":
        fr = pf.frontier(tick, TZ)
        k = next(i for i, t in enumerate(t0 + i * timedelta(minutes=15) for i in range(n)) if t >= fr)
        assert prices[k] == pytest.approx(da[(t0 + k * timedelta(minutes=15)).astimezone(UTC)])
