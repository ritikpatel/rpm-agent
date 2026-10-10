"""The agents' only way to reach patient data and to act: care-api's /internal/agent endpoints.

Every call names the agent making it, so the audit log shows which agent read or did what."""

from __future__ import annotations

import httpx

from rpm_core.rag import Hit


class CareApi:
    def __init__(self, base_url: str, token: str, timeout_s: float = 20):
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/internal/agent", timeout=timeout_s,
                                 headers={"X-Service-Token": token, "X-Service-Name": "agent-service"})

    def _call(self, method: str, path: str, agent: str, **kwargs):
        r = self.http.request(method, path, headers={"X-Agent-Name": agent}, **kwargs)
        r.raise_for_status()
        return r.json()

    # ---- read
    def section(self, patient_id: str, name: str, agent: str, fresh: bool = False) -> dict:
        return self._call("GET", f"/patients/{patient_id}/{name}", agent, params={"fresh": str(fresh).lower()})

    def search(self, agent: str, query: str, patient_id: str | None = None, kind: str | None = None, k: int = 6,
               with_actions: bool = False) -> list[Hit]:
        rows = self._call("POST", "/guidelines/search", agent, json={
            "query": query[:600], "patient_id": patient_id, "kind": kind, "k": k, "with_actions": with_actions})
        return [Hit(score=0.0, **row) for row in rows]

    # ---- act
    def timeline(self, alert_id: str, agent: str, kind: str, summary: str, details: dict | None = None) -> None:
        self._call("POST", f"/alerts/{alert_id}/timeline", agent,
                   json={"kind": kind, "summary": summary[:1000], "details": details})

    def raise_severity(self, alert_id: str, agent: str, severity: str, reason: str) -> bool:
        return bool(self._call("POST", f"/alerts/{alert_id}/raise", agent,
                               json={"severity": severity, "reason": reason[:500]}).get("raised"))

    def escalate(self, alert_id: str, agent: str, reason: str) -> None:
        self._call("POST", f"/alerts/{alert_id}/escalate", agent, json={"reason": reason[:500]})

    def brief(self, alert_id: str, agent: str, brief: dict | None, status: str, assessment: dict | None) -> None:
        self._call("POST", f"/alerts/{alert_id}/brief", agent,
                   json={"brief": brief, "status": status, "assessment": assessment})

    def open_alert(self, agent: str, event: dict) -> str | None:
        return self._call("POST", "/alerts", agent, json={"event": event}).get("alert_id")

    def device(self, patient_id: str, agent: str, cmd: str, args: dict) -> dict:
        return self._call("POST", f"/patients/{patient_id}/device", agent, json={"cmd": cmd, "args": args})

    def save_run(self, run: dict) -> None:
        self._call("POST", "/runs", "orchestrator", json=run)
