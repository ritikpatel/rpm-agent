from datetime import UTC, datetime, timedelta

from rpm_features.news2 import News2Input, compute_news2
from rpm_features.rules import RuleState, Snapshot, Thresholds, default_thresholds, evaluate

T0 = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


def snap(i: int, window_s: int = 30, **kw) -> Snapshot:
    end = T0 + timedelta(seconds=window_s * (i + 1))
    return Snapshot(window_start=end - timedelta(seconds=window_s), ts=end, **kw)


def run(snaps, th=None):
    th = th or Thresholds()
    state, fired = RuleState(), []
    for s in snaps:
        findings, state = evaluate(s, th, state)
        fired.append({f.type for f in findings})
    return fired, state


def test_hr_high_fires_only_once_sustained():
    fired, _ = run([snap(i, heart_rate=130) for i in range(5)])
    assert [("HR_SUSTAINED_HIGH" in f) for f in fired] == [False, False, False, True, True]


def test_low_quality_window_neither_starts_nor_resets():
    snaps = [snap(0, heart_rate=130), snap(1, heart_rate=60, hr_quality=0.2),
             snap(2, heart_rate=130), snap(3, heart_rate=130)]
    fired, _ = run(snaps)
    assert "HR_SUSTAINED_HIGH" in fired[3]


def test_motion_artifact_does_not_alert():
    fired, _ = run([snap(i, heart_rate=165, hr_quality=0.3) for i in range(10)])
    assert not any(fired)


def test_hysteresis_keeps_run_alive_but_clear_resets():
    fired, state = run([snap(0, heart_rate=130), snap(1, heart_rate=118)])
    assert "hr_high" in state.sustain_since
    _, state = evaluate(snap(2, heart_rate=110), Thresholds(), state)
    assert "hr_high" not in state.sustain_since


def test_spo2_low_and_critical():
    f_low, _ = evaluate(snap(0, spo2=88), Thresholds())
    f_crit, _ = evaluate(snap(0, spo2=82), Thresholds())
    assert [f.type for f in f_low] == ["SPO2_LOW"]
    assert [f.type for f in f_crit] == ["SPO2_CRITICAL"] and f_crit[0].severity == "critical"


def test_copd_thresholds_do_not_alert_at_target_saturation():
    th = default_thresholds("COPD", "home")
    findings, _ = evaluate(snap(0, spo2=89), th)
    assert findings == []


def test_news2_high_band_requires_alert():
    n = compute_news2(News2Input(resp_rate=24, spo2=91, on_oxygen=True, sbp=98, heart_rate=118,
                                 consciousness="A", temp=38.6))
    findings, _ = evaluate(snap(0, news2=n), Thresholds())
    assert findings[0].type == "NEWS2_HIGH" and findings[0].requires_alert


def test_news2_rising_trend():
    low = compute_news2(News2Input(resp_rate=16, spo2=97, sbp=120, heart_rate=95, consciousness="A", temp=37))
    up = compute_news2(News2Input(resp_rate=22, spo2=95, sbp=120, heart_rate=105, consciousness="A", temp=37))
    _, state = evaluate(snap(0, news2=low), Thresholds())
    findings, _ = evaluate(snap(10, news2=up), Thresholds(), state)
    assert "NEWS2_RISING" in {f.type for f in findings}


def test_rule_state_roundtrip():
    _, state = run([snap(0, heart_rate=130)])
    assert RuleState.from_json(state.to_json()) == state


def test_threshold_overrides_from_shadow():
    th = Thresholds().merged({"hr_high": 100, "unknown": 1, "spo2_low": None})
    assert th.hr_high == 100 and th.spo2_low == 90


def test_red_param_not_duplicated_when_dedicated_rule_fires():
    n = compute_news2(News2Input(spo2=87))
    findings, _ = evaluate(snap(0, spo2=87, news2=n), Thresholds())
    assert [f.type for f in findings] == ["SPO2_LOW"]
