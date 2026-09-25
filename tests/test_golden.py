"""The golden pin: every case in golden/cases.py must reproduce its committed
expected file byte for byte. A refactor that keeps the 172 unit tests green and
still moves a plan, a settlement or a score fails here, with the case name and
the first differing line.

Refresh on purpose with `python3 tests/golden/build.py expected`,
then explain each changed expected file in the commit that causes it."""
import difflib
import os

import pytest

import emhass_core as core
from golden import fixtures as F
from golden.cases import CASES

# The pin's inputs and expected outputs are real household data and are not
# shipped (gitignored). Without them every case here skips.
GOLDEN_DATA = {"plans/": F.PLANS, "expected/": F.EXPECTED, "actuals_5min.json.gz": F.ACTUALS_5MIN,
               "nordpool.json": F.NORDPOOL, "scores.csv": F.SCORES}
MISSING = sorted(k for k, v in GOLDEN_DATA.items() if not os.path.exists(v))
pytestmark = pytest.mark.skipif(
    bool(MISSING), reason="golden data absent (real household data, not shipped): tests/golden/" + ", ".join(MISSING))


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden(name):
    path = os.path.join(F.EXPECTED, name + ".json")
    assert os.path.exists(path), f"no expected file for {name}: run golden/build.py expected {name}"
    with open(path) as f:
        want = f.read()
    got = F.canonical(CASES[name](core))
    if got != want:
        diff = list(difflib.unified_diff(want.splitlines(), got.splitlines(), "expected", "got", lineterm="", n=2))
        head = "\n".join(diff[:40])
        pytest.fail(f"{name} drifted from expected/{name}.json ({len(diff)} diff lines):\n{head}")


def test_fixture_archive_reads_back():
    heads = core.plan_heads(F.PLANS)
    assert len(heads) >= 60
    assert all(h["plan_ts"] for _p, h in heads)
    assert {os.path.basename(p) for p, h in heads if h["replay"]} == {"20260904T190509.json.gz"}
    syn = {os.path.basename(p): h for p, h in core.plan_heads(F.SYN_PLANS)}
    assert "20261024T220500.json" in syn
    assert syn["20261025T120500.json.gz"]["optim_status"] == "Infeasible"
