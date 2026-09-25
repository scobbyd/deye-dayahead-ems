#!/usr/bin/env bash
# Deploy ha/config.json to the EMHASS add-on and restart it.
#
# Environment (set in the shell or in a .env file at the repo root, KEY=value):
#   HA_SSH   ssh target of the HA OS host, e.g. root@homeassistant.local or an
#            ssh-config alias. Needs the "Advanced SSH & Web Terminal" add-on
#            (or any SSH add-on) with /addon_configs and /config mounted.
#   HA_URL, HA_LLAT are read by check.sh, which this script ends with.
#
# config.json in this directory is the source of truth for every EMHASS
# parameter. The add-on reads /addon_configs/5b918bf2_emhass/config.json at
# boot only (params.pkl is rebuilt on start), so a deploy is: validate, copy,
# restart, wait, then diff the live config against the file.
#
# Never edit the configuration page in the EMHASS web UI: a save there
# rewrites the live file with a defaults-expanded blob and the next deploy
# silently reverts it. check.sh config catches the drift.
#
# The deploy ends with BOTH drift checks, because they compare different
# things: `config` is the add-on against this working copy, `health` is
# pyscript's binary_sensor.emhass_addon_healthy, which compares the add-on
# against /config/emhass/config.json on the HA host. That sensor gates the
# half-hourly re-plan, so this script also copies config.json there; a deploy
# that satisfies `config` alone would leave the dashboard red and re-planning
# stopped.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
# shellcheck source=/dev/null
. "$HERE/env.sh"
SLUG=5b918bf2_emhass          # the community add-on's fixed slug
HOST=5b918bf2-emhass          # its hostname on the Supervisor's internal network
CFG="$HERE/../config.json"

python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CFG"

echo "copying config.json to /addon_configs/$SLUG/ and /config/emhass/ on $HA_SSH"
ssh "$HA_SSH" "mkdir -p /addon_configs/$SLUG /config/emhass \
  && cat > /addon_configs/$SLUG/config.json.new \
  && mv /addon_configs/$SLUG/config.json.new /addon_configs/$SLUG/config.json \
  && cp /addon_configs/$SLUG/config.json /config/emhass/config.json" < "$CFG"

echo "restarting add-on $SLUG"
ssh "$HA_SSH" "ha apps restart $SLUG" >/dev/null

echo -n "waiting for the add-on API"
for _ in $(seq 1 45); do
  if ssh "$HA_SSH" "curl -sf -m 5 http://$HOST:5000/get-config >/dev/null 2>&1"; then echo " up"; up=1; break; fi
  echo -n .; sleep 4
done
[ "${up:-}" = 1 ] || { echo; echo "add-on API did not come up; check: ssh $HA_SSH 'ha apps logs $SLUG'"; exit 1; }

exec "$HERE/check.sh" config health
