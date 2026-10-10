"""A discharge summary per patient, written from the training data's own columns, for the guideline index.

The agents retrieve and cite these (`DS-<patient>#<n>`) for the patient's background. Only what a discharge letter
would contain is included. The data's ground-truth columns (trajectory type, deterioration and readmission labels)
are deliberately left out: an agent must never be handed the answer.
"""

from __future__ import annotations

from rpm_core.cohort import LAB_COLUMNS, CohortMember

from .documents import Document, split_sections


def _n(value, digits: int = 0) -> str:
    return "not recorded" if value is None else f"{value:.{digits}f}"


def build_summary(m: CohortMember) -> Document:
    p = m.record
    doc_id = f"DS-{m.patient_id}"
    title = f"Discharge summary — {m.display_name} ({m.patient_id})"
    who = f"{m.display_name}, {m.age}-year-old {'woman' if m.sex == 'F' else 'man'}"
    if p is None or not p.real:
        content = (f"## Patient\n{who}. Enrolled in post-discharge remote monitoring with a wrist wearable (heart "
                   f"rate, respiratory rate, SpO2, blood pressure, temperature).\n\n"
                   f"## Record\nNo admission record, diagnosis, laboratory results or medication list are held for "
                   f"this patient. Clinical judgement must rest on the monitored vital signs and their trend.\n")
        return Document(doc_id, "discharge_summary", title, "generated from the training data (synthetic patient)",
                        "team training data (derived)", content, m.patient_id, split_sections(doc_id, content))

    flags = [name for name, col in (("cardiac disease", "has_cardiac_diagnosis_flag"),
                                    ("respiratory disease", "has_respiratory_diagnosis_flag"),
                                    ("sepsis", "has_sepsis_diagnosis_flag")) if p.flag(col)]
    v = p.discharge_vitals
    labs = "; ".join(f"{LAB_COLUMNS[c][0]} {value:g} {LAB_COLUMNS[c][1]}".strip() for c, value in p.labs.items())
    at = "latest charted" if m.care_setting == "icu" else "at discharge"
    setting = "Currently admitted to the ICU." if m.care_setting == "icu" else \
        f"Discharged to: {(p.text('discharge_location') or 'not recorded').title()}."
    content = f"""## Admission
{who}. Primary diagnosis: {m.diagnosis or 'not recorded'}. Admission type: \
{p.text('admission_type') or 'not recorded'}, from {(p.text('admission_location') or 'not recorded').lower()}. \
Hospital stay {_n(p.value('length_of_stay_days'), 1)} \
days, of which {_n(p.value('icu_length_of_stay'), 1)} days in intensive care. {setting}

## History and risk factors
Documented diagnosis groups: {', '.join(flags) or 'none of cardiac, respiratory or sepsis'}. \
{_n(p.value('comorbidity_count'))} coded comorbidities, {_n(p.value('chronic_condition_count'))} chronic conditions. \
Admissions in the previous 90 days: {_n(p.value('prior_admission_count_90d'))}; previous 12 months: \
{_n(p.value('prior_admission_count_365d'))}. DRG severity {_n(p.value('drg_severity'))} of 4, DRG mortality risk \
{_n(p.value('drg_mortality'))} of 4. Procedures this stay: {_n(p.value('procedure_count_stay'))}\
{' (including surgery)' if p.flag('new_surgical_procedure_flag') else ''}.

## Vital signs, {at}
Heart rate {_n(v.get('heart_rate'))}/min, respiratory rate {_n(v.get('resp_rate'))}/min, SpO2 {_n(v.get('spo2'))}%, \
blood pressure {_n(v.get('sbp'))}/{_n(v.get('dbp'))} mmHg, temperature {_n(v.get('temp'), 1)} °C, \
GCS {_n(v.get('gcs'))}.

## Laboratory results, {at}
{labs or 'None recorded.'}

## Treatment
{'On antibiotics. ' if p.flag('on_antibiotic_flag') else ''}\
{'Received vasopressors during the stay. ' if p.flag('on_vasopressor_flag') else ''}\
{'Positive microbiology culture during the stay. ' if p.flag('positive_culture_flag') else ''}\
Discharge medications: {_n(p.value('discharge_med_count'))}\
{', including at least one high-risk medication' if p.flag('high_risk_med_flag') else ''}.
"""
    return Document(doc_id, "discharge_summary", title,
                    f"generated from the training data (admission {p.row['hadm_id']})",
                    "team training data (derived from MIMIC-IV demo)", content, m.patient_id,
                    split_sections(doc_id, content))


def build_all(members: list[CohortMember]) -> list[Document]:
    return [build_summary(m) for m in members]
