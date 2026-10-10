"""Alert lifecycle against a real Postgres (throwaway database). Skipped when no Postgres is reachable."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from care_api.alerts import AlertStore, CareTeam, EscalationPolicy, Notifier
from rpm_core.db import make_pool, migrate
from rpm_core.contracts import ClinicalEvent

ADMIN_URL = os.environ.get("RPM_TEST_PG", "postgresql://rpm:rpm@localhost:5432/rpm")


def _pg_available() -> bool:
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable (start `make up`)")


@pytest.fixture(scope="module")
def db_url():
    name = f"rpm_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    migrate(url)
    with psycopg.connect(url) as c:
        for pid, setting in (("p-1", "home"), ("p-2", "icu"), ("p-3", "home")):
            c.execute("INSERT INTO patients (patient_id, display_name, care_setting, primary_condition) "
                      "VALUES (%s, 'Daniel Haddad', %s, 'CHF')", (pid, setting))
            c.execute("INSERT INTO patient_status (patient_id, device_id) VALUES (%s, %s)", (pid, f"sim-{pid}"))
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def pool(db_url):
    p = make_pool(db_url, max_size=4)
    yield p
    p.close()


class FakeChannel:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.sent = name, fail, []

    def send(self, alert, tier_name, contact, link):
        if self.fail:
            raise RuntimeError("provider down")
        self.sent.append((alert["alert_id"], tier_name, link))
        return (contact.get("email") or contact.get("phone") or tier_name, "fake", None)


@pytest.fixture
def env(pool):
    policy = EscalationPolicy.load(timeout_scale=1.0)
    store = AlertStore(pool, policy, consumer=f"test-{uuid.uuid4().hex[:6]}")
    chans = {n: FakeChannel(n) for n in ("console", "email", "sms", "webhook", "watch")}
    notifier = Notifier(policy, CareTeam.load(), chans, "http://console")
    return store, notifier, chans, policy


def event(pid="p-1", severity="medium", etype="SPO2_LOW", minutes_ago=0.0):
    return ClinicalEvent(patient_id=pid, type=etype, severity=severity, rule_id="t", summary=f"{etype} test",
                         requires_alert=True, occurred_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))


def test_open_notify_ack_resolve(env):
    store, notifier, chans, _ = env
    res = store.ingest_event(event("p-1", "medium"))
    assert res.created
    sent = store.dispatch_pending(notifier.send)
    assert [r["alert_id"] for r in sent] == [res.alert_id]
    a = store.get(res.alert_id)
    assert a["tier"] == 1 and a["notify_pending_tier"] is None
    channels = {(n["channel"], n["status"]) for n in a["notifications"]}
    assert ("email", "sent") in channels and ("watch", "sent") in channels  # home patient gets reassurance
    assert all(n["latency_ms"] >= 0 for n in a["notifications"])
    assert store.dispatch_pending(notifier.send) == []  # outbox drained

    assert store.acknowledge(res.alert_id, "nurse.jane")["status"] == "acknowledged"
    assert store.run_due_escalations(datetime.now(UTC) + timedelta(hours=1)) == []  # ack stops the timer
    assert store.resolve(res.alert_id, "nurse.jane", "Called patient; recovered")["status"] == "resolved"
    kinds = [t["kind"] for t in store.get(res.alert_id)["timeline"]]
    assert kinds[:1] == ["opened"] and "acknowledged" in kinds and kinds[-1] == "resolved"


def test_duplicate_event_is_ignored(env):
    store, *_ = env
    ev = event("p-3", "medium")
    assert store.ingest_event(ev) is not None
    assert store.ingest_event(ev) is None
    store.resolve(store.active_for_patient("p-3")["alert_id"], "t", "cleanup")


def test_new_events_attach_and_worsening_repages_even_after_ack(env):
    store, notifier, _, _ = env
    first = store.ingest_event(event("p-2", "medium", "NEWS2_RED_PARAM"))
    store.dispatch_pending(notifier.send)
    store.acknowledge(first.alert_id, "nurse")
    second = store.ingest_event(event("p-2", "medium", "HR_SUSTAINED_HIGH"))
    assert second.alert_id == first.alert_id and not second.created and not second.severity_raised
    third = store.ingest_event(event("p-2", "critical", "SPO2_CRITICAL"))
    assert third.severity_raised
    a = store.get(first.alert_id)
    assert a["severity"] == "critical" and a["tier"] == 3 and a["status"] == "escalated"
    assert a["notify_pending_tier"] == 3 and set(a["event_types"]) >= {"NEWS2_RED_PARAM", "SPO2_CRITICAL"}
    store.dispatch_pending(notifier.send)
    store.resolve(first.alert_id, "t", "cleanup")


def test_timer_escalation_then_reminders_then_stop(env):
    store, notifier, _, policy = env
    res = store.ingest_event(event("p-1", "medium"))
    store.dispatch_pending(notifier.send)
    future = datetime.now(UTC)
    tiers = []
    for _ in range(2 + policy.max_reminders + 1):
        future += timedelta(hours=1)
        for row in store.run_due_escalations(future):
            tiers.append((row["tier"], row["reminders_sent"]))
        store.dispatch_pending(notifier.send)
    assert tiers[:2] == [(2, 0), (3, 0)]
    assert [r for _, r in tiers[2:]] == list(range(1, policy.max_reminders + 1)) + [policy.max_reminders]
    a = store.get(res.alert_id)
    assert a["next_escalation_at"] is None and a["status"] == "escalated"
    assert {n["tier"] for n in a["notifications"]} == {1, 2, 3}
    store.resolve(res.alert_id, "t", "cleanup")


def test_worsening_after_timer_escalation_stays_escalated_without_second_reassurance(env):
    store, notifier, chans, _ = env
    res = store.ingest_event(event("p-1", "medium"))
    store.dispatch_pending(notifier.send)
    future = datetime.now(UTC)
    for _ in range(2):  # timer: tier 1 -> 2 -> 3
        future += timedelta(hours=1)
        store.run_due_escalations(future)
        store.dispatch_pending(notifier.send)
    watch_before = len(chans["watch"].sent)
    assert store.ingest_event(event("p-1", "high", "EDGE_ALERT")).severity_raised
    assert store.ingest_event(event("p-1", "critical", "SPO2_CRITICAL")).severity_raised
    sent = store.dispatch_pending(notifier.send)
    a = store.get(res.alert_id)
    assert a["status"] == "escalated" and a["tier"] == 3 and a["severity"] == "critical"
    assert len(sent) == 1  # two raises before dispatch -> one page
    assert len(chans["watch"].sent) == watch_before  # the patient was reassured once, at open
    by_tier = {}
    for n in a["notifications"]:
        by_tier.setdefault((n["tier"], n["cause"]), []).append(n["latency_ms"])
    assert {k for k in by_tier} == {(1, "event"), (2, "timer"), (3, "timer"), (3, "event")}
    assert max(by_tier[(3, "event")]) < 5_000  # measured from the SpO2 event, not from when the alert opened
    store.resolve(res.alert_id, "t", "cleanup")


def test_failed_channel_is_recorded_and_others_still_sent(env, pool):
    store, _, chans, policy = env
    chans["email"] = FakeChannel("email", fail=True)
    notifier = Notifier(policy, CareTeam.load(), chans, "http://console")
    res = store.ingest_event(event("p-1", "high"))
    store.dispatch_pending(notifier.send)
    a = store.get(res.alert_id)
    by = {n["channel"]: n for n in a["notifications"]}
    assert by["email"]["status"] == "failed" and "provider down" in by["email"]["error"]
    assert by["sms"]["status"] == "sent" and by["console"]["status"] == "sent"
    store.resolve(res.alert_id, "t", "cleanup")


def test_minimum_necessary_phi_in_messages():
    from care_api.alerts import minimal_ref

    ref = minimal_ref({"display_name": "Daniel Haddad", "patient_id": "p-90000006"})
    assert ref == "D.H. (…0006)"
