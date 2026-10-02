"""EMHASS shadow planner: native compute module (no Home Assistant imports).

Loaded by pyscript/emhass_shadow.py through importlib and executed under
task.executor, so everything here runs natively, off the event loop. Pure
functions first (pytest-able from tests/), then HTTP to the
add-on, the plan archive, and the scoring.

Conventions
  power W; P_grid positive = import; P_batt positive = discharge (EMHASS)
  money EUR; positive = money out of the house
  timestamps tz-aware; the plan grid is 15-minute steps counted from t0, and
  every step arithmetic goes through UTC so DST days come out as 92 or 100
  steps instead of a wall-clock 96

This module is the FACADE: every name lives in the emhasscore package beside it
(one module per concern, see emhasscore/__init__.py) and is re-exported here, so
the wrapper's file-path import and the tests' `import emhass_core` are unchanged.
pyscript loads this file by path, so the package directory is put on sys.path
first, and a stale package is purged so a pyscript reload sees fresh modules.
"""
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
for _name in [m for m in _sys.modules if m == 'emhasscore' or m.startswith('emhasscore.')]:
    del _sys.modules[_name]

from emhasscore import plant, grid, series, objective, deye, addon, archive, writer, repair, slices, planning, scoring, ab, ladder, rebalance  # noqa: E402  (the submodules, for tests that patch one)
from emhasscore.plant import PLANT  # noqa: E402
from emhasscore.grid import (  # noqa: E402
    ceil_step, expected_steps, horizon, _parse_ts, _slot, STEP_H, STEP_MIN, step_times,
)
from emhasscore.series import (  # noqa: E402
    fifteen_min_series, GROWATT_SHARE, LOAD_MIN_REF_DAYS, LOAD_MIX_ALPHA, load_profile, LOAD_REF_DAYS,
    load_series, MAX_PV_GAPS, _median, price_series, pv_mix, PV_P10_MIX, pv_series, pv_split, tariff,
    window_series,
)
from emhasscore.objective import (  # noqa: E402
    apply_knobs, apply_plant, soc_target_level, aux_cut_decision, BATT_SIDE_A, BATT_SIDE_B_KW, bus_to_port_w, cells_to_port_w, conservatism, ETA_AC_DC_V2,
    ETA_C_V2, ETA_D_V2, ETA_DC_AC_V2, GRID_CHARGE_AC_MAX_W, STANDBY_LOAD_W, AUX_CUT_OFF_MIN_KWH, AUX_CUT_OFF_RATIO, AUX_CUT_ON_MIN_KWH, batt_power_limits,
    AUX_CUT_ON_RATIO, build_payload, CAPACITY_KWH, cut_thresholds, DEFICIT_BASE, ETA_BRIDGE, GRID_CAP_W,
    is_v2, knobs, LIVE_KNOBS, loss_adjustment, LP_MIP_REL_GAP, P_NOM_BATT_KW, P_NOM_INV_KW, Q_BRIDGE, Q_PORT,
    REBALANCE_BUDGET_H, REBALANCE_DWELL_H, REBALANCE_FULL_LEVEL, REBALANCE_LOOKBACK_D, REBALANCE_PULL, REBALANCE_TOP_V, rebalance_schedule, REBALANCE_SURPLUS_OFF_DAY, REBALANCE_SOC_FINAL_DAY, REBALANCE_PULL_DAY, REBALANCE_PULL_PER_DAY, rebalance_clock_days, SOC_FINAL_TARGET,
    SOC_MAX, SOC_MIN, soc_target_timestep, step_cost, stress_costs, SURPLUS_BASE, plan_etas, temp_latch, temp_ramp,
)
from emhasscore.deye import (  # noqa: E402
    clamp_write, deye_amps, DEYE_BASELINE, DEYE_BATT_DEADBAND_W, DEYE_CLAMP_DEADBAND_A, DEYE_CLAMP_MARGIN_A,
    DEYE_CLAMP_MARGIN_FRAC, deye_command, DEYE_CURRENT_MAX_A, deye_delivered_a, DEYE_CURRENT_STEP_A, DEYE_PACK_R_OHM, loaded_voltage,
    DEYE_DISCHARGE_DEADBAND_A, DEYE_GRID_CHARGE_DEADBAND_A, DEYE_CURRENT_QUANT_A, DEYE_GRID_DEADBAND_W,
    DEYE_PACK_V, deye_response, DEYE_TIER, ETA_C, ETA_D, integrate_soc, settle_slice, settle_step, soc_dwell_h,
    quantise_amps, setpoint_write, SETTLE_MARGIN, wanted_clamp, wanted_clamp_a,
)
from emhasscore.addon import (  # noqa: E402
    addon_holds_plan, emhass_get, emhass_post, health, ml_action, OMITTED_CONFIG_KEYS, publish, PUBLISH_MAP,
    solve,
)
from emhasscore.archive import (  # noqa: E402
    ARCHIVE_REACH, ARCHIVE_SUFFIX, aux_cut_state, day_slice, days_since_full, _HEAD_CACHE, iter_organic_plans,
    list_plans, load_plan, newest_plan_for_day, organic_plans, original_plan_for_day, plan_for_day,
    plan_heads, _plan_stem, virtual_soc_at, write_plan_archive,
)
from emhasscore.writer import (  # noqa: E402
    append_tick, apply_ceilings, baseline_record, calibrate_amps, ceiling_writes, compile_step, CURRENT_FIELDS, day_step, day_was_live, fold_writes,
    guard_memory, guard_standing, soc_floor_tripped, SOC_FLOOR_RELEASE_PTS, writer_ceilings,
    heat_cut_tripped, heat_cut_amps, HEAT_CUT_RELEASE_C, HEAT_CUT_FIELDS, HEAT_CUT_FALLBACK_V,
    held_record, last_counts, off_baseline, quantise_record, segment_of, SEGMENT_MEAN_INTENTS, WRITER_KNOBS,
    order_writes, read_anchor, soc_anchor_for_day, write_anchor,
    RESTORE_ORDER, same, step_in_force, wants_writes, writer_diff, WRITER_ENTITY, WRITER_FIELDS, WRITER_NEVER,
    WRITER_STALE_MIN, writer_tick,
)
from emhasscore.repair import (  # noqa: E402
    effective_load, PV_CURTAIL_SOC_PCT, PV_DAYLIGHT_W, pv_potential,
)
from emhasscore.slices import (  # noqa: E402
    compact_slice, rehydrate, rolled_slices, virtual_day,
)
from emhasscore.planning import (  # noqa: E402
    load_exact_key, run_plan,
)
from emhasscore.scoring import (  # noqa: E402
    cash_in_frame, hindsight_day, lambda_for, replay_day, soc_term,
)
from emhasscore.ladder import (  # noqa: E402
    LADDER_COLUMNS, LANE_COLUMNS, ladder_day, ladder_earned_series, ladder_hours_fill, ladder_run,
    ladder_shadow_series, ladder_summary, ladder_windows, hours_path, PUBLISH_HOUR, read_ladder, RUNGS,
    today_hours, upsert_ladder, upsert_ladder_hours, upsert_tariff_hours,
)
from emhasscore.ab import (  # noqa: E402
    ab_apply_plan, AB_LIST_KEYS, ab_load, AB_OWN_KEYS, _ab_payload, AB_RUNTIME_KEYS, _ab_solves, ab_store,
    ab_summary, ab_validate, ab_walk,
)


def rebalance_step(path: str, slice_start, series: list | None, n_past: int, level: float,
                   dwell_h: float | None = None) -> dict:
    """The wrapper's one call per tick for the rebalancing clock
    (emhasscore.rebalance): load the state file, fold today's settled quarters
    of `series` (the bank voltage's 15-minute means live) into it at `level`,
    save, return the state for inp["rebalance"]. A missing or unreadable file
    starts empty, which the schedule reads as overdue."""
    import json as _json
    import os as _os
    state = None
    try:
        with open(path) as f:
            state = _json.load(f)
    except (OSError, ValueError):
        state = None
    if series is not None and slice_start:
        state = rebalance.update(state, slice_start, series, int(n_past or 0), level,
                                 dwell_h=float(dwell_h) if dwell_h is not None else REBALANCE_DWELL_H)
    else:
        state = rebalance.load_state(state)
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(state, f)
    _os.replace(tmp, path)
    return state
