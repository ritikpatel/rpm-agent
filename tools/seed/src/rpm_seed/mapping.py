"""The hospital record (FHIR R4) for a cohort member, built only from the training data.

Real MIMIC patients get what the data holds about their admission: the encounter and ICU stay, the primary
diagnosis, the laboratory values and vital signs at discharge. Synthetic patients have no admission record in the
data, so they get a Patient and a finished Encounter and nothing more. Dates are shifted so each patient's discharge
falls where the cohort places it (hours ago for home patients; still admitted for ICU patients).

Ground-truth columns (trajectory, deterioration and readmission labels) are never written here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rpm_core.cohort import LAB_COLUMNS, CohortMember

TAG = {"system": "urn:rpm:tags", "code": "synthetic"}
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
LOINC = "http://loinc.org"
VITAL_CODES = {  # platform name -> (LOINC, display, unit)
    "heart_rate": ("8867-4", "Heart rate", "/min"), "resp_rate": ("9279-1", "Respiratory rate", "/min"),
    "spo2": ("59408-5", "Oxygen saturation", "%"), "sbp": ("8480-6", "Systolic blood pressure", "mm[Hg]"),
    "dbp": ("8462-4", "Diastolic blood pressure", "mm[Hg]"), "temp": ("8310-5", "Body temperature", "Cel"),
}
DEVICE_TYPES = {"watch": "Smartwatch (Wear OS)", "icu_monitor": "ICU bedside monitor", "simulator": "Wearable"}


def iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat(timespec="seconds")


def _meta() -> dict:
    return {"tag": [TAG]}


def _entry(resource: dict) -> dict:
    return {"resource": resource, "request": {"method": "PUT", "url": f"{resource['resourceType']}/{resource['id']}"}}


class PatientBundleBuilder:
    def __init__(self, anchor: datetime):
        self.anchor = anchor

    @staticmethod
    def _ref(m: CohortMember) -> dict:
        return {"reference": f"Patient/{m.patient_id}"}

    def patient(self, m: CohortMember) -> dict:
        given, _, family = m.display_name.partition(" ")
        return {"resourceType": "Patient", "id": m.patient_id, "meta": _meta(), "active": True,
                "identifier": [{"system": "urn:rpm:training-data:subject", "value": m.subject_id}],
                "name": [{"family": family, "given": [given]}],
                "gender": {"M": "male", "F": "female"}.get(m.sex, "unknown"),
                "birthDate": f"{self.anchor.year - m.age}-01-01"}

    def _discharge_time(self, m: CohortMember) -> datetime:
        return m.discharge_at or self.anchor

    def encounters(self, m: CohortMember) -> list[dict]:
        """One admission, with the ICU stay inside it for MIMIC patients. Open for patients still in the ICU."""
        ongoing = m.care_setting == "icu"
        end = self._discharge_time(m)
        p = m.record
        if p is None or not p.real:
            return [{"resourceType": "Encounter", "id": f"adm-{m.patient_id}", "meta": _meta(), "status": "finished",
                     "class": {"system": ACT_CODE, "code": "IMP", "display": "inpatient encounter"},
                     "subject": self._ref(m), "period": {"end": iso(end)}}]
        hadm = p.row["hadm_id"]
        start = (self.anchor if ongoing else end) - timedelta(days=max(m.los_days, 0.5))
        emergency = "EMER" in (p.text("admission_type") or "")
        adm = {
            "resourceType": "Encounter", "id": f"adm-{hadm}", "meta": _meta(),
            "status": "in-progress" if ongoing else "finished",
            "class": {"system": ACT_CODE, "code": "EMER" if emergency else "IMP",
                      "display": "emergency" if emergency else "inpatient encounter"},
            "type": [{"text": p.text("admission_type")}], "subject": self._ref(m),
            "period": {"start": iso(start), **({} if ongoing else {"end": iso(end)})},
            "reasonCode": [{"text": m.diagnosis}],
            "hospitalization": {"admitSource": {"text": p.text("admission_location")},
                                **({} if ongoing else {"dischargeDisposition": {"text": m.discharge_disposition}})},
        }
        icu_los = p.value("icu_length_of_stay") or 1.0
        icu_start = m.icu_admit_at if ongoing else start + timedelta(hours=2)
        icu = {
            "resourceType": "Encounter", "id": f"icu-{p.row['stay_id']}", "meta": _meta(),
            "status": "in-progress" if ongoing else "finished",
            "class": {"system": ACT_CODE, "code": "ACUTE", "display": "inpatient acute"},
            "serviceType": {"text": "Intensive care"}, "subject": self._ref(m),
            "partOf": {"reference": f"Encounter/adm-{hadm}"},
            "period": {"start": iso(icu_start),
                       **({} if ongoing else {"end": iso(min(end, icu_start + timedelta(days=icu_los)))})},
        }
        return [adm, icu]

    def conditions(self, m: CohortMember, encounter_id: str) -> list[dict]:
        if not m.diagnosis:
            return []
        return [{"resourceType": "Condition", "id": f"dx-{m.patient_id}", "meta": _meta(),
                 "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                                "code": "active"}]},
                 "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                           "code": "encounter-diagnosis"}]}],
                 "code": {"text": m.diagnosis}, "subject": self._ref(m),
                 "encounter": {"reference": f"Encounter/{encounter_id}"}}]

    def _observation(self, m: CohortMember, oid: str, category: str, code: str, display: str, value: float,
                     unit: str, when: datetime, encounter_id: str) -> dict:
        return {"resourceType": "Observation", "id": oid, "meta": _meta(), "status": "final",
                "category": [{"coding": [{"system": OBS_CAT, "code": category}]}],
                "code": {"coding": [{"system": LOINC, "code": code, "display": display}], "text": display},
                "subject": self._ref(m), "encounter": {"reference": f"Encounter/{encounter_id}"},
                "effectiveDateTime": iso(when),
                "valueQuantity": {"value": round(float(value), 2), **({"unit": unit} if unit else {})}}

    def observations(self, m: CohortMember, encounter_id: str) -> list[dict]:
        """Laboratory values and vital signs charted at discharge (for ICU patients: the latest charted)."""
        p = m.record
        if p is None or not p.real:
            return []
        when = self._discharge_time(m) - timedelta(hours=2)
        out = [self._observation(m, f"lab-{m.patient_id}-{col.lower().replace('_', '-')}", "laboratory",
                                 LAB_COLUMNS[col][2],
                                 LAB_COLUMNS[col][0], value, LAB_COLUMNS[col][1], when, encounter_id)
               for col, value in p.labs.items()]
        out += [self._observation(m, f"vs-{m.patient_id}-{name.replace('_', '-')}", "vital-signs",
                                  *VITAL_CODES[name][:2], value,
                                  VITAL_CODES[name][2], when, encounter_id)
                for name, value in p.discharge_vitals.items() if name in VITAL_CODES]
        return out

    def device(self, m: CohortMember, device_id: str, kind: str) -> dict:
        return {"resourceType": "Device", "id": f"dev-{device_id}", "meta": _meta(), "status": "active",
                "identifier": [{"system": "urn:rpm:iot:thing", "value": device_id}],
                "deviceName": [{"name": device_id, "type": "user-friendly-name"}],
                "type": {"text": DEVICE_TYPES[kind]}, "patient": self._ref(m)}

    def bundle(self, m: CohortMember, devices: list[tuple[str, str]], include_labs: bool = True) -> dict:
        encounters = self.encounters(m)
        main = encounters[0]["id"]
        resources = [self.patient(m), *encounters, *self.conditions(m, main)]
        if include_labs:
            resources += self.observations(m, main)
        resources += [self.device(m, d, k) for d, k in devices]
        return {"resourceType": "Bundle", "type": "transaction", "entry": [_entry(r) for r in resources]}
