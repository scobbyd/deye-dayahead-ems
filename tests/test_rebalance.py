"""The rebalancing clock (emhasscore.rebalance) and its schedule."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from emhasscore import rebalance as rb
from emhasscore.objective import (REBALANCE_BUDGET_H, REBALANCE_DWELL_H, REBALANCE_FULL_LEVEL, REBALANCE_PULL,
                                  rebalance_clock_days, rebalance_schedule)

AMS = ZoneInfo("Europe/Amsterdam")
T0 = datetime(2026, 9, 10, 0, 0, tzinfo=AMS)
TOP = REBALANCE_FULL_LEVEL * 100.0


def _volts(top_from, top_to, n=96, hi=100.0, lo=60.0):
    """A day of 15-minute SOC means (%): at full from quarter top_from to top_to.
    (The name is from the voltage clock of 2026-10-03..10-10; the mechanics are the same.)"""
    return [hi if top_from <= k < top_to else lo for k in range(n)]


def _q(k):
    return T0 + timedelta(minutes=15 * k)


def test_the_defaults_are_seans_2026_10_10():
    # one settled quarter at full (mean SOC >= 99,5 %) resets the clock
    assert TOP == 99.5 and REBALANCE_DWELL_H == 0.0 and REBALANCE_BUDGET_H == 0.25


def test_nothing_on_record_is_overdue():
    s = rebalance_schedule(None)
    assert s["battery_soc_deficit_threshold"] == 1.0 and s["battery_soc_deficit_cost"] == REBALANCE_PULL
    assert s["battery_soc_surplus_cost"] == 0.0
    assert rb.days_since(None, T0) is None and rb.overdue_days() == rebalance_clock_days(None)


def test_every_quarter_at_full_counts_by_default():
    st = rb.update(None, T0, _volts(48, 49), 96, TOP)
    assert st["runs"] == [[_q(48).isoformat(), _q(49).isoformat()]] and rb.top_hours(st) == 0.25


def test_99_percent_is_not_full():
    st = rb.update(None, T0, _volts(48, 60, hi=99.0), 96, TOP)
    assert st["runs"] == [] and rb.days_since(st, T0 + timedelta(days=1)) is None


def test_with_a_minimum_a_stretch_counts_only_from_it_on():
    # dwell knob 1 h: 45 minutes at full count nothing
    st = rb.update(None, T0, _volts(48, 51), 96, TOP, dwell_h=1.0)
    assert st["runs"] == [] and rb.top_hours(st) == 0.0
    # exactly 60 minutes: the stretch counts, all of it
    st = rb.update(None, T0, _volts(48, 52), 96, TOP, dwell_h=1.0)
    assert st["runs"] == [[_q(48).isoformat(), _q(52).isoformat()]] and rb.top_hours(st) == 1.0


def test_reaching_full_resets_the_clock():
    # full from 17:00 to 18:00 on day 0: the clock counts from the newest quarter at full, 17:45
    st = rb.update(None, T0, _volts(68, 72), 96, TOP)
    assert rb.days_since(st, _q(72) + timedelta(days=3)) == 3.0 + 0.25 / 24
    # a later single quarter at full resets it again
    st = rb.update(st, T0 + timedelta(days=5), _volts(64, 65), 96, TOP)
    assert rb.days_since(st, T0 + timedelta(days=5, hours=16, minutes=15)) == 0.25 / 24


def test_the_stretch_continues_across_midnight_and_ticks_are_idempotent():
    day1 = _volts(92, 96)                              # 23:00 to midnight: 1 h, counts
    st = rb.update(None, T0, day1, 96, TOP)
    assert rb.top_hours(st) == 1.0
    assert rb.update(st, T0, day1, 96, TOP) == st      # the same day folded again: nothing double-counted
    st = rb.update(st, T0 + timedelta(days=1), _volts(0, 8), 8, TOP)   # on to 02:00 the next day: one stretch
    assert st["runs"] == [[_q(92).isoformat(), (T0 + timedelta(days=1, hours=2)).isoformat()]]


def test_a_gap_breaks_the_stretch_and_only_the_settled_part_counts():
    st = rb.update(None, T0, _volts(48, 56), 51, TOP)  # 8 quarters at the top, 3 settled
    assert st["runs"] == [[_q(48).isoformat(), _q(51).isoformat()]] and st["run_start"] == _q(48).isoformat()
    v = [100.0] * 3 + [None] + [100.0] * 3               # 45 min, a hole, 45 min: two stretches
    assert len(rb.update(None, T0, v, 7, TOP)["runs"]) == 2
    # with a 1 h minimum neither half counts
    assert rb.update(None, T0, v, 7, TOP, dwell_h=1.0)["runs"] == []


def test_stretches_older_than_the_lookback_are_dropped():
    st = rb.update(None, T0, _volts(0, 32), 96, TOP)
    st = rb.update(st, T0 + timedelta(days=31), _volts(0, 0), 96, TOP)
    assert st["runs"] == [] and rb.days_since(st, T0 + timedelta(days=32)) is None


def test_a_state_from_the_single_dwell_clock_starts_empty():
    old = {"last_full": "2026-09-25T17:45:00+02:00", "run_quarters": 0, "run_end": None, "seen": None}
    assert rb.load_state(old)["runs"] == [] and rb.days_since(old, T0) is None


def test_published_numbers_and_phases():
    st = rb.update(None, T0, _volts(48, 80), 96, TOP)                     # full 12:00-20:00 on day 0
    p = rb.published(st, _q(80) + timedelta(days=3))
    assert p["top_hours"] == 8.0 and p["clock_days"] == round(3.0 + 0.25 / 24, 2) and p["phase"] == "fresh"
    assert p["stretches"] == 1 and p["budget_start"] == _q(79).isoformat() and p["last_stretch_end"] == _q(80).isoformat()
    for days, phase in ((7.5, "surplus off"), (12.5, "end target"), (14.5, "pulling")):
        assert rb.published(st, T0 + timedelta(days=days, hours=20))["phase"] == phase, days
    # overdue: the graph keeps a value (the lookback), the phase says so
    p = rb.published(None, T0)
    assert p["top_hours"] == 0.0 and p["clock_days"] == 30.0 and p["phase"] == "overdue" and p["budget_start"] is None


# ---- the pull latch: on at day 14, off at the first settled quarter at full (2026-10-10) ----

def _hold(state, day, q_from, q_to, n_past=96, hi=100.0):
    """Fold a day at full from quarter q_from to q_to, then step the latch at the slice's end."""
    t = T0 + timedelta(days=day)
    st = rb.update(state, t, _volts(q_from, q_to, hi=hi), n_past, TOP)
    return rb.latch(st, t + timedelta(minutes=15 * n_past))


