"""Measured PV made exogenous again: the curtailment repair (raw Solcast on a throttled step) and the
effective load the scoreboard actually scores."""
from __future__ import annotations

from .grid import STEP_H
from .objective import ETA_BRIDGE
from .plant import PLANT


# PV curtailment repair (2026-09-05). Measured PV is exogenous only while
# the array is not being held back. The Deye throttles the MAIN array once the
# REAL pack is full with nowhere to export, which is a fact about the incumbent
# and not about the weather: on 09-05 the array made 1.496 W at 13:00 against a
# ~7.900 W potential purely because the real pack sat at 99 %. Handing that to
# the counterfactual starves a virtual pack that would have had room for it.
#
# The repair is Solcast, and the reason it is trustworthy exactly when it is
# needed is economic: curtailment is triggered by low or negative prices, which
# are caused by high national solar, which means clear skies here too. The
# microinverter would be the better irradiance reference in principle - it
# bypasses the Deye and is never curtailed - but it is shadowed through most of
# the afternoon hours when curtailment actually happens, so it cannot carry it.
PV_CURTAIL_SOC_PCT = float(PLANT["pv"]["curtail_soc_pct"])    # the crossing the Deye starts holding the main array back at


PV_DAYLIGHT_W = 200.0        # below this the forecast says nothing useful about the ratio


# THE RULE (2026-09-07, restating 09-05): real SOC >= 95 % AND export surplus
# off means the Deye is throttling; a throttled step takes raw Solcast (P50) as
# the available PV. Nothing else. A fitted daily scale (0,815 on 09-05) and a
# 5-minute peak guard were bolted on during 09-05/09-06 and together cut 5,95 kWh
# off that day's potential; both are retired. The Growatt is never an irradiance
# witness either. See the feedback_pv_curtailment_rule memory.


def pv_potential(pv_meas_w, solcast_w, curtailed, peak_w=None,
                 fit_scale: bool = True,
                 min_clean: int = 0,
                 daylight_w: float = PV_DAYLIGHT_W,
                 micro_meas_w=None, micro_fc_w=None) -> tuple[list[float], dict]:
    """Measured PV with the curtailed steps replaced by what the array could
    have made. Returns (pv_pot_w, info).

    MUST-TAKE (2026-09-06). micro_meas_w is the part of the measurement that was
    never throttled - the Growatt on the gen port. On a curtailed step it is
    already making everything it can, so scaling it up with the strings credits
    sun that was never withheld and inflates the fitted scale and
    reconstructed_kwh together. Given it, the repair runs on the curtailable
    remainder and the must-take half is added back untouched. micro_fc_w is the
    same quantity on the forecast side and defaults to micro_meas_w: only
    virtual_day knows each step's plan-in-force share, and everywhere else the
    difference biases the fit by (fc - meas) / main_fc, second order. Passing
    neither collapses to the pre-split behaviour exactly.

    Formerly two guards (a fitted daily SCALE and a 5-minute PEAK guard) sat
    between the mask and the substitution; see RETIRED below.

    RETIRED 2026-09-07: the fitted daily SCALE and the 5-minute PEAK
    guard. The rule is raw Solcast on a masked step, and a masked step whose
    measurement already exceeds Solcast keeps its measurement. `peak_w`,
    `fit_scale` and `min_clean` are accepted and ignored so the callers and the
    wrapper's peak plumbing need no change; info["scale"] is always 1,0.

    `curtailed` is per-step, either boolean or the recorder's 15-minute mean of
    the 0/1 mask sensor, in which case it is the fraction of the step that was
    curtailed and 0,5 is the threshold.
    """
    if micro_meas_w is not None or micro_fc_w is not None:
        mm = [float(v) for v in (micro_meas_w or [0.0] * len(pv_meas_w))]
        mf = [float(v) for v in (micro_fc_w if micro_fc_w is not None else mm)]
        if len(mm) == len(pv_meas_w) and len(mf) == len(solcast_w):
            def _sub(a, b):
                return [max(0.0, float(x) - y) for x, y in zip(a, b)]
            pot, info = pv_potential(_sub(pv_meas_w, mm), _sub(solcast_w, mf), curtailed,
                                     _sub(peak_w, mm) if peak_w is not None else None,
                                     fit_scale, min_clean, daylight_w)
            return [round(pt + m, 1) for pt, m in zip(pot, mm)], info
    n = len(pv_meas_w)
    if len(solcast_w) != n or len(curtailed) != n:
        return [round(float(v), 1) for v in pv_meas_w], {
            "scale": 1.0, "scale_source": "length_mismatch", "clean_steps": 0,
            "curtailed_steps": 0, "repaired_steps": 0, "reconstructed_kwh": 0.0}
    mask = [float(c) >= 0.5 for c in curtailed]
    clean = sum(1 for i in range(n) if not mask[i] and float(solcast_w[i]) >= daylight_w)
    out, repaired, extra = [], 0, 0.0
    for i in range(n):
        m = float(pv_meas_w[i])
        if mask[i] and float(solcast_w[i]) >= daylight_w:
            p = max(m, float(solcast_w[i]))
            if p > m:
                repaired += 1
                extra += (p - m) * STEP_H / 1000.0
            out.append(round(p, 1))
        else:
            out.append(round(m, 1))
    return out, {"scale": 1.0, "scale_source": "raw", "clean_steps": clean,
                 "curtailed_steps": sum(1 for x in mask if x), "repaired_steps": repaired,
                 "freed_steps": 0, "peak_guard": False,
                 "reconstructed_kwh": round(extra, 3)}


def effective_load(grid_w, batt_dc_w, pv_w, eta_bridge: float = ETA_BRIDGE) -> list[float]:
    """The load the site really presented, per step, W:

        L_eff = G_meas + B_meas_ac + PV_meas

    which is the measured load PLUS the ~4,2 kWh/day of standby, conversion loss
    and unmetered circuits that sits between the load sensor and the meter. It
    exists so the hindsight LP optimises the quantity the scoreboard actually
    scores. `L_eff - PV` is by construction the base of replay_day's difference
    form, so the LP's own P_grid comes out equal to G_replay: without it the LP
    maximises its absolute trace `load - PV - battery` while we settle the
    difference, and the two disagree wherever the residual pushes a step across
    the import/export kink in step_cost. It also means the 20/20 lane never
    touches the contested load entity, exactly as the replayed lane does not."""
    out = []
    for i in range(len(grid_w)):
        b = float(batt_dc_w[i])
        b_ac = b * eta_bridge if b >= 0 else b / eta_bridge
        out.append(round(float(grid_w[i]) + b_ac + float(pv_w[i]), 1))
    return out
