"""Structured-output schemas for the agents (strict JSON Schema for the API + Pydantic for validation)."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

ActionName = Literal["REQUEST_SPOT_CHECK", "SEND_CHECKIN", "REQUEST_HOME_READINGS", "SET_PROFILE_ELEVATED",
                     "SET_PROFILE_CRITICAL", "ESCALATE", "NONE"]
ACTIONS = list(get_args(ActionName))


class Finding(BaseModel):
    finding: str
    evidence: str


class RecommendedAction(BaseModel):
    action: ActionName
    reason: str


class Assessment(BaseModel):
    signal_quality: Literal["reliable", "possible_artifact", "insufficient_data"]
    concern_level: Literal["low", "medium", "high", "critical"]
    clinical_picture: str
    key_findings: list[Finding] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    model_evidence_note: str
    confidence: Literal["low", "medium", "high"]


class Citation(BaseModel):
    ref: str
    supports: str


class Brief(BaseModel):
    headline: str
    situation: str
    background: str
    assessment: str
    recommendation: str
    citations: list[Citation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required or list(props), "properties": props}


_STR = {"type": "string"}
ASSESSMENT_SCHEMA = _obj({
    "signal_quality": {"type": "string", "enum": ["reliable", "possible_artifact", "insufficient_data"]},
    "concern_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
    "clinical_picture": _STR,
    "key_findings": {"type": "array", "items": _obj({"finding": _STR, "evidence": _STR})},
    "recommended_actions": {"type": "array", "items": _obj({"action": {"type": "string", "enum": ACTIONS},
                                                            "reason": _STR})},
    "model_evidence_note": _STR,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})
BRIEF_SCHEMA = _obj({
    "headline": _STR, "situation": _STR, "background": _STR, "assessment": _STR, "recommendation": _STR,
    "citations": {"type": "array", "items": _obj({"ref": _STR, "supports": _STR})},
    "uncertainties": {"type": "array", "items": _STR},
})
