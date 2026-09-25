import json
from datetime import UTC, datetime, timedelta

import pytest

from rpm_contracts import DeviceEvent, Telemetry
from rpm_icu_gateway import hl7
from rpm_icu_gateway.gateway import Gateway
from rpm_icu_gateway.outbox import Outbox

# Hand-written fixtures (not produced by hl7.oru) so codec bugs cannot cancel out. Vendor-style: LOINC for some
# parameters, Fahrenheit temperature, a PID segment, an X-status (not obtainable) observation, a custom separator.
VENDOR_ORU = "\r".join([
    "MSH|^~\\&|PHILIPS|sim-icu-1001|RPM-GW|ICU|20261002100000+0400||ORU^R01^ORU_R01|MSG42|P|2.5",
    "PID|||MRN12345^^^HOSP^MR||Doe^John",
    "OBR|1|||69837^MDC_DEV_METER_PHYSIO_MULTI_PARAM_MDS^MDC|||20261002100000",
    "OBX|1|NM|8867-4^Heart rate^LN||124|/min|||||F",
    "OBX|2|NM|150456^MDC_PULS_OXIM_SAT_O2^MDC||88|262688^MDC_DIM_PERCENT^MDC|||||F",
    "OBX|3|NM|151562^MDC_RESP_RATE^MDC||26|264928^MDC_DIM_RESP_PER_MIN^MDC|||||F",
    "OBX|4|NM|150021^MDC_PRESS_BLD_NONINV_SYS^MDC||88|266016^MDC_DIM_MMHG^MDC|||||F",
    "OBX|5|NM|150364^MDC_TEMP_BODY^MDC||101.3|266560^MDC_DIM_FAHR^MDC|||||F",
    "OBX|6|NM|150022^MDC_PRESS_BLD_NONINV_DIA^MDC||--|266016^MDC_DIM_MMHG^MDC|||||X",
    "OBX|7|NM|999999^VENDOR_PRIVATE^99PHL||1|||||F",
]) + "\r"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.t


class FakeUplink:
    def __init__(self, up=True):
        self.up, self.sent = up, []

    @property
    def connected(self):
        return self.up

    def publish(self, topic, payload):
        if not self.up:
            return False
        self.sent.append((topic, json.loads(payload)))
        return True

    def close(self):
        pass


@pytest.fixture
def gw(tmp_path):
    clock = Clock()
    g = Gateway("icu-gw-01", Outbox(tmp_path / "ob.db"), window_s=15, clock=clock)
    g.clock_ref = clock
    return g


def test_mllp_decoder_handles_split_and_coalesced_frames_and_garbage():
    d = hl7.MllpDecoder()
    a, b = hl7.frame("MSH|^~\\&|A"), hl7.frame("MSH|^~\\&|B")
    assert d.feed(b"noise" + a[:5]) == []
    assert d.feed(a[5:] + b) == ["MSH|^~\\&|A", "MSH|^~\\&|B"]
    with pytest.raises(hl7.Hl7Error):
        d.feed(hl7.VT + b"x" * (hl7.MAX_MESSAGE + 10))


def test_parse_vendor_oru_offset_timestamp_and_custom_separator():
    m = hl7.parse(VENDOR_ORU)
    assert m.message_type == "ORU^R01^ORU_R01" and m.control_id == "MSG42" and m.device == "sim-icu-1001"
    assert m.sent_at == datetime(2026, 10, 2, 6, 0, tzinfo=UTC)  # +04:00 (Dubai) normalised to UTC
    assert m.had_pid and len(m.observations) == 7
    alt = hl7.parse("MSH#$~\\&#APP#bed-7#X#Y#20261002100000##ORU$R01#C1#P#2.6\rOBX#1#NM#8867-4$HR$LN##80#")
    assert alt.device == "bed-7" and alt.message_type == "ORU^R01" and alt.observations[0].value == "80"


