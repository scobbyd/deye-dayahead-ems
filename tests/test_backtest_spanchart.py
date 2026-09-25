"""spanchart: chaining a pretend-plant winter lane in front of a live-plant lane."""
import pandas as pd

from backtest import spanchart


def _row(lane, date, cash, bal=-1.0):
    return {"lane": lane, "source": "rec", "date": date, "cash_eur": cash, "supplier_net_eur": bal,
            "supplier_da_eur": bal, "nobatt_cash_eur": 0.5}


def test_chain_relabels_the_earlier_rows_and_drops_their_supplier_columns():
    raw = pd.DataFrame([_row("foresight_winter", "2026-02-14", -3.0), _row("foresight_winter", "2026-02-15", -4.0),
                        _row("foresight_winter", "2026-02-16", -9.0),      # the overlap day stays the winter lane's
                        _row("foresight", "2026-02-16", -5.0), _row("foresight", "2026-02-17", -6.0)])
    out, join = spanchart.chain(raw, ["foresight_winter:foresight"], warm="2026-02-16")
    assert join == "2026-02-15"
    got = out[out["lane"] == "foresight"].sort_values("date")
    assert list(got["date"]) == ["2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17"]
    assert list(got["cash_eur"]) == [-3.0, -4.0, -5.0, -6.0]
    assert got["supplier_net_eur"].isna().tolist() == [True, True, False, False]
    assert list(out[out["lane"] == "foresight_winter"]["date"]) == ["2026-02-16"]


def test_chain_without_a_match_changes_nothing():
    raw = pd.DataFrame([_row("foresight", "2026-02-16", -5.0)])
    out, join = spanchart.chain(raw, ["foresight_winter:foresight"])
    assert join is None and out.equals(raw)


def test_chain_warm_drops_the_lanes_cold_days_and_takes_the_front_up_to_it():
    raw = pd.DataFrame([_row("foresight_winter", "2026-02-14", -3.0), _row("foresight_winter", "2026-02-15", -4.0),
                        _row("foresight", "2026-02-10", -1.0), _row("foresight", "2026-02-16", -5.0)])
    out, join = spanchart.chain(raw, ["foresight_winter:foresight"], warm="2026-02-16")
    assert join == "2026-02-15"
    assert list(out.sort_values("date")["date"]) == ["2026-02-14", "2026-02-15", "2026-02-16"]
