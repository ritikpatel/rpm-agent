"""Replay harness on a small cohort (needs the git-ignored dataset; skipped without it)."""

from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[3] / "Training Data"
pytestmark = pytest.mark.skipif(not (DATA / "rpm_training_sample.csv").exists(),
                                reason="the team's training data is not available")


def test_small_replay_detects_injected_deterioration_and_restores_clocks():
    from rpm_eval.cli import _cohort
    from rpm_eval.replay import DATASET_EPISODE, Case, metrics, run
    from rpm_simulator import engine

    members = _cohort(str(DATA), "rpm-v1", 3, 0)
    stable = [m for m in members if m.care_setting == "home" and m.record.trajectory == "stable"][:8]
    declining = [m for m in members if m.care_setting == "home" and m.origin == "synthetic"
                 and m.record.trajectory == "declining"][:4]
    icu = [m for m in members if m.care_setting == "icu"]
    cases = [Case(stable[0].patient_id, stable[0].device_id, "home", "sepsis", 5400, 180, 1200),
             Case(icu[0].patient_id, icu[0].device_id, "icu", "hypoxia", 5400, 120, 600),
             Case(stable[1].patient_id, stable[1].device_id, "home", "motion_artifact", 5400, 1, 300)]
    before = (engine.time, engine.utcnow)
    result = run(stable + declining + icu, hours=2.5, cases=cases)
    assert (engine.time, engine.utcnow) == before  # module globals restored
    m = metrics(result)
    rows = {c["scenario"]: c for c in m["cases"] if c["scenario"] != DATASET_EPISODE}
    assert rows["sepsis"]["detected"] and rows["hypoxia"]["detected"]
    assert not rows["motion_artifact"]["detected"]
    assert m["windows_scored"] > 1000 and m["sensitivity"]["negative_scenarios_alerting"] == 0


def test_guardrails_all_hold():
    from rpm_eval.guardrails import cases

    broken = [c.name for c in cases() if not c.held]
    assert not broken


def test_with_model_confirmation_a_partial_news2_waits_for_the_models():
    from rpm_eval.cli import _cohort
    from rpm_eval.replay import run
    from stream_processor.processor import AWAITING, CONFIRM_FIRST

    members = _cohort(str(DATA), "rpm-v1", 0, 0)
    declining = [m for m in members if m.care_setting == "home" and m.record.trajectory == "declining"][:6]
    result = run(declining, hours=1.5, confirm=True)
    held = [e for e in result.events if e.summary.endswith(AWAITING)]
    assert held and not any(e.requires_alert for e in held)                  # recorded, never paged on their own
    for e in result.events:
        if e.requires_alert and e.context == "home" and e.type in CONFIRM_FIRST:
            assert "; confirmed by " in e.summary or "risk models unavailable" in e.summary
            assert e.evidence["confirmed_by"] is None or e.evidence["confirmed_by"]["model"] in (
                "recovery_monitoring", "early_deterioration")
    c = result.confirmations
    assert c["held"] == len(held) and c["held"] == c.get("confirmed", 0) + c.get("unanswered", 0) + c["not_confirmed"]
    assert c.get("unanswered", 0) == 0                                        # the models answer in process
