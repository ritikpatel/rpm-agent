"""Train/export the platform's BASELINE risk models into the model-package format served by model-serving.

These exist so the pipeline runs end to end and the plug-in contract is concrete. The data team replaces them by
publishing packages with the same layout (see models/README.md); nothing else changes.

  uv run --group train python models/train_baselines.py --mimic-dir synthetic_mimic_150patients --out models/registry
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from rpm_core.features.features import (
    ICU_MORTALITY_FEATURES,
    INSTABILITY_FEATURES,
    READMISSION_FEATURES,
    aggregate_vitals,
    icu_mortality_features,
    news2_for_row,
    readmission_features,
)
from rpm_core.mimic import MimicDataset, to_float
from rpm_core.mimic.cohort import drg_severity, prior_admissions
from rpm_core.mimic.dataset import GCS_ITEMS, VITAL_ITEMS

VERSION = "1.0.0"
VALIDATION_AUROC = 0.7  # minimum evidence before a model may influence escalation


def stay_rows(ds: MimicDataset, stay_id: str, hypercapnic: bool) -> list[dict]:
    """One row per charted timepoint with vitals + GCS total + NEWS2."""
    by_time: dict[str, dict] = defaultdict(dict)
    for r in ds.chartevents_by_stay.get(stay_id, []):
        v = to_float(r["valuenum"])
        if v is None:
            continue
        if r["itemid"] in VITAL_ITEMS:
            by_time[r["charttime"]][VITAL_ITEMS[r["itemid"]]] = v
        elif r["itemid"] in GCS_ITEMS:
            lo, hi = GCS_ITEMS[r["itemid"]]
            by_time[r["charttime"]].setdefault("_gcs", {})[r["itemid"]] = max(lo, min(hi, round(v)))
    rows = []
    for t in sorted(by_time):
        row = by_time[t]
        parts = row.pop("_gcs", {})
        row["gcs"] = float(sum(parts.values())) if len(parts) == 3 else None
        row["news2"] = news2_for_row(row, hypercapnic)
        rows.append(row)
    return rows


def build_datasets(ds: MimicDataset):
    readmit_X, readmit_y, icu_X, icu_y = [], [], [], []
    for sid, adms in ds.admissions_by_subject.items():
        pat = ds.patients.get(sid)
        if not pat:
            continue
        for adm in adms:
            stays = ds.icustays_by_hadm.get(adm["hadm_id"], [])
            condition = adm.get("condition")
            rows = stay_rows(ds, stays[0]["stay_id"], condition == "COPD") if stays else []
            agg = aggregate_vitals(rows)
            profile = {"age": to_float(pat.get("anchor_age")), "sex": pat.get("gender"), "condition": condition,
                       "los_days": to_float(adm.get("length_of_stay_days")),
                       "prior_admissions_12m": prior_admissions(adms, adm),
                       "drg_severity": drg_severity(ds, adm["hadm_id"]),
                       "discharge_disposition": adm.get("discharge_location")}
            died = adm.get("hospital_expire_flag") == "1"
            if not died and adm.get("readmit_30d_flag") in ("0.0", "1.0", "0", "1"):
                f, _ = readmission_features(profile, agg)
                readmit_X.append([f[k] for k in READMISSION_FEATURES])
                readmit_y.append(int(float(adm["readmit_30d_flag"])))
            if stays:
                hours = (to_float(stays[0].get("los")) or 0) * 24
                f, _ = icu_mortality_features(profile, agg, hours)
                icu_X.append([f[k] for k in ICU_MORTALITY_FEATURES])
                icu_y.append(1 if died else 0)
    return (np.array(readmit_X, dtype=np.float32), np.array(readmit_y),
            np.array(icu_X, dtype=np.float32), np.array(icu_y))


def evaluate(pipe: Pipeline, X, y) -> dict:
    folds = min(5, int(y.sum()), int((1 - y).sum()))
    proba = cross_val_predict(pipe, X, y, cv=StratifiedKFold(folds, shuffle=True, random_state=7),
                              method="predict_proba")[:, 1]
    return {"cv_folds": folds, "auroc": round(float(roc_auc_score(y, proba)), 3),
            "auprc": round(float(average_precision_score(y, proba)), 3),
            "brier": round(float(brier_score_loss(y, proba)), 3), "n": int(len(y)), "positives": int(y.sum()),
            "prevalence": round(float(y.mean()), 3)}


def export(out: Path, name: str, estimator, features: list[str], feature_means: dict, meta: dict, card: str):
    pkg = out / name / VERSION
    pkg.mkdir(parents=True, exist_ok=True)
    clf = estimator.steps[-1][1] if isinstance(estimator, Pipeline) else estimator
    onx = convert_sklearn(estimator, initial_types=[("input", FloatTensorType([None, len(features)]))],
                          options={id(clf): {"zipmap": False}}, target_opset=17)
    (pkg / "model.onnx").write_bytes(onx.SerializeToString())
    auroc = (meta.get("metrics") or {}).get("auroc")
    meta = {"name": name, "version": VERSION, "features": features, "feature_means": feature_means,
            # Serving and agents treat unvalidated models as weak evidence and label them in the console.
            "validated": bool(auroc is not None and auroc >= VALIDATION_AUROC),
            "validation_rule": f"cross-validated AUROC >= {VALIDATION_AUROC}",
            "output": "probability of positive class (ONNX output 'probabilities'[:, 1])",
            "trained_at": datetime.now(UTC).isoformat(timespec="seconds"), "owner": "platform (baseline)", **meta}
    (pkg / "metadata.json").write_text(json.dumps(meta, indent=2))
    (pkg / "model_card.md").write_text(card.format(**{k: json.dumps(v) if isinstance(v, dict) else v
                                                     for k, v in meta.items()}))
    print(f"exported {pkg}  metrics={meta.get('metrics')}")


CARD = """# Model card — {name} v{version}

