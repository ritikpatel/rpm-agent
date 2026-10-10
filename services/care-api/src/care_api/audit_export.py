"""rpm-audit-export: ship new audit_log rows to write-once storage as FHIR R4 AuditEvent (NDJSON, gzip).

Runs hourly (Helm CronJob). Each batch is hash-chained to the previous one (audit_exports.prev_sha256, also stored as
object metadata), so a missing or altered batch is detectable. Only rows older than SETTLE seconds are taken: audit
inserts commit in milliseconds, so a sequence id cannot commit after a later id has been exported; and only the
settled prefix (in id order) is taken, so the watermark never skips a row.

    rpm-audit-export --dest s3://<audit-bucket>/app-audit      (AWS: Object Lock bucket)
    rpm-audit-export --dest ./data/audit                        (local directory)
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from rpm_core.aws import client
from rpm_core.db import wait_for_db
from rpm_core.logging import setup_logging
from rpm_core.settings import BaseServiceSettings

log = logging.getLogger("rpm_audit_export")
SETTLE_S = 60
NS = uuid.UUID("5b0e3f4e-6a1f-4f0e-9d8a-3a4f1c2b7e10")  # stable AuditEvent ids from audit_log ids
ACTION = {"read": ("R", "read"), "create": ("C", "create"), "update": ("U", "update"), "delete": ("D", "delete")}


def audit_event(row: dict) -> dict:
    """One audit_log row as a FHIR R4 AuditEvent (RESTful operation by a clinician through the console API)."""
    code, interaction = ACTION.get(row["action"], ("E", row["action"]))
    entities = []
    if row["resource_id"]:
        entities.append({"what": {"reference": f"{row['resource_type']}/{row['resource_id']}"},
                         "type": {"system": "http://hl7.org/fhir/resource-types", "code": row["resource_type"]}})
    if row["patient_id"]:
        entities.append({"what": {"reference": f"Patient/{row['patient_id']}"},
                         "role": {"system": "http://terminology.hl7.org/CodeSystem/object-role", "code": "1",
                                  "display": "Patient"}})
    details = row.get("details") or {}
    return {
        "resourceType": "AuditEvent",
        "id": str(uuid.uuid5(NS, str(row["id"]))),
        "type": {"system": "http://terminology.hl7.org/CodeSystem/audit-event-type", "code": "rest",
                 "display": "RESTful Operation"},
        "subtype": [{"system": "http://hl7.org/fhir/restful-interaction", "code": interaction}],
        "action": code,
        "recorded": row["at"].astimezone(UTC).isoformat(),
        "outcome": "0" if row["outcome"] == "success" else "4",
        "agent": [{"who": {"identifier": {"system": "urn:rpm:user", "value": row["actor"]}},
                   "altId": details.get("sub"), "requestor": True,
                   "role": [{"text": row["actor_role"] or "unknown"}],
                   **({"network": {"address": row["source_ip"], "type": "2"}} if row["source_ip"] else {})}],
        "source": {"observer": {"display": "rpm-api"},
                   "type": [{"system": "http://terminology.hl7.org/CodeSystem/security-source-type", "code": "4"}]},
        "entity": entities,
        "extension": [{"url": "urn:rpm:audit-log-id", "valueInteger": row["id"]},
                      {"url": "urn:rpm:request-path", "valueString": details.get("path", "")}],
    }


class Sink:
    def __init__(self, dest: str, region: str, endpoint: str | None = None):
        self.dest = dest.rstrip("/")
        if self.dest.startswith("s3://"):
            self.bucket, _, self.prefix = self.dest[5:].partition("/")
            self.s3 = client("s3", region, endpoint)

    def put(self, key: str, body: bytes, meta: dict[str, str]) -> str:
        if self.dest.startswith("s3://"):
            full = f"{self.prefix}/{key}" if self.prefix else key
            # Object Lock buckets need a checksum on every put; retention comes from the bucket default
            self.s3.put_object(Bucket=self.bucket, Key=full, Body=body, ContentType="application/fhir+ndjson",
                               ContentEncoding="gzip", Metadata=meta, ChecksumAlgorithm="SHA256")
            return f"s3://{self.bucket}/{full}"
        path = Path(self.dest) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.with_suffix(".meta.json").write_text(json.dumps(meta))
        return str(path)


def export_once(conn: psycopg.Connection, sink: Sink, batch_size: int = 50_000) -> dict | None:
    with conn.transaction():
        conn.execute("LOCK TABLE audit_exports IN EXCLUSIVE MODE")  # one exporter at a time
        last = conn.execute("SELECT last_id, sha256 FROM audit_exports ORDER BY batch_id DESC LIMIT 1").fetchone()
        after, prev_sha = (last[0], last[1]) if last else (0, None)
        with conn.cursor(row_factory=dict_row) as cur:
            candidates = cur.execute(
                "SELECT *, at < now() - make_interval(secs => %s) AS settled FROM audit_log WHERE id > %s "
                "ORDER BY id LIMIT %s", (SETTLE_S, after, batch_size)).fetchall()
        # Export only the settled prefix in id order: the watermark must never pass a row that is not yet
        # exported, or that row would be skipped for ever.
        rows = []
        for r in candidates:
            if not r.pop("settled"):
                break
            rows.append(r)
        if not rows:
            return None
        body = gzip.compress("".join(json.dumps(audit_event(r), separators=(",", ":")) + "\n"
                                     for r in rows).encode(), mtime=0)
        sha = hashlib.sha256(body).hexdigest()
        now = datetime.now(UTC)
        key = f"{now:%Y/%m/%d}/audit-{rows[0]['id']:012d}-{rows[-1]['id']:012d}.ndjson.gz"
        meta = {"first-id": str(rows[0]["id"]), "last-id": str(rows[-1]["id"]), "count": str(len(rows)),
                "sha256": sha, "prev-sha256": prev_sha or "genesis"}
        where = sink.put(key, body, meta)
        conn.execute("INSERT INTO audit_exports (first_id, last_id, row_count, object_key, sha256, prev_sha256) "
                     "VALUES (%s,%s,%s,%s,%s,%s)", (rows[0]["id"], rows[-1]["id"], len(rows), where, sha, prev_sha))
    return {"object": where, "rows": len(rows), "sha256": sha, "prev_sha256": prev_sha}


def main(argv: list[str] | None = None) -> None:
    cfg = BaseServiceSettings()
    ap = argparse.ArgumentParser(prog="rpm-audit-export", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="s3://bucket/prefix or a local directory")
    ap.add_argument("--s3-endpoint", default=None)
    a = ap.parse_args(argv)
    setup_logging("audit-export", cfg.log_level)
    wait_for_db(cfg.database_url)
    sink = Sink(a.dest, cfg.aws_region, a.s3_endpoint)
    total = 0
    with psycopg.connect(cfg.database_url, autocommit=True) as conn:
        while (batch := export_once(conn, sink)) is not None:
            total += batch["rows"]
            log.info("audit batch exported", extra=batch)
    log.info("audit export done", extra={"rows": total})


if __name__ == "__main__":
    main()
