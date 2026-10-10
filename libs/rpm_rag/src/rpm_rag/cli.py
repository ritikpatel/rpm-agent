"""rpm-rag: build the retrieval index and query it.

  rpm-rag index --mimic-dir ./synthetic_mimic_150patients --protocols ./knowledge/protocols
  rpm-rag search "rising heart rate and fever after discharge" --patient p-90000001
"""

from __future__ import annotations

import argparse
import base64
import os
import time
from datetime import UTC, datetime

import httpx

from rpm_common.data import fetch_dir
from rpm_common.db import make_pool, migrate, wait_for_db
from rpm_common.logging import setup_logging
from rpm_mimic import MimicDataset, build_cohort

from .discharge import build_all
from .documents import load_protocols
from .embeddings import make_embedder
from .index import RagIndex


def _fhir_document(base: str, doc) -> None:
    """Mirror patient summaries into FHIR as DocumentReference (FHIR stays the clinical system of record)."""
    body = {"resourceType": "DocumentReference", "id": doc.doc_id.lower().replace("_", "-"), "status": "current",
            "meta": {"tag": [{"system": "urn:rpm:tags", "code": "synthetic"}]},
            "type": {"text": doc.title.split(" — ")[0]}, "subject": {"reference": f"Patient/{doc.patient_id}"},
            "date": datetime.now(UTC).isoformat(), "description": doc.title,
            "content": [{"attachment": {"contentType": "text/markdown", "title": doc.title,
                                        "data": base64.b64encode(doc.content.encode()).decode()}}]}
    httpx.put(f"{base}/DocumentReference/{body['id']}", json=body, timeout=20,
              headers={"Content-Type": "application/fhir+json"}).raise_for_status()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rpm-rag", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["index", "search"])
    ap.add_argument("query", nargs="?")
    ap.add_argument("--patient")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--mimic-dir", default=os.environ.get("RPM_MIMIC_DIR", "./synthetic_mimic_150patients"))
    ap.add_argument("--protocols", default=os.environ.get("RPM_PROTOCOLS_DIR", "./knowledge/protocols"))
    ap.add_argument("--database-url", default=os.environ.get("RPM_DATABASE_URL",
                                                             "postgresql://rpm:rpm@localhost:5432/rpm"))
    ap.add_argument("--embeddings", default=os.environ.get("RPM_EMBEDDINGS", "hash"), choices=["hash", "titan"])
    ap.add_argument("--region", default=os.environ.get("RPM_AWS_REGION", "ap-southeast-2"))
    ap.add_argument("--fhir-url", default=os.environ.get("RPM_FHIR_BASE_URL"))
    a = ap.parse_args(argv)
    setup_logging("rpm-rag", os.environ.get("RPM_LOG_LEVEL", "INFO"))
    wait_for_db(a.database_url)
    migrate(a.database_url)
    pool = make_pool(a.database_url, max_size=2)
    index = RagIndex(pool, make_embedder(a.embeddings, a.region))

    if a.command == "index":
        t0 = time.monotonic()
        protocols = load_protocols(fetch_dir(a.protocols))
        ds = MimicDataset(fetch_dir(a.mimic_dir))
        members = build_cohort(ds, datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0))
        summaries = build_all(ds, members)
        n = index.upsert(protocols) + index.upsert(summaries)
        mirrored = 0
        if a.fhir_url:
            for doc in summaries:
                try:
                    _fhir_document(a.fhir_url, doc)
                    mirrored += 1
                except httpx.HTTPError:
                    pass
        print(f"indexed {len(protocols)} protocols + {len(summaries)} patient summaries = {n} chunks "
              f"({index.embedder.name}) in {time.monotonic() - t0:.1f}s; {mirrored} mirrored to FHIR")
    else:
        for h in index.search(a.query or "", a.patient, a.k):
            print(f"{h.score:.4f}  {h.chunk_id:22s} {h.kind:17s} {h.heading[:40]:40s} {h.title[:50]}")
    pool.close()


if __name__ == "__main__":
    main()
