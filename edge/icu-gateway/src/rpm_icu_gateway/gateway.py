"""ICU bedside gateway: HL7 v2 (MLLP) from bedside monitors -> per-bed windows + bedside NEWS2/alarms -> durable
outbox -> cloud uplink as the gateway's IoT thing.

Privacy: monitors may send PID segments; the gateway never forwards them. Bedside devices are identified by their
monitor id (MSH-4), which the cloud maps to a patient server-side, exactly like a watch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime

from prometheus_client import Counter, Gauge, Histogram
from pydantic import ValidationError

from rpm_contracts import Command, GatewayConfig, utcnow
from rpm_contracts.models import DEVICE_ID_PATTERN

from . import hl7
from .bedside import Bed, close_window
from .codes import metric_of, value_of
from .outbox import Outbox
from .uplink import Uplink

log = logging.getLogger(__name__)

HL7_MESSAGES = Counter("rpm_gw_hl7_messages_total", "HL7 messages received", ["result"])
OBSERVATIONS = Counter("rpm_gw_observations_total", "OBX observations", ["result"])
PHI_STRIPPED = Counter("rpm_gw_pid_segments_dropped_total", "PID segments received and not forwarded")
PUBLISHED = Counter("rpm_gw_published_total", "Messages delivered to the cloud", ["kind", "buffered"])
BACKLOG = Gauge("rpm_gw_outbox_depth", "Messages waiting for the cloud link")
LINK_UP = Gauge("rpm_gw_uplink_connected", "1 when the cloud uplink is connected")
BEDSIDE_ALARMS = Counter("rpm_gw_bedside_alarms_total", "Bedside alarms raised locally", ["rule", "severity"])
MONITOR_TO_UPLINK = Histogram("rpm_gw_window_to_publish_seconds", "Window end -> accepted by the uplink",
                              buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 60, 300, 900))

PROFILE_WINDOW_S = {"normal": 15, "elevated": 10, "critical": 5}  # ICU cadence: tighter than home monitoring
BUFFERED_AFTER_S = 60


class Gateway:
    def __init__(self, thing: str, outbox: Outbox, *, window_s: int = 15, beds: set[str] | None = None,
                 hypercapnic: set[str] | None = None, clock: Callable[[], datetime] = utcnow):
        self.thing, self.outbox, self.window_s, self.clock = thing, outbox, window_s, clock
        self.allowed = beds  # None = any well-formed monitor id (the cloud registry is the real allow-list)
        self.hypercapnic = hypercapnic or set()
        self.beds: dict[str, Bed] = {}
        self.uplink: Uplink | None = None
        self._lock = threading.Lock()
        # State that must survive a restart: the cloud's per-bed config and each bed's best GCS (the NEWS2
        # consciousness baseline). Without the latter a restart would relearn a depressed GCS as "normal".
        saved = outbox.get("config")
        self.config: GatewayConfig | None = GatewayConfig.model_validate_json(saved) if saved else None
        self._gcs_best: dict[str, float] = json.loads(outbox.get("gcs_best") or "{}")

    # ------------------------------------------------------------------ HL7 in
    def on_hl7(self, text: str) -> str:
        """Handle one HL7 message and return the ACK to send back (the monitor retransmits until it gets one)."""
        try:
            msg = hl7.parse(text)
        except (hl7.Hl7Error, ValueError, IndexError) as exc:
            HL7_MESSAGES.labels("malformed").inc()
            return hl7.ack(None, "AE", f"malformed: {exc}", facility=self.thing)
        if not msg.message_type.startswith("ORU^R01"):
            HL7_MESSAGES.labels("unsupported").inc()
            return hl7.ack(msg, "AR", f"unsupported message type {msg.message_type}", facility=self.thing)
        device = msg.device
        if not re.fullmatch(DEVICE_ID_PATTERN, device or "") or (self.allowed and device not in self.allowed):
            HL7_MESSAGES.labels("unknown_device").inc()
            return hl7.ack(msg, "AR", "unknown bedside device", facility=self.thing)
        if msg.had_pid:
            PHI_STRIPPED.inc()
        now = self.clock()
        with self._lock:
            bed = self.beds.get(device)
            if bed is None:
                bed = self.beds[device] = Bed(device, self.window_s, gcs_best=self._gcs_best.get(device))
                self._configure(bed)
            for o in msg.observations:
                metric = metric_of(o)
                if metric is None:
                    OBSERVATIONS.labels("unmapped").inc()
                    continue
                value = value_of(o, metric)
                if value is None:
                    OBSERVATIONS.labels("unusable").inc()
                    continue
                # window by gateway receipt time: monitor clocks drift, and the gateway clock is NTP-disciplined
                bed.add(metric, value, now)
                OBSERVATIONS.labels("ok").inc()
        HL7_MESSAGES.labels("ok").inc()
        return hl7.ack(msg, "AA", facility=self.thing)

    # ------------------------------------------------------------------ windows -> outbox
    def tick(self) -> int:
        now = self.clock()
        n = 0
        with self._lock:
            due = [b for b in self.beds.values() if b.due(now)]
            for bed in due:
                telemetry, alarm = close_window(bed, now, self.outbox.next_seq)
                if alarm is not None:
                    d = alarm.data or {}
                    BEDSIDE_ALARMS.labels(d["rule"], d["severity"]).inc()
                    log.warning("BEDSIDE ALARM", extra={"bed": bed.device_id, "rule": d["rule"],
                                                        "severity": d["severity"], "news2": d.get("news2")})
                    self.outbox.add("events", alarm.to_json(), priority=1)
                self.outbox.add("telemetry", telemetry.to_json(), created=telemetry.ts.window_end.timestamp())
                n += 1
            best = {b.device_id: b.gcs_best for b in self.beds.values() if b.gcs_best is not None}
            if best != self._gcs_best:
                self._gcs_best = best
                self.outbox.put("gcs_best", json.dumps(best))
        BACKLOG.set(self.outbox.size())
        return n

    # ------------------------------------------------------------------ outbox -> cloud
    def drain(self, batch: int = 200) -> int:
        up = self.uplink
        LINK_UP.set(1 if up and up.connected else 0)
        if up is None or not up.connected:
            return 0
        sent = 0
        while True:
            rows = self.outbox.peek(batch)
            if not rows:
                break
            done = []
            for row_id, kind, created, payload in rows:
                doc = json.loads(payload)
                now = time.time()
                buffered = kind == "telemetry" and now - created > BUFFERED_AFTER_S
                doc.setdefault("ts", {})["edge_sent"] = utcnow().isoformat()
                if buffered:
                    doc.setdefault("edge", {})["buffered"] = True
                if not up.publish(f"rpm/v1/dev/{self.thing}/{kind}", json.dumps(doc, separators=(",", ":"))):
                    break
                done.append(row_id)
                PUBLISHED.labels(kind, str(buffered).lower()).inc()
                if kind == "telemetry":
                    MONITOR_TO_UPLINK.observe(max(0.0, now - created))
            self.outbox.remove(done)
            sent += len(done)
            if len(done) < len(rows):
                break
        BACKLOG.set(self.outbox.size())
        return sent

    # ------------------------------------------------------------------ cloud -> gateway
    def on_message(self, kind: str, payload: bytes) -> None:
        (self.on_config if kind == "config" else self.on_command)(payload)

    def _configure(self, bed: Bed) -> None:
        c = self.config.beds.get(bed.device_id) if self.config else None
        bed.hypercapnic = (c.spo2_scale == 2) if c and c.spo2_scale else bed.device_id in self.hypercapnic
        bed.on_o2 = c.on_supplemental_o2 if c else None
        if c and c.gcs_baseline is not None:  # the cloud saw this episode before we did
            bed.gcs_best = max(bed.gcs_best or c.gcs_baseline, c.gcs_baseline)

    def on_config(self, payload: bytes) -> None:
        """Retained per-bed clinical config (rpm.gateway_config.v1): applied to current and future beds, persisted
        so it holds through restarts before the broker re-delivers it."""
        try:
            cfg = GatewayConfig.model_validate_json(payload)
        except ValidationError:
            log.warning("invalid gateway config ignored")
            return
        if cfg.gateway_id != self.thing or (self.config and cfg.issued_at < self.config.issued_at):
            return  # not ours, or older than what we already hold
        with self._lock:
            self.config = cfg
            for bed in self.beds.values():
                self._configure(bed)
        self.outbox.put("config", cfg.to_json())
        log.info("gateway config applied", extra={"beds": len(cfg.beds),
                                                  "spo2_scale_2": sum(1 for b in cfg.beds.values()
                                                                      if b.spo2_scale == 2)})

    def on_command(self, payload: bytes) -> None:
        """Commands addressed to the gateway thing; args.device_id selects the bed."""
        try:
            cmd = Command.model_validate_json(payload)
        except ValidationError:
            log.warning("invalid command ignored")
            return
        if cmd.expired():
            return
        args = cmd.args or {}
        bed = self.beds.get(str(args.get("device_id", "")))
        if cmd.cmd == "SET_PROFILE" and bed and args.get("profile") in PROFILE_WINDOW_S:
            bed.window_s = PROFILE_WINDOW_S[args["profile"]]
            log.info("bed profile changed", extra={"bed": bed.device_id, "window_s": bed.window_s})
        else:
            log.info("command not applicable at the ICU gateway", extra={"cmd": cmd.cmd})

    # ------------------------------------------------------------------ nurse-station view
    def status(self) -> dict:
        with self._lock:
            beds = [{"bed": b.device_id, "news2": b.news2.total if b.news2 else None,
                     "alarm": {"rule": b.alarm.rule, "severity": b.alarm.severity,
                               "since": b.alarm.since.isoformat()} if b.alarm else None,
                     "latest": {k: round(v, 1) for k, v in b.latest.items()},
                     "last_sample": b.last_sample.isoformat() if b.last_sample else None}
                    for b in sorted(self.beds.values(), key=lambda b: -(b.news2.total if b.news2 else -1))]
        return {"gateway": self.thing, "uplink_connected": bool(self.uplink and self.uplink.connected),
                "outbox_depth": self.outbox.size(), "beds": beds}


# ---------------------------------------------------------------------- servers
async def serve_mllp(gw: Gateway, host: str, port: int) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        decoder = hl7.MllpDecoder()
        try:
            while data := await reader.read(65536):
                for text in decoder.feed(data):
                    writer.write(hl7.frame(gw.on_hl7(text)))
                await writer.drain()
        except (hl7.Hl7Error, ConnectionError) as exc:
            log.warning("MLLP connection dropped", extra={"peer": str(peer), "error": str(exc)[:200]})
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    log.info("MLLP listener started", extra={"host": host, "port": port})
    return server


async def serve_status(gw: Gateway, host: str, port: int) -> asyncio.Server:
    """GET /status (JSON nurse-station view) and GET /healthz. Deliberately tiny: no framework on the edge."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace").split()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            path = line[1] if len(line) > 1 else "/"
            if path.startswith("/status"):
                code, body = "200 OK", json.dumps(gw.status())
            elif path.startswith("/healthz"):
                code, body = "200 OK", '{"ok":true}'
            else:
                code, body = "404 Not Found", '{"error":"not found"}'
            data = body.encode()
            writer.write(f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                         f"Connection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
