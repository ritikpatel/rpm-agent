"""Model packages: <root>/<name>/<semver>/{model.onnx, metadata.json, model_card.md}. The newest version is served."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

log = logging.getLogger(__name__)


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


class ModelPackage:
    def __init__(self, path: Path):
        self.path = path
        self.meta = json.loads((path / "metadata.json").read_text())
        self.name, self.version = self.meta["name"], self.meta["version"]
        self.features: list[str] = self.meta["features"]
        self.means: dict[str, float] = self.meta["feature_means"]
        self.session = ort.InferenceSession(str(path / "model.onnx"), providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        outs = [o.name for o in self.session.get_outputs()]
        self.prob_output = "probabilities" if "probabilities" in outs else outs[-1]

    def _proba(self, rows: np.ndarray) -> np.ndarray:
        return self.session.run([self.prob_output], {self.input: rows.astype(np.float32)})[0][:, 1]

    def band(self, p: float) -> str:
        b = self.meta.get("bands", {"medium": 0.2, "high": 0.4})
        return "high" if p >= b["high"] else "medium" if p >= b["medium"] else "low"

    def predict(self, features: dict[str, float], top_k: int = 5) -> Prediction:
        missing = [f for f in self.features if f not in features]
        if missing:
            raise ValueError(f"missing features for {self.name}: {missing}")
        x = np.array([[features[f] for f in self.features]], dtype=np.float32)
        # Occlusion attribution (model-agnostic): replace one feature at a time with its training mean.
        batch = np.repeat(x, len(self.features) + 1, axis=0)
        for i, f in enumerate(self.features):
            batch[i + 1, i] = self.means[f]
        probs = self._proba(batch)
        p = float(probs[0])
        contrib = [{"feature": f, "value": round(float(x[0, i]), 3), "reference": self.means[f],
                    "contribution": round(p - float(probs[i + 1]), 4)} for i, f in enumerate(self.features)]
        contrib.sort(key=lambda c: abs(c["contribution"]), reverse=True)
        return Prediction(round(p, 4), self.band(p), [c for c in contrib[:top_k] if c["contribution"] != 0])

    def describe(self) -> dict:
        return {k: self.meta.get(k) for k in ("name", "version", "task", "label", "bands", "metrics", "validated",
                                              "validation_rule", "owner", "trained_at", "limitations", "features")}


class Registry:
    def __init__(self, root: Path):
        self.models: dict[str, ModelPackage] = {}
        for model_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
            versions = sorted((v for v in model_dir.iterdir() if (v / "metadata.json").exists()), key=_semver)
            if versions:
                pkg = ModelPackage(versions[-1])
                self.models[pkg.name] = pkg
                log.info("model loaded", extra={"model": pkg.name, "version": pkg.version,
                                                "validated": pkg.meta.get("validated")})
        if not self.models:
            log.warning("no model packages found", extra={"root": str(root)})

    def get(self, name: str) -> ModelPackage:
        if name not in self.models:
            raise KeyError(name)
        return self.models[name]
