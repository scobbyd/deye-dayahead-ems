"""The rebalancing clock (emhasscore.rebalance) and its schedule."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from emhasscore import rebalance as rb
from emhasscore.objective import (REBALANCE_BUDGET_H, REBALANCE_DWELL_H, REBALANCE_PULL, REBALANCE_TOP_V,
                                  rebalance_clock_days, rebalance_schedule)

AMS = ZoneInfo("Europe/Amsterdam")
T0 = datetime(2026, 9, 10, 0, 0, tzinfo=AMS)
TOP = REBALANCE_TOP_V


def _volts(top_from, top_to, n=96, hi=56.6, lo=53.0):
    """A day of 15-minute bank-voltage means: at the top from quarter top_from to top_to."""
    return [hi if top_from <= k < top_to else lo for k in range(n)]


def _q(k):
    return T0 + timedelta(minutes=15 * k)


def test_the_defaults_are_seans_2026_10_03():
    assert REBALANCE_TOP_V == 56.0 and REBALANCE_DWELL_H == 1.0 and REBALANCE_BUDGET_H == 8.0


def test_nothing_on_record_is_overdue():
    s = rebalance_schedule(None)
    assert s["battery_soc_deficit_threshold"] == 1.0 and s["battery_soc_deficit_cost"] == REBALANCE_PULL
    assert s["battery_soc_surplus_cost"] == 0.0
    assert rb.days_since(None, T0) is None and rb.overdue_days() == rebalance_clock_days(None)


def test_a_stretch_counts_only_from_the_minimum_on():
    # 45 minutes at the top: nothing counts
    st = rb.update(None, T0, _volts(48, 51), 96, TOP)
    assert st["runs"] == [] and rb.top_hours(st) == 0.0
    # exactly 60 minutes: the stretch counts, all of it
    st = rb.update(None, T0, _volts(48, 52), 96, TOP)
    assert st["runs"] == [[_q(48).isoformat(), _q(52).isoformat()]] and rb.top_hours(st) == 1.0


def test_the_clock_is_the_age_of_the_newest_budget_hours():
    # 8 h at the top from 12:00 to 20:00 on day 0: the budget began at 12:00
    st = rb.update(None, T0, _volts(48, 80), 96, TOP)
    assert rb.days_since(st, T0 + timedelta(days=3, hours=12)) == 3.0
    # 7 h only: the lookback does not hold the budget, overdue
    st7 = rb.update(None, T0, _volts(48, 76), 96, TOP)
    assert rb.days_since(st7, T0 + timedelta(days=1)) is None


def test_shorter_stretches_on_several_days_add_up():
    st = None
    for d in range(4):                                  # 2 h a day at the top, 14:00 to 16:00
        st = rb.update(st, T0 + timedelta(days=d), _volts(56, 64), 96, TOP)
    assert rb.top_hours(st) == 8.0
    # the newest 8 h began with the first stretch: day 0, 14:00
    assert rb.days_since(st, T0 + timedelta(days=4, hours=14)) == 4.0
    # a 30-minute touch on day 4 adds nothing
    st = rb.update(st, T0 + timedelta(days=4), _volts(56, 58), 96, TOP)
    assert rb.top_hours(st) == 8.0


def test_a_hold_keeps_the_clock_high_until_the_budget_is_in():
    old = rb.update(None, T0, _volts(0, 32), 96, TOP)                     # 8 h on day 0, 00:00-08:00
    hold_day = T0 + timedelta(days=15)
    for k in (4, 16, 31):                                                 # 1 h, 4 h, 7 h 45 into a night hold
        st = rb.update(old, hold_day, _volts(0, k), k, TOP)
        assert rb.days_since(st, hold_day + timedelta(minutes=15 * k)) > 14.0, k
    st = rb.update(old, hold_day, _volts(0, 32), 32, TOP)                 # the 8th hour: the budget is in
    assert rb.days_since(st, hold_day + timedelta(hours=8)) == 8 / 24


def test_the_stretch_continues_across_midnight_and_ticks_are_idempotent():
    day1 = _volts(92, 96)                              # 23:00 to midnight: 1 h, counts
    st = rb.update(None, T0, day1, 96, TOP)
    assert rb.top_hours(st) == 1.0
    assert rb.update(st, T0, day1, 96, TOP) == st      # the same day folded again: nothing double-counted
    st = rb.update(st, T0 + timedelta(days=1), _volts(0, 8), 8, TOP)   # on to 02:00 the next day: one stretch
    assert st["runs"] == [[_q(92).isoformat(), (T0 + timedelta(days=1, hours=2)).isoformat()]]


def test_a_gap_breaks_the_stretch_and_only_the_settled_part_counts():
    st = rb.update(None, T0, _volts(48, 56), 51, TOP)  # 8 quarters at the top, 3 settled
    assert st["runs"] == [] and st["run_start"] == _q(48).isoformat()
    v = [56.6] * 3 + [None] + [56.6] * 3               # 45 min, a hole, 45 min: neither counts
    assert rb.update(None, T0, v, 7, TOP)["runs"] == []


def test_stretches_older_than_the_lookback_are_dropped():
    st = rb.update(None, T0, _volts(0, 32), 96, TOP)
    st = rb.update(st, T0 + timedelta(days=31), _volts(0, 0), 96, TOP)
    assert st["runs"] == [] and rb.days_since(st, T0 + timedelta(days=32)) is None


def test_a_state_from_the_single_dwell_clock_starts_empty():
    old = {"last_full": "2026-09-25T17:45:00+02:00", "run_quarters": 0, "run_end": None, "seen": None}
    assert rb.load_state(old)["runs"] == [] and rb.days_since(old, T0) is None


def test_published_numbers_and_phases():
    st = rb.update(None, T0, _volts(48, 80), 96, TOP)                     # 8 h, day 0 12:00-20:00
    p = rb.published(st, T0 + timedelta(days=3, hours=12))
    assert p["top_hours"] == 8.0 and p["clock_days"] == 3.0 and p["phase"] == "fresh"
    assert p["stretches"] == 1 and p["budget_start"] == _q(48).isoformat() and p["last_stretch_end"] == _q(80).isoformat()
    for days, phase in ((7.5, "surplus off"), (12.5, "end target"), (14.5, "pulling")):
        assert rb.published(st, T0 + timedelta(days=days, hours=12))["phase"] == phase, days
    # overdue: the graph keeps a value (the lookback), the phase says so
    p = rb.published(None, T0)
    assert p["top_hours"] == 0.0 and p["clock_days"] == 30.0 and p["phase"] == "overdue" and p["budget_start"] is None


# ---- the pull latch (2026-10-03): on at day 14, off at 6 h at the top in 48 h ----

def _hold(state, day, q_from, q_to, n_past=96):
    """Fold a day with a stretch at the top from quarter q_from to q_to, then step the latch at the slice's end."""
    t = T0 + timedelta(days=day)
    st = rb.update(state, t, _volts(q_from, q_to), n_past, TOP)
    return rb.latch(st, t + timedelta(minutes=15 * n_past))