def test_gateway_maps_codes_units_strips_pid_and_acks(gw):
    reply = hl7.parse(gw.on_hl7(VENDOR_ORU))
    assert "MSA|AA|MSG42" in gw.on_hl7(VENDOR_ORU)
    assert reply.message_type.startswith("ACK")
    bed = gw.beds["sim-icu-1001"]
    assert bed.samples["heart_rate"] == [124.0, 124.0]
    assert round(bed.samples["temp"][0], 1) == 38.5          # 101.3 F -> C
    assert "dbp" not in bed.samples                           # status X is not a measurement
    gw.clock_ref.t += timedelta(seconds=15)
    gw.tick()
    (_, kind, _, payload), = [r for r in gw.outbox.peek() if r[1] == "telemetry"]
    t = Telemetry.model_validate_json(payload)
    assert t.device_id == "sim-icu-1001" and t.source == "icu_gateway" and t.context == "icu"
    assert "MRN12345" not in payload and "Doe" not in payload  # PID never leaves the gateway
    assert t.metrics.heart_rate.n == 2 and t.metrics.spo2.value == 88.0 and t.seq == 2  # the alarm event took seq 1


def test_bedside_alarm_fires_once_latches_and_reannounces(gw):
    gw.on_hl7(VENDOR_ORU)  # NEWS2: RR 26 (3) SpO2 88 (3) SBP 88 (3) HR 124 (2) T 38.5 (1) -> 12
    gw.clock_ref.t += timedelta(seconds=15)
    gw.tick()
    events = [DeviceEvent.model_validate_json(p) for _, k, _, p in gw.outbox.peek() if k == "events"]
    assert len(events) == 1 and events[0].type == "EDGE_ALERT"
    assert events[0].data["rule"] == "NEWS2_HIGH" and events[0].data["severity"] == "high"
    assert events[0].data["news2"] >= 7
    for _ in range(3):  # persists: latched, no storm
        gw.on_hl7(VENDOR_ORU)
        gw.clock_ref.t += timedelta(seconds=15)
        gw.tick()
    assert sum(1 for r in gw.outbox.peek(1000) if r[1] == "events") == 1
    gw.clock_ref.t += timedelta(minutes=11)
    gw.on_hl7(VENDOR_ORU)
    gw.clock_ref.t += timedelta(seconds=15)
    gw.tick()
    assert sum(1 for r in gw.outbox.peek(1000) if r[1] == "events") == 2  # re-announced after 10 min
    assert gw.status()["beds"][0]["alarm"]["rule"] == "NEWS2_HIGH"


def test_rejects_unknown_devices_wrong_types_and_garbage(tmp_path):
    g = Gateway("icu-gw-01", Outbox(tmp_path / "o.db"), beds={"sim-icu-2"})
    assert "MSA|AR" in g.on_hl7(VENDOR_ORU)                                   # not on the allow-list
    assert "MSA|AR" in g.on_hl7(VENDOR_ORU.replace("ORU^R01^ORU_R01", "ADT^A01"))
    assert "MSA|AE" in g.on_hl7("not hl7 at all")
    assert "MSA|AR" in g.on_hl7(VENDOR_ORU.replace("sim-icu-1001", "bad id!"))


def test_store_and_forward_marks_backfill_and_events_go_first(gw):
    up = FakeUplink(up=False)
    gw.uplink = up
    for _ in range(4):  # one minute of windows with the cloud link down
        gw.on_hl7(VENDOR_ORU)
        gw.clock_ref.t += timedelta(seconds=15)
        gw.tick()
    assert gw.drain() == 0 and gw.outbox.size() == 5  # 4 telemetry + 1 alarm event, durable
    up.up = True
    import time as _t
    real = _t.time
    _t.time = lambda: gw.clock_ref.t.timestamp() + 120  # link restored two minutes later
    try:
        assert gw.drain() == 5
    finally:
        _t.time = real
    topics = [t for t, _ in up.sent]
    assert topics[0] == "rpm/v1/dev/icu-gw-01/events"                    # the alarm jumps the queue
    assert set(topics[1:]) == {"rpm/v1/dev/icu-gw-01/telemetry"}         # published as the gateway thing
    assert all(doc["edge"]["buffered"] for _, doc in up.sent[1:])
    assert [doc["seq"] for _, doc in up.sent[1:]] == sorted(doc["seq"] for _, doc in up.sent[1:])
    assert gw.outbox.size() == 0


def test_outbox_survives_restart_and_drops_oldest_telemetry_first(tmp_path):
    ob = Outbox(tmp_path / "o.db", capacity=3)
    ob.add("events", "e1", priority=1)
    for i in range(4):
        ob.add("telemetry", f"t{i}")
    assert [r[3] for r in ob.peek()] == ["e1", "t2", "t3"]
    assert ob.next_seq("bed") == 1 and ob.next_seq("bed") == 2
    ob.close()
    ob2 = Outbox(tmp_path / "o.db", capacity=3)
    assert ob2.size() == 3 and ob2.next_seq("bed") == 3


