"""UUIDv7 (RFC 9562) generator: time-ordered ids used as idempotency keys (Python < 3.14 lacks uuid.uuid7)."""

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = ((ts_ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
