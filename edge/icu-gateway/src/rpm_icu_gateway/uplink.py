"""Cloud uplinks. As a Greengrass component the gateway publishes through the nucleus over IPC (the core device's
own IoT identity and connection); as a plain container it holds its own MQTT session (mutual TLS to IoT Core, or
plain MQTT to the local Mosquitto). Both deliver the gateway thing's cmd and (retained) config messages to
`on_message(kind, payload)`."""

from __future__ import annotations

import logging
import os
import ssl
import threading
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)
MessageHandler = Callable[[str, bytes], None]  # (kind: "cmd" | "config", payload)
INBOUND = ("cmd", "config")


class Uplink(Protocol):
    @property
    def connected(self) -> bool: ...

    def publish(self, topic: str, payload: str) -> bool: ...

    def close(self) -> None: ...


class MqttUplink:
    def __init__(self, thing: str, host: str, port: int, on_message: MessageHandler, *, ca: str | None = None,
                 cert: str | None = None, key: str | None = None):
        import paho.mqtt.client as mqtt

        self._connected = threading.Event()
        # client id == thing name: the IoT policy only lets a certificate connect as its own thing
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=thing, protocol=mqtt.MQTTv311,
                        clean_session=True)
        if cert:
            c.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        c.reconnect_delay_set(1, 30)
        c.max_inflight_messages_set(50)

        def on_connect(client, _ud, _flags, reason, _props):
            if reason.is_failure:
                log.error("uplink connect failed", extra={"reason": str(reason)})
                return
            client.subscribe([(f"rpm/v1/dev/{thing}/{kind}", 1) for kind in INBOUND])
            self._connected.set()
            log.info("uplink connected", extra={"host": host, "port": port, "tls": bool(cert)})

        def on_disconnect(_client, _ud, _flags, reason, _props):
            self._connected.clear()
            log.warning("uplink disconnected", extra={"reason": str(reason)})

        c.on_connect, c.on_disconnect = on_connect, on_disconnect
        c.on_message = lambda _c, _ud, msg: on_message(msg.topic.rsplit("/", 1)[-1], msg.payload)
        c.connect_async(host, port, keepalive=30)
        c.loop_start()
        self.client = c

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def publish(self, topic: str, payload: str) -> bool:
        if not self.connected:
            return False
        info = self.client.publish(topic, payload, qos=1)
        try:
            info.wait_for_publish(timeout=5)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class GreengrassUplink:
    """Publishes via the Greengrass nucleus (IPC). The nucleus owns the mTLS session and spools QoS1 messages while
    offline, so `publish` succeeds as soon as the nucleus has accepted the message."""

    def __init__(self, thing: str, on_message: MessageHandler):
        from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2  # only present on a core device
        from awsiot.greengrasscoreipc.model import QOS

        self._qos = QOS.AT_LEAST_ONCE
        self.ipc = GreengrassCoreIPCClientV2()
        for kind in INBOUND:
            self.ipc.subscribe_to_iot_core(
                topic_name=f"rpm/v1/dev/{thing}/{kind}", qos=self._qos,
                on_stream_event=lambda ev, k=kind: on_message(k, bytes(ev.message.payload)))
        log.info("greengrass IPC uplink ready", extra={"thing": thing})

    @property
    def connected(self) -> bool:
        return True  # the nucleus handles connectivity and spooling

    def publish(self, topic: str, payload: str) -> bool:
        try:
            self.ipc.publish_to_iot_core(topic_name=topic, qos=self._qos, payload=payload.encode())
            return True
        except Exception as exc:  # noqa: BLE001 - spooler full or nucleus restarting: keep it in our outbox
            log.warning("IPC publish failed", extra={"error": str(exc)[:200]})
            return False

    def close(self) -> None:
        self.ipc.close()


def running_on_greengrass() -> bool:
    return bool(os.environ.get("AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT"))
