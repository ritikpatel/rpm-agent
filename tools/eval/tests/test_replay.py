"""Replay harness on a small cohort (needs the git-ignored dataset; skipped without it)."""

from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[2] / "synthetic_mimic_150patients"
pytestmark = pytest.mark.skipif(not DATA.exists(), reason="dataset not present")


def test_small_replay_detects_injected_deterioration_and_restores_clocks():
    from rpm_eval.cli import _cohort
    from rpm_eval.replay import Case, metrics, run
    from rpm_icu_gateway import gateway as gw
    from rpm_simulator import engine

    members, series = _cohort(str(DATA), "rpm-v1", 25)
    home = [m for m in members if m.care_setting == "home"][:10]
    icu = [m for m in members if m.care_setting == "icu"][:3]
    cohort = home + icu
    cases = [Case(home[0].patient_id, home[0].device_id, "home", "sepsis", 5400, 180, 1200),
             Case(icu[0].patient_id, icu[0].device_id, "icu", "hypoxia", 5400, 120, 600),
             Case(home[1].patient_id, home[1].device_id, "home", "motion_artifact", 5400, 1, 300)]
    before = (engine.time, engine.utcnow, gw.time)
    result = run(cohort, {k: v for k, v in series.items() if k in {m.subject_id for m in icu}}, hours=2.5,
                 cases=cases)
    assert (engine.time, engine.utcnow, gw.time) == before  # module globals restored
    m = metrics(result)
    rows = {c["scenario"]: c for c in m["cases"]}
    assert rows["sepsis"]["detected"] and rows["hypoxia"]["detected"]
    assert not rows["motion_artifact"]["detected"]
    assert m["windows_scored"] > 1000 and m["sensitivity"] == {"detected": 2, "cases": 2,
                                                               "negative_scenarios_alerting": 0}


def test_guardrails_all_hold():
    from rpm_eval.guardrails import cases

    broken = [c.name for c in cases() if not c.held]
    assert not broken
