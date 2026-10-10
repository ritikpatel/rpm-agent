import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from rpm_core.mimic import MimicDataset, build_cohort
from rpm_seed.mapping import PatientBundleBuilder, _icd_dx

DATA = Path(os.environ.get("MIMIC_DIR", Path(__file__).resolve().parents[3] / "synthetic_mimic_150patients"))
pytestmark = pytest.mark.skipif(not (DATA / "patients.csv").exists(), reason="MIMIC extract not available")
ANCHOR = datetime(2026, 10, 2, 12)


@pytest.fixture(scope="module")
def built():
    ds = MimicDataset(DATA)
    members = build_cohort(ds, ANCHOR)
    b = PatientBundleBuilder(ds, ANCHOR)
    return members, {m.patient_id: b.bundle(m, [(m.device_id, "simulator")]) for m in members}


def test_ids_are_unique_and_valid(built):
    _, bundles = built
    seen = set()
    for bundle in bundles.values():
        for e in bundle["entry"]:
            r = e["resource"]
            key = f"{r['resourceType']}/{r['id']}"
            assert key not in seen, key
            seen.add(key)
            assert len(r["id"]) <= 64 and all(c.isalnum() or c in "-." for c in r["id"])
            assert e["request"]["url"] == key


def _references(x, found: set | None = None) -> set:
    found = set() if found is None else found
    if isinstance(x, dict):
        if "reference" in x:
            found.add(x["reference"])
        for v in x.values():
            _references(v, found)
    elif isinstance(x, list):
        for v in x:
            _references(v, found)
    return found


def test_references_resolve_within_bundle(built):
    _, bundles = built
    for bundle in bundles.values():
        ids = {f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in bundle["entry"]}
        refs = _references(bundle)
        assert refs <= ids, refs - ids


def test_nothing_dated_after_anchor(built):
    _, bundles = built
    text = json.dumps(list(bundles.values()))
    assert not re.search(r'"21\d\d-\d\d-\d\d', text)  # MIMIC's shifted 21xx dates never leak
    for bundle in bundles.values():
        for e in bundle["entry"]:
            ts = e["resource"].get("effectiveDateTime")
            if ts:
                assert datetime.fromisoformat(ts).replace(tzinfo=None) <= ANCHOR


def test_icu_index_encounter_in_progress(built):
    members, bundles = built
    m = next(x for x in members if x.care_setting == "icu")
    enc = next(e["resource"] for e in bundles[m.patient_id]["entry"]
               if e["resource"]["id"] == f"adm-{m.index_hadm_id}")
    assert enc["status"] == "in-progress" and "end" not in enc["period"]


def test_vitals_are_loinc_coded(built):
    _, bundles = built
    vitals = [e["resource"] for b in bundles.values() for e in b["entry"]
              if e["resource"]["resourceType"] == "Observation"
              and e["resource"]["category"][0]["coding"][0]["code"] == "vital-signs"]
    assert vitals and all(v["code"]["coding"][0]["system"] == "http://loinc.org" for v in vitals)


@pytest.mark.parametrize("code,ver,expected", [("J449", "10", "J44.9"), ("I10", "10", "I10"), ("4280", "9", "428.0"),
                                               ("E8490", "9", "E849.0")])
def test_icd_formatting(code, ver, expected):
    assert _icd_dx(code, ver)[1] == expected
