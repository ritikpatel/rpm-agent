"""SQS ingest consumer and S3 archiver against an in-memory queue with real SQS semantics (visibility, redelivery,
no ordering). No AWS, Redis or Postgres needed."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rpm_core.sqs import SqsReader
from stream_processor.consumer import SqsConsumer, event_order

T0 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class FakeQueue:
    """The SQS calls the reader makes. A received message stays invisible until deleted, released or `expire()`d."""

    def __init__(self, bodies=(), shuffle: random.Random | None = None, age_s: float = 0):
        sent = int((time.time() - age_s) * 1000)  # age_s: how long the messages have been waiting (a backlog)
        self.visible = [{"MessageId": uuid.uuid4().hex, "Body": b, "receives": 0, "sent": sent}
                        for b in bodies]
        self.in_flight: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.shuffle = shuffle
        self.lock = threading.Lock()  # the reader drains with concurrent calls
        self.receive_calls = 0

    def get_queue_url(self, QueueName):  # noqa: N803 - boto3 keyword names
        return {"QueueUrl": f"http://sqs.local/{QueueName}"}

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds, MessageSystemAttributeNames):  # noqa: N803
        with self.lock:
            self.receive_calls += 1
            if self.shuffle:
                self.shuffle.shuffle(self.visible)
            taken, self.visible = self.visible[:MaxNumberOfMessages], self.visible[MaxNumberOfMessages:]
            out = []
            for m in taken:
                m["receives"] += 1
                receipt = uuid.uuid4().hex
                self.in_flight[receipt] = m
                out.append({"MessageId": m["MessageId"], "ReceiptHandle": receipt, "Body": m["Body"],
                            "Attributes": {"SentTimestamp": str(m["sent"]),
                                           "ApproximateReceiveCount": str(m["receives"])}})
            return {"Messages": out} if out else {}

    def delete_message_batch(self, QueueUrl, Entries):  # noqa: N803
        with self.lock:
            assert len(Entries) <= 10
            for e in Entries:
                self.deleted.append(self.in_flight.pop(e["ReceiptHandle"])["Body"])
            return {"Successful": [{"Id": e["Id"]} for e in Entries]}

    def change_message_visibility_batch(self, QueueUrl, Entries):  # noqa: N803
        with self.lock:
            assert all(e["VisibilityTimeout"] == 0 for e in Entries)
            self.visible = [self.in_flight.pop(e["ReceiptHandle"]) for e in Entries] + self.visible
            return {"Successful": []}

    def get_queue_attributes(self, QueueUrl, AttributeNames):  # noqa: N803
        return {"Attributes": {"ApproximateNumberOfMessages": str(len(self.visible)),
                               "ApproximateNumberOfMessagesNotVisible": str(len(self.in_flight))}}

    def expire(self) -> None:
        """The visibility timeout passes: everything received and not deleted is delivered again."""
        with self.lock:
            self.visible += self.in_flight.values()
            self.in_flight = {}


def record(device: str, seq: int, at: datetime | None = None) -> dict:
    at = at or T0 + timedelta(seconds=30 * seq)
    return {"msg_id": f"{device}-{seq}", "device_id": device, "seq": seq, "thing": device, "kind": "telemetry",
            "ts": {"window_end": at.isoformat()}}


def queue_of(records, **kw) -> FakeQueue:
    return FakeQueue([json.dumps(r) for r in records], **kw)


def consumer(q: FakeQueue, **kw) -> SqsConsumer:
    return SqsConsumer(SqsReader(q, "rpm-telemetry", wait_s=0), **kw)


# ---------------------------------------------------------------------- reader
def test_reader_returns_at_once_when_nothing_is_queued_behind_the_first_page():
    q = queue_of([record("a", i) for i in range(3)])
    assert len(SqsReader(q, "q", wait_s=0).receive(500)) == 3 and q.receive_calls == 1


def test_reader_drains_a_backlog_up_to_the_limit():
    q = queue_of([record("a", i) for i in range(1000)])
    reader = SqsReader(q, "q", wait_s=0, parallelism=4)
    assert len(reader.receive(500)) == 500
    assert reader.depth() == {"visible": 500, "in_flight": 500}


# ---------------------------------------------------------------------- consumer
def test_shuffled_backlog_is_processed_in_event_time_order_and_deleted():
    records = [record(d, i) for i in range(40) for d in ("a", "b", "c")]
    q = queue_of(records, shuffle=random.Random(7))
    seen: list[dict] = []
    assert consumer(q).poll_once(seen.extend, wait_s=0)
    assert len(seen) == 120 and seen == sorted(seen, key=event_order)
    for d in ("a", "b", "c"):
        assert [r["seq"] for r in seen if r["device_id"] == d] == list(range(40))
    assert len(q.deleted) == 120 and not q.visible and not q.in_flight


def test_events_sort_with_telemetry_by_their_own_timestamp():
    sos = {"msg_id": "e1", "seq": 9, "kind": "events", "ts": {"occurred": (T0 + timedelta(seconds=45)).isoformat()}}
    assert event_order(record("a", 1)) < event_order(sos) < event_order(record("a", 2))
    assert event_order({"ts": {"window_end": "not a time"}, "seq": "x"}) == (0.0, 0)


def test_undecodable_message_is_deleted_and_never_blocks_the_batch():
    q = FakeQueue(["{not json", json.dumps([1, 2]), json.dumps(record("a", 1))])
    seen: list[dict] = []
    assert consumer(q).poll_once(seen.extend, wait_s=0)
    assert [r["msg_id"] for r in seen] == ["a-1"] and len(q.deleted) == 3


def test_one_bad_record_cannot_hold_the_batch_back():
    q = queue_of([record("a", i) for i in range(5)])
    stored: list[str] = []

    def handle(batch):
        if any(r["msg_id"] == "a-2" for r in batch):
            raise RuntimeError("cannot store a-2")
        stored.extend(r["msg_id"] for r in batch)

    assert consumer(q).poll_once(handle, wait_s=0)  # the others went through: no back-off
    assert stored == ["a-0", "a-1", "a-3", "a-4"] and len(q.deleted) == 4
    # the bad record is neither deleted nor released: SQS redelivers it after the visibility timeout, and moves
    # it to the dead-letter queue after maxReceiveCount
    assert not q.visible and [m["Body"] for m in q.in_flight.values()] == [json.dumps(record("a", 2))]
    q.expire()
    assert consumer(q).poll_once(handle, wait_s=0) is False and q.in_flight  # still failing, alone: back off


def test_dependency_outage_releases_the_batch_and_backs_off():
    q = queue_of([record("a", i) for i in range(25)])
    up = {"db": False}
    stored: list[str] = []

    def handle(batch):
        if not up["db"]:
            raise ConnectionError("postgres is down")
        stored.extend(r["msg_id"] for r in batch)

    c = consumer(q, healthy=lambda: up["db"])
    assert c.poll_once(handle, wait_s=0) is False
    assert not q.deleted and not q.in_flight and len(q.visible) == 25  # visible again at once, nothing lost
    up["db"] = True
    assert c.poll_once(handle, wait_s=0)
    assert stored == [f"a-{i}" for i in range(25)]
    assert len(q.deleted) == 25


def test_crash_before_delete_redelivers_everything():
    q = queue_of([record("a", i) for i in range(12)])
    reader = SqsReader(q, "q", wait_s=0)
    assert len(reader.receive(500)) == 12  # ... and the process dies here
    q.expire()
    seen: list[dict] = []
    assert SqsConsumer(reader).poll_once(seen.extend, wait_s=0)
    assert len(seen) == 12 and len(q.deleted) == 12


# ---------------------------------------------------------------------- catching up on a backlog
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def roughly_ordered(records: list[dict], rng: random.Random, spread: int) -> list[dict]:
    """What SQS hands out from a backlog: the right order overall, shuffled locally."""
    keyed = [(i + rng.uniform(0, spread), r) for i, r in enumerate(records)]
    return [r for _, r in sorted(keyed, key=lambda kr: kr[0])]


def late_windows(q: FakeQueue, **kw) -> tuple[int, int]:
    """Drain the queue; count records the processor would flag late (older than the newest applied per device)."""
    high: dict[str, int] = {}
    late = seen = 0

    def handle(batch):
        nonlocal late, seen
        for r in batch:
            seen += 1
            late += r["seq"] < high.get(r["device_id"], -1)
            high[r["device_id"]] = max(high.get(r["device_id"], -1), r["seq"])

    c = consumer(q, batch_limit=50, **kw)
    for _ in range(200):
        c.poll_once(handle, wait_s=0)
        if not q.visible and not q.in_flight:
            break
    return late, seen


def backlog(seed: int = 3) -> list[str]:
    records = [record(d, i) for i in range(30) for d in ("a", "b", "c", "d", "e")]  # 150 records, 5 per window
    return [json.dumps(r) for r in roughly_ordered(records, random.Random(seed), spread=12)]


def test_backlog_shuffled_across_batches_is_reordered_while_catching_up():
    without = late_windows(FakeQueue(backlog(), age_s=120), reorder_window=0)
    held = late_windows(FakeQueue(backlog(), age_s=120), reorder_window=25)
    assert without[1] == held[1] == 150  # everything is processed either way
    assert without[0] > 0 and held[0] == 0  # sorting inside a batch is not enough; the reorder window is


def test_nothing_is_held_when_the_queue_keeps_up():
    q = queue_of([record("a", i) for i in range(8)])  # fresh messages, less than a full batch
    seen: list[dict] = []
    c = consumer(q, reorder_window=250)
    assert c.poll_once(seen.extend, wait_s=0) and len(seen) == 8 and not c._held and len(q.deleted) == 8


def test_held_records_are_processed_after_max_hold_and_released_on_shutdown():
    q, clock = queue_of([record("a", i) for i in range(6)], age_s=120), Clock()
    seen: list[dict] = []
    c = consumer(q, reorder_window=4, max_hold_s=10, clock=clock)
    c.poll_once(seen.extend, wait_s=0)
    assert [r["seq"] for r in seen] == [0, 1] and len(c._held) == 4 and len(q.in_flight) == 4
    clock.t = 11  # nothing older arrived: the held records go through, in order, well inside the visibility timeout
    c.poll_once(seen.extend, wait_s=0)
    assert [r["seq"] for r in seen] == [0, 1, 2, 3, 4, 5] and not c._held and len(q.deleted) == 6

    q2 = queue_of([record("b", i) for i in range(6)], age_s=120)
    c2 = consumer(q2, reorder_window=4)
    c2.poll_once(seen.extend, wait_s=0)
    stop = threading.Event()
    stop.set()
    c2.run(seen.extend, stop)  # SIGTERM: held records go straight back to the queue for the next instance
    assert len(q2.visible) == 4 and not q2.in_flight and not c2._held
