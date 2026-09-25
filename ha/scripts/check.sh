#!/usr/bin/env bash
# Containment and drift checks for the EMHASS add-on. Read-only.
#
# Environment (set in the shell or in a .env file at the repo root, KEY=value;
# see env.sh):
#   HA_URL   base URL of Home Assistant (the address you open it at, port included)  (health, run, contain)
#   HA_LLAT  a long-lived access token                                        (health, run)
#   HA_SSH   ssh target of the HA OS host                                     (contain, config)
#
#   check.sh            run everything
#   check.sh contain    add-on is reachable only from inside the HA host, no unattended updates
#   check.sh config     live add-on config equals ha/config.json
#   check.sh health     binary_sensor.emhass_addon_healthy is on (pyscript, against /config/emhass/config.json)
#   check.sh actions    no automation in ha/packages acts on an EMHASS forecast entity
#   check.sh run        dry-run plan via pyscript; prints horizon, predicted steps, gaps, status, seconds
#   check.sh layers     emhasscore modules import one way only and never the emhass_core facade
#
# Exit code is the number of failed checks.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
# shellcheck source=/dev/null
. "$HERE/env.sh"
SLUG=5b918bf2_emhass          # the community add-on's fixed slug
HOST=5b918bf2-emhass          # its hostname on the Supervisor's internal network
CFG="$HERE/../config.json"
fail=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; fail=$((fail+1)); }
skip() { echo "  skip  $*"; }

addon_json() { ssh "$HA_SSH" "ha apps info $SLUG --raw-json" 2>/dev/null; }

check_contain() {
  echo "containment"
  if ! need_env HA_SSH HA_URL; then skip "containment needs HA_SSH and HA_URL"; return; fi
  local info; info=$(addon_json)
  if [ -z "$info" ] || ! echo "$info" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("result")=="ok" else 1)' 2>/dev/null; then
    skip "add-on $SLUG not installed"; return
  fi
  # 1. host port mapping disabled
  if echo "$info" | python3 -c 'import json,sys; n=json.load(sys.stdin)["data"].get("network") or {}; sys.exit(0 if n.get("5000/tcp") is None else 1)'; then
    ok "host port 5000/tcp is null (ingress only)"; else bad "host port 5000/tcp is mapped; set it to null in the add-on Network panel"; fi
  # 2. nothing answers on the HA host's LAN address
  if curl -s -m 4 -o /dev/null "http://$HA_HOST:5000/get-config"; then
    bad "http://$HA_HOST:5000 answers from the LAN"; else ok "http://$HA_HOST:5000 unreachable from the LAN"; fi
  # 3. reachable inside the Supervisor network (the SSH add-on stands in for Core)
  if ssh "$HA_SSH" "curl -sf -m 5 http://$HOST:5000/get-config >/dev/null 2>&1"; then
    ok "http://$HOST:5000 reachable inside the HA host"; else bad "http://$HOST:5000 not reachable inside the HA host"; fi
  # 4. no unattended updates (upgrades are a manual step in the HA UI; EMHASS
  #    0.16 and 0.17 were breaking minor releases). If you run a nightly
  #    add-on updater, exclude update.emhass_update there as well.
  if echo "$info" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["data"].get("auto_update") is False else 1)'; then
    ok "supervisor auto_update is false"; else bad "supervisor auto_update is on"; fi
  echo "$info" | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print("  info  version", d.get("version"), "state", d.get("state"), "ingress", d.get("ingress"))'
}

check_config() {
  echo "config drift"
  if ! need_env HA_SSH; then skip "config drift needs HA_SSH"; return; fi
  local live; live=$(ssh "$HA_SSH" "curl -sf -m 10 http://$HOST:5000/get-config" 2>/dev/null)
  if [ -z "$live" ]; then skip "add-on API not reachable"; return; fi
  python3 - "$CFG" <<'PY' "$live"
import json, sys
want = json.load(open(sys.argv[1]))
live = json.loads(sys.argv[2])
# /get-config never echoes these two keys (verified on v0.18.1); absence is not drift.
OMITTED = {"data_path", "heat_topology"}
diffs = [(k, want[k], live.get(k, "<absent>")) for k in want
         if not (k in OMITTED and k not in live) and live.get(k, "<absent>") != want[k]]
extra = [k for k in live if k not in want]
if diffs:
    for k, w, l in diffs: print(f"  FAIL  {k}: repo={w!r} live={l!r}")
    sys.exit(1)
print(f"  ok    live config matches config.json ({len(want)} keys; {len(extra)} live-only keys ignored)")
PY
  [ $? -eq 0 ] || fail=$((fail+1))
}

check_actions() {
  echo "no actuation on EMHASS entities"
  python3 - "$REPO/ha/packages" <<'PY'
import sys, re, pathlib, yaml
root = pathlib.Path(sys.argv[1])
FORBID = re.compile(r"sensor\.(emhass_da_[a-z0-9_]+|p_pv_forecast|p_load_forecast|p_batt_forecast|soc_batt_forecast|p_grid_forecast|p_hybrid_inverter|p_pv_curtailment|p_deferrable\d+|unit_load_cost|unit_prod_price|total_cost_fun_value|optim_status)\b")
class L(yaml.SafeLoader): pass
L.add_multi_constructor("!", lambda loader, suffix, node: None)
hits = []
def walk_actions(node, path):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("action", "actions", "sequence", "then", "else", "default"):
                if FORBID.search(yaml.safe_dump(v, allow_unicode=True) if v is not None else ""):
                    hits.append(path)
            walk_actions(v, path)
    elif isinstance(node, list):
        for i in node: walk_actions(i, path)
for f in sorted(root.rglob("*.yaml")):
    try: docs = list(yaml.load_all(f.read_text(), Loader=L))
    except Exception as e: print(f"  warn  {f.relative_to(root)}: {e}"); continue
    for d in docs:
        if isinstance(d, dict) and "automation" in d:
            for a in d["automation"] or []:
                walk_actions(a, f"{f.relative_to(root)}:{(a or {}).get('alias') or (a or {}).get('id')}")
if hits:
    for h in sorted(set(hits)): print(f"  FAIL  automation acts on an EMHASS forecast entity: {h}")
    sys.exit(1)
print("  ok    no automation action references an EMHASS forecast entity")
PY
  [ $? -eq 0 ] || fail=$((fail+1))
}

