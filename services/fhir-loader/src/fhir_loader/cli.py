"""fhir-loader: load the cohort into HAPI FHIR and seed the device registry.

  fhir-loader all --mimic-dir ./synthetic_mimic_150patients
  fhir-loader registry --demo-patient auto --demo-device gw-demo-01   # hand one patient to the real watch
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from rpm_common.data import fetch_dir
from rpm_common.db import migrate, wait_for_db
from rpm_common.devices import DeviceCommander
from rpm_common.logging import setup_logging
from rpm_mimic import CohortMember, MimicDataset, build_cohort, pick_demo_patient

from .client import FhirClient
from .mapping import PatientBundleBuilder
from .registry import gateway_config, seed

log = logging.getLogger("fhir_loader")


def load_fhir(a, members: list[CohortMember], ds: MimicDataset, anchor: datetime, demo) -> None:
    fhir = FhirClient(a.fhir_url)
    fhir.wait_ready()
    builder = PatientBundleBuilder(ds, anchor)
    t0, done, total = time.monotonic(), 0, 0

    def one(m: CohortMember) -> int:
        devices = [(demo[1], "watch")] if demo and demo[0] == m.patient_id else \
                  [(m.device_id, "bedside" if m.care_setting == "icu" else "simulator")]
        b = builder.bundle(m, devices, include_labs=not a.skip_labs)
        fhir.transaction(b)
        return len(b["entry"])

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(one, m): m for m in members}
        for f in as_completed(futures):
            total += f.result()
            done += 1
            if done % 25 == 0 or done == len(members):
                log.info("fhir load progress", extra={"patients": done, "resources": total,
                                                      "elapsed_s": round(time.monotonic() - t0, 1)})
    print(f"FHIR: {done} patients, {total} resources in {time.monotonic() - t0:.0f}s "
          f"(server now has {fhir.count('Patient')} Patient, {fhir.count('Observation')} Observation)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fhir-loader", description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("target", choices=["all", "fhir", "registry"])
    ap.add_argument("--mimic-dir", default=os.environ.get("RPM_MIMIC_DIR", "./synthetic_mimic_150patients"))
    ap.add_argument("--fhir-url", default=os.environ.get("RPM_FHIR_BASE_URL", "http://localhost:8080/fhir"))
    ap.add_argument("--database-url", default=os.environ.get("RPM_DATABASE_URL",
                                                             "postgresql://rpm:rpm@localhost:5432/rpm"))
    ap.add_argument("--icu-count", type=int, default=int(os.environ.get("RPM_ICU_COUNT", "25")))
    ap.add_argument("--seed", default=os.environ.get("RPM_COHORT_SEED", "rpm-v1"))
    ap.add_argument("--demo-patient", default=os.environ.get("RPM_DEMO_PATIENT"),
                    help="patient id streamed by the real watch, or 'auto'")
    ap.add_argument("--demo-device", default=os.environ.get("RPM_DEMO_DEVICE", "gw-demo-01"))
    ap.add_argument("--icu-gateway", default=os.environ.get("RPM_ICU_GATEWAY") or None,
                    help="IoT thing of the ICU bedside gateway that relays the ICU monitors (HL7 path)")
    ap.add_argument("--skip-labs", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args(argv)
    setup_logging("fhir-loader", os.environ.get("RPM_LOG_LEVEL", "INFO"))

    anchor = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    ds = MimicDataset(fetch_dir(a.mimic_dir))
    members = build_cohort(ds, anchor, icu_count=a.icu_count, seed=a.seed)
    demo = None
    chosen = pick_demo_patient(members, a.demo_patient)
    if a.demo_patient and chosen is None:
        raise SystemExit(f"demo patient {a.demo_patient!r} not found in cohort")
    if chosen:
        demo = (chosen.patient_id, a.demo_device)
        print(f"demo patient {chosen.patient_id} ({chosen.display_name}, {chosen.condition}) will be streamed by "
              f"device {a.demo_device}; the simulator excludes it automatically")

    if a.target in ("all", "registry"):
        wait_for_db(a.database_url)
        migrate(a.database_url)
        print(f"registry: {seed(a.database_url, members, demo, icu_gateway=a.icu_gateway)}")
        if a.icu_gateway:
            # retained, so the gateway gets its beds' SpO2 scale on every (re)connect, even before this job reruns
            cfg = gateway_config(a.database_url, a.icu_gateway)
            DeviceCommander(mode=os.environ.get("RPM_DEVICE_COMMAND_MODE", "mqtt"),
                            region=os.environ.get("RPM_AWS_REGION", "us-east-1"),
                            iot_endpoint=os.environ.get("RPM_IOT_ENDPOINT") or None,
                            mqtt_host=os.environ.get("RPM_MQTT_HOST", "localhost"),
                            mqtt_port=int(os.environ.get("RPM_MQTT_PORT", "1883"))).publish_raw(
                f"rpm/v1/dev/{a.icu_gateway}/config", cfg.to_json(), retain=True)
            print(f"gateway config: {a.icu_gateway} <- {len(cfg.beds)} beds, "
                  f"{sum(1 for b in cfg.beds.values() if b.spo2_scale == 2)} on SpO2 scale 2 (retained)")
    if a.target in ("all", "fhir"):
        load_fhir(a, members, ds, anchor, demo)


if __name__ == "__main__":
    main()
