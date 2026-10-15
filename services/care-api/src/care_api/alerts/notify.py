"""Notification channels. Messages follow HIPAA "minimum necessary": e-mail carries initials, the last four
characters of the patient id, severity and a console link, never vitals or diagnoses. Details stay behind the
authenticated console. Every channel failure is recorded; one failing channel never blocks the others."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

import httpx

from rpm_core.contracts import utcnow
from rpm_core.devices import DeviceCommander

from .policy import CareTeam, EscalationPolicy

log = logging.getLogger(__name__)

PATIENT_REASSURANCE = ("Your care team has been alerted about your latest readings and will contact you shortly. "
                       "If you feel very unwell, call your local emergency number.")


def initials(name: str) -> str:
    return ".".join(p[0].upper() for p in name.split() if p) + "."


def minimal_ref(alert: dict) -> str:
    return f"{initials(alert['display_name'])} (…{alert['patient_id'][-4:]})"


class Channel(Protocol):
    name: str

    def send(self, alert: dict, tier_name: str, contact: dict, link: str) -> tuple[str, str, str | None]:
        """Returns (recipient, provider_ref, error). Raise on failure."""


class ConsoleChannel:
    name = "console"

    def send(self, alert, tier_name, contact, link):
        return (tier_name, "live-feed", None)  # the console shows active alerts in real time over WebSocket


def recipients(contact: dict) -> list[str]:
    """A tier's e-mail addresses: `email` may be one address or a list (a tier is usually several people)."""
    value = contact.get("email") or []
    return [value] if isinstance(value, str) else [str(v) for v in value if v]


class EmailChannel:
    name = "email"

    def __init__(self, backend: str, sender: str, smtp_host: str = "localhost", smtp_port: int = 1025,
                 region: str = "ap-southeast-2", ses=None):
        self.backend, self.sender = backend, sender
        self.smtp_host, self.smtp_port = smtp_host, smtp_port
        if backend == "ses":
            if ses is None:
                import boto3
                from botocore.config import Config

                # the SES sandbox allows one message a second: let the SDK pace and retry instead of failing a page
                ses = boto3.client("sesv2", region_name=region, config=Config(
                    retries={"mode": "adaptive", "max_attempts": 6}, connect_timeout=5, read_timeout=10))
            self.ses = ses

    def _one(self, to: str, subject: str, body: str) -> str:
        if self.backend == "ses":
            resp = self.ses.send_email(FromEmailAddress=self.sender, Destination={"ToAddresses": [to]},
                                       Content={"Simple": {"Subject": {"Data": subject},
                                                           "Body": {"Text": {"Data": body}}}})
            return resp.get("MessageId", "")
        if self.backend == "smtp":
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
            msg.set_content(body)
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=5) as s:
                s.send_message(msg)
            return "smtp"
        log.info("email (log backend)", extra={"to": to, "subject": subject})
        return "log"

    def send(self, alert, tier_name, contact, link):
        to = recipients(contact)
        if not to:
            raise ValueError(f"no email configured for {tier_name}")
        sev = alert["severity"].upper()
        subject = f"[RPM {sev}] Patient {minimal_ref(alert)} needs review"
        body = (f"Severity: {sev}\nEscalation tier: {tier_name}\nPatient: {minimal_ref(alert)}\n"
                f"Opened: {alert['opened_at']:%Y-%m-%d %H:%M UTC}\n\nReview and acknowledge in the secure console:\n"
                f"{link}\n\nThis message intentionally contains no clinical details. Do not reply.")
        sent, refs, failed = [], [], []
        for address in to:   # one message each: nobody sees who else is on call, and one bad address blocks no one
            try:
                refs.append(self._one(address, subject, body))
                sent.append(address)
            except Exception as exc:  # noqa: BLE001
                why = "address not verified in Amazon SES" if "not verified" in str(exc) else str(exc)[:120]
                failed.append(why)
                log.warning("alert e-mail not delivered", extra={"to": address, "error": str(exc)[:200]})
        # what the console shows: the role on call and how many were reached, never names or addresses
        who = contact.get("name") or tier_name
        if not sent:
            raise RuntimeError(f"{who}: none of {len(to)} delivered ({'; '.join(sorted(set(failed)))})")
        reached = f"{who} ({len(sent)} {'person' if len(sent) == 1 else 'people'})"
        if failed:
            reached += f"; {len(failed)} not delivered: {'; '.join(sorted(set(failed)))}"
        return (reached, refs[0], None)


class WebhookChannel:
    name = "webhook"

    def send(self, alert, tier_name, contact, link):
        url = contact.get("webhook")
        if not url:
            return (tier_name, "", "skipped: no webhook configured")
        payload = {"type": "rpm.alert", "alert_id": str(alert["alert_id"]), "severity": alert["severity"],
                   "patient_ref": minimal_ref(alert), "tier": tier_name, "link": link}
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        return (f"{contact.get('name') or tier_name} (pager)", str(r.status_code), None)


class WatchChannel:
    """Patient-facing reassurance on the watch (fixed, clinician-approved wording; never model-generated)."""

    name = "watch"

    def __init__(self, commander: DeviceCommander):
        self.commander = commander

    def send(self, alert, tier_name, contact, link):
        device = alert.get("device_id")
        if alert.get("care_setting") != "home" or not device:
            return ("patient", "", "skipped: not a home-monitored patient")
        cmd = self.commander.send(device, "NOTIFY", {"title": "Care team alerted", "text": PATIENT_REASSURANCE,
                                                     "priority": "high"}, issued_by="care-api", ttl_s=900)
        return (device, str(cmd.cmd_id), None)


class Notifier:
    def __init__(self, policy: EscalationPolicy, team: CareTeam, channels: dict[str, Channel], console_url: str):
        self.policy, self.team, self.channels, self.console_url = policy, team, channels, console_url.rstrip("/")

    def _deliver(self, channel_name: str, alert: dict, tier_name: str, contact: dict, link: str) -> dict:
        ch = self.channels.get(channel_name)
        rec = {"channel": channel_name, "recipient": tier_name, "status": "skipped",
               "cause": alert.get("notify_cause") or "event"}
        if ch is None:
            rec["error"] = "channel not configured"
            return rec
        try:
            recipient, ref, note = ch.send(alert, tier_name, contact, link)
            rec.update(recipient=recipient, provider_ref=ref, status="skipped" if note else "sent", error=note)
        except Exception as exc:  # noqa: BLE001 - record and continue with other channels
            log.warning("notification failed", extra={"channel": channel_name, "error": str(exc)[:200]})
            rec.update(status="failed", error=str(exc)[:500])
        # Measured from what caused this page (sensor time, timer due time or request time), not the alert's first
        # event: a late SOS on a two-hour-old alert should read 1 s, not two hours.
        cause_at: datetime = alert.get("notify_cause_at") or alert["first_event_at"]
        rec["latency_ms"] = max(0, int((utcnow() - cause_at).total_seconds() * 1000))
        return rec

    def send(self, alert: dict, level: int) -> list[dict]:
        tier = self.policy.tier(level)
        contact = self.team.contact(tier.role, alert.get("care_team"))
        link = f"{self.console_url}/alerts/{alert['alert_id']}"
        records = [self._deliver(c, alert, contact.get("name", tier.name), contact, link) for c in tier.channels]
        first_page = alert["reminders_sent"] == 0 and level == self.policy.initial(alert["severity"]) \
            and alert["status"] == "open"
        if first_page:
            records += [self._deliver(c, alert, "patient", {}, link) for c in self.policy.patient_channels_on_open]
        return records
