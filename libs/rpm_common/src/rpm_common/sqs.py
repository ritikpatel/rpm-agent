"""Batched reads from one SQS queue: long-poll for the first messages, then drain in parallel while a backlog exists.

SQS returns at most 10 messages per call, so a single sequential reader tops out at a few hundred messages a second.
Draining with a few concurrent short polls lifts that ceiling and, as important, lets the caller see a large slice of
a backlog at once (standard queues do not preserve order; the caller sorts what it drained).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

SQS_BATCH = 10  # API limit for ReceiveMessage / DeleteMessageBatch / ChangeMessageVisibilityBatch


class SqsReader:
    def __init__(self, sqs, queue: str, *, wait_s: int = 10, parallelism: int = 4):
        self.sqs = sqs
        self.queue = queue
        self.wait_s = wait_s  # must stay below the client's read timeout (rpm_common.aws: 15 s)
        self.parallelism = max(1, parallelism)
        self._pool = ThreadPoolExecutor(max_workers=self.parallelism, thread_name_prefix=f"sqs-{queue}")
        self._url: str | None = None

    # ------------------------------------------------------------------ queue
    @property
    def url(self) -> str:
        if self._url is None:
            self._url = self.sqs.get_queue_url(QueueName=self.queue)["QueueUrl"]
        return self._url

    def wait_ready(self, stop: threading.Event, timeout_s: float = 120) -> None:
        deadline = time.monotonic() + timeout_s
        while not stop.is_set():
            try:
                _ = self.url
                return
            except (BotoCoreError, ClientError) as exc:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"SQS queue {self.queue} not available") from exc
                stop.wait(2)

    def depth(self) -> dict[str, int]:
        """Approximate backlog: messages waiting, and messages received but not yet deleted."""
        attrs = self.sqs.get_queue_attributes(QueueUrl=self.url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return {"visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
                "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))}

    # ------------------------------------------------------------------ read
    def _receive(self, wait_s: int) -> list[dict]:
        resp = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=SQS_BATCH, WaitTimeSeconds=wait_s,
                                        MessageSystemAttributeNames=["SentTimestamp", "ApproximateReceiveCount"])
        return resp.get("Messages", [])

    def receive(self, limit: int = 500, wait_s: int | None = None) -> list[dict]:
        """Up to `limit` messages. Blocks for at most `wait_s` when the queue is empty; never waits for more once
        something has arrived, so a quiet queue adds no latency."""
        msgs = self._receive(self.wait_s if wait_s is None else wait_s)
        if len(msgs) < SQS_BATCH:  # not a full page: nothing is queued behind these
            return msgs
        while len(msgs) < limit:
            calls = min(self.parallelism, -(-(limit - len(msgs)) // SQS_BATCH))
            got = [m for batch in self._pool.map(self._receive, [0] * calls) for m in batch]
            msgs.extend(got)
            # Short polls sample a subset of SQS hosts, so a round is rarely completely full even with a backlog;
            # stop only when it comes back mostly empty.
            if len(got) < calls * SQS_BATCH // 2:
                break
        return msgs

    # ------------------------------------------------------------------ settle
    def _chunks(self, messages: list[dict]) -> list[list[dict]]:
        return [messages[i:i + SQS_BATCH] for i in range(0, len(messages), SQS_BATCH)]

    def delete(self, messages: list[dict]) -> int:
        """Delete processed messages; returns how many could not be deleted (they are redelivered later)."""
        def one(chunk: list[dict]) -> int:
            resp = self.sqs.delete_message_batch(QueueUrl=self.url, Entries=[
                {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(chunk)])
            return len(resp.get("Failed", []))

        failed = sum(self._pool.map(one, self._chunks(messages)))
        if failed:
            log.warning("sqs delete failed for some messages; they will be redelivered",
                        extra={"queue": self.queue, "count": failed})
        return failed

    def release(self, messages: list[dict]) -> None:
        """Make unprocessed messages visible again now instead of after the visibility timeout (best effort)."""
        def one(chunk: list[dict]) -> None:
            self.sqs.change_message_visibility_batch(QueueUrl=self.url, Entries=[
                {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"], "VisibilityTimeout": 0}
                for i, m in enumerate(chunk)])

        try:
            list(self._pool.map(one, self._chunks(messages)))
        except (BotoCoreError, ClientError) as exc:
            log.warning("sqs release failed; messages return after the visibility timeout",
                        extra={"queue": self.queue, "error": str(exc)[:200]})

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def sent_ms(message: dict) -> int | None:
    raw = message.get("Attributes", {}).get("SentTimestamp")
    return int(raw) if raw is not None else None


def receive_count(message: dict) -> int:
    return int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))
