"""The library solver must be a drop-in for the add-on: same contract, same
plant, same optimum on an archived payload."""
import glob, gzip, json, os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

emhass = pytest.importorskip("emhass")
from backtest.solver import LibrarySolver, patch_solve
from emhasscore.grid import ceil_step

HERE = os.path.dirname(os.path.abspath(__file__))
PLANS = os.path.join(HERE, "golden", "plans")


def _load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _organic_doc():
    for p in sorted(glob.glob(os.path.join(PLANS, "*.json*")), reverse=True):
        doc = _load(p)
        if doc.get("optim_status") == "Optimal" and not doc.get("replay") and doc.get("payload"):
            return doc
    pytest.skip("no organic Optimal plan in the golden archive (tests/golden/plans is your own pin)")


def test_contract_shape(tmp_path):
    doc = _organic_doc()
    res = LibrarySolver(data_dir=tmp_path)("http://library", doc["payload"])
    assert res["ok"] is True, res.get("message")
    assert len(res["rows"]) == doc["payload"]["prediction_horizon"]
    assert set(res["rows"][0]) >= {"P_PV", "P_Load", "P_batt", "SOC_opt", "P_grid", "P_grid_pos",
                                   "P_grid_neg", "P_PV_curtailment", "unit_load_cost",
                                   "unit_prod_price", "cost_profit", "optim_status", "timestamp"}
    assert res["rows"][0]["optim_status"] == "Optimal"
    assert res["last_run"]["status"] == "ok" and res["last_run"]["action"] == "naive-mpc-optim"
    assert res["last_run"]["emhass_version"] == "0.18.2"


def test_now_pins_the_solve_to_a_historical_instant(tmp_path):
    """LibrarySolver.now, when set, anchors naive-mpc-optim's horizon on that
    instant instead of the real wall clock: row 0's timestamp is the
    ceil-to-15-minute of it. Left unset (every other test here), behaviour is
    unchanged - the pre-existing tests do not touch `now` and still pass."""
    doc = _organic_doc()
    lib = LibrarySolver(data_dir=tmp_path)
    tick = datetime(2026, 7, 23, 10, 13, tzinfo=ZoneInfo("Europe/Amsterdam"))
    lib.now = tick
    res = lib("http://library", doc["payload"])
    assert res["ok"] is True, res.get("message")
    assert len(res["rows"]) == doc["payload"]["prediction_horizon"]
    first = datetime.fromisoformat(res["rows"][0]["timestamp"].replace("Z", "+00:00"))
    assert first == ceil_step(tick).astimezone(timezone.utc)
    # pandas.Timestamp.now is restored after the call: a solve right after,
    # with `now` unset again, is not still pinned to the historical tick.
    lib.now = None
    res2 = lib("http://library", doc["payload"])
    assert res2["ok"] is True, res2.get("message")
    first2 = datetime.fromisoformat(res2["rows"][0]["timestamp"].replace("Z", "+00:00"))
    assert first2 != first


def test_now_stamps_the_synthesized_last_run(monkeypatch, tmp_path):
    """When last_run.json never lands (test_last_run_fallback_when_record_is_a_noop's
    scenario), the rebuilt record is stamped from `now` - the pinned solve's
    own instant - not the real wall clock."""
    import emhass.last_run as last_run
    monkeypatch.setattr(last_run, "record", lambda *a, **k: "1970-01-01T00:00:00Z")
    doc = _organic_doc()
    lib = LibrarySolver(data_dir=tmp_path)
    lib.now = datetime(2026, 7, 23, 10, 13, tzinfo=ZoneInfo("Europe/Amsterdam"))
    res = lib("http://library", doc["payload"])
    assert res["ok"] is True, res.get("message")
    assert res["last_run"]["timestamp"] == "2026-07-23T08:13:00Z"


def test_reproduces_archived_objective(tmp_path):
    doc = _organic_doc()
    res = LibrarySolver(data_dir=tmp_path)("http://library", doc["payload"])
    obj = sum(float(r["cost_profit"]) for r in res["rows"])
    arch = sum(float(r["cost_profit"]) for r in doc["rows"])
    assert abs(obj - arch) < 0.01, (obj, arch)
    assert abs(float(res["rows"][-1]["SOC_opt"]) - float(doc["rows"][-1]["SOC_opt"])) < 0.01


