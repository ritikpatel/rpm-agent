# Readmission risk (placeholder) v0.1.0

- **Predicts:** Unplanned hospital readmission within 30 days of discharge.
- **Status:** placeholder so the platform runs end to end. Tiya's model replaces it.
- **Runs when:** discharge, daily; for patients in: home.
- **Training data:** synthetic MIMIC-IV-shaped extract (150 patients, 250 admissions), or none where the extract has
  no labels (weights set by hand; see `models/train_placeholders.py`).
- **Limits:** Placeholder, not validated. Shown as weak evidence; cannot open or raise an alert.
