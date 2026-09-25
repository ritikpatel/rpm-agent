"""MQTT fleet publisher. Locally talks plain MQTT to Mosquitto; on AWS uses mutual TLS to IoT Core with the
simulator fleet certificate. Devices are spread over N connections because IoT Core limits publishes per
connection (~100/s)."""

from __future__ import annotations

import logging
import ssl
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    client_prefix: str = "sim-fleet"
    shards: int = 1
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    keepalive: int = 60


MessageHandler = Callable[[str, bytes], None]


class FleetPublisher:
    def __init__(self, cfg: MqttConfig, on_message: MessageHandler | None = None):
        self.cfg = cfg
        self.on_message = on_message
        self._subs: list[str] = []
        self._connected = [threading.Event() for _ in range(cfg.shards)]
        self.clients = [self._make_client(i) for i in range(cfg.shards)]

    def _make_client(self, idx: int) -> mqtt.Client:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.cfg.client_prefix}-{idx}",
                        protocol=mqtt.MQTTv311, clean_session=True)
        if self.cfg.tls_cert:
            c.tls_set(ca_certs=self.cfg.tls_ca, certfile=self.cfg.tls_cert, keyfile=self.cfg.tls_key,
                      tls_version=ssl.PROTOCOL_TLS_CLIENT)
        elif self.cfg.tls_ca:
            c.tls_set(ca_certs=self.cfg.tls_ca, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        c.max_inflight_messages_set(100)
        c.max_queued_messages_set(0)  # unbounded: QoS1 messages queue while reconnecting
        c.reconnect_delay_set(1, 30)

        def on_connect(client, _ud, _flags, reason, _props):
            if reason.is_failure:
                log.error("mqtt connect failed", extra={"shard": idx, "reason": str(reason)})
                return
            log.info("mqtt connected", extra={"shard": idx, "host": self.cfg.host})
            if idx == 0:
                for topic in self._subs:
                    client.subscribe(topic, qos=1)
            self._connected[idx].set()

        def on_disconnect(_client, _ud, _flags, reason, _props):
            self._connected[idx].clear()
            log.warning("mqtt disconnected", extra={"shard": idx, "reason": str(reason)})

        def on_message(_client, _ud, msg):
            if self.on_message:
                try:
                    self.on_message(msg.topic, msg.payload)
                except Exception:
                    log.exception("message handler failed", extra={"topic": msg.topic})

        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
        return c

    def subscribe(self, topic_filter: str) -> None:
        """Subscriptions live on shard 0 and are re-established on every reconnect."""
        self._subs.append(topic_filter)
        if self._connected[0].is_set():
            self.clients[0].subscribe(topic_filter, qos=1)

    def connect(self, timeout: float = 30) -> None:
        for c in self.clients:
            c.connect_async(self.cfg.host, self.cfg.port, keepalive=self.cfg.keepalive)
            c.loop_start()
        for i, ev in enumerate(self._connected):
            if not ev.wait(timeout):
                raise TimeoutError(f"MQTT shard {i} could not connect to {self.cfg.host}:{self.cfg.port}")

    def publish(self, key: str, topic: str, payload: str | bytes, qos: int = 1) -> None:
        shard = zlib.crc32(key.encode()) % len(self.clients)
        info = self.clients[shard].publish(topic, payload, qos=qos)
        if info.rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN):
            log.warning("publish failed", extra={"topic": topic, "rc": info.rc})

    def close(self) -> None:
        for c in self.clients:
            c.disconnect()
            c.loop_stop()
