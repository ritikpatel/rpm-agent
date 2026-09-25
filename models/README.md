# Model packages (plug-in contract)

`model-serving` serves the newest version of every package under `models/registry/` (locally) or
`s3://<data-bucket>/models/` (AWS). The data team publishes a model by exporting this layout; no code changes.

```
models/registry/<name>/<semver>/
  model.onnx       input "input": float32[N, len(features)]  ->  output "probabilities": [N, 2]
  metadata.json    name, version, task, label, features[] (order!), feature_means{}, bands{medium, high},
                   metrics{auroc, auprc, brier, n, positives}, validated (bool), validation_rule, limitations
  model_card.md    human-readable card
```

- Build features with `rpm_features.features` (`readmission_features`, `icu_mortality_features`,
  `instability_features`); these are the functions serving uses.
- `validated: true` requires cross-validated AUROC ≥ 0.7; anything else is shown as "unvalidated baseline — weak
  evidence" to clinicians and agents.
- Baselines: `make models` (runs `train_baselines.py`). Outputs are git-ignored because they derive from the
  confidential extract.
