"""Per-batch processing of the unified stream.

Device records: validate -> dedupe -> resolve patient -> merge latest vitals -> NEWS2 -> rules -> suppress
duplicates -> persist (one transaction) -> hand clinical events to care-api -> live fan-out -> ask for risk scores.
EHR records (admission, discharge, lab) update the patient's care setting and lab history in the same transaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from rpm_core.contracts import (
    SEVERITY_RANK,
    ClinicalEvent,
    DeviceEvent,
    EhrEvent,
    IngestMeta,
    Metrics,
    SpO2,
    Telemetry,
    TelemetryTs,
    split_ingest_record,
    utcnow,
)
from rpm_core.features import (
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
from .sinks import Emitter, PostgresSink
from .state import LocalState

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
SUPPRESS_S: dict[str, int] = {"DATA_GAP": 1800, "NEWS2_RISING": 1800, "DEVICE_SILENT": 3600, "RISK_HIGH": 3600,
                              "ADMITTED": 0, "DISCHARGED": 0, "LAB_RESULT": 0}
# A record that waited longer than this between IoT ingest and processing belongs to a backlog. The ingest queue
# does not hand a backlog out in order, so "sequence numbers are missing" proves nothing until it has drained.
CATCH_UP_S = 5.0


@dataclass
class Batch:
    vitals: list[dict] = field(default_factory=list)
    status: dict[str, dict] = field(default_factory=dict)
    events: list[ClinicalEvent] = field(default_factory=list)
    live: list[dict] = field(default_factory=list)
    processed: list[str] = field(default_factory=list)
    claimed: list[tuple[str, str]] = field(default_factory=list)  # suppression slots taken by this batch
    ehr: list[dict] = field(default_factory=list)                 # admissions, discharges and lab results
    score: dict[str, str] = field(default_factory=dict)           # patient -> risk scoring trigger
    moved: dict[str, str] = field(default_factory=dict)           # patient -> care setting changed in this batch


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
    def __init__(self, registry: DeviceRegistry, state: LocalState, pg: PostgresSink, emitter: Emitter, *,
                 min_quality: float = 0.6, suppress_s: int = 900, gap_tolerance: int = 3,
                 clock: Callable[[], datetime] = utcnow, request_scores: Callable[[str, str], None] | None = None):
        self.registry = registry
        self.state = state
        self.pg = pg
        self.emitter = emitter
        self.request_scores = request_scores or (lambda _patient_id, _trigger: None)
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
                if meta.kind == "ehr":
                    self._ehr(EhrEvent.model_validate(payload), meta, b)
                    b.processed.append(msg_id)
                    M.RECORDS.labels(meta.kind, "ok").inc()
                    continue
                if payload.get("device_id") != meta.thing:
                    # the topic is authoritative (the IoT policy pins it to the certificate): a mismatch is a spoof
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
        try:
            self.pg.write(b.vitals, b.status, b.events, self.state.take_dirty_rule_states(), b.ehr)
            self.emitter.emit(b.events)
        except Exception:
            # Not delivered: free the suppression slots this batch took, or the retry would find its own events
            # already "emitted" and swallow them (a duplicate event is safe; a dropped alert is not).
            self.state.release_emit(b.claimed)
            raise
        for patient_id in b.moved:
            # The registry and the working state were read while the batch was in flight, before the new care
            # setting was committed. Drop them again now that it is, so nothing from the old setting survives.
            self.state.forget(patient_id)
            self.registry.invalidate(patient_id)
        self.state.mark_seen(b.processed)
        self.state.publish_live(b.live)
        for patient_id, trigger in b.score.items():
            self.request_scores(patient_id, trigger)  # asynchronous: scoring never delays the rules path
        M.BATCH_SECONDS.observe(time.perf_counter() - t0)

    # ------------------------------------------------------------------ EHR events
    def _ehr(self, ev: EhrEvent, meta: IngestMeta, b: Batch) -> None:
        b.ehr.append({"event_id": ev.msg_id, "patient_id": ev.patient_id, "type": ev.type,
                      "occurred_at": ev.occurred_at, "data": ev.data})
        ref = PatientRef(ev.patient_id, "icu" if ev.type == "admission" else "home", None, False, False, {})
        trace = {"msg_id": ev.msg_id, "ingest_at": meta.ingest_at.isoformat(), "processed_at": self.clock().isoformat()}
        if ev.type == "lab":
            flag = ev.data.get("flag")
            summary = f"Lab result: {ev.data.get('name', ev.data.get('code', 'lab'))} {ev.data.get('value')}" \
                      f" {ev.data.get('unit') or ''}".rstrip() + (f" ({flag})" if flag else "")
            self._emit(b, ref, None, Finding("LAB_RESULT", "info", "ehr.lab", summary, False, ev.data),
                       ev.occurred_at, trace, None, context=None)
            b.score[ev.patient_id] = "lab"
            return
        # A change of care setting invalidates everything scored in the old one: ICU values and baselines must not
        # be carried into home monitoring, and the ICU monitor stops being this patient's device.
        self.state.forget(ev.patient_id)
        self.registry.invalidate(ev.patient_id)
        b.moved[ev.patient_id] = ref.context
        b.vitals[:] = [v for v in b.vitals if v["patient_id"] != ev.patient_id or v["context"] == ref.context]
        b.status.pop(ev.patient_id, None)
        if ev.type == "discharge":
            self._emit(b, ref, None, Finding("DISCHARGED", "info", "ehr.discharge",
                                             "Discharged from hospital; home monitoring started", False, ev.data),
                       ev.occurred_at, trace, None)
            b.score[ev.patient_id] = "discharge"
        else:
            self._emit(b, ref, None, Finding("ADMITTED", "info", "ehr.admission", "Admitted to ICU", False, ev.data),
                       ev.occurred_at, trace, None)

    # ------------------------------------------------------------------ telemetry
    def _telemetry(self, t: Telemetry, meta: IngestMeta, b: Batch, spot: bool = False) -> None:
        ref = self.registry.resolve(t.device_id)
        if ref is None:
            M.RECORDS.labels("telemetry", "unassigned_device").inc()
            return
        pid, ctx = ref.patient_id, ref.context
        if t.context != b.moved.get(pid, ctx) or t.context != ctx:
            # ICU monitors report only for admitted patients, wearables only for patients at home
            M.RECORDS.labels("telemetry", "wrong_setting").inc()
            return
        wend, wstart = t.ts.window_end, t.ts.window_start
        buffered = bool(t.edge and t.edge.buffered)
        flat = _flatten(t.metrics)
        th = default_thresholds(ref.condition, ctx).merged(ref.thresholds)

        catching_up = (_secs(meta.ingest_at, self.clock()) or 0.0) > CATCH_UP_S
        self._check_sequence(ref, t.device_id, t.seq, buffered or catching_up, wend, b)
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
                        news2=news2, activity=flat.get("activity", (None, None))[0])
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
            b.score.setdefault(pid, "vitals")
        b.live.append({"type": "vitals", "patient_id": pid, "device_id": t.device_id, "context": ctx,
                       "ts": wend.isoformat(), "vitals": {k: v for k, v in fresh.items()},
                       "news2": {"total": news2.total, "band": news2.band, "partial": news2.partial} if news2 else None,
                       "buffered": buffered, "late": late})
        self._observe_latency(t, meta, processed_at, ctx, buffered)

    def _check_sequence(self, ref: PatientRef, device_id: str, seq: int, unordered: bool, occurred: datetime,
                        b: Batch) -> None:
        """Advance the device's sequence high-water mark; report a gap only where arrival order can be trusted
        (not for store-and-forward backfill, not while draining a backlog)."""
        prev = self.state.swap_max(f"seq:{device_id}", seq)
        if prev is None or unordered:
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
    def _emit(self, b: Batch, ref: PatientRef, device_id: str | None, f: Finding, occurred: datetime,
              trace: dict | None, news2: dict | None, context: str | None = "") -> None:
        ttl = DEVICE_EVENT_POLICY.get(f.type, (None, None, SUPPRESS_S.get(f.type, self.suppress_s)))[2]
        if not self.state.should_emit(ref.patient_id, f.type, SEVERITY_RANK[f.severity], ttl):
            M.SUPPRESSED.labels(f.type).inc()
            return
        if ttl > 0:
            b.claimed.append((ref.patient_id, f.type))
        ev = ClinicalEvent(patient_id=ref.patient_id, device_id=device_id,
                           context=ref.context if context == "" else context, type=f.type,
                           severity=f.severity, occurred_at=occurred, detected_at=self.clock(), rule_id=f.rule_id,
                           summary=f.summary, requires_alert=f.requires_alert, evidence=f.evidence, news2=news2,
                           trace=trace)
        b.events.append(ev)
        b.live.append({"type": "event", "event": ev.to_dict()})
        M.EVENTS.labels(f.type, f.severity).inc()
        log.info("clinical event", extra={"patient_id": ref.patient_id, "type": f.type, "severity": f.severity,
                                          "summary": f.summary})

    def raise_finding(self, ref: PatientRef, device_id: str | None, f: Finding, occurred: datetime) -> bool:
        """Emit a finding raised outside a telemetry batch (risk scores, silent devices). True if it went out."""
        b = Batch()
        self._emit(b, ref, device_id, f, occurred, None, None)
        if not b.events:
            return False
        try:
            self.pg.write([], {}, b.events)
            self.emitter.emit(b.events)
        except Exception:
            self.state.release_emit(b.claimed)
            raise
        self.state.publish_live(b.live)
        return True

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


def risk_finding(score: dict) -> Finding:
    """A risk model crossed into its high band. Unvalidated placeholder models inform but never page."""
    pct = round(float(score["probability"]) * 100)
    validated = bool(score.get("validated"))
    return Finding("RISK_HIGH", "high" if validated else "info", f"risk.{score['model']}",
                   f"{score.get('title', score['model'])}: {pct}% ({score['band']})"
                   + ("" if validated else " [unvalidated model]"), validated,
                   {"model": score["model"], "version": score.get("version"), "probability": score["probability"],
                    "band": score["band"], "top_factors": score.get("top_factors", [])[:5], "validated": validated})


def silent_device_finding(context: str, device_id: str, minutes: float, alerting: bool = False) -> Finding:
    """A device that has stopped reporting. It is always recorded; it is an alert only for an ICU patient, and only
    when silent-device alerts are switched on (RPM_SILENT_DEVICE_ALERTS)."""
    severity, alert = ("medium", True) if context == "icu" and alerting else ("low", False)
    return Finding("DEVICE_SILENT", severity, "pipeline.device_silent",
                   f"No data from {device_id} for {minutes:.0f} min", alert, {"silent_minutes": round(minutes, 1)})


__all__ = ["Processor", "risk_finding", "silent_device_finding", "STALE_S"]