def test_the_latch_engages_at_day_14_and_when_overdue():
    st = rb.update(None, T0, _volts(76, 80), 96, TOP)                     # full 19:00-20:00 on day 0
    assert rb.latch(st, T0 + timedelta(days=13, hours=20))["latch_since"] is None
    on = rb.latch(st, T0 + timedelta(days=14, hours=20))
    assert on["latch_since"] == (T0 + timedelta(days=14, hours=20)).isoformat()
    assert rb.latch(None, T0)["latch_since"] == T0.isoformat()             # nothing on record: overdue, latched


def test_the_pull_keeps_stepping_up_until_the_pack_is_full():
    st = rb.update(None, T0, _volts(76, 80), 96, TOP)
    st = rb.latch(st, T0 + timedelta(days=14, hours=20))
    st = _hold(st, 15, 48, 56, hi=99.0)                                   # 2 h at 99 %: not full
    now = T0 + timedelta(days=16)
    assert st["latch_since"] is not None and rb.days_since(st, now) >= 15.0
    assert rebalance_schedule(rb.days_since(st, now))["battery_soc_deficit_cost"] == 0.002


def test_one_quarter_at_full_releases_the_latch_and_the_clock_reads_from_it():
    st = rb.latch(None, T0)                                               # nothing on record: latched
    st = _hold(st, 0, 68, 69)                                             # 17:00-17:15 at full
    assert st["latch_since"] is None and st["released"] == (T0 + timedelta(days=1)).isoformat()
    later = T0 + timedelta(days=2)
    assert abs(rb.days_since(st, later) - (2 - 17 / 24)) < 1e-9
    assert rb.latch(st, later)["latch_since"] is None                     # and it does not re-latch
    assert rb.published(st, later)["phase"] == "fresh"
