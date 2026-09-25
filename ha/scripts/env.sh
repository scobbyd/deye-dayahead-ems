# Sourced by deploy.sh, check.sh and seed_helpers.sh. Not executable on its own.
#
# Three variables describe your Home Assistant instance. Set them in the
# shell, or put KEY=value lines in a .env file at the repo root (gitignored).
# A value already in the environment wins over the file.
#
#   HA_URL   base URL of Home Assistant (the address you open it at, port included)
#   HA_LLAT  a long-lived access token (profile page, Security tab)
#   HA_SSH   ssh target of the HA OS host, e.g. root@homeassistant.local or an
#            ssh-config alias; needs an SSH add-on with /config and
#            /addon_configs mounted (deploy.sh and check.sh contain use it)
#
# Each script says which of the three it needs; a missing one makes that
# script (or that check) stop with a message rather than guess.
_env_file="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/.env"
if [ -f "$_env_file" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|'#'*) continue ;; esac
    _k=${_line%%=*}; _v=${_line#*=}
    case "$_k" in HA_URL|HA_LLAT|HA_SSH) [ -n "${!_k:-}" ] || export "$_k=$_v" ;; esac
  done < "$_env_file"
fi
unset _env_file _line _k _v
HA_URL=${HA_URL:-}
HA_LLAT=${HA_LLAT:-}
HA_SSH=${HA_SSH:-}
HA_URL=${HA_URL%/}
# the host part of HA_URL, for the LAN reachability probe in check.sh
HA_HOST=$(printf '%s' "$HA_URL" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
need_env() {  # need_env VAR...: true when every named variable is set
  local v
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then echo "  $v is not set (environment or .env at the repo root; see ha/scripts/env.sh)"; return 1; fi
  done
}
