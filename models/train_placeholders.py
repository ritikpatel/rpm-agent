"""Fit the three PLACEHOLDER models so the platform runs end to end before the data team's models arrive.

Each placeholder is a real plug-in (models/CONTRACT.md): this script only produces `model.joblib` and fills the
measured parts of `metadata.json`. The readmission model is fitted on the synthetic extract; the other two have no
labels there, so they are logistic models with weights set by hand. None is validated: the console and the agents
label them as weak evidence, and they cannot page anyone.

    uv run python models/train_placeholders.py --mimic-dir synthetic_mimic_150patients
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
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

from rpm_core.features.news2 import News2Input, compute_news2
from rpm_core.mimic import MimicDataset, to_float
from rpm_core.mimic.cohort import drg_severity, prior_admissions
from rpm_core.mimic.dataset import GCS_ITEMS, VITAL_ITEMS

ROOT = Path(__file__).parent / "plugins"
VALIDATION_AUROC = 0.7   # minimum cross-validated AUROC before a model may be marked validated


def plugin_features(name: str, version: str = "0.1.0"):
    spec = importlib.util.spec_from_file_location(f"train_{name}", ROOT / name / version / "features.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stay_rows(ds: MimicDataset, stay_id: str, hypercapnic: bool) -> list[dict]:
    """One row per charted timepoint with vitals, GCS total and NEWS2 (the shape plug-ins see in ctx['vitals'])."""
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
        gcs = float(sum(parts.values())) if len(parts) == 3 else None
        row["gcs"] = gcs
        row["news2"] = compute_news2(News2Input(
            resp_rate=row.get("resp_rate"), spo2=row.get("spo2"), on_oxygen=False, sbp=row.get("sbp"),
            heart_rate=row.get("heart_rate"), consciousness=None if gcs is None else ("A" if gcs >= 15 else "C"),
            temp=row.get("temp"), hypercapnic_resp_failure=hypercapnic)).total
        rows.append(row)
    return rows


def readmission_dataset(ds: MimicDataset, feats) -> tuple[pd.DataFrame, np.ndarray]:
    X, y = [], []
    for sid, adms in ds.admissions_by_subject.items():
        pat = ds.patients.get(sid)
        if not pat:
            continue
        for adm in adms:
            if adm.get("hospital_expire_flag") == "1" or adm.get("readmit_30d_flag") not in ("0.0", "1.0", "0", "1"):
                continue
            stays = ds.icustays_by_hadm.get(adm["hadm_id"], [])
            condition = adm.get("condition")
            ctx = {"profile": {"age": to_float(pat.get("anchor_age")), "sex": pat.get("gender"),
                               "primary_condition": condition,
                               "los_days": to_float(adm.get("length_of_stay_days")),
                               "prior_admissions_12m": prior_admissions(adms, adm),
                               "drg_severity": drg_severity(ds, adm["hadm_id"]),
                               "discharge_disposition": adm.get("discharge_location")},
                   "vitals": stay_rows(ds, stays[0]["stay_id"], condition == "COPD") if stays else []}
            row = feats.build(ctx)
            X.append([row[f] for f in feats.FEATURES])
            y.append(int(float(adm["readmit_30d_flag"])))
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
    auroc = (extra.get("metrics") or {}).get("auroc")
    meta.update(extra, validated=bool(auroc is not None and auroc >= VALIDATION_AUROC),
                trained_at=datetime.now(UTC).isoformat(timespec="seconds"),
                libraries={"python": sys.version.split()[0], "scikit-learn": sklearn.__version__,
                           "numpy": np.__version__, "pandas": pd.__version__})
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{name} {version}: metrics={meta.get('metrics')} validated={meta['validated']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mimic-dir", default="synthetic_mimic_150patients")
    a = ap.parse_args()

    feats = plugin_features("readmission_risk")
    X, y = readmission_dataset(MimicDataset(a.mimic_dir), feats)
    if len(y) < 20 or y.sum() < 5:
        sys.exit("not enough labelled admissions to fit the readmission placeholder")

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
