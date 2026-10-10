"""Fit the three PLACEHOLDER models so the platform runs end to end before the data team's models arrive.

Each placeholder is a real plug-in (models/CONTRACT.md): this script only produces `model.joblib` and fills the
measured parts of `metadata.json`. The readmission model is fitted on the 63 MIMIC patients of the team's training
data (the only ones with a readmission label); the other two are logistic models with weights set by hand. None is
validated: the console and the agents label them as weak evidence, and they cannot page anyone.

    uv run python models/train_placeholders.py --data-dir "Training Data"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from rpm_core.cohort import TrainingData, build_cohort
from rpm_core.cohort.cohort import profile_for_models
from rpm_core.cohort.training import num

ROOT = Path(__file__).parent / "plugins"


def plugin_features(name: str, version: str = "0.1.0"):
    spec = importlib.util.spec_from_file_location(f"train_{name}", ROOT / name / version / "features.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readmission_dataset(data: TrainingData, feats) -> tuple[pd.DataFrame, np.ndarray]:
    """One row per MIMIC patient, built by the plug-in's own build(ctx) from the context the risk service would
    give it at discharge: the registry profile and the vital signs charted at discharge."""
    anchor = datetime(2026, 1, 1)
    X, y = [], []
    for m in build_cohort(data, anchor, icu=0, home=0):
        p = m.record
        label = num(p.row.get("readmit_30d_flag")) if p.real else None
        if label is None:
            continue
        vitals = [{**p.discharge_vitals, "ts": anchor, "news2": None}]
        row = feats.build({"now": anchor, "care_setting": "home", "profile": profile_for_models(m),
                           "vitals": vitals, "daily": [], "labs": [], "hours_since_discharge": 0.0})
        X.append([row[f] for f in feats.FEATURES])
        y.append(int(label))
    return pd.DataFrame(X, columns=feats.FEATURES), np.array(y)


def cross_validate(pipe: Pipeline, X: pd.DataFrame, y: np.ndarray) -> dict:
    folds = min(5, int(y.sum()), int((1 - y).sum()))
    proba = cross_val_predict(pipe, X, y, cv=StratifiedKFold(folds, shuffle=True, random_state=7),
                              method="predict_proba")[:, 1]
    return {"cv_folds": folds, "auroc": round(float(roc_auc_score(y, proba)), 3),
            "auprc": round(float(average_precision_score(y, proba)), 3),
            "brier": round(float(brier_score_loss(y, proba)), 3), "n": int(len(y)), "positives": int(y.sum())}


def hand_set(features: list[str], weights: dict[str, float], centre: dict[str, float], intercept: float):
    clf = LogisticRegression()
    clf.classes_ = np.array([0, 1])
    clf.coef_ = np.array([[weights.get(f, 0.0) for f in features]])
    clf.intercept_ = np.array([intercept - sum(weights[f] * c for f, c in centre.items())])
    clf.n_features_in_ = len(features)
    clf.feature_names_in_ = np.array(features, dtype=object)
    return clf


def publish(name: str, model, extra: dict, version: str = "0.1.0") -> None:
    pkg = ROOT / name / version
    joblib.dump(model, pkg / "model.joblib")
    meta_path = pkg / "metadata.json"
    meta = json.loads(meta_path.read_text())
    # A placeholder is never marked validated, whatever it scores: 63 discharges with 10 readmissions cannot
    # establish a model, and a validated model is allowed to page the care team.
    meta.update(extra, validated=False,
                trained_at=datetime.now(UTC).isoformat(timespec="seconds"),
                libraries={"python": sys.version.split()[0], "scikit-learn": sklearn.__version__,
                           "numpy": np.__version__, "pandas": pd.__version__})
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{name} {version}: metrics={meta.get('metrics')} validated={meta['validated']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="Training Data")
    a = ap.parse_args()

    feats = plugin_features("readmission_risk")
    X, y = readmission_dataset(TrainingData(a.data_dir), feats)
    if len(y) < 20 or y.sum() < 5:
        sys.exit("not enough labelled discharges to fit the readmission placeholder")

    def lr() -> Pipeline:
        return Pipeline([("scale", StandardScaler()), ("lr", LogisticRegression(C=0.5, max_iter=2000))])

    publish("readmission_risk", lr().fit(X, y),
            {"metrics": cross_validate(lr(), X, y),
             "feature_means": {f: round(float(v), 4) for f, v in X.mean().items()}})

    feats = plugin_features("early_deterioration")
    publish("early_deterioration", hand_set(
        feats.FEATURES,
        {"news2_now": 0.55, "news2_delta_1h": 0.45, "hr_now": 0.02, "hr_slope_per_h": 0.015, "spo2_min_1h": -0.18,
         "rr_now": 0.12, "partial_news2": -0.3},
        {"hr_now": 80, "spo2_min_1h": 95, "rr_now": 16}, -5.2),
        {"metrics": {"note": "not fitted: weights set by hand, no outcome labels in the extract"}})

    feats = plugin_features("recovery_monitoring")
    publish("recovery_monitoring", hand_set(
        feats.FEATURES,
        {"hr_rest_24h": 0.04, "hr_rest_change": 0.08, "spo2_mean_24h": -0.25, "spo2_min_24h": -0.15,
         "news2_max_24h": 0.35, "news2_mean_change": 0.6, "active_share_24h": -1.5, "days_since_discharge": -0.03},
        {"hr_rest_24h": 72, "spo2_mean_24h": 96.5, "spo2_min_24h": 94}, -2.6),
        {"metrics": {"note": "not fitted: weights set by hand, no outcome labels in the extract"}})


if __name__ == "__main__":
    main()
