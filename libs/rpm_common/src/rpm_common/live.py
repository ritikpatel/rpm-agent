"""Live fan-out to consoles over Redis pub/sub (the api's WebSocket relays the channel)."""

import json

import redis

LIVE_CHANNEL = "rpm:live"


def publish(r: redis.Redis, message: dict) -> None:
    r.publish(LIVE_CHANNEL, json.dumps(message, default=str))
