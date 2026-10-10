"""Escalation policy and care-team directory (YAML, version-controlled).

Per-patient contact overrides live in `patients.care_team`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

SEVERITY_RANK = {"medium": 1, "high": 2, "critical": 3}

DEFAULT_POLICY = """
# Tier 1 is paged first for medium alerts; high and critical alerts start higher. An unacknowledged alert moves up one
# tier after `ack_timeout_s`. The last tier repeats as reminders.
tiers:
  - {level: 1, name: RPM nurse,           role: nurse,     channels: [console, email],          ack_timeout_s: 300}
  - {level: 2, name: On-call physician,   role: physician, channels: [console, email, sms],     ack_timeout_s: 300}
  - {level: 3, name: Rapid response team, role: emergency, channels: [console, sms, email, webhook], ack_timeout_s: 600}
initial_tier: {medium: 1, high: 2, critical: 3}
patient_channels_on_open: [watch]      # home patients get a reassurance message on their watch
max_reminders: 3
"""

DEFAULT_CARE_TEAM = """
nurse:     {name: On-call RPM nurse,   email: nurse@rpm.local,     phone: "+10000000001"}
physician: {name: On-call physician,   email: physician@rpm.local, phone: "+10000000002"}
emergency: {name: Rapid response team, email: rrt@rpm.local,       phone: "+10000000003", webhook: ""}
"""


def clamp_severity(severity: str) -> str:
    """Alerts exist only for medium and above; anything lower that still requires an alert is treated as medium."""
    return severity if severity in SEVERITY_RANK else "medium"


@dataclass(frozen=True)
class Tier:
    level: int
    name: str
    role: str
    channels: tuple[str, ...]
    ack_timeout_s: int


@dataclass(frozen=True)
class EscalationPolicy:
    tiers: dict[int, Tier]
    initial_tier: dict[str, int]
    patient_channels_on_open: tuple[str, ...] = ()
    max_reminders: int = 3
    timeout_scale: float = 1.0  # demo mode: e.g. 0.2 turns 5-minute timeouts into 1 minute

    @classmethod
    def load(cls, path: str | None = None, timeout_scale: float = 1.0) -> EscalationPolicy:
        doc = yaml.safe_load(Path(path).read_text() if path else DEFAULT_POLICY)
        tiers = {t["level"]: Tier(t["level"], t["name"], t["role"], tuple(t["channels"]), int(t["ack_timeout_s"]))
                 for t in doc["tiers"]}
        return cls(tiers=tiers, initial_tier=doc["initial_tier"],
                   patient_channels_on_open=tuple(doc.get("patient_channels_on_open", [])),
                   max_reminders=int(doc.get("max_reminders", 3)), timeout_scale=timeout_scale)

    def initial(self, severity: str) -> int:
        return self.initial_tier[clamp_severity(severity)]

    def tier(self, level: int) -> Tier:
        return self.tiers[level]

    def next_level(self, level: int) -> int | None:
        higher = sorted(lv for lv in self.tiers if lv > level)
        return higher[0] if higher else None

    def timeout(self, level: int) -> timedelta:
        return timedelta(seconds=max(10, self.tiers[level].ack_timeout_s * self.timeout_scale))


@dataclass
class CareTeam:
    contacts: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> CareTeam:
        return cls(yaml.safe_load(Path(path).read_text() if path else DEFAULT_CARE_TEAM))

    def contact(self, role: str, patient_override: dict | None = None) -> dict:
        base = dict(self.contacts.get(role, {}))
        base.update((patient_override or {}).get(role, {}))
        return base
