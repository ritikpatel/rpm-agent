"""Consumer for the device ingest queue (SQS standard): long-poll, drain, process, delete. At-least-once.

A standard queue may deliver a message twice and does not order messages. Neither is assumed downstream:
- duplicates: processing is idempotent on `msg_id` (unique in `vitals`, Redis dedupe for events);
- order: the processor works in event time. A window older than the newest one already applied for its device is
  stored and flagged `late`, never merged into live state.
To keep the late path rare, each drained batch is sorted by event time before it is processed, and one thread
processes batches: per-device state in Redis is read-modify-write, so a device must never be processed twice at
once (docs/adr/0014).

Catching up after an outage, SQS hands a backlog out only roughly in order, so sorting inside a batch is not enough:
records that belong before the end of one batch turn up in the next. While the consumer is behind, the newest
`reorder_window` records of each batch are therefore held back and sorted together with the next drain. Nothing is
held when the queue is keeping up, so the live path gains no latency, and nothing is held longer than `max_hold_s`
(well inside the queue's visibility timeout).

Failure handling:
- Postgres or Redis down: the batch is released and nothing more is received until they answer again, so an outage
  does not burn receive counts (which would push healthy messages into the dead-letter queue).
- anything else: the batch is retried record by record, so one bad record cannot hold the rest back. A record that
  still fails stays invisible until the queue's visibility timeout, is retried then, and moves to the dead-letter
  queue after maxReceiveCount receives.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from rpm_common.sqs import SqsReader, receive_count, sent_ms

from . import metrics as M

log = logging.getLogger(__name__)

Handler = Callable[[list[dict]], None]


def event_order(record: dict) -> tuple[float, int]:
    """Sort key: event time (telemetry window end, device event time), then the device's sequence number."""
    ts = record.get("ts")
    raw = (ts.get("window_end") or ts.get("occurred")) if isinstance(ts, dict) else None
    try:
        when = datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        when = 0.0  # malformed: the processor rejects it; its position in the batch is irrelevant
    seq = record.get("seq")
    return when, seq if isinstance(seq, int) else 0


