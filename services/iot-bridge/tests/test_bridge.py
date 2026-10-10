"""The local IoT-rule emulator must add exactly what the AWS rule's SQL adds, and must not lose messages to a
partial SQS failure."""

import json

from botocore.exceptions import EndpointConnectionError

from iot_bridge.main import enrich, send_all


def test_enrich_matches_the_rule_sql():
    doc = enrich("rpm/v1/dev/gw-demo-01/telemetry", b'{"device_id": "gw-demo-01", "seq": 7}', 1_760_000_000_000)
    assert doc == {"device_id": "gw-demo-01", "seq": 7, "ingest_ts_ms": 1_760_000_000_000, "thing": "gw-demo-01",
                   "kind": "telemetry"}
    # the thing comes from the topic, never from the payload
    assert enrich("rpm/v1/dev/sim-1/events", b'{"thing": "sim-9"}', 1)["thing"] == "sim-1"


def test_enrich_ignores_topics_outside_the_rule():
    for topic in ("rpm/v1/dev/x/cmd", "rpm/v1/dev/x/config", "rpm/v1/sim/control", "rpm/v1/dev/x/telemetry/extra"):
        assert enrich(topic, b"{}", 1) is None


class FlakySqs:
    def __init__(self, fail_first_call=(), down_for=0):
        self.sent: list[str] = []
        self.calls = 0
        self.fail_first_call, self.down_for = set(fail_first_call), down_for

    def send_message_batch(self, QueueUrl, Entries):  # noqa: N803 - boto3 keyword names
        self.calls += 1
        assert len(Entries) <= 10
        if self.calls <= self.down_for:
            raise EndpointConnectionError(endpoint_url=QueueUrl)
        failed = [e for e in Entries if self.calls == 1 and e["Id"] in self.fail_first_call]
        self.sent += [e["MessageBody"] for e in Entries if e not in failed]
        return {"Failed": [{"Id": e["Id"]} for e in failed]}


def test_send_all_batches_by_ten_and_resends_only_failed_entries(monkeypatch):
    monkeypatch.setattr("iot_bridge.main.time.sleep", lambda _s: None)
    bodies = [json.dumps({"n": n}) for n in range(23)]
    sqs = FlakySqs(fail_first_call={"3", "7"})
    assert send_all(sqs, "http://sqs/q", bodies) == 0
    assert sorted(sqs.sent) == sorted(bodies) and sqs.calls == 4  # 3 batches + 1 resend of two entries


def test_send_all_rides_out_a_short_outage_and_reports_what_it_could_not_send(monkeypatch):
    monkeypatch.setattr("iot_bridge.main.time.sleep", lambda _s: None)
    sqs = FlakySqs(down_for=2)
    assert send_all(sqs, "http://sqs/q", ["a", "b"]) == 0 and sqs.sent == ["a", "b"]
    assert send_all(FlakySqs(down_for=99), "http://sqs/q", ["a", "b"]) == 2
