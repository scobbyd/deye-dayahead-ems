"""yearly: chaining runs into one year over the metrics sidecar."""
import pandas as pd

from backtest import yearly


def _row(lane, date, cash, nobatt=1.0, gain=2.0):
    return {"lane": lane, "source": "rec", "date": date, "cash_eur": cash, "nobatt_cash_eur": nobatt,
            "batt_gain_eur": gain, "grid_import_kwh": 1.0, "grid_export_kwh": 1.0, "grid_to_batt_kwh": 1.0,
            "batt_in_kwh": 1.0, "batt_out_kwh": 1.0, "pv_kwh": 1.0, "pv_curtailed_kwh": 0.0,
            "load_kwh": 1.0, "cycles": 0.5}


def _df():
    return pd.DataFrame([_row("winter", "2026-02-14", -1.0), _row("winter", "2026-02-15", -2.0),
                         _row("winter", "2026-02-16", -99.0),      # the overlap day belongs to the summer run
                         _row("summer", "2026-02-16", -3.0), _row("summer", "2026-02-17", -4.0)])


def test_parse_lane():
    assert yearly.parse_lane("2026 salderen=a,b") == ("2026 salderen", ["a", "b"])
    assert yearly.parse_lane("solo") == ("solo", ["solo"])


def test_chained_lane_takes_each_run_up_to_the_next_ones_first_day():
    d = yearly.lane_days(_df(), ["winter", "summer"], "2026-02-14", "2026-02-17")
    assert list(d["date"]) == ["2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17"]
    assert list(d["cash_eur"]) == [-1.0, -2.0, -3.0, -4.0]


def test_summary_flips_the_cash_sign_and_counts_days():
    t = yearly.summarize(_df(), ["year=winter,summer"], "2026-02-14", "2026-02-17")
    assert t.loc["year", "days"] == 4
    assert t.loc["year", "earned_eur"] == 10.0
    assert t.loc["year", "nobatt_earned_eur"] == -4.0
    assert t.loc["year", "gain_per_day"] == 2.0
    assert (t.loc["year", "from"], t.loc["year", "to"]) == ("2026-02-14", "2026-02-17")


def test_span_bounds_and_monthly():
    t = yearly.summarize(_df(), ["year=winter,summer"], "2026-02-15", "2026-02-16")
    assert t.loc["year", "days"] == 2
    m = yearly.monthly(_df(), ["year=winter,summer"], "2026-02-14", "2026-02-17")
    assert list(m["month"]) == ["2026-02"] and m.loc[0, "days"] == 4


def test_a_cut_date_ends_a_run_before_the_next_one_starts():
    """The pretend winter stops where its frame stops, even when the next run
    has days of its own before that."""
    df = pd.DataFrame([_row("winter", "2026-02-14", -1.0), _row("winter", "2026-02-15", -2.0),
                       _row("summer", "2026-02-14", -8.0), _row("summer", "2026-02-15", -9.0),
                       _row("summer", "2026-02-16", -3.0)])
    d = yearly.lane_days(df, ["winter@2026-02-15", "summer"], "2026-02-14", "2026-02-16")
    assert list(d["date"]) == ["2026-02-14", "2026-02-15", "2026-02-16"]
    assert list(d["cash_eur"]) == [-1.0, -2.0, -3.0]