class SqsConsumer:
    def __init__(self, reader: SqsReader, *, batch_limit: int = 1000, reorder_window: int | None = None,
                 behind_s: float = 5.0, max_hold_s: float = 10.0, healthy: Callable[[], bool] | None = None,
                 max_backoff_s: float = 15.0, clock: Callable[[], float] = time.monotonic):
        self.reader = reader
        self.batch_limit = batch_limit
        # Records held back per batch while catching up (0 = off). Half a batch by default: measured against SQS
        # in ap-southeast-2, 1,000 / 500 put 1,200- and 3,000-message backlogs back in order (docs/adr/0014).
        self.reorder_window = batch_limit // 2 if reorder_window is None else reorder_window
        self.behind_s = behind_s              # queue age above which the consumer counts as catching up
        self.max_hold_s = max_hold_s
        self.healthy = healthy or (lambda: True)
        self.max_backoff_s = max_backoff_s
        self.clock = clock
        self._held: list[tuple[dict, dict, float]] = []  # (message, record, received at)

    @staticmethod
    def _decode(messages: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
        """((message, record) pairs, undecodable messages)."""
        pairs, bad = [], []
        for m in messages:
            try:
                record = json.loads(m["Body"])
                if not isinstance(record, dict):
                    raise ValueError("body is not a JSON object")
            except (ValueError, KeyError):
                bad.append(m)
                log.warning("undecodable message dropped", extra={"message_id": m.get("MessageId")})
                continue
            pairs.append((m, record))
        return pairs, bad

    def _order(self, fresh: list[tuple[dict, dict]], behind: bool) -> list[tuple[dict, dict]]:
        """Merge held and fresh records in event-time order; while behind, keep the newest ones for the next drain."""
        now = self.clock()
        items = sorted(self._held + [(m, r, now) for m, r in fresh], key=lambda it: event_order(it[1]))
        self._held = []
        if behind and self.reorder_window:
            cut = max(0, len(items) - self.reorder_window)
            self._held = [it for it in items[cut:] if now - it[2] < self.max_hold_s]
            items = items[:cut] + [it for it in items[cut:] if now - it[2] >= self.max_hold_s]
        M.HELD.set(len(self._held))
        return [(m, r) for m, r, _ in items]

    def poll_once(self, handle: Handler, wait_s: int | None = None) -> bool:
        """Receive and settle one batch. False means the caller should back off before polling again."""
        if self._held:
            wait_s = 0  # held records must not sit through a long poll
        messages = self.reader.receive(self.batch_limit, wait_s)
        if not messages and not self._held:
            M.QUEUE_AGE.set(0)
            return True
        sent = [s for s in map(sent_ms, messages) if s is not None]
        age = max(0.0, time.time() - min(sent) / 1000) if sent else 0.0
        M.QUEUE_AGE.set(age)
        M.MESSAGES.labels("redelivered").inc(sum(1 for m in messages if receive_count(m) > 1))

        fresh, done = self._decode(messages)  # undecodable bodies can never succeed: delete, do not retry
        M.MESSAGES.labels("undecodable").inc(len(done))
        pairs = self._order(fresh, behind=len(messages) >= self.batch_limit or age > self.behind_s)
        ok = True
        try:
            if pairs:
                handle([record for _, record in pairs])
            done += [m for m, _ in pairs]
        except Exception:
            if not self.healthy():
                log.exception("batch failed while a dependency is down; released for retry")
                held, self._held = [m for m, _, _ in self._held], []
                self.reader.release([m for m, _ in pairs] + held)
                M.HELD.set(0)
                M.MESSAGES.labels("failed").inc(len(pairs))
                ok = False
            else:
                log.exception("batch failed; retrying record by record")
                passed = self._one_by_one(handle, pairs)
                done += passed
                M.MESSAGES.labels("failed").inc(len(pairs) - len(passed))
                ok = bool(passed)  # nothing got through although the dependencies answer: slow down
        if done:
            self.reader.delete(done)
            M.MESSAGES.labels("processed").inc(len(done))
        return ok

    @staticmethod
    def _one_by_one(handle: Handler, pairs: list[tuple[dict, dict]]) -> list[dict]:
        passed = []
        for m, record in pairs:
            try:
                handle([record])
                passed.append(m)
            except Exception:
                log.exception("record failed; left on the queue for redelivery",
                              extra={"msg_id": record.get("msg_id"), "receives": receive_count(m)})
        return passed

    def run(self, handle: Handler, stop: threading.Event) -> None:
        self.reader.wait_ready(stop)
        failures = 0
        while not stop.is_set():
            try:
                ok = self.poll_once(handle)
            except Exception:  # receive / delete errors (SQS unreachable): keep the consumer alive
                log.exception("ingest poll failed")
                ok = False
            if ok:
                failures = 0
                continue
            failures += 1
            stop.wait(min(self.max_backoff_s, 0.5 * 2 ** min(failures, 6)))
            delay = 1.0
            while not stop.is_set() and not self.healthy():
                log.warning("dependency down; ingest paused", extra={"retry_in_s": delay})
                stop.wait(delay)
                delay = min(delay * 2, self.max_backoff_s)
        if self._held:  # shutting down: hand held records straight back instead of waiting out the visibility timeout
            self.reader.release([m for m, _, _ in self._held])
            self._held = []


def depth_loop(readers: dict[str, SqsReader], stop: threading.Event, interval_s: float = 15) -> None:
    """Depth gauges for the ingest queue and its dead-letter queue (anything in the DLQ was not processed)."""
    while not stop.wait(interval_s):
        for name, reader in readers.items():
            try:
                for state, n in reader.depth().items():
                    M.QUEUE_DEPTH.labels(name, state).set(n)
            except Exception as exc:  # noqa: BLE001 - a metrics probe must never stop the engine
                log.debug("queue depth probe failed", extra={"queue": name, "error": str(exc)[:200]})
