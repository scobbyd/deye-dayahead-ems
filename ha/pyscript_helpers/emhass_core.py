"""EMHASS planner: native compute module (no Home Assistant imports).

Loaded by pyscript/emhass_shadow.py, the live planner wrapper ("shadow" in the
name is historical), through importlib and executed under
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
The exports are generated: for each module in emhasscore.LAYERS, every name the
module defines at its top level (not the names it imports). A public name that
two modules define raises ImportError at import time.
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

# Plain import statements, not importlib.import_module: Home Assistant's blocking-call
# detector patches import_module on the event loop, and not builtins.__import__.
import ast as _ast
import emhasscore as _pkg  # noqa: E402


def _targets(t):
    """The names an assignment target binds: a Name, or the Names inside a tuple
    or list target. `CACHE[k] = v` and `X.attr = v` bind nothing."""
    if isinstance(t, _ast.Name):
        return [t.id]
    if isinstance(t, (_ast.Tuple, _ast.List)):
        return [n for e in t.elts for n in _targets(e)]
    if isinstance(t, _ast.Starred):
        return _targets(t.value)
    return []


def _defined(module):
    """Names a module binds at its top level (def, class, assignment target,
    also under if/try/with/for), in source order. Imported names are not
    included, so a module's imports are not re-exported from it. The source
    comes through the module's loader, which reads with io.open_code and not
    builtins.open (Home Assistant flags that one on the event loop)."""
    names = []

    def visit(body):
        for node in body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, _ast.Assign):
                for t in node.targets:
                    names.extend(_targets(t))
            elif isinstance(node, (_ast.AnnAssign, _ast.AugAssign)):
                names.extend(_targets(node.target))
            elif isinstance(node, _ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, _ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for h in node.handlers:
                    visit(h.body)
            elif isinstance(node, (_ast.With, _ast.For)):
                visit(node.body)

    visit(_ast.parse(module.__spec__.loader.get_source(module.__name__)).body)
    return [n for n in names if not (n.startswith("__") and n.endswith("__"))]


# The private names the hand-kept facade exported, and the module each came
# from. Only these keep resolving if a second module ever defines the same name.
_LEGACY_PRIVATE = {
    "_parse_ts": "grid", "_slot": "grid", "_median": "series", "_HEAD_CACHE": "archive",
    "_plan_stem": "archive", "_ab_payload": "ab", "_ab_solves": "ab",
}

# The submodules first (tests patch one, e.g. core.addon), then every name each
# defines, in LAYERS order. A public name in two modules is an error; a
# single-underscore name in two modules is left out (it has no one owner),
# unless _LEGACY_PRIVATE names its module.
_owners = {}
_private_dupes = set()
for _m in _pkg.LAYERS:
    __import__("emhasscore." + _m)
    globals()[_m] = getattr(_pkg, _m)
for _m in _pkg.LAYERS:
    for _n in _defined(globals()[_m]):
        if _n in _pkg.LAYERS:
            raise ImportError(f"emhasscore.{_m} defines {_n}, which is the name of a module")
        if _n in _owners and _owners[_n] != _m:
            if not _n.startswith("_"):
                raise ImportError(f"emhasscore.{_owners[_n]} and emhasscore.{_m} both define {_n}")
            _private_dupes.add(_n)
        _owners.setdefault(_n, _m)
for _n, _m in _owners.items():
    if _n in _private_dupes:
        _m = _LEGACY_PRIVATE.get(_n)
    if _m is not None:
        globals()[_n] = getattr(globals()[_m], _n)
del _m, _n, _owners, _private_dupes, _LEGACY_PRIVATE, _pkg, _defined, _targets, _ast