def test_the_latch_engages_at_day_14_and_when_overdue():
    st = rb.update(None, T0, _volts(48, 80), 96, TOP)                     # 8 h on day 0, 12:00-20:00
    assert rb.latch(st, T0 + timedelta(days=13, hours=12))["latch_since"] is None
    on = rb.latch(st, T0 + timedelta(days=14, hours=12))
    assert on["latch_since"] == (T0 + timedelta(days=14, hours=12)).isoformat()
    assert rb.latch(None, T0)["latch_since"] == T0.isoformat()             # nothing on record: overdue, latched


def test_a_short_hold_does_not_release_the_latch_and_the_pull_keeps_stepping_up():
    # 6 h of short tops 4-10 days ago, 2 h older: the old clock dropped to ~10 d after 2 h at the top
    st = rb.update(None, T0, _volts(48, 56), 96, TOP)                     # 2 h on day 0
    for d in (6, 8, 10):
        st = rb.update(st, T0 + timedelta(days=d), _volts(64, 72), 96, TOP)   # 2 h on days 6, 8, 10
    st = rb.latch(st, T0 + timedelta(days=14, hours=12))                   # day 14,5 since day 0: latched
    assert st["latch_since"] is not None
    st = _hold(st, 15, 48, 56)                                            # 2 h at the top on day 15
    now = T0 + timedelta(days=16)
    assert st["latch_since"] is not None
    assert rb.raw_days(st, now) < 12                                      # the budget alone would let go
    assert rb.days_since(st, now) >= 15.5                                 # the latch holds it: pull on, 2nd day
    assert rebalance_schedule(rb.days_since(st, now))["battery_soc_deficit_cost"] == 0.002


def test_six_hours_in_48_releases_and_the_release_counts_as_a_balance():
    st = rb.update(None, T0, _volts(48, 80), 96, TOP)                     # 8 h on day 0
    st = rb.latch(st, T0 + timedelta(days=14, hours=12))
    st = _hold(st, 15, 40, 56)                                            # 4 h on day 15, 10:00-14:00
    assert st["latch_since"] is not None                                  # 4 h of 6
    st = _hold(st, 16, 72, 80)                                            # 2 h on day 16, 18:00-20:00: 6 h in 48 h
    assert st["latch_since"] is None and st["released"] == (T0 + timedelta(days=17)).isoformat()
    # the budget alone reaches back to day 0 (6 h now + 2 h then): the release keeps the clock fresh
    later = T0 + timedelta(days=18)
    assert rb.days_since(st, later) == 1.0
    assert rb.latch(st, later)["latch_since"] is None                     # and it does not re-latch
    assert rb.published(st, later)["phase"] == "fresh"
