"""Model plug-ins: <root>/<name>/<semver>/{model.txt | model.joblib | model.onnx, features.py, metadata.json,
model_card.md}. model.txt is a LightGBM booster in LightGBM's own text format (portable across versions, and unlike
a pickle it cannot run code when loaded).

A plug-in is everything a data scientist hands over for one model: the fitted model, the code that builds its
features from the patient context, and its description. The newest version of each model is served. The contract is
written up for the data team in models/CONTRACT.md and checked by `make model-check`."""

from __future__ import annotations

import importlib.util
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

TRIGGERS = {"vitals", "lab", "discharge", "daily", "on_demand"}
REQUIRED_META = ("name", "version", "title", "outcome", "owner", "triggers", "care_settings", "bands")


class ContractError(ValueError):
    """The plug-in does not follow models/CONTRACT.md."""


def _semver(p: Path) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in p.name.split("."))
    except ValueError:
        return (-1,)


@dataclass
class Prediction:
    probability: float
    band: str
    top_factors: list[dict]
    features: dict[str, float]
    contributions: dict[str, float] | None = None   # every feature's share of this score (LightGBM models)


class ModelPlugin:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.meta: dict[str, Any] = json.loads((path / "metadata.json").read_text())
        except (OSError, ValueError) as exc:
            raise ContractError(f"{path}: metadata.json missing or not JSON") from exc
        missing = [k for k in REQUIRED_META if k not in self.meta]
        if missing:
            raise ContractError(f"{path}: metadata.json lacks {missing}")
        self.name, self.version = self.meta["name"], self.meta["version"]
        bad = set(self.meta["triggers"]) - TRIGGERS
        if bad:
            raise ContractError(f"{self.name}: unknown triggers {sorted(bad)}")
        if not {"medium", "high"} <= set(self.meta["bands"]):
            raise ContractError(f"{self.name}: bands needs 'medium' and 'high' thresholds")

        spec = importlib.util.spec_from_file_location(f"rpm_plugin_{self.name}_{self.version.replace('.', '_')}",
                                                      path / "features.py")
        if spec is None or spec.loader is None or not (path / "features.py").exists():
            raise ContractError(f"{self.name}: features.py missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not isinstance(getattr(module, "FEATURES", None), list) or not callable(getattr(module, "build", None)):
            raise ContractError(f"{self.name}: features.py must define FEATURES (list) and build(ctx)")
        self.features: list[str] = list(module.FEATURES)
        self._build = module.build
        self._explain = getattr(module, "explain", None)   # the model's own reading of its result (optional)
        self.means: dict[str, float] = self.meta.get("feature_means") or {}
        self.requires: list[str] = list(self.meta.get("requires") or [])
        self._booster = None

        if (path / "model.txt").exists():
            import lightgbm as lgb

            self._model, self._onnx = None, None
            self._booster = lgb.Booster(model_file=str(path / "model.txt"))
            if self._booster.feature_name() != self.features:
                raise ContractError(f"{self.name}: model.txt was fitted on different features than FEATURES "
                                    f"(first difference near {self._first_diff(self._booster.feature_name())})")
        elif (path / "model.joblib").exists():
            import joblib

            self._model, self._onnx = joblib.load(path / "model.joblib"), None
            if not hasattr(self._model, "predict_proba"):
                raise ContractError(f"{self.name}: model.joblib must provide predict_proba")
        elif (path / "model.onnx").exists():
            import onnxruntime as ort

            self._model = None
            self._onnx = ort.InferenceSession(str(path / "model.onnx"), providers=["CPUExecutionProvider"])
            self._input = self._onnx.get_inputs()[0].name
            outs = [o.name for o in self._onnx.get_outputs()]
            self._output = "probabilities" if "probabilities" in outs else outs[-1]
        else:
            raise ContractError(f"{self.name}: model.txt, model.joblib or model.onnx missing")

    def _first_diff(self, names: list[str]) -> str:
        for a, b in zip(names, self.features, strict=False):
            if a != b:
                return f"{a!r} vs {b!r}"
        return f"{len(names)} vs {len(self.features)} features"

    # ------------------------------------------------------------------ contract
    def applies(self, trigger: str, care_setting: str, ctx: dict | None = None) -> bool:
        """`requires` lists parts of the context the model cannot do without (for example `record`: a model
        trained only on patients with a hospital record is not run for patients who have none)."""
        if ctx is not None and any(not ctx.get(k) for k in self.requires):
            return False
        return care_setting in self.meta["care_settings"] and \
            (trigger == "on_demand" or trigger in self.meta["triggers"])

    @property
    def min_interval_s(self) -> float:
        return float(self.meta.get("min_interval_s", 0))

    def build(self, ctx: dict) -> dict[str, float]:
        feats = self._build(ctx)
        missing = [f for f in self.features if f not in feats]
        if missing:
            raise ContractError(f"{self.name}: build() did not return {missing}")
        out = {}
        for f in self.features:
            try:
                v = float(feats[f])
            except (TypeError, ValueError) as exc:
                raise ContractError(f"{self.name}: feature {f} is not a number ({feats[f]!r})") from exc
            if not math.isfinite(v):
                raise ContractError(f"{self.name}: feature {f} is not finite; impute it in build()")
            out[f] = v
        return out

    def _proba(self, rows: np.ndarray) -> np.ndarray:
        if self._booster is not None:
            import pandas as pd

            return np.asarray(self._booster.predict(pd.DataFrame(rows, columns=self.features)))
        if self._onnx is not None:
            return self._onnx.run([self._output], {self._input: rows.astype(np.float32)})[0][:, 1]
        import pandas as pd

        return np.asarray(self._model.predict_proba(pd.DataFrame(rows, columns=self.features)))[:, 1]

    def band(self, p: float) -> str:
        b = self.meta["bands"]
        return "high" if p >= b["high"] else "medium" if p >= b["medium"] else "low"

    def predict(self, features: dict[str, float], top_k: int = 5) -> Prediction:
        x = np.array([[features[f] for f in self.features]], dtype=np.float64)
        if self._booster is not None and not self.means:
            return self._predict_lightgbm(x, features, top_k)
        refs = [f for f in self.features if f in self.means]
        # Occlusion attribution (works for any model): replace one feature at a time with its reference value.
        batch = np.repeat(x, len(refs) + 1, axis=0)
        for i, f in enumerate(refs):
            batch[i + 1, self.features.index(f)] = self.means[f]
        probs = self._proba(batch)
        p = float(probs[0])
        contrib = [{"feature": f, "value": round(features[f], 3), "reference": self.means[f],
                    "contribution": round(p - float(probs[i + 1]), 4)} for i, f in enumerate(refs)]
        contrib.sort(key=lambda c: abs(c["contribution"]), reverse=True)
        return Prediction(round(p, 4), self.band(p), [c for c in contrib[:top_k] if c["contribution"] != 0],
                          features)

    def _predict_lightgbm(self, x: np.ndarray, features: dict[str, float], top_k: int) -> Prediction:
        """LightGBM explains its own predictions: each feature's contribution to this score (in log-odds; the
        console shows the direction). Used when the plug-in gives no `feature_means` for occlusion."""
        import pandas as pd

        frame = pd.DataFrame(x, columns=self.features)
        p = float(self._booster.predict(frame)[0])
        contrib = self._booster.predict(frame, pred_contrib=True)[0][:-1]   # the last entry is the bias
        order = np.argsort(-np.abs(contrib))
        top = [{"feature": self.features[i], "value": round(float(x[0, i]), 3),
                "contribution": round(float(contrib[i]), 4)} for i in order[:top_k] if abs(contrib[i]) >= 1e-4]
        return Prediction(round(p, 4), self.band(p), top, features,
                          {f: float(c) for f, c in zip(self.features, contrib, strict=True)})

    def explain(self, ctx: dict, pred: Prediction) -> dict | None:
        """What the result means, in the model owner's terms (features.py `explain`): a headline, a status, details
        and the reasons. None for a plug-in without one; a failure here never loses the score itself."""
        if self._explain is None:
            return None
        try:
            out = self._explain(ctx, pred.features, pred.probability, pred.contributions or {})
        except Exception as exc:  # noqa: BLE001
            log.error("model explanation failed", extra={"model": self.name, "error": str(exc)[:300]})
            return None
        return out if isinstance(out, dict) and isinstance(out.get("headline"), dict) else None

    def describe(self) -> dict:
        keys = ("name", "version", "title", "outcome", "owner", "triggers", "care_settings", "bands", "metrics",
                "validated", "min_interval_s", "limitations", "trained_at", "libraries", "requires", "label",
                "short_label", "headline")
        return {**{k: self.meta.get(k) for k in keys}, "validated": bool(self.meta.get("validated")),
                "features": self.features}


class PluginRegistry:
    def __init__(self, root: Path):
        self.models: dict[str, ModelPlugin] = {}
        self.errors: dict[str, str] = {}
        for model_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
            versions = sorted((v for v in model_dir.iterdir() if (v / "metadata.json").exists()), key=_semver)
            if not versions:
                continue
            try:
                plugin = ModelPlugin(versions[-1])
            except Exception as exc:  # noqa: BLE001 - one broken plug-in must not take the others down
                self.errors[model_dir.name] = str(exc)
                log.error("model plug-in rejected", extra={"model": model_dir.name, "error": str(exc)[:300]})
                continue
            self.models[plugin.name] = plugin
            log.info("model plug-in loaded", extra={"model": plugin.name, "version": plugin.version,
                                                    "validated": bool(plugin.meta.get("validated"))})
        if not self.models:
            log.warning("no model plug-ins found", extra={"root": str(root)})