**Status:** platform BASELINE (logistic regression). Replace with the data team's model; the serving contract is
`metadata.json` + `model.onnx` (input `input` float32[N, len(features)], output `probabilities`[N, 2]).

- **Task:** {task}
- **Label:** {label}
- **Training data:** {training_data}
- **Metrics (cross-validated):** {metrics}
- **Intended use:** decision support inside the RPM platform; never an autonomous clinical decision.
- **Limitations:** {limitations}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mimic-dir", default="synthetic_mimic_150patients")
    ap.add_argument("--out", default="models/registry")
    a = ap.parse_args()
    out = Path(a.out)
    ds = MimicDataset(a.mimic_dir)
    rX, ry, iX, iy = build_datasets(ds)
    if len(ry) < 20 or ry.sum() < 5:
        sys.exit("not enough labelled admissions to train the readmission baseline")

    def lr():
        return Pipeline([("scale", StandardScaler()), ("lr", LogisticRegression(C=0.5, max_iter=2000))])

    common = {"training_data": "synthetic MIMIC-IV-shaped extract (150 patients / 250 admissions)",
              "limitations": "tiny synthetic training set; trained on ICU-charted vitals, served on wearable "
                             "vitals (distribution shift); probabilities are not externally calibrated."}
    for name, X, y, feats, task, label, bands in (
        ("readmission_30d", rX, ry, READMISSION_FEATURES, "30-day all-cause readmission after discharge",
         "admissions.readmit_30d_flag (patients discharged alive)", {"medium": 0.2, "high": 0.4}),
        ("icu_mortality", iX, iy, ICU_MORTALITY_FEATURES, "in-hospital mortality for ICU stays",
         "admissions.hospital_expire_flag", {"medium": 0.1, "high": 0.3}),
    ):
        metrics = evaluate(lr(), X, y)
        pipe = lr().fit(X, y)
        export(out, name, pipe, feats, {f: round(float(v), 4) for f, v in zip(feats, X.mean(axis=0), strict=True)},
               {"task": task, "label": label, "bands": bands, "metrics": metrics, **common}, CARD)

    # Instability: no outcome labels exist in the extract, so this is an expert-initialised logistic over the
    # short-horizon trend features (documented as such). Weights are per unit of each feature.
    weights = {"news2_now": 0.55, "news2_delta_1h": 0.45, "hr_now": 0.02, "hr_slope_per_h": 0.015,
               "spo2_min_1h": -0.18, "rr_now": 0.12, "partial_news2": -0.3}
    centre = {"hr_now": 80, "spo2_min_1h": 95, "rr_now": 16}
    clf = LogisticRegression()
    clf.classes_ = np.array([0, 1])
    clf.coef_ = np.array([[weights[f] for f in INSTABILITY_FEATURES]])
    clf.intercept_ = np.array([-5.2 - sum(weights[f] * c for f, c in centre.items())])
    clf.n_features_in_ = len(INSTABILITY_FEATURES)
    means = {"news2_now": 1.0, "news2_delta_1h": 0.0, "hr_now": 80.0, "hr_slope_per_h": 0.0, "spo2_min_1h": 96.0,
             "rr_now": 16.0, "partial_news2": 1.0}
    export(out, "instability_6h", clf, INSTABILITY_FEATURES, means,
           {"task": "risk of clinical instability requiring escalation within ~6 h", "label": "none (expert prior)",
            "bands": {"medium": 0.2, "high": 0.5}, "metrics": {"note": "not trained; expert-initialised weights"},
            "weights": weights, **common}, CARD)


if __name__ == "__main__":
    main()
