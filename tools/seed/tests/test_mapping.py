"""The FHIR record built from the training data: valid ids, resolvable references, no ground-truth leakage."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from rpm_core.cohort import TrainingData, build_cohort
from rpm_core.rag.discharge import build_all
from rpm_seed.mapping import PatientBundleBuilder

DATA = Path(os.environ.get("RPM_DATA_DIR", Path(__file__).resolve().parents[3] / "Training Data"))
pytestmark = pytest.mark.skipif(not (DATA / "rpm_training_sample.csv").exists(),
                                reason="the team's training data is not available")
ANCHOR = datetime(2026, 10, 2, 12)
LEAKS = ("trajectory", "declining", "recovering", "deterioration", "readmit", "severity_fraction")


@pytest.fixture(scope="module")
def built():
    members = build_cohort(TrainingData(DATA), ANCHOR)
    b = PatientBundleBuilder(ANCHOR)
    return members, {m.patient_id: b.bundle(m, [(m.device_id, "simulator")]) for m in members}


def test_ids_are_unique_and_valid(built):
    _, bundles = built
    seen = set()
    for bundle in bundles.values():
        for e in bundle["entry"]:
            r = e["resource"]
            key = (r["resourceType"], r["id"])
            assert key not in seen and re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", r["id"]), key
            seen.add(key)
            assert e["request"] == {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}


def test_references_resolve_within_bundle(built):
    _, bundles = built
    for bundle in bundles.values():
        ids = {f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in bundle["entry"]}
        for ref in re.findall(r'"reference": "([^"]+)"', json.dumps(bundle)):
            assert ref in ids, ref


def test_admitted_patients_have_an_open_encounter_and_discharged_ones_do_not(built):
    members, bundles = built
    for m in members:
        status = {e["resource"]["status"] for e in bundles[m.patient_id]["entry"]
                  if e["resource"]["resourceType"] == "Encounter"}
        assert status == ({"in-progress"} if m.care_setting == "icu" else {"finished"})


def test_real_patients_get_labs_and_synthetic_patients_get_no_invented_record(built):
    members, bundles = built
    for m in members:
        kinds = [e["resource"]["resourceType"] for e in bundles[m.patient_id]["entry"]]
        if m.origin == "mimic_real":
            labs = [e["resource"] for e in bundles[m.patient_id]["entry"] if e["resource"]["resourceType"] ==
                    "Observation" and e["resource"]["category"][0]["coding"][0]["code"] == "laboratory"]
            assert len(labs) == len(m.record.labs) > 5 and "Condition" in kinds
        else:
            assert sorted(kinds) == ["Device", "Encounter", "Patient"]


def test_ground_truth_never_reaches_the_record_or_the_summaries(built):
    members, bundles = built
    text = (json.dumps(bundles) + " ".join(d.content for d in build_all(members))).lower()
    assert not [w for w in LEAKS if w in text]