def test_patch_blocks_http(tmp_path):
    import emhasscore.addon as addon
    import emhasscore.scoring as scoring
    lib = LibrarySolver(data_dir=tmp_path)
    with patch_solve(scoring.hindsight_day, lib):
        assert scoring.solve is lib
        with pytest.raises(RuntimeError):
            addon.emhass_post("http://x", "naive-mpc-optim", {})
    assert scoring.solve is addon.solve


def test_library_solve_leaves_no_socket(monkeypatch, tmp_path):
    """The whole point of the library path: nothing leaves the process. Any
    connect() or DNS lookup during a solve is a defect, not a retry. Also
    proves the guard is host-independent: with the "empty" token,
    get_ha_config falls back to SUPERVISOR_TOKEN and would GET
    http://supervisor/core/api/config if the constructor left it set."""
    import socket

    def deny(*a, **k):
        raise RuntimeError("network attempt during a library solve")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "a-real-supervisor-token")
    doc = _organic_doc()
    res = LibrarySolver(data_dir=tmp_path)("http://library", doc["payload"])
    assert res["ok"] is True, res.get("message")
    assert len(res["rows"]) == doc["payload"]["prediction_horizon"]


def _infeasible_payload():
    """The archived payload with the SOC pinned at its floor, a 5 kW load and a
    100 W grid cap: nothing can carry the load, so the MILP and the relaxed LP
    both come back infeasible."""
    pay = dict(_organic_doc()["payload"])
    n = int(pay["prediction_horizon"])
    pay.update(soc_init=float(pay["battery_minimum_state_of_charge"]),
               soc_final=float(pay["battery_minimum_state_of_charge"]),
               pv_power_forecast=[0.0] * n, load_power_forecast=[5000.0] * n,
               maximum_power_from_grid=100, maximum_power_to_grid=100)
    return pay


def test_infeasible_reports_like_the_addon(tmp_path):
    """emhasscore.addon.solve returns ok False when last-run is not ok; the
    library gate must do the same, with the record the library itself wrote."""
    res = LibrarySolver(data_dir=tmp_path)("http://library", _infeasible_payload())
    assert res["ok"] is False
    assert res["last_run"]["status"] == "infeasible"
    assert res["last_run"]["infeasible"] is True
    assert "rows" not in res
    assert "infeasible" in res["message"]


def test_last_run_fallback_when_record_is_a_noop(monkeypatch, tmp_path):
    """_record_optim_snapshot is best-effort in the library: when last_run.json
    never lands, the same record is rebuilt from the rows' optim_status."""
    import emhass.last_run as last_run
    monkeypatch.setattr(last_run, "record", lambda *a, **k: "1970-01-01T00:00:00Z")
    doc = _organic_doc()
    lib = LibrarySolver(data_dir=tmp_path)
    res = lib("http://library", doc["payload"])
    assert not (tmp_path / "last_run.json").exists()
    assert res["ok"] is True, res.get("message")
    assert len(res["rows"]) == doc["payload"]["prediction_horizon"]
    lr = res["last_run"]
    assert lr["status"] == "ok" and lr["action"] == "naive-mpc-optim"
    assert lr["infeasible"] is False and lr["error_message"] is None
    assert lr["emhass_version"] == "0.18.2" and lr["source"] == "library"
    assert lr["timestamp"].endswith("Z")


def test_spring_dst_horizon_is_padded_to_the_requested_instants(tmp_path):
    """2026-03-28 23:45 to 2026-03-30 24:00 local is 189 steps; the library
    returns 188 on its own. The solver pads the request and trims the answer,
    so run_plan's rows[0] == t0 and len(rows) == n both hold."""
    pytest.importorskip("emhass")
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from emhasscore.grid import horizon, step_times
    z = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 3, 28, 23, 43, tzinfo=z)
    t0, n = horizon(now, "Europe/Amsterdam", days_ahead=2)
    assert n == 189
    s = LibrarySolver(data_dir=str(tmp_path))
    s.now = now
    payload = {"optimization_time_step": 15, "prediction_horizon": n, "soc_init": 0.5, "soc_final": 0.5,
               "pv_power_forecast": [0.0] * n, "load_cost_forecast": [0.1] * n,
               "prod_price_forecast": [0.08] * n, "load_power_forecast": [300.0] * n}
    r = s("http://library", payload)
    assert r["ok"] and r.get("dst_padded") is True and len(r["rows"]) == n
    got = [datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc) for x in r["rows"]]
    assert got == [t.astimezone(timezone.utc) for t in step_times(t0, n)]
    assert "_dst_padded" not in payload
