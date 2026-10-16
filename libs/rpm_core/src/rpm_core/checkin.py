"""How a check-in answer reads to a person. Question ids are "<topic>-<suffix>" (breathless-d4e7e415, readings-clin);
the topic says what was asked, the suffix only matches the answer to its question and is never shown."""

from __future__ import annotations

# What each topic asks about, as a short phrase: "Patient answered Yes to “more short of breath than usual”"
TOPICS = {
    "breathless": "more short of breath than usual",
    "chest_pain": "chest pain, pressure or tightness",
    "dizzy": "dizzy, faint, confused or heart racing",
    "fever": "feverish, shivery or chills",
    "readings": "home blood pressure and temperature measured",
}


def topic(question_id: str | None) -> str | None:
    return (question_id or "").split("-", 1)[0] or None


def describe(answer: object, question_id: str | None) -> str:
    """'Yes to “more short of breath than usual”'; an unknown question reads as the answer alone."""
    said = str(answer if answer is not None else "no answer").strip() or "no answer"
    about = TOPICS.get(topic(question_id) or "")
    said = said[:1].upper() + said[1:]
    return f"{said} to “{about}”" if about else said
