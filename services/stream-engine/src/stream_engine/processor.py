"""Per-batch processing: validate -> dedupe -> resolve patient -> merge latest vitals -> NEWS2 -> rules ->
suppress duplicates -> persist (single transaction) -> emit events -> live fan-out."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from rpm_contracts import (
    SEVERITY_RANK,
    ClinicalEvent,
    DeviceEvent,
    IngestMeta,
    Metrics,
    SpO2,
    Telemetry,
    TelemetryTs,
    split_ingest_record,
    utcnow,
)
from rpm_features import (
    Finding,
    News2Input,
    RuleState,
    Snapshot,
    compute_news2,
    consciousness_from_gcs,
    default_thresholds,
    evaluate,
)

from . import metrics as M
from .registry import DeviceRegistry, PatientRef
from .sinks import PostgresSink, SqsEmitter
from .state import RedisState

log = logging.getLogger(__name__)

# How long a reading stays usable for NEWS2 after it was taken (seconds).
STALE_S: dict[str, dict[str, int]] = {
    "home": {"heart_rate": 300, "spo2": 1800, "resp_rate": 21600, "sbp": 21600, "temp": 21600, "gcs": 21600},
    "icu": {"heart_rate": 300, "spo2": 600, "resp_rate": 900, "sbp": 1800, "temp": 3600, "gcs": 14400},
}
SCORED = ("heart_rate", "spo2", "resp_rate", "sbp", "dbp", "temp", "gcs", "hrv_rmssd", "skin_temp")

# Device events -> (severity, requires_alert, suppression seconds)
DEVICE_EVENT_POLICY: dict[str, tuple[str, bool, int]] = {
    "SOS": ("critical", True, 60),
    "FALL": ("critical", True, 60),
    "EDGE_ALERT": ("medium", True, 900),
    "CHECKIN_REPLY": ("info", False, 0),
    "DEVICE_STATUS": ("low", False, 3600),
}
SUPPRESS_S: dict[str, int] = {"DATA_GAP": 1800, "NEWS2_RISING": 1800, "DEVICE_SILENT": 3600}


@dataclass
class Batch:
    vitals: list[dict] = field(default_factory=list)
    status: dict[str, dict] = field(default_factory=dict)
    events: list[ClinicalEvent] = field(default_factory=list)
    live: list[dict] = field(default_factory=list)
    processed: list[str] = field(default_factory=list)


def _flatten(m: Metrics) -> dict[str, tuple[float | str, float | None]]:
    out: dict[str, tuple[float | str, float | None]] = {}
    for name in type(m).model_fields:
        v = getattr(m, name)
        if v is None:
            continue
        if name == "heart_rate":
            out[name] = (v.mean, v.quality)
        elif name == "activity":
            out[name] = (v.value, None)
        else:
            out[name] = (v.value, getattr(v, "quality", None))
    return out


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    return None if a is None or b is None else (b - a).total_seconds()


class Processor:
    def __init__(self, registry: DeviceRegistry, state: RedisState, pg: PostgresSink, sqs: SqsEmitter, *,
                 min_quality: float = 0.6, suppress_s: int = 900, gap_tolerance: int = 3,
                 clock: Callable[[], datetime] = utcnow):
        self.registry = registry
        self.state = state
        self.pg = pg
        self.sqs = sqs
        self.min_quality = min_quality
        self.suppress_s = suppress_s
        self.gap_tolerance = gap_tolerance
        self.clock = clock

    # ------------------------------------------------------------------ batch
    def handle_batch(self, records: list[dict]) -> None:
        t0 = time.perf_counter()
        seen = self.state.seen([str(r.get("msg_id")) for r in records])
        b = Batch()
        for rec in records:
            msg_id = str(rec.get("msg_id"))
            if msg_id in seen or msg_id in b.processed:
                M.RECORDS.labels(rec.get("kind", "?"), "duplicate").inc()
                continue
            try:
                meta, payload = split_ingest_record(rec)
                if payload.get("device_id") != meta.thing and not self._relayed_by(payload.get("device_id"),
                                                                                  meta.thing):
                    # the topic is authoritative (IoT policy pins it to the cert); a mismatch is a spoof attempt
                    # unless the registry names this thing as the device's gateway (ICU bedside monitors)
                    M.RECORDS.labels(meta.kind, "device_mismatch").inc()
                    log.warning("device_id does not match topic", extra={"thing": meta.thing,
                                                                          "payload_device": payload.get("device_id")})
                    continue
                if meta.kind == "telemetry":
                    self._telemetry(Telemetry.model_validate(payload), meta, b)
                else:
                    self._device_event(DeviceEvent.model_validate(payload), meta, b)
                b.processed.append(msg_id)
                M.RECORDS.labels(meta.kind, "ok").inc()
            except (ValidationError, KeyError, TypeError, ValueError) as exc:
                M.RECORDS.labels(rec.get("kind", "?"), "invalid").inc()
                log.warning("invalid record skipped", extra={"msg_id": msg_id, "error": str(exc)[:300]})
        self.pg.write(b.vitals, b.status, b.events)
        self.sqs.emit(b.events)
        self.state.mark_seen(b.processed)
        self.state.publish_live(b.live)
        M.BATCH_SECONDS.observe(time.perf_counter() - t0)

    def _relayed_by(self, device_id: str | None, thing: str) -> bool:
        ref = self.registry.resolve(device_id) if device_id else None
        return ref is not None and ref.gateway_id is not None and ref.gateway_id == thing

    # ------------------------------------------------------------------ telemetry
    def _telemetry(self, t: Telemetry, meta: IngestMeta, b: Batch, spot: bool = False) -> None:
        ref = self.registry.resolve(t.device_id)
        if ref is None:
            M.RECORDS.labels("telemetry", "unassigned_device").inc()
            return
        pid, ctx = ref.patient_id, ref.context
        wend, wstart = t.ts.window_end, t.ts.window_start
        buffered = bool(t.edge and t.edge.buffered)
        flat = _flatten(t.metrics)
        th = default_thresholds(ref.condition, ctx).merged(ref.thresholds)

        self._check_sequence(ref, t.device_id, t.seq, buffered, wend, b)
        late = False
        if not spot:  # on-demand spot checks interleave with windows and must not reorder them
            prev_end = self.state.swap_max(f"wend:{t.device_id}", int(wend.timestamp() * 1000))
            late = prev_end is not None and int(wend.timestamp() * 1000) < prev_end

        good = {k: v for k, (v, q) in flat.items()
                if k in SCORED and (q is None or q >= th.min_quality) and isinstance(v, int | float)}
        if late:
            merged = {k: {"v": v, "ts": wend.isoformat()} for k, v in good.items()}
        else:
            updates = {k: {"v": v, "ts": wend.isoformat()} for k, v in good.items()}
            best = self.state.get_latest(pid).get("gcs_best")
            if "gcs" in good and (best is None or good["gcs"] > best["v"]):
                updates["gcs_best"] = {"v": good["gcs"], "ts": wend.isoformat()}
            self.state.update_latest(pid, updates)
            merged = self.state.get_latest(pid)
        gcs_best = merged.get("gcs_best", {}).get("v")
        fresh = {k: rec["v"] for k, rec in merged.items()
                 if k in STALE_S[ctx] and (wend - datetime.fromisoformat(rec["ts"])).total_seconds() <= STALE_S[ctx][k]}
        fresh.update({k: v for k, v in good.items() if k not in STALE_S[ctx]})

        news2 = None
        if any(k in fresh for k in ("heart_rate", "spo2", "resp_rate", "sbp", "temp")):
            news2 = compute_news2(News2Input(
                resp_rate=fresh.get("resp_rate"), spo2=fresh.get("spo2"), on_oxygen=ref.on_o2, sbp=fresh.get("sbp"),
                heart_rate=fresh.get("heart_rate"),
                consciousness=consciousness_from_gcs(fresh["gcs"], gcs_best) if "gcs" in fresh else None,
                temp=fresh.get("temp"), hypercapnic_resp_failure=ref.hypercapnic,
            ))

        hr, hr_q = flat.get("heart_rate", (None, None))
        spo2, spo2_q = flat.get("spo2", (None, None))
        snap = Snapshot(window_start=wstart, ts=wend, heart_rate=hr, hr_quality=hr_q, spo2=spo2, spo2_quality=spo2_q,
                        news2=news2)
        prior = RuleState() if late else RuleState.from_json(self.state.get_rule_state(pid))
        findings, new_state = evaluate(snap, th, prior)
        if not late:
            self.state.set_rule_state(pid, new_state.to_json())

        processed_at = self.clock()
        trace = {"msg_id": str(t.msg_id), "window_end": wend.isoformat(), "edge_sent": t.ts.edge_sent.isoformat(),
                 "ingest_at": meta.ingest_at.isoformat(), "processed_at": processed_at.isoformat()}
        n2 = news2.as_dict() if news2 else None
        for f in findings:
            if late:
                f = Finding(f.type, f.severity, f.rule_id, f.summary + " (late data)", f.requires_alert,
                            {**f.evidence, "late": True})
            self._emit(b, ref, t.device_id, f, wend, trace, n2)

        b.vitals.append({
            "patient_id": pid, "device_id": t.device_id, "ts": wend, "msg_id": str(t.msg_id), "source": t.source,
            "context": ctx, "heart_rate": hr,
            "hr_min": t.metrics.heart_rate.min if t.metrics.heart_rate else None,
            "hr_max": t.metrics.heart_rate.max if t.metrics.heart_rate else None,
            **{k: (flat[k][0] if k in flat else None) for k in
               ("hrv_rmssd", "spo2", "resp_rate", "sbp", "dbp", "temp", "skin_temp", "gcs", "steps", "activity")},
            "quality": {k: q for k, (_, q) in flat.items() if q is not None},
            "news2": news2.total if news2 else None, "news2_band": news2.band if news2 else None,
            "news2_partial": news2.partial if news2 else None,
            "edge_flags": (t.edge.flags if t.edge else None) or None, "buffered": buffered, "late": late,
            "edge_sent": t.ts.edge_sent, "ingest_at": meta.ingest_at, "processed_at": processed_at,
        })
        if not late:
            latest = {k: merged[k] for k in merged}
            if "activity" in flat:
                latest["activity"] = {"v": flat["activity"][0], "ts": wend.isoformat()}
            if t.device and t.device.battery_pct is not None:
                latest["battery_pct"] = {"v": t.device.battery_pct, "ts": wend.isoformat()}
            b.status[pid] = {"patient_id": pid, "device_id": t.device_id, "last_seen": wend, "latest": latest,
                             "news2": news2.total if news2 else None, "news2_band": news2.band if news2 else None,
                             "news2_partial": news2.partial if news2 else None, "news2_detail": n2}
        b.live.append({"type": "vitals", "patient_id": pid, "device_id": t.device_id, "context": ctx,
                       "ts": wend.isoformat(), "vitals": {k: v for k, v in fresh.items()},
                       "news2": {"total": news2.total, "band": news2.band, "partial": news2.partial} if news2 else None,
                       "buffered": buffered, "late": late})
        self._observe_latency(t, meta, processed_at, ctx, buffered)

    def _check_sequence(self, ref: PatientRef, device_id: str, seq: int, buffered: bool, occurred: datetime,
                        b: Batch) -> None:
        prev = self.state.swap_max(f"seq:{device_id}", seq)
        if prev is None or buffered:
            return
        missing = seq - prev - 1
        if missing >= self.gap_tolerance:
            self._emit(b, ref, device_id, Finding(
                "DATA_GAP", "info", "pipeline.data_gap", f"{missing} messages missing from {device_id}", False,
                {"from_seq": prev, "to_seq": seq, "missing": missing}), occurred, None, None)

    # ------------------------------------------------------------------ device events
    def _device_event(self, ev: DeviceEvent, meta: IngestMeta, b: Batch) -> None:
        ref = self.registry.resolve(ev.device_id)
        if ref is None:
            M.RECORDS.labels("events", "unassigned_device").inc()
            return
        data = ev.data or {}
        if ev.type == "SPOT_CHECK_RESULT":
            if data.get("metric", "spo2") == "spo2" and data.get("value") is not None:
                t = Telemetry(msg_id=ev.msg_id, device_id=ev.device_id, seq=ev.seq, source=ev.source,
                              context=ref.context,
                              ts=TelemetryTs(window_start=ev.ts.occurred, window_end=ev.ts.occurred,
                                             edge_sent=ev.ts.edge_sent),
                              metrics=Metrics(spo2=SpO2(value=data["value"], quality=data.get("quality"),
                                                        spot_check=True)))
                self._telemetry(t, meta, b, spot=True)
            return
        self.state.swap_max(f"seq:{ev.device_id}", ev.seq)
        severity, requires_alert, _ = DEVICE_EVENT_POLICY[ev.type]
        if ev.type == "EDGE_ALERT" and data.get("severity") in SEVERITY_RANK:
            severity = data["severity"]
        summary = {
            "SOS": "Patient pressed SOS on the watch",
            "FALL": "Fall detected by the watch",
            "EDGE_ALERT": f"On-device alert: {data.get('rule', 'unspecified')}",
            "CHECKIN_REPLY": f"Check-in reply: {data.get('answer')!r} to {data.get('question_id')}",
            "DEVICE_STATUS": f"Device status: {data.get('reason', 'update')}",
        }[ev.type]
        processed_at = self.clock()
        trace = {"msg_id": str(ev.msg_id), "window_end": ev.ts.occurred.isoformat(),
                 "edge_sent": ev.ts.edge_sent.isoformat(), "ingest_at": meta.ingest_at.isoformat(),
                 "processed_at": processed_at.isoformat()}
        self._emit(b, ref, ev.device_id, Finding(ev.type, severity, f"device.{ev.type.lower()}", summary,
                                                  requires_alert, data), ev.ts.occurred, trace, None)
        e2e = _secs(ev.ts.occurred, processed_at)
        if e2e is not None and e2e >= 0:
            M.HOP_LATENCY.labels("end_to_end", ref.context).observe(e2e)

    # ------------------------------------------------------------------ emit
    def _emit(self, b: Batch, ref: PatientRef, device_id: str, f: Finding, occurred: datetime, trace: dict | None,
              news2: dict | None) -> None:
        ttl = DEVICE_EVENT_POLICY.get(f.type, (None, None, SUPPRESS_S.get(f.type, self.suppress_s)))[2]
        if not self.state.should_emit(ref.patient_id, f.type, SEVERITY_RANK[f.severity], ttl):
            M.SUPPRESSED.labels(f.type).inc()
            return
        ev = ClinicalEvent(patient_id=ref.patient_id, device_id=device_id, context=ref.context, type=f.type,
                           severity=f.severity, occurred_at=occurred, detected_at=self.clock(), rule_id=f.rule_id,
                           summary=f.summary, requires_alert=f.requires_alert, evidence=f.evidence, news2=news2,
                           trace=trace)
        b.events.append(ev)
        b.live.append({"type": "event", "event": ev.to_dict()})
        M.EVENTS.labels(f.type, f.severity).inc()
        log.info("clinical event", extra={"patient_id": ref.patient_id, "type": f.type, "severity": f.severity,
                                          "summary": f.summary})

    @staticmethod
    def _observe_latency(t: Telemetry, meta: IngestMeta, processed_at: datetime, ctx: str, buffered: bool) -> None:
        if buffered:  # intentionally delayed by store-and-forward; excluded from live-latency SLOs
            return
        hops = {
            "edge": _secs(t.ts.window_end, t.ts.edge_sent),
            "uplink": _secs(t.ts.edge_sent, meta.ingest_at),
            "pipeline": _secs(meta.ingest_at, processed_at),
            "end_to_end": _secs(t.ts.window_end, processed_at),
        }
        for hop, v in hops.items():
            if v is None:
                continue
            if v < 0:
                M.CLOCK_SKEW.labels(hop).inc()
                v = 0.0
            M.HOP_LATENCY.labels(hop, ctx).observe(v)


def silent_device_finding(context: str, device_id: str, minutes: float) -> Finding:
    severity, alert = ("medium", True) if context == "icu" else ("low", False)
    return Finding("DEVICE_SILENT", severity, "pipeline.device_silent",
                   f"No data from {device_id} for {minutes:.0f} min", alert, {"silent_minutes": round(minutes, 1)})


__all__ = ["Processor", "silent_device_finding", "STALE_S"]
