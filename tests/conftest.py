"""Put the repo on the path for the tests.

  repo root            `import emhasscore`, `import backtest`
  ha/pyscript_helpers  the emhass_core facade the wrapper loads by file path;
                       the tests import it the same way (`import emhass_core`)
  tests/               the golden package (test_golden.py, golden/build.py)
"""
import pathlib
import sys

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT / "ha" / "pyscript_helpers"))
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))
