"""rpm-icu-gateway: run the ICU bedside gateway (plain container, systemd service or Greengrass component).

Configuration comes from RPM_GW_* environment variables (a Greengrass recipe sets them from component config).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import start_http_server
from pydantic_settings import BaseSettings, SettingsConfigDict

from .gateway import Gateway, serve_mllp, serve_status
from .outbox import Outbox
from .uplink import GreengrassUplink, MqttUplink, running_on_greengrass

log = logging.getLogger("rpm_icu_gateway")


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RPM_GW_", extra="ignore")

    thing: str = "icu-gw-01"                 # the gateway's IoT thing name (= Greengrass core device name)
    mllp_host: str = "0.0.0.0"
    mllp_port: int = 2575                    # IANA-registered HL7 MLLP port
    status_port: int = 8081
    metrics_port: int = 9110
    window_s: int = 15
    outbox_path: str = "gw-outbox.db"
    uplink: str = "auto"                     # auto | mqtt | greengrass
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    beds: str = ""                           # optional allow-list of monitor ids (comma-separated)
    hypercapnic_beds: str = ""               # fallback SpO2-scale-2 beds until the cloud's retained config arrives
    log_level: str = "INFO"


class _Json(logging.Formatter):
    """One JSON object per line. Kept local: the edge package does not pull in the cloud services' dependencies."""

    _std = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, r: logging.LogRecord) -> str:
        doc = {"ts": datetime.fromtimestamp(r.created, UTC).isoformat(timespec="milliseconds"), "level": r.levelname,
               "service": "icu-gateway", "logger": r.name, "msg": r.getMessage()}
        doc.update({k: v for k, v in r.__dict__.items() if k not in self._std and not k.startswith("_")})
        if r.exc_info:
            doc["exc"] = self.formatException(r.exc_info)
        return json.dumps(doc, default=str)


def _split(v: str) -> set[str]:
    return {x.strip() for x in v.split(",") if x.strip()}


async def run(cfg: GatewaySettings) -> None:
    Path(cfg.outbox_path).parent.mkdir(parents=True, exist_ok=True)
    outbox = Outbox(cfg.outbox_path)
    gw = Gateway(cfg.thing, outbox, window_s=cfg.window_s, beds=_split(cfg.beds) or None,
                 hypercapnic=_split(cfg.hypercapnic_beds))
    use_gg = cfg.uplink == "greengrass" or (cfg.uplink == "auto" and running_on_greengrass())
    gw.uplink = GreengrassUplink(cfg.thing, gw.on_message) if use_gg else MqttUplink(
        cfg.thing, cfg.mqtt_host, cfg.mqtt_port, gw.on_message, ca=cfg.tls_ca, cert=cfg.tls_cert, key=cfg.tls_key)
    mllp = await serve_mllp(gw, cfg.mllp_host, cfg.mllp_port)
    status = await serve_status(gw, cfg.mllp_host, cfg.status_port)
    log.info("icu gateway started", extra={"thing": cfg.thing, "uplink": "greengrass" if use_gg else "mqtt",
                                           "backlog": outbox.size()})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        gw.tick()
        await asyncio.to_thread(gw.drain)  # blocking MQTT acks stay off the MLLP event loop
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            pass
    mllp.close()
    status.close()
    gw.uplink.close()
    outbox.close()


def main() -> None:
    cfg = GatewaySettings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Json())
    logging.basicConfig(level=cfg.log_level.upper(), handlers=[handler], force=True)
    start_http_server(cfg.metrics_port)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
