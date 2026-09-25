from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class FhirClient:
    def __init__(self, base_url: str, timeout_s: float = 180):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout_s, headers={"Content-Type": "application/fhir+json",
                                                             "Accept": "application/fhir+json"})

    def wait_ready(self, timeout_s: float = 300) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if self.http.get(f"{self.base}/metadata", timeout=10).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"FHIR server {self.base} not ready")
            time.sleep(3)

    def transaction(self, bundle: dict, retries: int = 3) -> dict:
        for attempt in range(retries):
            resp = self.http.post(self.base, json=bundle)
            if resp.status_code < 300:
                return resp.json()
            if resp.status_code < 500 or attempt == retries - 1:
                raise RuntimeError(f"FHIR transaction failed ({resp.status_code}): {resp.text[:800]}")
            time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def count(self, resource_type: str) -> int:
        r = self.http.get(f"{self.base}/{resource_type}", params={"_summary": "count"})
        r.raise_for_status()
        return r.json().get("total", 0)
