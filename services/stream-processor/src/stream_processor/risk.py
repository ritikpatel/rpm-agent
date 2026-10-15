"""Asks the risk service for scores without holding up the rules path.

The processor only names a patient and why ("vitals", "lab", "discharge"); a worker thread makes the call. Scores are
throttled per patient for vitals (a window arrives every few seconds; the models do not need that rate), and a model
that enters its high band is handed back to the processor, which raises it like any other finding.

A medium finding for a patient at home waits for the confirming models (`confirm`): they are scored at once, and the
finding is handed back as an alert when one of them is at or above its alert threshold. When none can answer (the
risk service is down, or no confirming model applies), it is handed back without a score and pages on the rule alone.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import httpx

from . import metrics as M

log = logging.getLogger(__name__)


class RiskScorer:
    def __init__(self, base_url: str, token: str, on_high: Callable[[str, dict], None], *,
                 on_confirmed: Callable[[str, dict, dict | None], None] | None = None,
                 min_interval_s: float = 60, timeout_s: float = 10):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                 headers={"X-Service-Token": token, "X-Service-Name": "stream-processor"})
        self.on_high = on_high
        self.on_confirmed = on_confirmed or (lambda _p, _pending, _score: None)
        self._confirm: dict[str, list[dict]] = {}
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}
        self._bands: dict[tuple[str, str], str] = {}
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue[tuple[str, str]] = queue.Queue()   # ("score" | "confirm", patient)

    def request(self, patient_id: str, trigger: str) -> None:
        now = time.monotonic()
        with self._lock:
            if trigger == "vitals" and now - self._last.get(patient_id, -1e9) < self.min_interval_s:
                return
            if patient_id in self._pending:
                if trigger != "vitals":
                    self._pending[patient_id] = trigger  # the more specific reason wins
                return
            self._pending[patient_id] = trigger
            self._last[patient_id] = now
        self._q.put(("score", patient_id))

    def confirm(self, patient_id: str, pending: dict) -> None:
        """Ask the confirming models about a held finding (never throttled: it is waiting to page)."""
        with self._lock:
            waiting = self._confirm.setdefault(patient_id, [])
            waiting.append(pending)
            if len(waiting) > 1:
                return   # already queued: one scoring answers every finding held for this patient
        self._q.put(("confirm", patient_id))

    def _post(self, patient_id: str, trigger: str) -> list[dict]:
        r = self.http.post("/v1/score", json={"patient_id": patient_id, "trigger": trigger})
        r.raise_for_status()
        return r.json()

    def _score(self, patient_id: str, trigger: str) -> None:
        self._bands_seen(patient_id, self._post(patient_id, trigger))

    def _confirm_now(self, patient_id: str) -> None:
        with self._lock:
            held = self._confirm.pop(patient_id, [])
        try:
            scores = [s for s in self._post(patient_id, "confirm") if s.get("confirms_alerts")]
        except Exception as exc:  # noqa: BLE001 - no answer must not swallow a finding
            log.warning("confirming models unavailable; paging on the rule", extra={"patient_id": patient_id,
                                                                                     "error": str(exc)[:200]})
            scores = None
        agree = sorted((s for s in scores or [] if s["band"] == "high"),
                       key=lambda s: (not s.get("validated"), -float(s["probability"])))   # validated first
        if scores:   # a model that confirms a finding is reported in that alert, not again as a risk finding
            self._bands_seen(patient_id, scores, quiet={s["model"] for s in agree} if held else set())
        outcome = "confirmed" if agree else "unavailable" if not scores else "not_confirmed"
        for pending in held:
            M.CONFIRMATIONS.labels(outcome).inc()
            if outcome == "not_confirmed":
                log.info("finding not confirmed by the risk models", extra={
                    "patient_id": patient_id, "type": pending["type"],
                    "scores": {s["model"]: round(float(s["probability"]), 3) for s in scores or []}})
                continue
            self.on_confirmed(patient_id, pending, agree[0] if agree else None)

    def _bands_seen(self, patient_id: str, scores: list[dict], quiet: set[str] = frozenset()) -> None:
        for score in scores:
            key = (patient_id, score["model"])
            entered_high = score["band"] == "high" and self._bands.get(key) != "high"
            self._bands[key] = score["band"]
            if entered_high and score["model"] not in quiet:
                self.on_high(patient_id, score)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                kind, patient_id = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if kind == "confirm":
                try:
                    self._confirm_now(patient_id)
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    log.error("confirmation failed", extra={"patient_id": patient_id, "error": str(exc)[:200]})
                continue
            with self._lock:
                trigger = self._pending.pop(patient_id, "vitals")
            try:
                self._score(patient_id, trigger)
            except Exception as exc:  # noqa: BLE001 - scoring is advisory; the rules path already ran
                log.warning("risk scoring failed", extra={"patient_id": patient_id, "error": str(exc)[:200]})
