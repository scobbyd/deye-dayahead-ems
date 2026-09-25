"""The CSV-fed actuals must be the golden fixtures' actuals: same shape, same
values, for a day both cover."""
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import pytest
import emhass_core as core
from backtest.actuals import (raw_stats_from_csv, day_actuals, window_actuals, export_states_from_csv,
                              export_off, fallback_mask, merge_export_states, refresh_export_states)
from golden import fixtures as F

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
CSV = os.path.join(DATA, "recorder_5min.csv")              # the recorder's 5-minute statistics, synced
EXPORT_SW = os.path.join(DATA, "export_switch.csv")        # the export switch's state history, synced


@pytest.fixture(autouse=True)
def _need_data(request):
    """The four tests on the synced recorder CSV skip without it; the pure
    functions below run everywhere."""
    if request.node.name.startswith(("test_csv_", "test_window_", "test_without_", "test_switch_")):
        if not (os.path.isfile(CSV) and os.path.isfile(EXPORT_SW) and os.path.exists(F.ACTUALS_5MIN)):
            pytest.skip(f"no synced recorder data ({CSV}, {EXPORT_SW}) or golden actuals")


def test_csv_matches_golden_shape_and_values():
    stats = raw_stats_from_csv(CSV)
    day = date(2026, 9, 5)
    got = day_actuals(stats, core, day)
    ref = F.actuals_for_day(core, day)
    assert set(got) >= set(ref)
    for k in ("pv_w", "load_w", "grid_w", "batt_dc_w", "micro_w"):
        assert len(got[k]) == 96
        assert max(abs(a - b) for a, b in zip(got[k], ref[k])) < 1.0, k


def test_window_covers_horizon():
    stats = raw_stats_from_csv(CSV)
    t0 = datetime(2026, 9, 4, 23, 45, tzinfo=ZoneInfo("Europe/Amsterdam"))
    win = window_actuals(stats, core, t0, 193)
    assert all(len(win[k]) == 193 for k in ("pv_w", "grid_w", "batt_dc_w", "micro_w"))


def test_without_switch_history_equals_the_fixtures_exactly():
    """No export_pts: the fixtures' SOC-only fallback, every series to the bit."""
    stats = raw_stats_from_csv(CSV)
    for day in (date(2026, 9, 3), date(2026, 9, 5)):
        got, ref = day_actuals(stats, core, day), F.actuals_for_day(core, day)
        assert set(got) == set(ref)
        for k in ref:
            assert got[k] == ref[k], (day, k)


def test_switch_history_narrows_the_mask_like_the_wrapper():
    """The wrapper's _fallback_mask is SOC > 95 % AND the export switch off; the
    SOC half alone over-fires. 2026-09-05's window: 50 steps on SOC alone, 29
    with the switch; the day itself 38 against 19."""
    stats = raw_stats_from_csv(CSV)
    pts = export_states_from_csv(EXPORT_SW)
    t0 = datetime(2026, 9, 4, 23, 15, tzinfo=ZoneInfo("Europe/Amsterdam"))
    soc_only, both = window_actuals(stats, core, t0, 195), window_actuals(stats, core, t0, 195, pts)
    assert sum(soc_only["curtailed"]) == 50 and sum(both["curtailed"]) == 29
    assert all(b <= s for s, b in zip(soc_only["curtailed"], both["curtailed"]))
    day = date(2026, 9, 5)
    assert sum(day_actuals(stats, core, day)["curtailed"]) == 38
    assert sum(day_actuals(stats, core, day, None, pts)["curtailed"]) == 19
    for k in ("pv_w", "grid_w", "batt_dc_w", "soc_pct", "micro_w", "pv_peak_w"):
        assert soc_only[k] == both[k], k          # the history touches the mask and nothing else


