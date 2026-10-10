# Early deterioration (placeholder) v0.1.0

- **Predicts:** Clinical deterioration needing escalation within about 6 hours.
- **Status:** placeholder so the platform runs end to end. Harnoor's model replaces it.
- **Runs when:** vitals, lab; for patients in: icu, home.
- **Training data:** synthetic MIMIC-IV-shaped extract (150 patients, 250 admissions), or none where the extract has
  no labels (weights set by hand; see `models/train_placeholders.py`).
- **Limits:** Placeholder, not validated. Shown as weak evidence; cannot open or raise an alert.
