"""MIMIC-IV-shaped rows -> FHIR R4 resources (one transaction bundle per patient, idempotent PUTs).

Terminology: vitals use LOINC + UCUM; diagnoses ICD-9/10-CM; procedures ICD-9/10-PCS; the cohort condition is
also recorded as a SNOMED CT problem-list entry. Unmapped items keep a local `urn:rpm:mimic:*` code.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime

from rpm_core.mimic import CohortMember, MimicDataset, parse_ts, to_float

LOINC, UCUM, SNOMED = "http://loinc.org", "http://unitsofmeasure.org", "http://snomed.info/sct"
ICD10CM, ICD9CM = "http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-9-cm"
ICD10PCS = "http://www.cms.gov/Medicare/Coding/ICD10"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
SYNTHETIC_TAG = {"system": "urn:rpm:tags", "code": "synthetic", "display": "Synthetic MIMIC-shaped data"}

# chartevents itemid -> (LOINC, display, UCUM unit, value transform)
VITALS = {
    "220045": ("8867-4", "Heart rate", "/min"),
    "220277": ("59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%"),
    "220210": ("9279-1", "Respiratory rate", "/min"),
    "220179": ("8480-6", "Systolic blood pressure", "mm[Hg]"),
    "220180": ("8462-4", "Diastolic blood pressure", "mm[Hg]"),
    "223762": ("8310-5", "Body temperature", "Cel"),
    "220739": ("9267-6", "Glasgow coma score eye opening", "{score}"),
    "223900": ("9270-0", "Glasgow coma score verbal", "{score}"),
    "223901": ("9268-4", "Glasgow coma score motor", "{score}"),
}
GCS_RANGE = {"220739": (1, 4), "223900": (1, 5), "223901": (1, 6)}
CONDITION_SNOMED = {
    "COPD": ("13645005", "Chronic obstructive lung disease"),
    "CHF": ("42343007", "Congestive heart failure"),
    "IHD": ("414545008", "Ischemic heart disease"),
    "Sepsis": ("91302008", "Sepsis"),
    "Stroke": ("230690007", "Cerebrovascular accident"),
}
OMR = {"BMI (kg/m2)": ("39156-5", "Body mass index", "kg/m2"), "Weight (Lbs)": ("29463-7", "Body weight", "[lb_av]")}


def rid(prefix: str, *parts: str) -> str:
    """Deterministic FHIR id (<= 64 chars, [A-Za-z0-9-.])."""
    return f"{prefix}-{hashlib.sha1('|'.join(parts).encode()).hexdigest()[:20]}"


def iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


def _icd_dx(code: str, version: str) -> tuple[str, str]:
    if version == "10":
        return ICD10CM, code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"
    if code.startswith("E") and len(code) > 4:
        return ICD9CM, f"{code[:4]}.{code[4:]}"
    return ICD9CM, code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"


def _meta() -> dict:
    return {"tag": [SYNTHETIC_TAG]}


class PatientBundleBuilder:
    def __init__(self, ds: MimicDataset, anchor: datetime):
        self.ds = ds
        self.anchor = anchor
        self.dx_titles = {(r["icd_code"], r["icd_version"]): r["long_title"]
                          for r in ds.table("d_icd_diagnoses")}
        self.px_titles = {(r["icd_code"], r["icd_version"]): r["long_title"]
                          for r in ds.table("d_icd_procedures")}
        self.lab_items = ds.dictionary("d_labitems", "itemid")
        self._by_subject: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for table in ("diagnoses_icd", "procedures_icd", "labevents", "prescriptions", "omr"):
            for r in ds.table(table):
                self._by_subject[r["subject_id"]][table].append(r)

    # ------------------------------------------------------------------ helpers
    def _past(self, m: CohortMember, ts: datetime | None) -> datetime | None:
        """Shift into the demo timeline; drop anything that would lie in the future (ICU data yet to stream)."""
        s = m.shift(ts)
        return s if s is not None and s <= self.anchor else None

    def _ref(self, m: CohortMember) -> dict:
        return {"reference": f"Patient/{m.patient_id}", "display": m.display_name}

    # ------------------------------------------------------------------ resources
    def patient(self, m: CohortMember) -> dict:
        given, family = m.display_name.split(" ", 1)
        return {
            "resourceType": "Patient", "id": m.patient_id, "meta": _meta(), "active": True,
            "identifier": [{"system": "urn:rpm:mimic:subject_id", "value": m.subject_id}],
            "name": [{"use": "official", "family": family, "given": [given], "text": m.display_name}],
            "gender": {"M": "male", "F": "female"}.get(m.sex, "unknown"),
            "birthDate": str(self.anchor.year - m.age),
        }

    def encounters(self, m: CohortMember) -> list[dict]:
        out = []
        for adm in self.ds.admissions_by_subject.get(m.subject_id, []):
            start = self._past(m, parse_ts(adm["admittime"]))
            if start is None:
                continue
            end = self._past(m, parse_ts(adm["dischtime"]))
            ongoing = adm["hadm_id"] == m.index_hadm_id and m.care_setting == "icu"
            emergency = "EMER" in adm.get("admission_type", "")
            enc = {
                "resourceType": "Encounter", "id": f"adm-{adm['hadm_id']}", "meta": _meta(),
                "status": "in-progress" if ongoing else "finished",
                "class": {"system": ACT_CODE, "code": "EMER" if emergency else "IMP",
                          "display": "emergency" if emergency else "inpatient encounter"},
                "type": [{"text": adm.get("admission_type")}],
                "subject": self._ref(m),
                "period": {"start": iso(start), **({"end": iso(end)} if end and not ongoing else {})},
                "reasonCode": [{"text": adm.get("condition")}],
                "hospitalization": {
                    "admitSource": {"text": adm.get("admission_location")},
                    **({"dischargeDisposition": {"text": adm.get("discharge_location")}} if not ongoing else {}),
                },
            }
            out.append(enc)
            for stay in self.ds.icustays_by_hadm.get(adm["hadm_id"], []):
                s_start = self._past(m, parse_ts(stay["intime"]))
                if s_start is None:
                    continue
                s_end = self._past(m, parse_ts(stay["outtime"]))
                out.append({
                    "resourceType": "Encounter", "id": f"icu-{stay['stay_id']}", "meta": _meta(),
                    "status": "in-progress" if ongoing else "finished",
                    "class": {"system": ACT_CODE, "code": "ACUTE", "display": "inpatient acute"},
                    "serviceType": {"text": stay.get("first_careunit")},
                    "subject": self._ref(m), "partOf": {"reference": f"Encounter/adm-{adm['hadm_id']}"},
                    "period": {"start": iso(s_start), **({"end": iso(s_end)} if s_end and not ongoing else {})},
                    "location": [{"location": {"display": stay.get("first_careunit")}}],
                })
        return out

    def conditions(self, m: CohortMember, encounter_ids: set[str]) -> list[dict]:
        out = []
        snomed = CONDITION_SNOMED.get(m.condition)
        out.append({
            "resourceType": "Condition", "id": f"cond-{m.subject_id}", "meta": _meta(),
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                           "code": "active"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                      "code": "problem-list-item"}]}],
            "code": {"coding": [{"system": SNOMED, "code": snomed[0], "display": snomed[1]}] if snomed else [],
                     "text": m.condition},
            "subject": self._ref(m),
        })
        for r in self._by_subject[m.subject_id]["diagnoses_icd"]:
            enc = f"adm-{r['hadm_id']}"
            if enc not in encounter_ids:
                continue
            system, code = _icd_dx(r["icd_code"], r["icd_version"])
            out.append({
                "resourceType": "Condition", "id": f"dx-{r['hadm_id']}-{r['seq_num']}", "meta": _meta(),
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                          "code": "encounter-diagnosis"}]}],
                "code": {"coding": [{"system": system, "code": code,
                                     "display": self.dx_titles.get((r["icd_code"], r["icd_version"]))}]},
                "subject": self._ref(m), "encounter": {"reference": f"Encounter/{enc}"},
            })
        return out

    def vital_observations(self, m: CohortMember, encounter_ids: set[str]) -> list[dict]:
        out = []
        for adm in self.ds.admissions_by_subject.get(m.subject_id, []):
            for stay in self.ds.icustays_by_hadm.get(adm["hadm_id"], []):
                enc = f"icu-{stay['stay_id']}"
                if enc not in encounter_ids:
                    continue
                for r in self.ds.chartevents_by_stay.get(stay["stay_id"], []):
                    spec, val = VITALS.get(r["itemid"]), to_float(r["valuenum"])
                    ts = self._past(m, parse_ts(r["charttime"]))
                    if not spec or val is None or ts is None:
                        continue
                    if r["itemid"] in GCS_RANGE:
                        lo, hi = GCS_RANGE[r["itemid"]]
                        val = float(max(lo, min(hi, round(val))))
                    code, display, unit = spec
                    out.append({
                        "resourceType": "Observation", "id": rid("ce", stay["stay_id"], r["itemid"], r["charttime"]),
                        "meta": _meta(), "status": "final",
                        "category": [{"coding": [{"system": OBS_CAT, "code": "vital-signs"}]}],
                        "code": {"coding": [{"system": LOINC, "code": code, "display": display}], "text": display},
                        "subject": self._ref(m), "encounter": {"reference": f"Encounter/{enc}"},
                        "effectiveDateTime": iso(ts),
                        "valueQuantity": {"value": round(val, 1), "unit": unit, "system": UCUM, "code": unit},
                    })
        return out

    def lab_observations(self, m: CohortMember, encounter_ids: set[str]) -> list[dict]:
        out = []
        for r in self._by_subject[m.subject_id]["labevents"]:
            enc = f"adm-{r['hadm_id']}"
            ts, val = self._past(m, parse_ts(r["charttime"])), to_float(r["valuenum"])
            if enc not in encounter_ids or ts is None or val is None:
                continue
            item = self.lab_items.get(r["itemid"], {})
            label = item.get("label") or f"Lab item {r['itemid']}"
            obs = {
                "resourceType": "Observation", "id": f"lab-{r['labevent_id']}", "meta": _meta(), "status": "final",
                "category": [{"coding": [{"system": OBS_CAT, "code": "laboratory"}]}],
                "code": {"coding": [{"system": "urn:rpm:mimic:d_labitems", "code": r["itemid"], "display": label}],
                         "text": label},
                "subject": self._ref(m), "encounter": {"reference": f"Encounter/{enc}"},
                "effectiveDateTime": iso(ts), "valueQuantity": {"value": val},
            }
            lo, hi = to_float(r.get("ref_range_lower")), to_float(r.get("ref_range_upper"))
            if lo is not None or hi is not None:
                obs["referenceRange"] = [{**({"low": {"value": lo}} if lo is not None else {}),
                                          **({"high": {"value": hi}} if hi is not None else {})}]
            if r.get("flag") == "abnormal":
                obs["interpretation"] = [{"coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": "A"}]}]
            out.append(obs)
        return out

    def medications(self, m: CohortMember, encounter_ids: set[str]) -> list[dict]:
        out = []
        for r in self._by_subject[m.subject_id]["prescriptions"]:
            enc = f"adm-{r['hadm_id']}"
            start = self._past(m, parse_ts(r["starttime"]))
            if enc not in encounter_ids or start is None or not r.get("drug"):
                continue
            stop = m.shift(parse_ts(r["stoptime"]))
            ongoing = stop is None or stop > self.anchor
            active = m.care_setting == "icu" and r["hadm_id"] == m.index_hadm_id and ongoing
            dosage = " ".join(x for x in (r.get("route"), f"{r['doses_per_24_hrs']}x/24h"
                                          if r.get("doses_per_24_hrs") else None) if x)
            out.append({
                "resourceType": "MedicationRequest", "id": f"rx-{r['pharmacy_id']}", "meta": _meta(),
                "status": "active" if active else "completed", "intent": "order",
                "medicationCodeableConcept": {"text": r["drug"]},
                "subject": self._ref(m), "encounter": {"reference": f"Encounter/{enc}"},
                "authoredOn": iso(start),
                **({"dosageInstruction": [{"text": dosage}]} if dosage else {}),
            })
        return out

    def procedures(self, m: CohortMember, encounter_ids: set[str]) -> list[dict]:
        out = []
        for r in self._by_subject[m.subject_id]["procedures_icd"]:
            enc = f"adm-{r['hadm_id']}"
            ts = self._past(m, parse_ts(r["chartdate"]))
            if enc not in encounter_ids or ts is None:
                continue
            system = ICD10PCS if r["icd_version"] == "10" else ICD9CM
            out.append({
                "resourceType": "Procedure", "id": f"px-{r['hadm_id']}-{r['seq_num']}", "meta": _meta(),
                "status": "completed",
                "code": {"coding": [{"system": system, "code": r["icd_code"],
                                     "display": self.px_titles.get((r["icd_code"], r["icd_version"]))}]},
                "subject": self._ref(m), "encounter": {"reference": f"Encounter/{enc}"},
                "performedDateTime": iso(ts),
            })
        return out

    def omr(self, m: CohortMember) -> list[dict]:
        out = []
        for r in self._by_subject[m.subject_id]["omr"]:
            ts = self._past(m, parse_ts(r["chartdate"]))
            if ts is None:
                continue
            base = {"meta": _meta(), "status": "final", "subject": self._ref(m), "effectiveDateTime": iso(ts)}
            if r["result_name"] in OMR:
                code, display, unit = OMR[r["result_name"]]
                val = to_float(r["result_value"])
                if val is not None:
                    out.append({"resourceType": "Observation", **base,
                                "id": rid("omr", m.subject_id, r["chartdate"], r["result_name"]),
                                "category": [{"coding": [{"system": OBS_CAT, "code": "vital-signs"}]}],
                                "code": {"coding": [{"system": LOINC, "code": code, "display": display}]},
                                "valueQuantity": {"value": val, "unit": unit, "system": UCUM, "code": unit}})
            elif r["result_name"] == "Blood Pressure" and "/" in r["result_value"]:
                sys_, dia = (to_float(x) for x in r["result_value"].split("/", 1))
                if sys_ is not None and dia is not None:
                    out.append({"resourceType": "Observation", **base,
                                "id": rid("omr", m.subject_id, r["chartdate"], "bp"),
                                "category": [{"coding": [{"system": OBS_CAT, "code": "vital-signs"}]}],
                                "code": {"coding": [{"system": LOINC, "code": "85354-9",
                                                     "display": "Blood pressure panel"}]},
                                "component": [
                                    {"code": {"coding": [{"system": LOINC, "code": "8480-6"}]},
                                     "valueQuantity": {"value": sys_, "unit": "mm[Hg]", "system": UCUM,
                                                       "code": "mm[Hg]"}},
                                    {"code": {"coding": [{"system": LOINC, "code": "8462-4"}]},
                                     "valueQuantity": {"value": dia, "unit": "mm[Hg]", "system": UCUM,
                                                       "code": "mm[Hg]"}}]})
        return out

    def device(self, m: CohortMember, device_id: str, kind: str) -> dict:
        return {
            "resourceType": "Device", "id": f"dev-{device_id}", "meta": _meta(), "status": "active",
            "identifier": [{"system": "urn:rpm:iot:thing", "value": device_id}],
            "deviceName": [{"name": device_id, "type": "user-friendly-name"}],
            "type": {"text": {"watch": "Smartwatch (Galaxy Watch / Wear OS)",
                              "bedside": "ICU bedside monitor (via edge gateway)",
                              "simulator": "Simulated wearable"}[kind]},
            "patient": self._ref(m),
        }

    # ------------------------------------------------------------------ bundle
    def bundle(self, m: CohortMember, devices: list[tuple[str, str]], include_labs: bool = True) -> dict:
        resources = [self.patient(m)]
        encs = self.encounters(m)
        resources += encs
        enc_ids = {e["id"] for e in encs}
        resources += self.conditions(m, enc_ids)
        resources += self.vital_observations(m, enc_ids)
        if include_labs:
            resources += self.lab_observations(m, enc_ids)
        resources += self.medications(m, enc_ids)
        resources += self.procedures(m, enc_ids)
        resources += self.omr(m)
        resources += [self.device(m, d, k) for d, k in devices]
        return {
            "resourceType": "Bundle", "type": "transaction",
            "entry": [{"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
                      for r in resources],
        }