def test_export_off_walks_the_state_at_the_step_start():
    pts = [(1000.0, "on"), (2000.0, "off"), (2500.0, "unavailable"), (3700.0, "on")]
    t0 = datetime.fromtimestamp(1900.0, tz=timezone.utc)
    # steps start at 1900, 2800, 3700, 4600: on (state at t0), off, on (the change at 3700 counts), on
    assert export_off(pts, t0, 4) == [0.0, 1.0, 0.0, 0.0]
    # nothing recorded at or before the window and nothing inside it: no history, the SOC half alone
    assert export_off(pts, datetime.fromtimestamp(100.0, tz=timezone.utc), 1) is None
    # a change inside the window with no state at its start: the first change is the state
    # from step 0 on, as the wrapper's walk has it (cur = pts[0])
    assert export_off(pts, datetime.fromtimestamp(100.0, tz=timezone.utc), 2) == [0.0, 0.0]
    assert fallback_mask(None, t0, 3, [96.0, 96.0, 90.0], core.PV_CURTAIL_SOC_PCT) == [1.0, 1.0, 0.0]
    assert fallback_mask(pts, t0, 3, [96.0, 96.0, 96.0], core.PV_CURTAIL_SOC_PCT) == [0.0, 1.0, 0.0]


def test_merge_export_states_drops_the_start_marker_and_duplicates():
    """A refetch merges on (timestamp, state): rows already in the file are not
    doubled, the endpoint's state-at-start marker (stamped on `start` itself)
    is dropped, a new transition is added, and the result is sorted."""
    existing = [("2026-09-01T10:00:00+00:00", "off"), ("2026-09-01T12:00:00+00:00", "on")]
    history = [[{"entity_id": "switch.inverter_export_surplus", "state": "on", "attributes": {},
                 "last_changed": "2026-09-01T11:00:00+00:00", "last_updated": "2026-09-01T11:00:00+00:00"},
                {"state": "on", "last_changed": "2026-09-01T12:00:00+00:00"},          # already in the file
                {"state": "unavailable", "last_changed": "2026-09-01T13:00:00+00:00"},  # kept: the walk filters
                {"state": "off", "last_changed": "2026-09-02T08:00:00+00:00"}]]         # new
    out = merge_export_states(existing, history, "2026-09-01T11:00:00+00:00")
    assert out == [("2026-09-01T10:00:00+00:00", "off"), ("2026-09-01T12:00:00+00:00", "on"),
                   ("2026-09-01T13:00:00+00:00", "unavailable"), ("2026-09-02T08:00:00+00:00", "off")]
    # the same fetch again changes nothing; an empty response keeps the file
    assert merge_export_states(out, history, "2026-09-01T11:00:00+00:00") == out
    assert merge_export_states(out, [[]], "2026-09-01T11:00:00+00:00") == out
    # a marker at a start the file has no row for is still not a transition
    marker = [[{"state": "on", "last_changed": "2026-09-03T00:00:00+00:00"}]]
    assert merge_export_states(out, marker, "2026-09-03T00:00:00+00:00") == out


def test_refresh_export_states_writes_the_merged_file(monkeypatch, tmp_path):
    """The HTTP wrapper around the merge, with requests.get stubbed: the call the
    CSV was built with, the file read back in, the merged rows written out."""
    import requests
    calls = []

    class Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers, timeout))
        return Resp([[{"state": "on", "last_changed": "2026-09-01T00:00:00+00:00"},      # the start marker
                      {"state": "off", "last_changed": "2026-09-01T06:00:00+00:00"},
                      {"state": "on", "last_changed": "2026-09-01T07:00:00+00:00"}]])
    monkeypatch.setattr(requests, "get", fake_get)
    path = tmp_path / "sw.csv"
    path.write_text("ts_utc,state\n2026-08-31T20:00:00+00:00,on\n2026-09-01T06:00:00+00:00,off\n")
    n = refresh_export_states(str(path), "http://ha.test/", "tok", "2026-09-01T00:00:00+00:00",
                              "2026-09-02T00:00:00+00:00")
    assert n == 3
    # csv.writer's own line ends, the layout the committed CSV has (read_text would fold them)
    assert path.read_bytes() == (b"ts_utc,state\r\n2026-08-31T20:00:00+00:00,on\r\n"
                                 b"2026-09-01T06:00:00+00:00,off\r\n2026-09-01T07:00:00+00:00,on\r\n")
    url, params, headers, timeout = calls[0]
    assert url == "http://ha.test/api/history/period/2026-09-01T00:00:00+00:00"
    assert params == {"filter_entity_id": "switch.inverter_export_surplus", "end_time": "2026-09-02T00:00:00+00:00",
                      "minimal_response": "", "no_attributes": ""}
    assert headers == {"Authorization": "Bearer tok"} and timeout == 60
    assert export_states_from_csv(str(path))[-1][1] == "on"
