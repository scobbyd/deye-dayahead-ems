"""EMHASS as a library, behind the add-on's solve() contract.

The live wrapper posts a payload to the add-on and reads the plan back from
/api/v1/plan. Here the same payload goes through the library's own action path
(build_config -> build_params -> set_input_data_dict -> naive_mpc_optim) with
the deployed add-on configuration (ha/config.json), and the rows come out of
plan_store.serialize, which is exactly what the add-on's endpoint serves. Every
forecast, price, load and SOC arrives as a runtime list, so the library never
contacts Home Assistant.

Two facts of emhass 0.18.2 shape the secrets below. RetrieveHass.get_ha_config
only skips its HTTP GET of /api/config when the token is missing or the literal
"empty" (the library's own documented way to force a local config); any other
string is used as a real token, the request fails and set_input_data_dict
returns False. And with the token empty the SUPERVISOR_TOKEN environment
variable is consulted, which a dev box never sets.

THE LIBRARY'S OWN CLOCK. emhass.forecast.Forecast anchors the whole forecast
horizon on pd.Timestamp.now(tz=...) at construction time, not on anything in
the payload and not on the documented emhass.utils._get_now mocking seam
(that seam feeds a different, unused code path - confirmed by patching it
alone and finding no effect). A caller solving a historical instant instead
of the real wall clock - a backtest walk - sets `LibrarySolver.now` and
__call__ pins pandas.Timestamp.now itself for the scope of one solve, which
is the only seam available. Left None (the default), behaviour is
byte-for-byte what it was before this existed.
"""
import asyncio
import contextlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time
import warnings
from datetime import datetime, timezone

import pandas as pd

from emhasscore.plant import PLANT

# emhass itself is imported lazily, inside LibrarySolver (__init__, _build_params, _solve,
# __call__), so this module - and patch_solve, which needs none of it - stays importable
# (ladder.py's default-solver construction and patch application) on an interpreter without
# the library; only building or running a LibrarySolver pays for the import.

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_JSON = HERE.parent / "ha" / "config.json"          # the add-on's config, as deployed
# the site from plant.json; "empty" keeps the library off the network (see above)
SECRETS = {"hass_url": "empty", "long_lived_token": "empty", "time_zone": str(PLANT["site"]["timezone"]),
           "Latitude": float(PLANT["site"]["latitude"]), "Longitude": float(PLANT["site"]["longitude"]),
           "Altitude": float(PLANT["site"]["altitude_m"])}


