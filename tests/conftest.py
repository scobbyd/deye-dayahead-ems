"""Put the repo on the path for the tests.

  repo root            `import emhasscore`, `import backtest`
  ha/pyscript_helpers  the emhass_core facade the wrapper loads by file path;
                       the tests import it the same way (`import emhass_core`)
  tests/               the golden package (test_golden.py, golden/build.py)
"""
import pathlib
import sys

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT / "ha" / "pyscript_helpers"))
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))


# Tests synced from the reference site that need what this repo does not ship:
# its archived plans and writer ticks (real household data), or its internal
# go-live tools. They run on the reference site and skip here.
import pytest  # noqa: E402

HAVE_SITE_DATA = (TESTS / "golden" / "plans").is_dir()
NEEDS_SITE_DATA = {
    "test_writer_day_0905_never_writes_a_dangerous_field_the_plan_did_not_import",
    "test_virtual_day_soc_anchor_resets_the_pack_at_a_step_and_at_zero_equals_soc_start",
    "test_virtual_day_takes_the_measured_pack_meter_and_soc_over_the_past_in_live_mode",
    "test_virtual_day_charges_the_loss_back_only_where_no_meter_settled",
    "test_guard_against_the_archive_of_2026_09_26_saves_exactly_the_six_phantom_writes",
    "test_rolled_slices_and_rehydrate_carry_yesterdays_anchor_and_measured_lanes",
    "test_ladder_actual_rung_takes_the_anchor_and_the_measured_lanes_on_a_live_day",
}
NEEDS_SITE_TOOL = {
    "test_writer_entity_map_agrees_with_the_go_live_harness",
    "test_writer_review_flags_an_off_tick_that_left_a_diff_standing",
    "test_walk_tracking_prices_the_gap_between_the_standing_register_and_the_plan",
    "test_walk_tracking_reports_the_signed_value_at_the_step_price",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.originalname in NEEDS_SITE_TOOL:
            item.add_marker(pytest.mark.skip(reason="uses an internal go-live tool of the reference site (not shipped)"))
        elif item.originalname in NEEDS_SITE_DATA and not HAVE_SITE_DATA:
            item.add_marker(pytest.mark.skip(reason="golden data absent (real household data, not shipped)"))