check_layers() {
  echo "package layering (emhasscore)"
  python3 - "$REPO/emhasscore" <<'PY'
import ast, pathlib, sys
pkg = pathlib.Path(sys.argv[1])
# import direction, one way: a module may import only the ones before it
order = ["plant", "grid", "series", "objective", "deye", "addon", "archive", "repair", "scoreboard",
         "slices", "rebalance", "planning", "scoring", "ladder", "ab"]
present = [m for m in order if (pkg / f"{m}.py").exists()]
bad = []
for name in present:
    tree = ast.parse((pkg / f"{name}.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            deps = [node.module] if node.module else [a.name for a in node.names]   # from .x import y / from . import x
            for dep in deps:
                if dep not in order or order.index(dep) >= order.index(name):
                    bad.append(f"{name} imports {dep}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m.startswith("emhass_core") or m.startswith("emhasscore") for m in mods):
                bad.append(f"{name} imports {mods} absolutely")
if bad:
    for b in bad: print(f"  FAIL  {b}")
    sys.exit(1)
print(f"  ok    {len(present)} modules import strictly downward and none imports the facade")
PY
  [ $? -eq 0 ] || fail=$((fail+1))
}

check_health() {
  # check_config compares the add-on against THIS working copy. pyscript's
  # emhass_health compares it against /config/emhass/config.json on the HA
  # host (deploy.sh copies it there), and that sensor gates the half-hourly
  # re-plan. So a hand-copied config can satisfy check_config and still leave
  # the dashboard red and re-planning stopped.
  echo "drift sensor (pyscript, against /config/emhass/config.json on the HA host)"
  if ! need_env HA_URL HA_LLAT; then skip "health needs HA_URL and HA_LLAT"; return; fi
  if ! curl -sf -m 90 -o /dev/null -X POST -H "Authorization: Bearer $HA_LLAT" \
       -H "Content-Type: application/json" \
       "$HA_URL/api/services/pyscript/emhass_health" -d '{}'; then
    bad "pyscript.emhass_health did not run (is pyscript loaded?)"; return
  fi
  local st; st=$(curl -sf -m 20 -H "Authorization: Bearer $HA_LLAT" \
    "$HA_URL/api/states/binary_sensor.emhass_addon_healthy")
  if [ -z "$st" ]; then bad "could not read binary_sensor.emhass_addon_healthy"; return; fi
  if python3 - <<'PY2' "$st"
import json, sys
d = json.loads(sys.argv[1]); a = d.get("attributes", {})
drift = a.get("config_drift") or []
hz = (a.get("healthz") or {}).get("status")
if d.get("state") == "on":
    print(f"  ok    binary_sensor.emhass_addon_healthy is on (healthz {hz}, no drift)")
    sys.exit(0)
if hz != "ok":
    print(f"  FAIL  healthz is {hz!r}: {str(a.get('healthz'))[:160]}")
    sys.exit(1)
print(f"  FAIL  sensor is off; /config/emhass/config.json on the HA host differs on {len(drift)} key(s): "
      f"{', '.join(drift[:8])}{' ...' if len(drift) > 8 else ''}")
print("  info  run ha/scripts/deploy.sh: it copies ha/config.json to both the add-on and /config/emhass/")
sys.exit(1)
PY2
  then :; else fail=$((fail+1)); fi
}

check_run() {
  echo "dry-run plan through the live add-on (pyscript.emhass_plan_day dry_run)"
  if ! need_env HA_URL HA_LLAT; then skip "run needs HA_URL and HA_LLAT"; return; fi
  local out; out=$(curl -sf -m 300 -X POST -H "Authorization: Bearer $HA_LLAT" -H "Content-Type: application/json" \
    "$HA_URL/api/services/pyscript/emhass_plan_day?return_response" -d '{"dry_run": true}')
  if [ -z "$out" ]; then bad "service call failed (is pyscript loaded?)"; return; fi
  if echo "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin); r = d.get("service_response", d)
print("  info ", {k: r.get(k) for k in ("t0", "n", "n_predicted_steps", "pv_gap_steps", "optim_status", "seconds", "cost_eur", "message")})
sys.exit(0 if r.get("ok") else 1)'; then ok "dry run solved Optimal"; else bad "dry run failed"; fi
}

for arg in "${@:-all}"; do
  case "$arg" in
    contain) check_contain ;;
    config)  check_config ;;
    health)  check_health ;;
    actions) check_actions ;;
    run)     check_run ;;
    layers)  check_layers ;;
    all)     check_contain; check_config; check_health; check_actions; check_layers ;;
    *) echo "usage: $0 [contain|config|health|actions|run|layers]..."; exit 64 ;;
  esac
done
[ $fail -eq 0 ] && echo "all checks passed" || echo "$fail check(s) failed"
exit $fail
