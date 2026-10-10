"""rpm-seed: prepare a fresh environment.

  rpm-seed all            # everything below, in order
  rpm-seed registry       # patients and their devices in the operational store
  rpm-seed fhir           # the hospital record: patients, encounters, conditions, labs, medications
  rpm-seed index          # the guideline index: protocols + each patient's discharge summary
  rpm-seed subscriptions  # ask the FHIR server to notify care-api of admissions, discharges and lab results

The subscriptions are created last, after the historical record is loaded, so only new changes become events.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import httpx

from rpm_core.cohort import CohortMember, TrainingData, build_cohort
from rpm_core.data import fetch_dir
from rpm_core.db import make_pool, migrate, wait_for_db
from rpm_core.logging import setup_logging
from rpm_core.rag import RagIndex, load_protocols, make_embedder
from rpm_core.rag.discharge import build_all

from .client import FhirClient
from .mapping import PatientBundleBuilder
from .registry import seed

log = logging.getLogger("rpm_seed")
FHIR_JSON = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
SUBSCRIPTIONS = {
    "rpm-encounters": ("Encounter?", "Admissions and discharges for the RPM platform"),
    "rpm-labs": ("Observation?category=laboratory", "Lab results for the RPM platform"),
}


def load_fhir(a, members: list[CohortMember], anchor: datetime) -> None:
    fhir = FhirClient(a.fhir_url)
    fhir.wait_ready()
    builder = PatientBundleBuilder(anchor)
    t0, done, total = time.monotonic(), 0, 0

    def one(m: CohortMember) -> int:
        kind = "icu_monitor" if m.care_setting == "icu" else "simulator"
        b = builder.bundle(m, [(m.device_id, kind)], include_labs=not a.skip_labs)
        fhir.transaction(b)
        return len(b["entry"])

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(one, m): m for m in members}
        for f in as_completed(futures):
            total += f.result()
            done += 1
            if done % 10 == 0 or done == len(members):
                log.info("fhir load progress", extra={"patients": done, "resources": total,
                                                      "elapsed_s": round(time.monotonic() - t0, 1)})
    print(f"FHIR: {done} patients, {total} resources in {time.monotonic() - t0:.0f}s "
          f"(server now has {fhir.count('Patient')} Patient, {fhir.count('Observation')} Observation)")


def subscriptions(a, enable: bool) -> None:
    """rest-hook Subscriptions with fixed ids, so re-running replaces them instead of adding duplicates."""
    with httpx.Client(base_url=a.fhir_url.rstrip("/"), timeout=30, headers=FHIR_JSON) as http:
        for sid, (criteria, reason) in SUBSCRIPTIONS.items():
            if not enable:
                r = http.delete(f"/Subscription/{sid}")
                if r.status_code not in (200, 204, 404, 410):
                    r.raise_for_status()
                continue
            http.put(f"/Subscription/{sid}", json={
                "resourceType": "Subscription", "id": sid, "status": "requested", "reason": reason,
                "criteria": criteria,
                "channel": {"type": "rest-hook", "endpoint": a.hook_url, "payload": "application/fhir+json",
                            "header": [f"X-Service-Token: {a.service_token}"]}}).raise_for_status()
    print(f"FHIR subscriptions {'-> ' + a.hook_url if enable else 'removed'}: {', '.join(SUBSCRIPTIONS)}")


def _fhir_document(base: str, doc) -> None:
    """Mirror patient summaries into FHIR as DocumentReference (FHIR stays the clinical system of record)."""
    body = {"resourceType": "DocumentReference", "id": doc.doc_id.lower().replace("_", "-"), "status": "current",
            "meta": {"tag": [{"system": "urn:rpm:tags", "code": "synthetic"}]},
            "type": {"text": doc.title.split(" — ")[0]}, "subject": {"reference": f"Patient/{doc.patient_id}"},
            "date": datetime.now(UTC).isoformat(), "description": doc.title,
            "content": [{"attachment": {"contentType": "text/markdown", "title": doc.title,
                                        "data": base64.b64encode(doc.content.encode()).decode()}}]}
    httpx.put(f"{base}/DocumentReference/{body['id']}", json=body, timeout=20, headers=FHIR_JSON).raise_for_status()


def index(a, members: list[CohortMember], mirror: bool) -> None:
    t0 = time.monotonic()
    pool = make_pool(a.database_url, max_size=2)
    rag = RagIndex(pool, make_embedder(a.embeddings, a.region))
    protocols = load_protocols(fetch_dir(a.protocols))
    summaries = build_all(members)
    n = rag.upsert(protocols) + rag.upsert(summaries)
    mirrored = 0
    if mirror:
        for doc in summaries:
            try:
                _fhir_document(a.fhir_url, doc)
                mirrored += 1
            except httpx.HTTPError:
                pass
    pool.close()
    print(f"index: {len(protocols)} protocols + {len(summaries)} patient summaries = {n} chunks "
          f"({rag.embedder.name}) in {time.monotonic() - t0:.1f}s; {mirrored} mirrored to FHIR")


def main(argv: list[str] | None = None) -> None:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="rpm-seed", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("target", choices=["all", "registry", "fhir", "index", "subscriptions"])
    ap.add_argument("--data-dir", default=env("RPM_DATA_DIR", "./Training Data"),
                    help="the team's training data: the only source of patients")
    ap.add_argument("--protocols", default=env("RPM_PROTOCOLS_DIR", "./knowledge/protocols"))
    ap.add_argument("--fhir-url", default=env("RPM_FHIR_BASE_URL", "http://localhost:8080/fhir"))
    ap.add_argument("--database-url", default=env("RPM_DATABASE_URL", "postgresql://rpm:rpm@localhost:5432/rpm"))
    ap.add_argument("--hook-url", default=env("RPM_FHIR_HOOK_URL", "http://care-api:8000/hooks/fhir"),
                    help="where the FHIR server reaches care-api")
    ap.add_argument("--service-token", default=env("RPM_SERVICE_TOKEN", "dev-service-token"))
    ap.add_argument("--icu", type=int, default=int(env("RPM_COHORT_ICU", "6")),
                    help="patients shown as admitted to the ICU")
    ap.add_argument("--home", type=int, default=int(env("RPM_COHORT_HOME", "24")),
                    help="discharged patients on wearables (0 = everyone else in the data)")
    ap.add_argument("--seed", default=env("RPM_COHORT_SEED", "rpm-v1"))
    ap.add_argument("--embeddings", default=env("RPM_EMBEDDINGS", "hash"), choices=["hash", "titan"])
    ap.add_argument("--region", default=env("RPM_AWS_REGION", "ap-southeast-2"))
    ap.add_argument("--skip-labs", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args(argv)
    setup_logging("rpm-seed", env("RPM_LOG_LEVEL", "INFO"))

    anchor = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    data = TrainingData(fetch_dir(a.data_dir))
    members = build_cohort(data, anchor, a.icu, a.home, a.seed)
    icu = sum(1 for m in members if m.care_setting == "icu")
    real = sum(1 for m in members if m.origin == "mimic_real")
    print(f"cohort: {len(members)} of the {len(data)} patients in the training data ({icu} in ICU, "
          f"{len(members) - icu} at home; {real} MIMIC, {len(members) - real} synthetic)")
    everything = a.target == "all"

    if everything or a.target in ("registry", "index"):
        wait_for_db(a.database_url)
        migrate(a.database_url)
    if everything or a.target == "registry":
        print(f"registry: {seed(a.database_url, members)}")
    if everything:
        FhirClient(a.fhir_url).wait_ready()
        subscriptions(a, enable=False)   # loading history must not look like new admissions and results
    if everything or a.target == "fhir":
        load_fhir(a, members, anchor)
    if everything or a.target == "index":
        index(a, members, mirror=everything)
    if everything or a.target == "subscriptions":
        subscriptions(a, enable=True)


if __name__ == "__main__":
    main()
