import pytest

from rpm_features.news2 import (
    BAND_HIGH,
    BAND_LOW,
    BAND_LOW_MEDIUM,
    BAND_MEDIUM,
    News2Input,
    compute_news2,
    consciousness_from_gcs,
    gcs_to_acvpu,
    score_heart_rate,
    score_resp_rate,
    score_sbp,
    score_spo2_scale1,
    score_spo2_scale2,
    score_temp,
)


@pytest.mark.parametrize("rr,expected", [(8, 3), (9, 1), (11, 1), (12, 0), (20, 0), (21, 2), (24, 2), (25, 3),
                                         (11.4, 1), (11.5, 0)])
def test_resp_rate_bands(rr, expected):
    assert score_resp_rate(rr) == expected


@pytest.mark.parametrize("hr,expected", [(40, 3), (41, 1), (50, 1), (51, 0), (90, 0), (91, 1), (110, 1),
                                         (111, 2), (130, 2), (131, 3)])
def test_heart_rate_bands(hr, expected):
    assert score_heart_rate(hr) == expected


@pytest.mark.parametrize("sbp,expected", [(90, 3), (91, 2), (100, 2), (101, 1), (110, 1), (111, 0), (219, 0),
                                          (220, 3)])
def test_sbp_bands(sbp, expected):
    assert score_sbp(sbp) == expected


@pytest.mark.parametrize("t,expected", [(35.0, 3), (35.1, 1), (36.0, 1), (36.1, 0), (38.0, 0), (38.1, 1),
                                        (39.0, 1), (39.1, 2)])
def test_temp_bands(t, expected):
    assert score_temp(t) == expected


@pytest.mark.parametrize("s,expected", [(91, 3), (92, 2), (93, 2), (94, 1), (95, 1), (96, 0)])
def test_spo2_scale1(s, expected):
    assert score_spo2_scale1(s) == expected


@pytest.mark.parametrize("s,o2,expected", [(83, False, 3), (84, False, 2), (86, False, 1), (88, False, 0),
                                           (92, True, 0), (95, False, 0), (93, True, 1), (95, True, 2),
                                           (97, True, 3)])
def test_spo2_scale2(s, o2, expected):
    assert score_spo2_scale2(s, o2) == expected


def test_all_normal_is_zero_low():
    r = compute_news2(News2Input(resp_rate=16, spo2=97, on_oxygen=False, sbp=120, heart_rate=70,
                                 consciousness="A", temp=37.0))
    assert (r.total, r.band, r.partial) == (0, BAND_LOW, False)


def test_deteriorating_patient_is_high():
    r = compute_news2(News2Input(resp_rate=24, spo2=91, on_oxygen=True, sbp=98, heart_rate=118,
                                 consciousness="A", temp=38.6))
    assert r.total == 12 and r.band == BAND_HIGH
    assert r.red_params == ("spo2",)


def test_single_red_parameter_is_low_medium():
    r = compute_news2(News2Input(resp_rate=16, spo2=97, on_oxygen=False, sbp=120, heart_rate=135,
                                 consciousness="A", temp=37.0))
    assert (r.total, r.band) == (3, BAND_LOW_MEDIUM)


def test_medium_band():
    r = compute_news2(News2Input(resp_rate=22, spo2=94, on_oxygen=False, sbp=120, heart_rate=105,
                                 consciousness="A", temp=38.5))
    assert (r.total, r.band) == (5, BAND_MEDIUM)


def test_new_confusion_scores_three():
    r = compute_news2(News2Input(resp_rate=16, spo2=97, on_oxygen=False, sbp=120, heart_rate=70,
                                 consciousness="C", temp=37.0))
    assert r.params["consciousness"] == 3 and r.band == BAND_LOW_MEDIUM


def test_copd_uses_scale2():
    r = compute_news2(News2Input(spo2=89, on_oxygen=False, hypercapnic_resp_failure=True))
    assert r.spo2_scale == 2 and r.params["spo2"] == 0


def test_partial_wearable_score_reports_missing():
    r = compute_news2(News2Input(heart_rate=112, spo2=93))
    assert r.partial
    assert set(r.missing) == {"resp_rate", "sbp", "consciousness", "temp"}
    assert "air_or_oxygen=air" in r.assumed
    assert r.total == 4  # HR 2 + SpO2 2


def test_explain_mentions_scoring_params():
    r = compute_news2(News2Input(heart_rate=112, spo2=93))
    assert "heart_rate 2" in r.explain() and "missing" in r.explain()


@pytest.mark.parametrize("gcs,acvpu", [(15, "A"), (14, "C"), (10, "V"), (6, "P"), (3, "U"), (14.6, "A")])
def test_gcs_to_acvpu(gcs, acvpu):
    assert gcs_to_acvpu(gcs) == acvpu


@pytest.mark.parametrize("gcs,best,expected", [
    (14, 14, "A"),   # chronically 14: stable, not new confusion
    (13, 14, "A"),   # 1-point wobble
    (12, 14, "V"),   # drop of 2 -> new
    (14, 15, "A"),
    (13, 15, "C"),   # drop of 2 from 15
    (12, 12, "V"),   # absolute <= 12 always scores
    (15, None, "A"), (14, None, "C"),
])
def test_consciousness_relative_to_baseline(gcs, best, expected):
    assert consciousness_from_gcs(gcs, best) == expected
