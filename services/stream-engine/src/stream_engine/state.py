"""Hot per-device / per-patient state in Redis (shared by all stream-engine replicas)."""

from __future__ import annotations

import json
from collections.abc import Iterable

import redis

LIVE_CHANNEL = "rpm:live"

# Atomically store `value` if it is greater than the current one; return the previous value.
_SWAP_MAX = """
local cur = redis.call('GET', KEYS[1])
if (not cur) or tonumber(ARGV[1]) > tonumber(cur) then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
return cur
"""

# Emit only if nothing of equal/higher severity was emitted within the TTL (escalations always pass).
_SHOULD_EMIT = """
local cur = redis.call('GET', KEYS[1])
if (not cur) or tonumber(ARGV[1]) > tonumber(cur) then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""


class RedisState:
    def __init__(self, r: redis.Redis, dedupe_ttl_s: int = 86_400, state_ttl_s: int = 7 * 86_400):
        self.r = r
        self.dedupe_ttl_s = dedupe_ttl_s
        self.state_ttl_s = state_ttl_s
        self._swap_max = r.register_script(_SWAP_MAX)
        self._should_emit = r.register_script(_SHOULD_EMIT)

    # ---- idempotency
    def seen(self, msg_ids: list[str]) -> set[str]:
        if not msg_ids:
            return set()
        vals = self.r.mget([f"rpm:seen:{m}" for m in msg_ids])
        return {m for m, v in zip(msg_ids, vals, strict=True) if v is not None}

    def mark_seen(self, msg_ids: Iterable[str]) -> None:
        pipe = self.r.pipeline(transaction=False)
        for m in msg_ids:
            pipe.set(f"rpm:seen:{m}", 1, ex=self.dedupe_ttl_s)
        pipe.execute()

    # ---- ordering
    def swap_max(self, key: str, value: int) -> int | None:
        prev = self._swap_max(keys=[f"rpm:{key}"], args=[value, self.state_ttl_s])
        return int(prev) if prev is not None else None

    # ---- latest vitals per patient: {metric: {"v": value, "ts": iso}}
    def update_latest(self, patient_id: str, values: dict[str, dict]) -> None:
        if values:
            key = f"rpm:latest:{patient_id}"
            self.r.hset(key, mapping={k: json.dumps(v) for k, v in values.items()})
            self.r.expire(key, self.state_ttl_s)

    def get_latest(self, patient_id: str) -> dict[str, dict]:
        return {k.decode(): json.loads(v) for k, v in self.r.hgetall(f"rpm:latest:{patient_id}").items()}

    # ---- rule state
    def get_rule_state(self, patient_id: str) -> bytes | None:
        return self.r.get(f"rpm:rules:{patient_id}")

    def set_rule_state(self, patient_id: str, raw: str) -> None:
        self.r.set(f"rpm:rules:{patient_id}", raw, ex=self.state_ttl_s)

    # ---- alert-fatigue suppression
    def should_emit(self, patient_id: str, event_type: str, severity_rank: int, ttl_s: int) -> bool:
        if ttl_s <= 0:
            return True
        return bool(self._should_emit(keys=[f"rpm:suppress:{patient_id}:{event_type}"], args=[severity_rank, ttl_s]))

    # ---- live fan-out (api WebSocket)
    def publish_live(self, messages: list[dict]) -> None:
        if messages:
            pipe = self.r.pipeline(transaction=False)
            for m in messages:
                pipe.publish(LIVE_CHANNEL, json.dumps(m, default=str))
            pipe.execute()
