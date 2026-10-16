"""Retrain the early-deterioration model (deterioration in the next 6 hours) on the shared training data.

Why: the handed-over model (1.0.0) was fitted on a 300-patient file that is not in the hand-over and ranks the shared
data's patients no better than chance (AUROC 0.51). This refits it on `Training Data/rpm_training_sample.csv`, with
every input built by the platform's own feature builder (`models/plugins/early_deterioration/1.0.0/features.py`)
from the same patient context the risk service builds, so training and serving read identical inputs.

- Rows: every snapshot with a label (263 patients at 1, 3, 6, 12 and 24 hours after discharge).
- Inputs: the 1.0.0 inputs minus the six the platform cannot supply live (steps, active minutes, activity change and
  variability, pain and symptom scores: frozen at the last recorded snapshot when serving), and minus the hours since
  discharge. In this data a deterioration only ever starts within about 12 hours of discharge (no snapshot at 12 or
  24 hours is labelled), so with that input the model learns "early means risky" and scores every patient near zero
  from 24 hours on, which is when the platform monitors them. Without it, the model has to judge from physiology.
- Validation: 5-fold cross-validation with each patient's snapshots kept in one fold, repeated over three seeds; the
  number of trees is chosen inside each training fold, never on its test fold. Compared on the same rows with 1.0.0
  and with the NEWS2 score alone.
- Threshold: the highest probability at which the cross-validated predictions still catch 70% of deteriorations
  (the recall the author used for his "yellow" level).

Run (the training data is git-ignored):
    uv run --package risk-service python models/training/early_deterioration/train.py --out <folder>
It writes model.txt and metrics.json to <folder> and prints the evaluation. With `--handover <folder>` it also writes
`<folder>/deployment/artifacts/` in the layout of the author's hand-over (model .txt/.pkl, bundle .pkl, feature
manifest, thresholds, training metadata), so his `inference.py` and `app.py` serve the retrained model unchanged.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold

from risk_service.plugins import PluginRegistry
from rpm_core.cohort import TrainingData
from rpm_core.features import News2Input, compute_news2

REPO = Path(__file__).resolve().parents[3]
V1 = REPO / "models" / "plugins" / "early_deterioration" / "1.0.0"
LABEL = "deterioration_next_6h"
DISCHARGE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
VITALS = ("heart_rate", "resp_rate", "spo2", "sbp", "temp")
RECALL = 0.70
WATCH_RECALL = 0.90       # the hand-over's lower ("yellow") level
TIME = ("hours_since_discharge",)
SEEDS = (0, 1, 2)
PARAMS = {"objective": "binary", "learning_rate": 0.03, "num_leaves": 7, "min_data_in_leaf": 20,
          "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "metric": "auc",
          "verbosity": -1, "deterministic": True, "num_threads": 1}


def builder():
    spec = importlib.util.spec_from_file_location("early_deterioration_features", V1 / "features.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def context(p, hours: float) -> dict:
    """The context the risk service builds `hours` after discharge: the recorded readings up to then (vital signs
    only; the data's labels never reach a model), the record facts, the discharge state and the registration."""
    vitals = [{"ts": DISCHARGE + timedelta(hours=h), "source": "record", **{k: v.get(k) for k in VITALS}}
              for h, v in p.trajectory_points() if h <= hours]
    return {"now": DISCHARGE + timedelta(hours=hours), "care_setting": "home", "vitals": vitals, "daily": [],
            "labs": [], "record": p.record_facts, "last_recorded": p.last_recorded,
            "discharge_vitals": p.discharge_state, "profile": {"age": p.age, "sex": p.sex},
            "hours_since_discharge": hours}


def table(data: TrainingData, fb, features: list[str]):
    recovery = PluginRegistry(REPO / "models" / "plugins").models["recovery_monitoring"]
    v1 = lgb.Booster(model_file=str(V1 / "model.txt"))
    rows = []
    for p in data.patients.values():
        for s in p.snapshots:
            if s.get(LABEL) in (None, ""):
                continue
            h = float(s["hours_since_discharge"])
            ctx = context(p, h)
            built = fb.build(ctx)
            latest = ctx["vitals"][-1] if ctx["vitals"] else {}
            news2 = compute_news2(News2Input(resp_rate=latest.get("resp_rate"), spo2=latest.get("spo2"),
                                             on_oxygen=False, sbp=latest.get("sbp"),
                                             heart_rate=latest.get("heart_rate"), temp=latest.get("temp")))
            rows.append({"patient": p.patient_id, "real": p.real, "trajectory": p.trajectory, "hours": h,
                         "y": int(float(s[LABEL])), "x": [float(built[f]) for f in features],
                         "x_v1": [float(built[f]) for f in fb.FEATURES], "news2": news2.total,
                         "recovery": recovery.predict(recovery.build(ctx)).probability})
    y = np.array([r["y"] for r in rows])
    x = np.array([r["x"] for r in rows])
    v1_pred = v1.predict(np.array([r["x_v1"] for r in rows]))
    return rows, x, y, v1_pred


def fit(x, y, groups, seed: int):
    """Trees chosen by an inner grouped cross-validation on the training rows only."""
    inner = list(StratifiedGroupKFold(4, shuffle=True, random_state=seed + 100).split(x, y, groups))
    cv = lgb.cv({**PARAMS, "seed": seed}, lgb.Dataset(x, y), num_boost_round=1500, folds=inner,
                callbacks=[lgb.early_stopping(60, verbose=False)])
    rounds = len(cv["valid auc-mean"])
    return lgb.train({**PARAMS, "seed": seed}, lgb.Dataset(x, y), num_boost_round=rounds), rounds


def threshold_at_recall(pred, y, recall: float) -> float:
    pos = np.sort(pred[y == 1])[::-1]
    return float(pos[int(np.ceil(recall * len(pos))) - 1])


def spearman(a, b) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(REPO / "Training Data"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--handover", help="also write the hand-over layout (deployment/artifacts/) here")
    a = ap.parse_args()
    fb = builder()
    features = [f for f in fb.FEATURES if f not in fb.CARRIED and f not in TIME]
    rows, x, y, v1_pred = table(TrainingData(a.data), fb, features)
    groups = np.array([r["patient"] for r in rows])
    real = np.array([r["real"] for r in rows])

    oof_runs, rounds_seen = [], []
    for seed in SEEDS:
        oof = np.zeros(len(y))
        for tr, te in StratifiedGroupKFold(5, shuffle=True, random_state=seed).split(x, y, groups):
            model, rounds = fit(x[tr], y[tr], groups[tr], seed)
            oof[te] = model.predict(x[te])
            rounds_seen.append(rounds)
        oof_runs.append(oof)
    aucs = [roc_auc_score(y, o) for o in oof_runs]
    oof = np.mean(oof_runs, axis=0)
    thr = threshold_at_recall(oof, y, RECALL)
    above = oof >= thr
    traj = np.array([r["trajectory"] for r in rows])

    def auc(pred, mask=None):
        m = np.ones(len(y), bool) if mask is None else mask
        return round(float(roc_auc_score(y[m], pred[m])), 3)

    metrics = {
        "rows": int(len(y)), "patients": int(len(set(groups))), "events": int(y.sum()),
        "features": len(features),
        "cv": "5-fold, patients grouped, 3 seeds; trees chosen by an inner grouped 4-fold CV on each training fold",
        "cv_auroc_mean": round(statistics.mean(aucs), 3), "cv_auroc_by_seed": [round(v, 3) for v in aucs],
        "cv_auprc": round(float(average_precision_score(y, oof)), 3),
        "cv_auroc_mimic_rows": auc(oof, real), "cv_auroc_synthetic_rows": auc(oof, ~real),
        "cv_brier": round(float(brier_score_loss(y, oof)), 4), "event_rate": round(float(y.mean()), 4),
        "alert_threshold": round(thr, 4), "threshold_rule": f"caught {RECALL:.0%} of deteriorations in CV",
        "watch_threshold": round(threshold_at_recall(oof, y, WATCH_RECALL), 4),
        "watch_threshold_rule": f"caught {WATCH_RECALL:.0%} of deteriorations in CV",
        "at_threshold": {"recall": round(float(above[y == 1].mean()), 3),
                         "specificity": round(float((~above)[y == 0].mean()), 3),
                         "ppv": round(float(y[above].mean()), 3),
                         "share_of_snapshots_above": round(float(above.mean()), 3),
                         "stable_snapshots_above": round(float(above[traj == "stable"].mean()), 3),
                         "declining_snapshots_above": round(float(above[traj == "declining"].mean()), 3)},
        "mean_probability_by_course": {t: round(float(oof[traj == t].mean()), 3)
                                       for t in ("stable", "recovering", "declining")},
        "same_rows_v1_0_0_auroc": auc(v1_pred), "same_rows_news2_auroc": auc(np.array([r["news2"] for r in rows])),
        "agreement_with_recovery_spearman": round(spearman(oof, np.array([r["recovery"] for r in rows])), 3),
        "trees": int(statistics.median(rounds_seen)),
    }
    final = lgb.train({**PARAMS, "seed": 0}, lgb.Dataset(x, y, feature_name=features),
                      num_boost_round=metrics["trees"])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    final.save_model(str(out / "model.txt"))
    (out / "metrics.json").write_text(json.dumps({**metrics, "feature_names": features}, indent=2) + "\n")
    if a.handover:
        handover(Path(a.handover), final, features, metrics)
    print(json.dumps(metrics, indent=2))


def handover(root: Path, booster: lgb.Booster, features: list[str], metrics: dict) -> None:
    """The author's hand-over layout (deployment/artifacts/), for the 6-hour horizon only."""
    from datetime import datetime as dt

    import joblib

    art = root / "deployment" / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    label = LABEL
    # red = the platform's alert threshold, yellow = the lower watch level: in their natural order
    thresholds = {"red_threshold": metrics["alert_threshold"], "yellow_threshold": metrics["watch_threshold"]}
    booster.save_model(str(art / f"model_{label}.txt"))
    joblib.dump(booster, art / f"model_{label}.pkl")
    with (art / f"bundle_{label}.pkl").open("wb") as fh:
        pickle.dump({"model": booster, "feature_cols": features, "categorical_cols": [], "thresholds": thresholds}, fh)
    (art / "feature_manifest.json").write_text(json.dumps(
        {label: {"feature_cols": features, "categorical_cols": []}}, indent=2) + "\n")
    (art / "thresholds.json").write_text(json.dumps({label: thresholds}, indent=2) + "\n")
    (art / "training_metadata.json").write_text(json.dumps({
        "exported_at_utc": dt.now(UTC).isoformat(), "source_file": "rpm_training_sample.csv",
        "n_rows": metrics["rows"], "n_subjects": metrics["patients"],
        "horizons": {label: {**{k: metrics[k] for k in (
            "cv", "cv_auroc_mean", "cv_auroc_by_seed", "cv_auprc", "cv_auroc_mimic_rows", "cv_auroc_synthetic_rows",
            "cv_brier", "at_threshold", "same_rows_v1_0_0_auroc", "same_rows_news2_auroc")},
            "n_features_used": len(features), "thresholds": thresholds,
            "lightgbm_params": {**PARAMS, "seed": 0, "num_boost_round": metrics["trees"]}}}}, indent=2) + "\n")


if __name__ == "__main__":
    main()
