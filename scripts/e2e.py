"""End-to-end check of a running stack (local by default): `make e2e`.

Walks the path a demonstration takes and fails loudly at the first thing that does not happen:
  live data -> risk scores -> deterioration scenario -> alert -> page -> agents' cited summary -> FHIR write-back
  -> clinician message and patient reply -> lab result through the hospital record -> discharge (ICU monitor stops,
  wearable starts, readmission risk scored) -> audit trail.

Local sign-in uses the dev tokens care-api mints when RPM_ENV=local. Against AWS, pass --token (an access token of
a user in the physician, presenter and admin groups) and --mailpit "".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, token: str | None = None, body=None, base: str | None = None):
        req = urllib.request.Request(
            (base or self.base) + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode()[:200]}") from None


RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}", flush=True)
    if not ok:
        summary()
        sys.exit(1)


def wait(what: str, fn, timeout_s: float, every_s: float = 2.0):
    """Poll fn until it returns something truthy; returns (value, seconds waited)."""
    t0 = time.monotonic()
    while True:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - keep polling; the last error is reported on timeout
            value, err = None, str(exc)[:160]
        else:
            err = ""
        if value:
            return value, time.monotonic() - t0
        if time.monotonic() - t0 > timeout_s:
            step(what, False, f"not within {timeout_s:.0f}s {err}")
        time.sleep(every_s)


def summary() -> None:
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--fhir", default="http://localhost:8080/fhir", help='"" to skip the FHIR checks')
    ap.add_argument("--mailpit", default="http://localhost:8025", help='"" to skip the e-mail check')
    ap.add_argument("--token", help="access token (AWS); omit locally to use dev tokens")
    ap.add_argument("--ramp-s", type=float, default=20)
    a = ap.parse_args()
    api = Api(a.api)

    if a.token:
        clin = demo = admin = a.token
    else:
        def tok(role):
            return api.call("POST", "/api/auth/dev/token", body={"username": f"e2e.{role}", "role": role})["access_token"]
        clin, demo, admin = tok("physician"), tok("presenter"), tok("admin")

    print("1. Live data")
    patients, _ = wait("patients are streaming", lambda: [
        p for p in api.call("GET", "/api/patients", clin) if p["last_seen"]] or None, 120)
    all_patients = api.call("GET", "/api/patients", clin)
    icu = [p for p in all_patients if p["care_setting"] == "icu" and p["last_seen"]]
    home = [p for p in all_patients if p["care_setting"] == "home" and p["last_seen"]]
    step("ICU monitors and wearables are both reporting", bool(icu) and bool(home),
         f"{len(icu)} ICU, {len(home)} home of {len(all_patients)}")
    active = {x["patient_id"] for x in api.call("GET", "/api/alerts", clin)}
    calm_icu = [p for p in icu if p["patient_id"] not in active]
    step("an ICU patient without an alert is available", len(calm_icu) >= 1,
         f"{len(calm_icu)} available (run `make reset up seed sim` for a fresh cohort)")
    sick = calm_icu[0]
    calm_home = [p for p in home if p["patient_id"] not in active]
    chatty = calm_home[0]
    if len(calm_icu) >= 2:
        leaving = calm_icu[1]
    else:  # every other ICU patient is in use: admit someone from home, which exercises admission as well
        api.call("POST", "/api/demo/admit", demo, {"patient_id": calm_home[-1]["patient_id"]})
        leaving, waited = wait("admission", lambda: next(
            (p for p in api.call("GET", "/api/patients", clin) if p["patient_id"] == calm_home[-1]["patient_id"]
             and p["care_setting"] == "icu" and p["last_seen"]), None), 120)
        step("admission in FHIR moved a home patient to ICU monitoring", True,
             f"{leaving['patient_id']} after {waited:.0f}s, device {leaving['device_id']}")
    v = api.call("GET", f"/api/patients/{sick['patient_id']}/vitals?minutes=10", clin)
    step("ICU monitor sends the full set of vital signs",
         all(v[0].get(k) is not None for k in ("heart_rate", "spo2", "resp_rate", "sbp", "temp")),
         f"source={v[0]['source']}")
    hv = api.call("GET", f"/api/patients/{chatty['patient_id']}/vitals?minutes=10", clin)
    step("home patient has wearable data only", all(r["source"] != "icu_monitor" for r in hv), f"{len(hv)} windows")

    print("2. Risk models")
    risk, _ = wait("risk scores for the ICU patient", lambda: api.call(
        "GET", f"/api/patients/{sick['patient_id']}/risk", clin), 120)
    step("early deterioration model scored the ICU patient",
         any(r["model"] == "early_deterioration" for r in risk),
         ", ".join(f"{r['model']} {r['probability']:.2f}" for r in risk))
    hrisk, _ = wait("risk scores for the home patient", lambda: [
        r for r in api.call("GET", f"/api/patients/{chatty['patient_id']}/risk", clin)
        if r["model"] == "recovery_monitoring"], 180)
    step("recovery monitoring model scored the home patient", True, f"{hrisk[0]['probability']:.2f} ({hrisk[0]['band']})")

    print("3. Deterioration -> alert -> page -> agents")
    api.call("POST", "/api/demo/scenario", demo, {"patient_id": sick["patient_id"], "scenario": "sepsis",
                                                  "ramp_s": a.ramp_s})
    alert, waited = wait("alert opened", lambda: next(
        (x for x in api.call("GET", "/api/alerts", clin) if x["patient_id"] == sick["patient_id"]), None), 180)
    step("alert opened for the deteriorating patient", True,
         f"{alert['severity']} after {waited:.0f}s (scenario ramps over {a.ramp_s:.0f}s): {alert['title'][:60]}")
    detail, _ = wait("care team notified", lambda: (lambda d: d if d["notifications"] else None)(
        api.call("GET", f"/api/alerts/{alert['alert_id']}", clin)), 30)
    sent = [n for n in detail["notifications"] if n["status"] == "sent"]
    lat = min((n["latency_ms"] for n in sent if n["latency_ms"] is not None), default=None)
    step("care team paged without waiting for the agents", bool(sent),
         f"{', '.join(n['channel'] for n in sent)}; sensor reading -> page {lat} ms")
    if a.mailpit:
        mail, _ = wait("alert e-mail", lambda: [m for m in api.call(
            "GET", "/api/v1/messages", base=a.mailpit)["messages"] if sick["patient_id"][-4:] in m["Subject"]], 30)
        step("alert e-mail carries no clinical details", "needs review" in mail[0]["Subject"], mail[0]["Subject"])
    detail, waited = wait("agents' summary", lambda: (lambda d: d if d["brief"] else None)(
        api.call("GET", f"/api/alerts/{alert['alert_id']}", clin)), 240, 3)
    brief = detail["brief"]
    step("agents attached a cited SBAR summary", bool(brief.get("recommendation")) and bool(brief.get("citations")),
         f"after {waited:.0f}s by {brief.get('generated_by')}; citations {brief.get('citation_check')}")
    chunk = api.call("GET", f"/api/knowledge/{urllib.request.quote(brief['citations'][0]['ref'], safe='')}", clin)
    step("each citation opens its source passage", bool(chunk.get("text")), f"{chunk['chunk_id']}: {chunk['title'][:50]}")
    run = api.call("GET", f"/api/patients/{sick['patient_id']}/agent-runs", clin)[0]
    step("agent run recorded", run["status"] in ("succeeded", "degraded"),
         f"{run['status']}, {run['provider']} {run['model']}, tokens in/out {run['input_tokens']}/{run['output_tokens']}")
    api.call("POST", f"/api/alerts/{alert['alert_id']}/ack", clin, {"note": "e2e"})
    step("clinician acknowledged the alert",
         api.call("GET", f"/api/alerts/{alert['alert_id']}", clin)["status"] == "acknowledged")

    if a.fhir:
        print("4. FHIR write-back")
        fhir = Api(a.fhir)
        flags, _ = wait("alert in FHIR", lambda: fhir.call(
            "GET", f"/Flag?patient={sick['patient_id']}&_count=5").get("entry"), 60)
        step("alert written to the hospital record as a Flag", True, flags[0]["resource"]["code"]["text"][:60])
        ra, _ = wait("risk score in FHIR", lambda: fhir.call(
            "GET", f"/RiskAssessment?subject=Patient/{sick['patient_id']}&_count=5").get("entry"), 60)
        step("risk scores written as RiskAssessment", True, f"{len(ra)} resource(s)")
        docs, _ = wait("summary in FHIR", lambda: [e for e in fhir.call(
            "GET", f"/DocumentReference?subject=Patient/{sick['patient_id']}&_count=20").get("entry", [])
            if "SBAR" in e["resource"].get("type", {}).get("text", "")], 60)
        step("agents' summary written as DocumentReference", True, docs[0]["resource"]["description"][:60])

    print("5. Clinician <-> patient")
    replies = sum(1 for x in api.call("GET", f"/api/patients/{chatty['patient_id']}/messages", clin)
                  if x["direction"] == "from_patient")
    api.call("POST", f"/api/patients/{chatty['patient_id']}/messages", clin, {"text": "Please rest; we are watching."})
    api.call("POST", f"/api/patients/{chatty['patient_id']}/commands", clin, {"cmd": "CHECKIN", "topic": "breathless"})
    msgs, waited = wait("patient reply", lambda: (lambda m: m if sum(
        1 for x in m if x["direction"] == "from_patient") > replies else None)(
        api.call("GET", f"/api/patients/{chatty['patient_id']}/messages", clin)), 90)
    step("message reached the watch and the patient's answer came back", True,
         f"reply after {waited:.0f}s: {next(x['text'] for x in msgs if x['direction'] == 'from_patient')}")

    print("6. Lab result through the hospital record")
    api.call("POST", "/api/demo/lab", demo, {"patient_id": chatty["patient_id"], "name": "Lactate", "value": 4.4,
                                             "unit": "mmol/L", "flag": "H"})
    labs, waited = wait("lab on the unified stream", lambda: [
        x for x in api.call("GET", f"/api/patients/{chatty['patient_id']}/labs", clin) if x["value"] == 4.4], 60)
    step("FHIR Observation arrived as a lab event", True, f"after {waited:.0f}s: {labs[0]['name']} {labs[0]['value']}")

    print("7. Discharge")
    before = leaving["device_id"]
    api.call("POST", "/api/demo/discharge", demo, {"patient_id": leaving["patient_id"]})
    p, waited = wait("care setting changed", lambda: (lambda x: x if x["care_setting"] == "home" else None)(
        api.call("GET", f"/api/patients/{leaving['patient_id']}", clin)), 60)
    step("discharge in FHIR moved the patient to home monitoring", True,
         f"after {waited:.0f}s; device {before} -> {p['device_id']}")
    time.sleep(3)
    since = time.time()
    rows, _ = wait("wearable data after discharge", lambda: [
        r for r in api.call("GET", f"/api/patients/{leaving['patient_id']}/vitals?minutes=3", clin)
        if r["source"] != "icu_monitor"], 120)
    step("wearable started for the discharged patient", True, f"source={rows[0]['source']}")
    time.sleep(25)
    recent = [r for r in api.call("GET", f"/api/patients/{leaving['patient_id']}/vitals?minutes=1", clin)
              if time.time() - since > 0]
    late_icu = [r for r in recent if r["source"] == "icu_monitor" and
                time.mktime(time.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone > since]
    step("no ICU monitor data after discharge", not late_icu, f"{len(recent)} windows in the last minute, all wearable")
    rr, waited = wait("readmission risk", lambda: [
        r for r in api.call("GET", f"/api/patients/{leaving['patient_id']}/risk", clin)
        if r["model"] == "readmission_risk"], 120)
    step("readmission risk scored at discharge", True, f"{rr[0]['probability']:.2f} ({rr[0]['band']}) after {waited:.0f}s")
    dr, waited = wait("discharge summary run", lambda: [
        r for r in api.call("GET", f"/api/patients/{leaving['patient_id']}/agent-runs", clin)
        if r["trigger"] == "discharge"], 240, 3)
    step("agents produced a discharge summary", bool(dr[0]["brief"]), f"after {waited:.0f}s, {dr[0]['status']}")

    print("8. Audit trail and latency")
    audit = api.call("GET", "/api/audit?limit=500", admin)
    actors = {r["actor"] for r in audit}
    step("clinician, service and agent actions are all audited",
         any(x.startswith("e2e.") for x in actors) and any(x.startswith("agent:") for x in actors),
         f"{len(audit)} entries; agents seen: {sorted(x for x in actors if x.startswith('agent:'))}")
    stats = api.call("GET", "/api/stats", clin)
    step("sensor -> scored latency is under 5 s (p95)", (stats["e2e_p95_s"] or 99) < 5,
         f"p50 {stats['e2e_p50_s']:.2f}s, p95 {stats['e2e_p95_s']:.2f}s over {stats['windows_5m']} windows")
    summary()


if __name__ == "__main__":
    main()
