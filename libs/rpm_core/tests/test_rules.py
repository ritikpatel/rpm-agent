from datetime import UTC, datetime, timedelta

from rpm_core.features.news2 import News2Input, compute_news2
from rpm_core.features.rules import RuleState, Snapshot, Thresholds, default_thresholds, evaluate

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


# ---------------------------------------------------------------- week 4: context-aware alerting (replay-tuned)
def n2(total_rr: float = 16, spo2: float = 97, sbp: float = 120, hr: float = 75, gcs_c: str = "A"):
    return compute_news2(News2Input(resp_rate=total_rr, spo2=spo2, on_oxygen=False, sbp=sbp, heart_rate=hr,
                                    consciousness=gcs_c, temp=37.0))


def fired_types(snaps, th):
    fired, _ = run(snaps, th)
    return set().union(*fired)


def test_exertional_tachycardia_and_sleep_dip_are_physiology():
    th = default_thresholds("IHD", "home")
    assert "HR_SUSTAINED_HIGH" not in fired_types([snap(i, heart_rate=138, activity="moderate")   # 120 + 25
                                                   for i in range(10)], th)
    assert "HR_SUSTAINED_HIGH" in fired_types([snap(i, heart_rate=138, activity="resting") for i in range(10)], th)
    # AF with rapid ventricular response while walking still clears the exertional allowance
    assert "HR_SUSTAINED_HIGH" in fired_types([snap(i, heart_rate=165, activity="moderate") for i in range(10)], th)
    assert "HR_SUSTAINED_LOW" not in fired_types([snap(i, heart_rate=43, activity="sleep")        # 45 - 5
                                                  for i in range(10)], th)
    assert "HR_SUSTAINED_LOW" in fired_types([snap(i, heart_rate=38, activity="sleep") for i in range(10)], th)


def test_home_news2_bands_are_judged_at_rest_and_must_persist():
    th = default_thresholds("IHD", "home")
    medium = n2(total_rr=22, hr=115, spo2=95)  # RR 2 + HR 2 + SpO2 1 = 5
    assert medium.band == "medium"
    assert "NEWS2_MEDIUM" not in fired_types([snap(i, news2=medium, activity="moderate") for i in range(20)], th)
    assert "NEWS2_MEDIUM" not in fired_types([snap(0, news2=medium, activity="resting")], th)  # one reading
    fired, _ = run([snap(i, news2=medium, activity="resting") for i in range(24)], th)
    assert [i for i, f in enumerate(fired) if "NEWS2_MEDIUM" in f][0] == 19    # 20 x 30 s windows = 10 min held


def icu_run(scores, start=0, prior=None, step_s=60):
    """ICU windows every minute; returns alert-worthy finding types per window and the final state."""
    th = default_thresholds("Sepsis", "icu")
    state, per_window = prior or RuleState(), []
    for i, n in enumerate(scores):
        found, state = evaluate(snap(start + i, window_s=step_s, news2=n), th, state)
        per_window.append([f.type for f in found if f.requires_alert])
    return per_window, state


def test_icu_chronically_high_news2_alerts_only_until_a_baseline_exists():
    chronic = n2(total_rr=22, hr=112, spo2=93, sbp=110)  # RR 2 + HR 2 + SpO2 2 + SBP 1 = 7: high, every window
    assert chronic.band == "high" and chronic.total == 7
    windows, _ = icu_run([chronic] * 180)                # 3 hours
    assert windows[2] == ["NEWS2_HIGH"] and not windows[1]  # first hour: absolute scoring, after 3 min held
    assert not any(windows[60:])                         # afterwards the same score is this patient's baseline
    extreme = n2(total_rr=25, hr=112, spo2=93, sbp=100)  # 9: the catastrophe guard alerts whatever the baseline
    windows, _ = icu_run([extreme] * 120)
    assert all(w == ["NEWS2_HIGH"] for w in windows)


def test_icu_step_up_new_red_param_and_rise_still_alert_once_they_persist():
    windows, state = icu_run([n2()] * 120)               # stable NEWS2 0 for 2 h
    assert not any(windows[60:])
    flicker, _ = icu_run([n2(sbp=88), n2(), n2(sbp=88), n2()], start=120, prior=state)
    assert not any(flicker)                              # a red reading that does not persist is not escalated
    red, _ = icu_run([n2(sbp=88)] * 3, start=120, prior=state)               # SBP <= 90 scores 3: new red parameter
    assert red == [[], [], ["NEWS2_RED_PARAM"]]
    step, _ = icu_run([n2(total_rr=22, hr=112, spo2=95)] * 3, start=120, prior=state)  # 0 -> 5: medium band
    assert step[2] == ["NEWS2_MEDIUM"]
    high, _ = icu_run([n2(total_rr=25, hr=115, sbp=100, spo2=93)], start=120, prior=state)  # 0 -> 9: at once
    assert high[0] == ["NEWS2_HIGH"]


def test_icu_baseline_state_roundtrips():
    _, state = icu_run([n2(total_rr=21)] * 90)
    again = RuleState.from_json(state.to_json())
    assert again.baseline == state.baseline and len(again.baseline) == 90
