"""The settled rebalancing clock (emhasscore.rebalance) and its schedule."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from emhasscore import rebalance as rb
from emhasscore.objective import REBALANCE_PULL, REBALANCE_TARGET_DAYS, rebalance_schedule

AMS = ZoneInfo("Europe/Amsterdam")
T0 = datetime(2026, 9, 10, 0, 0, tzinfo=AMS)


def _soc(full_from, full_to, n=96, hi=100.0, lo=60.0):
    return [hi if full_from <= k < full_to else lo for k in range(n)]


def test_nothing_on_record_is_overdue():
    s = rebalance_schedule(None)
    assert s["battery_soc_deficit_threshold"] == 1.0 and s["battery_soc_deficit_cost"] == REBALANCE_PULL
    assert s["battery_soc_surplus_cost"] == 0.0
    assert rb.days_since(None, T0) is None and rb.overdue_days() == 2 * REBALANCE_TARGET_DAYS


def test_a_full_counts_only_after_the_dwell():
    # 100 % from 12:00 to 13:30 (6 quarters): under a 2 h dwell, no reset
    st = rb.update(None, T0, _soc(48, 54), n_past=96, dwell_h=2.0)
    assert st["last_full"] is None and st["run_quarters"] == 0
    # 12:00 to 14:15 (9 quarters): the dwell completes at 14:00 and the clock
    # follows the END of the stay, 14:15, so days_since counts from when the
    # pack last left the top
    st = rb.update(None, T0, _soc(48, 57), n_past=96, dwell_h=2.0)
    assert st["last_full"] == (T0 + timedelta(hours=14, minutes=15)).isoformat()
    assert rb.days_since(st, T0 + timedelta(days=3, hours=14, minutes=15)) == 3.0


def test_the_run_continues_across_midnight_and_ticks_are_idempotent():
    day1 = _soc(90, 96)                              # full from 22:30 to midnight, 6 quarters
    st = rb.update(None, T0, day1, n_past=96, dwell_h=2.0)
    assert st["last_full"] is None and st["run_quarters"] == 6
    # the same day folded again (a later tick re-reads it): nothing double-counted
    st2 = rb.update(st, T0, day1, n_past=96, dwell_h=2.0)
    assert st2 == st
    # the next day's first half hour (the only settled quarters so far) completes the dwell at 00:30
    day2 = _soc(0, 2)
    st3 = rb.update(st2, T0 + timedelta(days=1), day2, n_past=2, dwell_h=2.0)
    assert st3["last_full"] == (T0 + timedelta(days=1, minutes=30)).isoformat() and st3["run_quarters"] == 8


def test_a_gap_breaks_the_run_and_only_the_settled_half_counts():
    soc = _soc(48, 56) + []                          # 8 quarters at full, but only 52 are settled
    st = rb.update(None, T0, soc, n_past=52, dwell_h=2.0)
    assert st["last_full"] is None and st["run_quarters"] == 4
    # a None quarter (an unsettled hole) resets the run
    soc2 = [100.0] * 4 + [None] + [100.0] * 8
    st = rb.update(None, T0, soc2, n_past=13, dwell_h=2.0)
    assert st["last_full"] == (T0 + timedelta(minutes=15 * 13)).isoformat() and st["run_quarters"] == 8


def test_days_since_runs_from_the_last_dwell_not_the_first():
    st = rb.update(None, T0, _soc(0, 96), n_past=96, dwell_h=2.0)   # full all day: the last quarter is the newest full
    assert st["last_full"] == (T0 + timedelta(days=1)).isoformat()
