"""The offline hindsight lane must equal the live one to the cent on every day
the live scoreboard carries a settled hindsight.

The reference is golden/scores_2026-09-11.csv, the live scores.csv as synced on
2026-09-11 00:10. golden/scores.csv (the 09-07 snapshot) is NOT the reference:
its hindsight values came out of the core as it was before 09-07, the live
board was rescored by hand with the current core afterwards, and that older
file stays as it is because other golden cases read it."""
import csv, os
import pytest

pytest.importorskip("emhass")
from backtest.hindsight import (reproduce, load_nordpool, DEFAULT_CSV, DEFAULT_EXPORT_SW, DEFAULT_NORDPOOL,
                                DEFAULT_ARCHIVE, DEFAULT_SCORES)
from backtest.actuals import raw_stats_from_csv, export_states_from_csv

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(HERE, "golden")
LIVE_SCORES = os.path.join(GOLD, "scores_2026-09-11.csv")
GOLDEN_REACH = "2026-09-07"        # the golden archive's last plan of record covers this day


def _days(upto=None):
    if not os.path.exists(LIVE_SCORES):
        return []
    with open(LIVE_SCORES) as f:
        return [r["date"] for r in csv.DictReader(f)
                if r.get("hindsight_status") == "ok" and r.get("hindsight_eur") and (upto is None or r["date"] <= upto)]


@pytest.fixture(scope="module")
def inputs():
    missing = [p for p in (DEFAULT_CSV, DEFAULT_EXPORT_SW) if not os.path.isfile(p)]
    if missing:
        pytest.skip(f"no synced recorder data under data/: {', '.join(missing)}")
    return raw_stats_from_csv(DEFAULT_CSV), export_states_from_csv(DEFAULT_EXPORT_SW)


def _need_golden():
    if not (os.path.isdir(os.path.join(GOLD, "plans")) and os.path.isfile(os.path.join(GOLD, "nordpool.json"))
            and os.path.isfile(LIVE_SCORES)):
        pytest.skip("the golden pin (plans, nordpool.json, scores_2026-09-11.csv) is not built")


def test_main_reports_missing_inputs(tmp_path, capsys):
    from backtest import hindsight
    rc = hindsight.main(["--start", "2026-09-03", "--end", "2026-09-03", "--csv", str(tmp_path / "none.csv"),
                         "--archive", str(tmp_path / "plans")])
    assert rc == 2 and "none.csv" in capsys.readouterr().err


@pytest.mark.parametrize("day", _days(GOLDEN_REACH) or ["no-day"])
def test_reproduces_live_hindsight(day, inputs):
    if day == "no-day":
        pytest.skip("scores_2026-09-11.csv carries no settled hindsight inside the golden archive")
    _need_golden()
    stats, pts = inputs
    out = reproduce(day, os.path.join(GOLD, "plans"), stats, load_nordpool(os.path.join(GOLD, "nordpool.json")),
                    LIVE_SCORES, None, pts)
    assert out["status"] == "ok", out
    assert abs(out["delta"]) < 0.01, out


def test_soc_only_mask_is_not_the_live_lane(inputs):
    """The defect the switch history fixes: on the first repaired day the SOC-only
    mask over-fires and the lane lands 1,6 cents off. Pins the cause, so a
    future 'why do we need the export switch CSV' has its answer in the suite."""
    _need_golden()
    stats, _ = inputs
    out = reproduce("2026-09-03", os.path.join(GOLD, "plans"), stats,
                    load_nordpool(os.path.join(GOLD, "nordpool.json")), LIVE_SCORES)
    assert out["status"] == "ok", out
    assert abs(out["delta"]) >= 0.01, out


@pytest.mark.parametrize("day", _days() or ["no-day"])
def test_reproduces_live_hindsight_synced_archive(day, inputs):
    """Every settled day, over the live archive synced under data/ (untracked)."""
    archive, scores = DEFAULT_ARCHIVE, DEFAULT_SCORES
    if day == "no-day":
        pytest.skip("scores_2026-09-11.csv carries no settled hindsight")
    if not (os.path.isdir(archive) and os.path.isfile(scores) and os.path.isfile(DEFAULT_NORDPOOL)):
        pytest.skip(f"live archive not synced under data/ ({archive}, {scores}, {DEFAULT_NORDPOOL})")
    stats, pts = inputs
    out = reproduce(day, archive, stats, load_nordpool(DEFAULT_NORDPOOL), scores, None, pts)
    assert out["status"] == "ok", out
    assert out["live_eur"] is not None, f"{day}: settled in scores_2026-09-11.csv but not in the synced scores_live.csv ({out})"
    assert abs(out["delta"]) < 0.01, out
