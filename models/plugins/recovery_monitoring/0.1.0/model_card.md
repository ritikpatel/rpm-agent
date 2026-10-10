# Recovery monitoring (placeholder) v0.1.0

- **Predicts:** Recovery after discharge going off track.
- **Status:** placeholder so the platform runs end to end. Navya's model replaces it.
- **Runs when:** vitals, daily; for patients in: home.
- **Training data:** synthetic MIMIC-IV-shaped extract (150 patients, 250 admissions), or none where the extract has
  no labels (weights set by hand; see `models/train_placeholders.py`).
- **Limits:** Placeholder, not validated. Shown as weak evidence; cannot open or raise an alert.
