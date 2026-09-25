# ADR 0009 — Risk-model plug-in contract and honest baselines

**Status:** accepted · 2026-09-25

## Decision
- A model is a package `models/registry/<name>/<semver>/` containing:
  - `model.onnx` (input `input` float32[N, F], output `probabilities`[N, 2])
  - `metadata.json` (features in order, feature means, bands, cross-validated metrics, `validated` flag)
  - `model_card.md`
- `model-serving` loads the newest version of each model from a local directory or `s3://`.
  - Features are built with the shared `rpm_features.features` builders, so training and serving compute them
    identically.
  - Explanations use occlusion (each feature is replaced by its training mean), which is model-agnostic.
  - Each score is written back to FHIR as a `RiskAssessment`.
- **`validated`** is true only if cross-validated AUROC ≥ 0.7. Unvalidated models are labelled in the console and
  in FHIR (`baseline-model` tag), and the agents are instructed to treat them as weak evidence.
- MLflow (or any other tool) is free for experimentation; publishing means exporting to this package format.

## Current baselines (synthetic extract)
| Model | Data | CV AUROC | Validated |
|---|---|---|---|
| readmission_30d | 90 labelled discharges, 28 positive | 0.39 | no (no learnable signal in this extract) |
| icu_mortality | 250 ICU stays, 22 deaths | 0.65 | no |
| instability_6h | no labels; expert-initialised logistic | — | no |

These are placeholders that prove the contract. The data team's models replace them without code changes.