def run_sync(coro):
    """Run a coroutine from sync code, inside or outside a running loop (Jupyter)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    out, err = {}, {}

    def _t():
        try:
            out["v"] = asyncio.run(coro)
        except BaseException as e:      # noqa: BLE001 - re-raised on the caller's thread
            err["e"] = e
    t = threading.Thread(target=_t)
    t.start()
    t.join()
    if err:
        raise err["e"]
    return out["v"]


@contextlib.contextmanager
def _pinned_clock(now: datetime):
    """Pin pandas.Timestamp.now() to `now` for the scope of one library solve.

    See the module docstring's "THE LIBRARY'S OWN CLOCK". Restores the exact
    original class attribute afterwards (captured from pd.Timestamp's own
    __dict__, not a bound-method lookup, so the restore puts back the same
    descriptor rather than a lookalike). `now` may be naive or aware; it is
    converted to UTC once and re-localized per call the way the real
    classmethod's `tz` argument would (a string, a pytz zone, a ZoneInfo, or
    None for a naive result).
    """
    now_utc = pd.Timestamp(now)
    now_utc = now_utc.tz_convert("UTC") if now_utc.tzinfo else now_utc.tz_localize("UTC")

    def fake_now(cls, tz=None):
        return now_utc.tz_localize(None) if tz is None else now_utc.tz_convert(tz)

    orig = pd.Timestamp.__dict__["now"]
    pd.Timestamp.now = classmethod(fake_now)
    try:
        yield now_utc
    finally:
        pd.Timestamp.now = orig


class LibrarySolver:
    """Callable with the add-on's solve() signature. One instance keeps one data
    directory and the parsed params, so repeated solves only pay the LP.

    `now`, when set, is the instant naive_mpc_optim solves as of (see the
    module docstring); a backtest walk sets it before each call, one tick at
    a time. None (the default) leaves the real wall clock alone."""

    def __init__(self, config_path=CONFIG_JSON, data_dir=None, log_level=logging.WARNING):
        import emhass      # lazy: see the module docstring note above the imports
        self.logger = logging.getLogger("emhass.offline")
        self.logger.setLevel(log_level)
        # the no-network guarantee is host-dependent otherwise: with the "empty" token,
        # get_ha_config falls back to SUPERVISOR_TOKEN and GETs http://supervisor/core/api/config
        # when that env var is set, and build_secrets also reads EMHASS_URL
        os.environ.pop("SUPERVISOR_TOKEN", None)
        os.environ.pop("EMHASS_URL", None)
        self.data_path = pathlib.Path(data_dir or tempfile.mkdtemp(prefix="emhass-offline-"))
        self.data_path.mkdir(parents=True, exist_ok=True)
        root = pathlib.Path(emhass.__file__).resolve().parent
        self.emhass_conf = {
            "data_path": self.data_path, "root_path": root,
            "config_path": pathlib.Path(config_path),
            "defaults_path": root / "data" / "config_defaults.json",
            "associations_path": root / "data" / "associations.csv",
            "legacy_config_path": root / "data" / "config_emhass.yaml",
        }
        self.params, self.costfun = run_sync(self._build_params())
        self.calls = 0
        self.now = None

    async def _build_params(self):
        """web_server._build_configuration + _build_and_save_params, minus the
        pickle: build_config layers defaults < config.json (< a legacy yaml that
        does not exist here), build_params sorts the keys into the four conf
        dicts and stamps the secrets in, then costfun and logging_level land on
        optim_conf the way the add-on writes them before pickling params."""
        from emhass.utils import build_config, build_params
        conf = self.emhass_conf
        legacy = conf["legacy_config_path"]
        config = await build_config(conf, self.logger, str(conf["defaults_path"]),
                                    str(conf["config_path"]),
                                    str(legacy) if legacy.exists() else None)
        if isinstance(config, bool):
            raise RuntimeError(f"build_config failed on {conf['config_path']}")
        params = await build_params(conf, dict(SECRETS), config, self.logger)
        if isinstance(params, bool):
            raise RuntimeError("build_params failed (associations.csv missing?)")
        costfun = config.get("costfun", "profit")
        params["optim_conf"]["costfun"] = costfun
        params["optim_conf"]["logging_level"] = config.get("logging_level", "INFO")
        return params, costfun

    async def _solve(self, payload: dict):
        from emhass.command_line import naive_mpc_optim, set_input_data_dict
        idd = await set_input_data_dict(self.emhass_conf, self.costfun, json.dumps(self.params),
                                        json.dumps(payload), "naive-mpc-optim", self.logger)
        if not idd:
            raise RuntimeError("set_input_data_dict returned nothing")
        try:
            return await naive_mpc_optim(idd, self.logger)
        finally:
            # the add-on closes each request's RetrieveHass session; ours never
            # opened one, but close() is the contract and it is idempotent
            if "rh" in idd:
                await idd["rh"].close()

    def __call__(self, base_url: str, payload: dict, timeout: int = 180) -> dict:
        """The add-on's solve(): post, require last-run ok, read the plan. Here
        naive_mpc_optim itself writes last_run.json into our data_path (the very
        record /api/v1/last-run serves, stage_times and all) and the rows are
        plan_store.serialize of its result, which is what /api/v1/plan holds.
        The gate is the add-on's: ok only when last-run says ok, so an
        infeasible solve reports rather than returns rows. `timeout` is
        accepted for the contract and ignored; `lp_solver_timeout` in
        config.json governs the LP."""
        from emhass import plan_store
        t = time.monotonic()
        self.calls += 1
        dst_padded = bool(payload.get("_dst_padded"))
        payload = {k: v for k, v in payload.items() if k != "_dst_padded"}   # never reaches the library
        lr_path = self.data_path / "last_run.json"
        lr_path.unlink(missing_ok=True)
        clock = _pinned_clock(self.now) if self.now is not None else contextlib.nullcontext()
        try:
            with clock:
                opt_res = run_sync(self._solve(payload))
        except Exception as e:      # noqa: BLE001 - the add-on contract reports, never raises
            return {"ok": False, "message": f"library solve failed: {e}", "seconds": round(time.monotonic() - t, 1)}
        seconds = round(time.monotonic() - t, 1)
        if opt_res is None or isinstance(opt_res, bool) or len(opt_res) == 0:
            return {"ok": False, "message": "library solve returned no rows", "seconds": seconds}
        rows = plan_store.serialize(opt_res)
        n = int(payload.get("prediction_horizon") or 0)
        if n and len(rows) < n and not dst_padded:
            # THE LIBRARY LOSES STEPS ACROSS A SPRING DST CHANGE (found on the
            # 2026-03-29 replay): a 189-step horizon from 2026-03-28 23:45 came
            # back with 188 rows ending 23:30, and n+1 still gave 188. Asking for
            # n+4 (the missing hour) returns rows whose first n sit exactly on
            # the requested instants, so the request is padded by repeating each
            # list's last value and the answer trimmed to n. The LP then sees one
            # extra hour at its far end, negligible for the two days a year this
            # fires. The add-on itself will behave the same on the real day.
            padded = dict(payload, prediction_horizon=n + 4, _dst_padded=True)
            for k, v in payload.items():
                if isinstance(v, list) and len(v) == n:
                    padded[k] = list(v) + [v[-1]] * 4
            out = self(base_url, padded, timeout)
            if out.get("ok") and len(out["rows"]) >= n:
                out["rows"] = out["rows"][:n]
                out["dst_padded"] = True
            return out
        lr = self._last_run(lr_path, rows, self.now)
        if lr.get("status") != "ok":
            return {"ok": False, "message": f"last-run: {lr.get('status')} ({lr.get('error_message') or rows[0].get('optim_status')})",
                    "last_run": lr, "seconds": seconds}
        if lr.get("action") not in (None, "naive-mpc-optim"):
            return {"ok": False, "message": f"last-run is {lr.get('action')}, not naive-mpc-optim", "last_run": lr, "seconds": seconds}
        return {"ok": True, "rows": rows, "last_run": lr, "seconds": seconds}

    @staticmethod
    def _last_run(path: pathlib.Path, rows: list, now: datetime | None = None) -> dict:
        """The snapshot _record_optim_snapshot wrote, or (it is best-effort in
        the library) the same record rebuilt from the rows' optim_status.
        `now`, when given, stamps the synthesized record instead of the real
        wall clock - the pinned solve's own instant, not when this Python
        process happened to run it."""
        try:
            lr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = str(rows[0].get("optim_status"))
            ts = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
            lr = {"status": "ok" if status == "Optimal" else "infeasible" if status == "Infeasible" else "error",
                  "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "action": "naive-mpc-optim", "emhass_version": _version(),
                  "infeasible": status == "Infeasible", "error_message": None}
        lr["source"] = "library"
        return lr


def _version() -> str:
    import importlib.metadata as md
    return md.version("emhass")


@contextlib.contextmanager
def patch_solve(fn, solver):
    """Bind `solver` as `solve` in every loaded module that imported the add-on's
    solve, plus fn's module, and make the HTTP layer raise meanwhile. Same seam
    as tests/golden/fixtures.py::patched_solve."""
    import emhasscore.addon as addon
    orig = addon.solve
    with warnings.catch_warnings():
        # numpy/scipy keep deprecated lazy submodules whose getattr warns; the scan is
        # read-only. Deliberate twin of tests/golden/fixtures.py::patched_solve,
        # which must stay importable without the emhass library.
        warnings.simplefilter("ignore", DeprecationWarning)
        mods = {m for m in list(sys.modules.values()) if m is not None and getattr(m, "solve", None) is orig}
    mods.add(sys.modules[fn.__module__])
    saved = {m: getattr(m, "solve", None) for m in mods}
    post_saved = addon.emhass_post

    def no_http(*a, **k):
        raise RuntimeError("an unpatched solve reached the HTTP layer")
    for m in mods:
        setattr(m, "solve", solver)
    addon.emhass_post = no_http
    try:
        yield solver
    finally:
        addon.emhass_post = post_saved
        for m, old in saved.items():
            if old is None:
                delattr(m, "solve")
            else:
                setattr(m, "solve", old)
