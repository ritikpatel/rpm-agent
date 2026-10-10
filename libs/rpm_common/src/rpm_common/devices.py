"""Cloud -> device commands (rpm.command.v1 on rpm/v1/dev/{thing}/cmd).

Locally commands go straight to Mosquitto; on AWS they go through the IoT Core data plane (IAM-authorised,
`iot:Publish` on `rpm/v1/dev/*/cmd` only). Every command carries `issued_by` and an expiry for audit and safety.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta

from rpm_contracts import Command, utcnow

log = logging.getLogger(__name__)


class DeviceCommander:
    def __init__(self, *, mode: str, region: str = "ap-southeast-2", iot_endpoint: str | None = None,
                 mqtt_host: str = "localhost", mqtt_port: int = 1883):
        self.mode = mode  # "mqtt" (local broker) | "iot" (AWS IoT data plane) | "disabled"
        self._lock = threading.Lock()
        self._client = None
        if mode == "iot":
            import boto3

            kwargs = {"region_name": region}
            if iot_endpoint:
                kwargs["endpoint_url"] = f"https://{iot_endpoint}"
            self._iot = boto3.client("iot-data", **kwargs)
        elif mode == "mqtt":
            self._mqtt_host, self._mqtt_port = mqtt_host, mqtt_port

    def _mqtt(self):
        with self._lock:
            if self._client is None:
                import paho.mqtt.client as mqtt

                c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"cmd-{uuid.uuid4().hex[:10]}")
                c.reconnect_delay_set(1, 10)
                c.connect(self._mqtt_host, self._mqtt_port, keepalive=30)
                c.loop_start()
                self._client = c
            return self._client

    def publish_raw(self, topic: str, payload: str, retain: bool = False) -> None:
        """Non-command topics: simulator presenter controls, retained gateway config (needs iot:RetainPublish)."""
        if self.mode == "iot":
            self._iot.publish(topic=topic, qos=1, retain=retain, payload=payload.encode())
        elif self.mode == "mqtt":
            self._mqtt().publish(topic, payload, qos=1, retain=retain).wait_for_publish(timeout=5)

    def send(self, device_id: str, cmd: str, args: dict | None, issued_by: str, ttl_s: int = 600,
             via: str | None = None) -> Command:
        """via: the gateway thing that relays this device (ICU bedside monitors have no MQTT session of their own);
        the command goes to the gateway's topic with args.device_id naming the bed."""
        args = {**(args or {}), "device_id": device_id} if via else (args or {})
        command = Command(cmd=cmd, args=args, issued_by=issued_by, expires_at=utcnow() + timedelta(seconds=ttl_s))
        topic = f"rpm/v1/dev/{via or device_id}/cmd"
        payload = command.to_json()
        if self.mode == "iot":
            self._iot.publish(topic=topic, qos=1, payload=payload.encode())
        elif self.mode == "mqtt":
            self._mqtt().publish(topic, payload, qos=1).wait_for_publish(timeout=5)
        else:
            log.info("device commands disabled; not sent", extra={"device_id": device_id, "cmd": cmd})
        log.info("device command sent", extra={"device_id": device_id, "cmd": cmd, "issued_by": issued_by,
                                               "cmd_id": str(command.cmd_id)})
        return command
