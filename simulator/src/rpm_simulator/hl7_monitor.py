"""Bedside-monitor side of the ICU path: the simulator's ICU replay leaves as HL7 v2 ORU^R01 over MLLP to the ICU
gateway, the way a real monitor (or its central station) feeds an integration engine. Monitors do not store and
forward, so readings sent while the gateway is unreachable are lost, as they would be."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime

from rpm_icu_gateway import hl7
from rpm_icu_gateway.codes import MONITOR_CODES

log = logging.getLogger(__name__)


def observations(metrics: dict) -> list[tuple[str, str, str, float, str]]:
    out = []
    for metric, (code, text, system, unit) in MONITOR_CODES.items():
        m = metrics.get(metric)
        if not m:
            continue
        value = m["mean"] if metric == "heart_rate" else m["value"]
        out.append((code, text, system, round(float(value), 1), unit))
    return out


class Hl7MonitorLink:
    """One MLLP connection shared by all simulated monitors (like a central station), with reconnect and
    ACK checking. `send` is called from the simulator thread."""

    def __init__(self, host: str, port: int, timeout_s: float = 5.0):
        self.addr, self.timeout = (host, port), timeout_s
        self._sock: socket.socket | None = None
        self._decoder = hl7.MllpDecoder()
        self._lock = threading.Lock()
        self._n = 0
        self._last_warn = 0.0
        self.sent = self.failed = 0

    def _connect(self) -> socket.socket:
        s = socket.create_connection(self.addr, timeout=self.timeout)
        s.settimeout(self.timeout)
        self._decoder = hl7.MllpDecoder()
        log.info("MLLP connected to ICU gateway", extra={"gateway": f"{self.addr[0]}:{self.addr[1]}"})
        return s

    def send(self, device_id: str, telemetry_json: str) -> bool:
        t = json.loads(telemetry_json)
        obs = observations(t["metrics"])
        if not obs:
            return False
        observed = datetime.fromisoformat(t["ts"]["window_end"])
        with self._lock:
            self._n += 1
            msg = hl7.oru(device_id, f"SIM{self._n:09d}", observed, obs)
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._sock = self._connect()
                    self._sock.sendall(hl7.frame(msg))
                    acks: list[str] = []
                    while not acks:
                        data = self._sock.recv(4096)
                        if not data:
                            raise ConnectionError("gateway closed the connection")
                        acks = self._decoder.feed(data)
                    code = hl7.ack_code(acks[0])
                    if code != "AA":
                        log.warning("gateway rejected ORU", extra={"device_id": device_id, "ack": acks[0][-60:]})
                    self.sent += 1
                    return code == "AA"
                except (OSError, ConnectionError) as exc:
                    if self._sock is not None:
                        self._sock.close()
                    self._sock = None
                    if attempt == 2:
                        self.failed += 1
                        if time.monotonic() - self._last_warn > 30:
                            self._last_warn = time.monotonic()
                            log.warning("ICU gateway unreachable; monitor readings are being lost",
                                        extra={"error": str(exc)[:120], "failed": self.failed})
            return False

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None
