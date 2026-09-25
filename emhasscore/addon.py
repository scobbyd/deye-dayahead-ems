"""HTTP to the EMHASS add-on: solve, publish, health, the ML actions. The only module that talks to the network;
nothing here writes to the inverter."""
from __future__ import annotations

import time as _time
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .grid import _parse_ts


def emhass_post(base_url: str, action: str, payload: dict, timeout: int = 180) -> tuple[int, str]:
    req = urllib.request.Request(f"{base_url}/action/{action}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}"


def emhass_get(base_url: str, path: str, timeout: int = 30) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def solve(base_url: str, payload: dict, timeout: int = 180) -> dict:
    """POST naive-mpc-optim (one retry after 30 s), require last-run ok and newer
    than the request, then fetch the plan rows."""
    requested = datetime.now(timezone.utc) - timedelta(seconds=2)
    t_start = _time.monotonic()
    status, body = emhass_post(base_url, "naive-mpc-optim", payload, timeout)
    if status not in (200, 201):
        _time.sleep(30)
        status, body = emhass_post(base_url, "naive-mpc-optim", payload, timeout)
        if status not in (200, 201):
            return {"ok": False, "message": f"naive-mpc-optim HTTP {status}: {body[:200]}",
                    "seconds": round(_time.monotonic() - t_start, 1)}
    st, lr = emhass_get(base_url, "/api/v1/last-run")
    seconds = round(_time.monotonic() - t_start, 1)
    if st != 200 or not isinstance(lr, dict) or lr.get("status") != "ok":
        return {"ok": False, "message": f"last-run {st}: {str(lr)[:200]}", "last_run": lr, "seconds": seconds}
    if lr.get("action") not in (None, "naive-mpc-optim"):
        return {"ok": False, "message": f"last-run is {lr.get('action')}, not naive-mpc-optim", "last_run": lr, "seconds": seconds}
    if _parse_ts(lr["timestamp"]) < requested:
        return {"ok": False, "message": "last-run timestamp is older than the request", "last_run": lr, "seconds": seconds}
    st, plan = emhass_get(base_url, "/api/v1/plan")
    if st != 200 or not isinstance(plan, dict) or not plan.get("plan"):
        return {"ok": False, "message": f"plan endpoint {st}", "last_run": lr, "seconds": seconds}
    return {"ok": True, "rows": plan["plan"], "last_run": lr, "seconds": seconds}


PUBLISH_MAP = {
    "custom_batt_forecast_id": {"entity_id": "sensor.emhass_da_p_batt", "unit_of_measurement": "W", "friendly_name": "EMHASS plan battery power"},
    "custom_batt_soc_forecast_id": {"entity_id": "sensor.emhass_da_soc", "unit_of_measurement": "%", "friendly_name": "EMHASS plan SOC"},
    "custom_grid_forecast_id": {"entity_id": "sensor.emhass_da_p_grid", "unit_of_measurement": "W", "friendly_name": "EMHASS plan grid power"},
    "custom_pv_forecast_id": {"entity_id": "sensor.emhass_da_p_pv", "unit_of_measurement": "W", "friendly_name": "EMHASS plan PV"},
    "custom_load_forecast_id": {"entity_id": "sensor.emhass_da_p_load", "unit_of_measurement": "W", "friendly_name": "EMHASS plan load"},
    "custom_unit_load_cost_id": {"entity_id": "sensor.emhass_da_buy", "unit_of_measurement": "EUR/kWh", "friendly_name": "EMHASS plan buy price"},
    "custom_unit_prod_price_id": {"entity_id": "sensor.emhass_da_sell", "unit_of_measurement": "EUR/kWh", "friendly_name": "EMHASS plan sell price"},
    "custom_cost_fun_id": {"entity_id": "sensor.emhass_da_cost", "unit_of_measurement": "EUR", "friendly_name": "EMHASS plan objective"},
    "custom_optim_status_id": {"entity_id": "sensor.emhass_da_status", "unit_of_measurement": "", "friendly_name": "EMHASS plan status"},
    "custom_pv_curtailment_id": {"entity_id": "sensor.emhass_da_p_curtail", "unit_of_measurement": "W", "friendly_name": "EMHASS plan PV curtailment"},
    "custom_hybrid_inverter_id": {"entity_id": "sensor.emhass_da_p_hybrid", "unit_of_measurement": "W", "friendly_name": "EMHASS plan hybrid inverter"},
}


def publish(base_url: str, timeout: int = 60) -> dict:
    """publish-data with explicit entity ids: EMHASS writes the eleven
    sensor.emhass_da_* entities from its opt_res_latest.csv via the Supervisor."""
    st, body = emhass_post(base_url, "publish-data", PUBLISH_MAP, timeout)
    return {"ok": st in (200, 201), "status": st, "body": body[:200]}


def addon_holds_plan(doc_last_run, addon_last_run) -> bool:
    """True when the add-on's /api/v1/last-run IS the solve that produced the
    plan of record, so publish-data would serve that plan and not whatever
    reached the add-on since. Every archived doc keeps the last-run record of
    its own solve; the two agree on the timestamp exactly or not at all.
    Found 2026-09-07: two harness solves of archived pre-midnight plans at
    09:12, a pyscript reload at 09:26, and rehydrate at 09:28 republished them
    as the live plan for seven minutes - a midnight-anchored sun stamped from
    09:30 on every day-3 lane. The freshness gate reads the ARCHIVE; this reads
    the ADD-ON, and the two are only the same object when nothing else solved."""
    if not isinstance(doc_last_run, dict) or not isinstance(addon_last_run, dict):
        return False
    if addon_last_run.get("status") != "ok" or addon_last_run.get("action") != "naive-mpc-optim":
        return False
    a, b = doc_last_run.get("timestamp"), addon_last_run.get("timestamp")
    if not a or not b:
        return False
    try:
        return _parse_ts(a) == _parse_ts(b)
    except ValueError:
        return False


OMITTED_CONFIG_KEYS = {"data_path", "heat_topology"}   # /get-config never echoes these (v0.18.1)


def health(base_url: str, repo_cfg_path: str, max_age_s: int = 93600) -> dict:
    st, hz = emhass_get(base_url, f"/healthz?max_age_seconds={int(max_age_s)}")
    ok = st == 200 and isinstance(hz, dict) and hz.get("status") == "ok"
    drift = []
    st2, live = emhass_get(base_url, "/get-config")
    if st2 == 200 and isinstance(live, dict):
        try:
            with open(repo_cfg_path) as f:
                want = json.load(f)
            drift = sorted(k for k in want
                           if not (k in OMITTED_CONFIG_KEYS and k not in live) and live.get(k, "<absent>") != want[k])
        except Exception as e:                                   # unreadable repo file is drift too
            drift = [f"repo config unreadable: {e}"]
    else:
        ok = False
    age = None
    if isinstance(hz, dict) and hz.get("last_run_ts"):
        age = round((datetime.now(timezone.utc) - _parse_ts(hz["last_run_ts"])).total_seconds() / 3600, 1)
    return {"ok": bool(ok and not drift), "healthz": hz if isinstance(hz, dict) else {"http": st, "body": str(hz)[:120]},
            "drift": drift, "last_run_age_h": age}


def ml_action(base_url: str, action: str, payload: dict, timeout: int) -> dict:
    """forecast-model-fit / forecast-model-tune: post, then confirm via last-run."""
    t = _time.monotonic()
    st, body = emhass_post(base_url, action, payload, timeout)
    st2, lr = emhass_get(base_url, "/api/v1/last-run")
    ok = st in (200, 201) and isinstance(lr, dict) and lr.get("status") == "ok" and lr.get("action") in (None, action)
    return {"ok": bool(ok), "status": st, "seconds": round(_time.monotonic() - t, 1),
            "message": "ok" if ok else f"HTTP {st}: {body[:200]}", "last_run": lr if isinstance(lr, dict) else None}
