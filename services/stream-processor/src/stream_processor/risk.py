"""Asks the risk service for scores without holding up the rules path.

The processor only names a patient and why ("vitals", "lab", "discharge"); a worker thread makes the call. Scores are
throttled per patient for vitals (a window arrives every few seconds; the models do not need that rate), and a model
that enters its high band is handed back to the processor, which raises it like any other finding."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)


class RiskScorer:
    def __init__(self, base_url: str, token: str, on_high: Callable[[str, dict], None], *,
                 min_interval_s: float = 60, timeout_s: float = 10):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                 headers={"X-Service-Token": token, "X-Service-Name": "stream-processor"})
        self.on_high = on_high
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}
        self._bands: dict[tuple[str, str], str] = {}
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue[str] = queue.Queue()

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
        self._q.put(patient_id)

    def _score(self, patient_id: str, trigger: str) -> None:
        r = self.http.post("/v1/score", json={"patient_id": patient_id, "trigger": trigger})
        r.raise_for_status()
        for score in r.json():
            key = (patient_id, score["model"])
            entered_high = score["band"] == "high" and self._bands.get(key) != "high"
            self._bands[key] = score["band"]
            if entered_high:
                self.on_high(patient_id, score)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                patient_id = self._q.get(timeout=1)
            except queue.Empty:
                continue
            with self._lock:
                trigger = self._pending.pop(patient_id, "vitals")
            try:
                self._score(patient_id, trigger)
            except Exception as exc:  # noqa: BLE001 - scoring is advisory; the rules path already ran
                log.warning("risk scoring failed", extra={"patient_id": patient_id, "error": str(exc)[:200]})
