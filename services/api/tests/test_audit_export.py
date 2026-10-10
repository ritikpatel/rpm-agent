"""Audit export against a throwaway Postgres database (skipped when none is reachable)."""

import gzip
import hashlib
import json
import os
import uuid

import psycopg
import pytest

from rpm_api.audit_export import Sink, export_once
from rpm_common.db import migrate

ADMIN_URL = os.environ.get("RPM_TEST_PG", "postgresql://rpm:rpm@localhost:5432/rpm")


def _pg() -> bool:
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _pg(), reason="Postgres not reachable (start `make up`)")


@pytest.fixture
def db():
    name = f"rpm_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    migrate(url)
    with psycopg.connect(url, autocommit=True) as conn:
        yield conn
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE {name} WITH (FORCE)")


def add(conn, n, age_s=120, actor="nurse.jane"):
    for i in range(n):
        conn.execute("INSERT INTO audit_log (at, actor, actor_role, action, resource_type, resource_id, patient_id, "
                     "source_ip, details) VALUES (now() - make_interval(secs => %s), %s, 'nurse', 'read', "
                     "'Observation', NULL, %s, '10.0.0.7', '{\"path\": \"/api/x\", \"sub\": \"u-1\"}')",
                     (age_s, actor, f"p-{i}"))


def read(path):
    raw = open(path, "rb").read()
    return raw, [json.loads(line) for line in gzip.decompress(raw).decode().splitlines()]


def test_export_is_fhir_hash_chained_and_settled(db, tmp_path):
    sink = Sink(str(tmp_path), "us-east-1")
    add(db, 3)
    add(db, 2, age_s=0)                                    # too recent: may still have concurrent id gaps
    first = export_once(db, sink)
    assert first["rows"] == 3 and first["prev_sha256"] is None
    raw, events = read(first["object"])
    assert hashlib.sha256(raw).hexdigest() == first["sha256"]
    ev = events[0]
    assert ev["resourceType"] == "AuditEvent" and ev["action"] == "R" and ev["outcome"] == "0"
    assert ev["agent"][0]["who"]["identifier"]["value"] == "nurse.jane" and ev["agent"][0]["altId"] == "u-1"
    assert {"reference": "Patient/p-0"} in [e["what"] for e in ev["entity"]]
    assert export_once(db, sink) is None                   # nothing settled yet
    add(db, 2)                                             # older rows arriving after the recent ones
    assert export_once(db, sink) is None                   # the watermark must not jump the unsettled rows 4-5
    db.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change")  # test only: age the recent rows
    db.execute("UPDATE audit_log SET at = now() - interval '2 minutes'")
    db.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change")
    second = export_once(db, sink)
    assert second["rows"] == 4 and second["prev_sha256"] == first["sha256"]  # chained to the first batch
    ids = [e["extension"][0]["valueInteger"] for e in read(second["object"])[1]]
    assert ids == [4, 5, 6, 7]


def test_audit_log_is_append_only(db):
    add(db, 1)
    for sql in ("UPDATE audit_log SET actor = 'someone-else'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            db.execute(sql)
    assert db.execute("SELECT actor FROM audit_log").fetchone()[0] == "nurse.jane"