def test_ack_code():
    assert hl7.ack_code(hl7.ack(hl7.parse(VENDOR_ORU), "AA")) == "AA"
    assert hl7.ack_code("MSH|^~\\&|X\rMSA|AR|C1|nope\r") == "AR"
    assert hl7.ack_code("MSH|^~\\&|X\r") == ""


COPD_ORU = "\r".join([
    "MSH|^~\\&|MON|sim-icu-copd|RPM-GW|ICU|20261002100000||ORU^R01^ORU_R01|C1|P|2.6",
    "OBX|1|NM|147842^MDC_ECG_HEART_RATE^MDC||80|/min|||||F",
    "OBX|2|NM|150456^MDC_PULS_OXIM_SAT_O2^MDC||89|%|||||F",
    "OBX|3|NM|151562^MDC_RESP_RATE^MDC||18|/min|||||F",
    "OBX|4|NM|150021^MDC_PRESS_BLD_NONINV_SYS^MDC||125|mmHg|||||F",
    "OBX|5|NM|150364^MDC_TEMP_BODY^MDC||37.0|Cel|||||F",
    "OBX|6|NM|9269-2^GCS^LN||{gcs}|{{score}}|||||F",
]) + "\r"


def _window(g, oru):
    g.on_hl7(oru)
    g.clock_ref.t += timedelta(seconds=15)
    g.tick()
    return g.beds[hl7.parse(oru).device].news2


def test_retained_cloud_config_puts_copd_bed_on_spo2_scale_2_and_survives_restart(tmp_path):
    from rpm_contracts import GatewayConfig

    db = tmp_path / "gw.db"
    g = Gateway("icu-gw-01", Outbox(db), clock=Clock())
    g.clock_ref = g.clock
    assert _window(g, COPD_ORU.format(gcs=15)).params["spo2"] == 3        # scale 1: 89% scores 3 -> false alarm
    assert g.outbox.peek()[0][1] == "events"
    cfg = GatewayConfig(gateway_id="icu-gw-01", beds={"sim-icu-copd": {"spo2_scale": 2}})
    g.on_message("config", GatewayConfig(gateway_id="someone-else", beds={}).to_json().encode())  # not ours
    g.on_message("config", cfg.to_json().encode())
    assert _window(g, COPD_ORU.format(gcs=15)).params["spo2"] == 0        # scale 2: 88-92% is on target
    stale = GatewayConfig(gateway_id="icu-gw-01", issued_at=cfg.issued_at - timedelta(hours=1), beds={})
    g.on_message("config", stale.to_json().encode())                      # older retained copy is ignored
    g.outbox.close()
    g2 = Gateway("icu-gw-01", Outbox(db), clock=Clock())                  # restart, before the broker re-delivers
    g2.clock_ref = g2.clock
    assert _window(g2, COPD_ORU.format(gcs=15)).params["spo2"] == 0


def test_gcs_baseline_survives_restart_so_new_confusion_still_scores(tmp_path):
    db = tmp_path / "gw.db"
    g = Gateway("icu-gw-01", Outbox(db), clock=Clock())
    g.clock_ref = g.clock
    assert _window(g, COPD_ORU.format(gcs=15)).params["consciousness"] == 0
    g.outbox.close()
    g2 = Gateway("icu-gw-01", Outbox(db), clock=Clock())
    g2.clock_ref = g2.clock
    # GCS 13 against the remembered best of 15 is new confusion (+3); a fresh baseline would have called it normal
    assert _window(g2, COPD_ORU.format(gcs=13)).params["consciousness"] == 3


def test_cloud_gcs_baseline_lets_a_new_gateway_recognise_confusion(tmp_path):
    from rpm_contracts import GatewayConfig

    g = Gateway("icu-gw-01", Outbox(tmp_path / "gw.db"), clock=Clock())
    g.clock_ref = g.clock
    g.on_message("config", GatewayConfig(gateway_id="icu-gw-01",
                                         beds={"sim-icu-copd": {"gcs_baseline": 15}}).to_json().encode())
    # first GCS this gateway ever sees is 13; the cloud's episode baseline says the patient was 15
    assert _window(g, COPD_ORU.format(gcs=13)).params["consciousness"] == 3
