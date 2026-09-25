#!/usr/bin/env bash
# One-off after the package is loaded: seed the EMHASS tariff and scoring
# helpers (they carry no initial: on purpose). Re-run any time to reset.
#
# Environment (set in the shell or in a .env file at the repo root, KEY=value;
# see env.sh):
#   HA_URL   base URL of Home Assistant (the address you open it at, port included)
#   HA_LLAT  a long-lived access token
#
# The values are the reference plant's. The plant-specific ones are marked;
# the knob layer's values equal the constants in emhasscore (plant.json).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/env.sh"
need_env HA_URL HA_LLAT || exit 64
set_num() {
  curl -sf -X POST -H "Authorization: Bearer $HA_LLAT" -H "Content-Type: application/json" \
    "$HA_URL/api/services/input_number/set_value" \
    -d "{\"entity_id\":\"input_number.$1\",\"value\":$2}" >/dev/null && echo "  $1 = $2"
}
# tariff: Dutch 2026 first-bracket energy tax EXCL. BTW (0,11085 incl.); set yours
set_num emhass_energy_tax_eur_kwh 0.09161
set_num emhass_supplier_fee_eur_kwh 0.02
set_num emhass_btw_pct 21
set_num emhass_feedin_fee_eur_kwh 0.00
set_num emhass_lambda_frac 0.90
set_num emhass_growatt_share 0.288             # plant: pv.micro_share; 0 when you have no microinverter
set_num emhass_rebalance_dwell_h 2.0
set_num emhass_plan_stale_hours 26
set_num emhass_pv_p10_mix 0.20                 # plant: pv.p10_mix
# the live knob layer (core.LIVE_KNOBS); values = the constants in emhasscore
set_num emhass_stress_scale 1.0
set_num emhass_surplus_base 0.005
set_num emhass_deficit_threshold 0.20
set_num emhass_deficit_cost 0.01
set_num emhass_soc_final 0.50                  # plant: battery.soc_final_target
set_num emhass_weight_battery_discharge 0.01
set_num emhass_batt_power_max_w 12500          # plant: battery.p_nom_kw x 1000
set_num emhass_soc_min 0.10                    # plant: battery.soc_min
set_num emhass_soc_max 1.00                    # plant: battery.soc_max
set_num emhass_soc_target 0
set_num emhass_aux_cut_on_ratio 1.5
set_num emhass_aux_cut_on_min_kwh 2.0
set_num emhass_aux_cut_off_ratio 1.2
set_num emhass_aux_cut_off_min_kwh 1.0
# Dashboard-only: the ladder ledger points at the period containing today. It
# carries no initial: like every helper here, and an input_number without one
# seeds to its MIN, which here is -520 weeks.
set_num emhass_ladder_offset 0
curl -sf -X POST -H "Authorization: Bearer $HA_LLAT" -H "Content-Type: application/json" \
  "$HA_URL/api/services/input_datetime/set_datetime" \
  -d '{"entity_id":"input_datetime.emhass_soc_target_at","time":"17:00:00"}' >/dev/null && echo "  emhass_soc_target_at = 17:00"
echo "input_boolean.emhass_shadow_enabled is left as it is; turn it on after the first verified plan"
