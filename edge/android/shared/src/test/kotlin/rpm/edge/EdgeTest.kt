package rpm.edge

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

private val contracts = File(System.getProperty("contracts.dir"))
private fun schema(name: String) = RpmJson.parseToJsonElement(File(contracts, "schemas/$name.json").readText()).jsonObject

/** Checks that every key emitted exists in the JSON Schema and required keys are present (drift guard). */
private fun assertConforms(doc: JsonElement, node: JsonObject, root: JsonObject, path: String = "$") {
    val ref = node["\$ref"]?.jsonPrimitive?.content
    if (ref != null) return assertConforms(doc, root["\$defs"]!!.jsonObject[ref.substringAfterLast('/')]!!.jsonObject, root, path)
    val props = node["properties"]?.jsonObject ?: return
    val obj = doc.jsonObject
    obj.keys.forEach { k -> assertTrue("$path.$k not in schema", k in props) }
    (node["required"] as? JsonArray)?.forEach { r -> assertTrue("$path missing ${r.jsonPrimitive.content}", r.jsonPrimitive.content in obj) }
    obj.forEach { (k, v) -> if (v is JsonObject) assertConforms(v, props[k]!!.jsonObject, root, "$path.$k") }
}

class ContractsTest {
    @Test fun examplesDecode() {
        val t = RpmJson.decodeFromString<Telemetry>(File(contracts, "examples/telemetry.galaxy_watch.json").readText())
        assertEquals("gw-demo-01", t.deviceId)
        assertEquals(92.0, t.metrics.heartRate!!.mean, 0.0)
        val c = RpmJson.decodeFromString<Command>(File(contracts, "examples/command.set_profile.json").readText())
        assertEquals("SET_PROFILE", c.cmd)
    }

    @Test fun emittedTelemetryConformsToSchema() {
        val w = WindowAggregator(30, 0).apply {
            repeat(30) { add(Sample(it * 1000L, Metric.HEART_RATE, 80.0 + it % 3, 0.95)) }
            add(Sample(10_000, Metric.SPO2, 95.0, 0.9, spotCheck = true))
        }.close(30_000)!!
        val t = Telemetry(deviceId = "gw-demo-01", seq = 1, source = "wearos_emulator",
            ts = TelemetryTs(iso(0), iso(30_000), iso(30_100)), metrics = w.toMetrics(),
            edge = EdgeInfo(listOf("HR_HIGH"), "normal"), device = DeviceInfo(80.0, true, "x"))
        val s = schema("rpm.telemetry.v1")
        assertConforms(RpmJson.parseToJsonElement(RpmJson.encodeToString(t)), s, s)
    }
}

class WindowTest {
    @Test fun summarisesHeartRateAndHrv() {
        val w = WindowAggregator(30, 0)
        listOf(70.0, 72.0, 74.0).forEachIndexed { i, v -> w.add(Sample(i * 1000L, Metric.HEART_RATE, v, 1.0)) }
        listOf(850.0, 830.0, 870.0, 840.0).forEach { w.add(Sample(0, Metric.IBI_MS, it)) }
        val s = w.close(30_000)!!
        assertEquals(72.0, s.hrMean!!, 1e-9); assertEquals(70.0, s.hrMin!!, 0.0); assertEquals(74.0, s.hrMax!!, 0.0)
        assertTrue(s.hrvRmssd!! > 20)
        assertEquals("resting", s.activity)
    }

    @Test fun motionLowersQuality() {
        val calm = WindowAggregator(30, 0).apply {
            add(Sample(0, Metric.HEART_RATE, 80.0, 0.95)); repeat(20) { add(Sample(0, Metric.MOTION, 9.81)) }
        }.close(30_000)!!
        val shaking = WindowAggregator(30, 0).apply {
            add(Sample(0, Metric.HEART_RATE, 170.0, 0.95))
            repeat(20) { add(Sample(0, Metric.MOTION, if (it % 2 == 0) 4.0 else 16.0)) }
        }.close(30_000)!!
        assertTrue(calm.hrQuality!! > 0.9)
        assertTrue("shaking quality ${shaking.hrQuality}", shaking.hrQuality!! < 0.6)
        assertEquals("vigorous", shaking.activity)
    }

    @Test fun emptyWindowProducesNothing() = assertNull(WindowAggregator(30, 0).close(30_000))
}

class EdgeRulesTest {
    private fun window(i: Int, hr: Double, q: Double = 0.95, spo2: Double? = null) = WindowSummary(
        i * 30_000L, (i + 1) * 30_000L, hr, hr, hr, 30, q, null, spo2, 0.9, spo2 != null, null, 0.0, "resting", 0.0)

    @Test fun sustainedHighNeedsTwoMinutes() {
        val r = EdgeRules()
        val fired = (0..4).map { i -> r.evaluate(window(i, 130.0)).any { it.rule == "HR_SUSTAINED_HIGH" } }
        assertEquals(listOf(false, false, false, true, true), fired)
    }

    @Test fun lowQualityWindowsIgnored() {
        val r = EdgeRules()
        assertTrue((0..6).all { r.evaluate(window(it, 170.0, q = 0.3)).isEmpty() })
    }

    @Test fun spo2LowAndRateLimit() {
        val r = EdgeRules()
        val f = r.evaluate(window(0, 80.0, spo2 = 84.0)).single()
        assertEquals("SPO2_LOW", f.rule); assertEquals("high", f.severity)
        assertTrue(r.shouldAlert(f, 0)); assertFalse(r.shouldAlert(f, 60_000)); assertTrue(r.shouldAlert(f, 700_000))
    }
}

class UplinkerTest {
    class FakeTransport(var up: Boolean = true, var failAfter: Int = Int.MAX_VALUE) : Transport {
        val sent = mutableListOf<Pair<String, String>>()
        override val connected get() = up
        override fun publish(topic: String, payload: String): Boolean {
            if (sent.size >= failAfter) return false
            sent += topic to payload; return true
        }
    }

    private fun draft(kind: String, end: Long): String = if (kind == "telemetry")
        RpmJson.encodeToString(Telemetry(deviceId = "pending", seq = 0, source = "wearos_hs",
            ts = TelemetryTs(iso(end - 30_000), iso(end), iso(end)), metrics = Metrics(heartRate = Aggregate(80.0))))
    else RpmJson.encodeToString(DeviceEvent(deviceId = "pending", seq = 0, source = "wearos_hs", type = "SOS",
            ts = DeviceEventTs(iso(end), iso(end))))

    @Test fun eventsJumpTheQueueAndSeqIsMonotonic() {
        var now = 1_000_000L
        val t = FakeTransport(up = false)
        val u = Uplinker("gw-demo-01", InMemoryOutbox(), InMemorySeq(), t, clock = { now })
        u.enqueue("telemetry", draft("telemetry", now))
        u.enqueue("telemetry", draft("telemetry", now))
        u.enqueue("events", draft("events", now))
        now += 120_000  // two minutes offline
        t.up = true
        assertEquals(3, u.drain())
        assertEquals(listOf("events", "telemetry", "telemetry"), t.sent.map { it.first.substringAfterLast('/') })
        val docs = t.sent.map { RpmJson.parseToJsonElement(it.second).jsonObject }
        assertEquals(listOf(1L, 2L, 3L), docs.map { it["seq"]!!.jsonPrimitive.long })
        assertTrue(docs.all { it["device_id"]!!.jsonPrimitive.content == "gw-demo-01" })
        assertEquals("true", docs[1]["edge"]!!.jsonObject["buffered"]!!.jsonPrimitive.content)  // late -> backfill
        assertTrue(t.sent.all { it.first.startsWith("rpm/v1/dev/gw-demo-01/") })
    }

    @Test fun failedPublishKeepsMessageForRetry() {
        val t = FakeTransport(failAfter = 1)
        val box = InMemoryOutbox()
        val u = Uplinker("d-1", box, InMemorySeq(), t)
        repeat(3) { u.enqueue("telemetry", draft("telemetry", System.currentTimeMillis())) }
        assertEquals(1, u.drain()); assertEquals(2, box.size())
        t.failAfter = Int.MAX_VALUE
        assertEquals(2, u.drain()); assertEquals(0, box.size())
    }
}

class PipelineTest {
    private class Recorder : EdgeListener {
        val checkins = mutableListOf<String>(); var spot = 0; var alerts = 0
        override fun onCheckin(questionId: String, text: String, options: List<String>) { checkins += text }
        override fun onSpotCheckRequested(metric: String) { spot++ }
        override fun onLocalAlert(finding: EdgeFinding) { alerts++ }
    }

    @Test fun windowsCommandsAndEvents() {
        var now = 0L
        val drafts = mutableListOf<Pair<String, String>>()
        val rec = Recorder()
        val p = EdgePipeline("wearos_emulator", { k, d -> drafts += k to d }, rec, clock = { now })
        repeat(31) { now = it * 1000L; p.onSample(Sample(now, Metric.HEART_RATE, 88.0, 0.9)); p.tick() }
        assertEquals("telemetry", drafts.single().first)

        val cmd = """{"schema":"rpm.command.v1","cmd_id":"c1","cmd":"SET_PROFILE","issued_at":"2026-01-01T00:00:00Z","args":{"profile":"critical"}}"""
        assertEquals("profile CRITICAL", p.onCommand(cmd)); assertEquals(Profile.CRITICAL, p.profile)
        val checkin = """{"schema":"rpm.command.v1","cmd_id":"c2","cmd":"CHECKIN","issued_at":"2026-01-01T00:00:00Z","args":{"question_id":"q1","text":"Breathless?","options":["Yes","No"]}}"""
        p.onCommand(checkin); assertEquals(listOf("Breathless?"), rec.checkins)
        val expired = """{"schema":"rpm.command.v1","cmd_id":"c3","cmd":"SPOT_CHECK","issued_at":"1970-01-01T00:00:00Z","expires_at":"1970-01-01T00:00:00.001Z"}"""
        now = 10_000; p.onCommand(expired); assertEquals(0, rec.spot)

        p.checkinAnswer("q1", "Yes")
        val reply = RpmJson.parseToJsonElement(drafts.last().second).jsonObject
        assertEquals("CHECKIN_REPLY", reply["type"]!!.jsonPrimitive.content)
        assertNotNull(reply["data"])
    }

    // ------------------------------------------------------------------ provisioning
    private fun provDir(json: String?, withKeys: Boolean): File {
        val dir = kotlin.io.path.createTempDirectory("prov").toFile()
        json?.let { File(dir, "provisioning.json").writeText(it) }
        if (withKeys) listOf("device.p12", "ca.pem").forEach { n ->
            File(dir, n).writeBytes(javaClass.getResourceAsStream("/pki/$n")!!.readBytes())
        }
        return dir
    }

    @Test fun provisioningLoadsTlsIdentityAndRefusesHalfProvisionedDevices() {
        assertNull(Provisioning.load(provDir(null, false)))  // not provisioned -> dev defaults
        val plain = Provisioning.load(provDir("""{"mqtt_uri":"tcp://10.0.2.2:1883","device_id":"gw-demo-01"}""", false))!!
        assertEquals("gw-demo-01", plain.deviceId)
        assertNull(plain.socketFactory)
        val tls = """{"mqtt_uri":"ssl://example:8883","device_id":"gw-demo-01","p12_password":"test-pass"}"""
        assertNotNull(Provisioning.load(provDir(tls, true))!!.socketFactory)
        // ssl:// without key material must fail loudly, never fall back to an unauthenticated connection
        assertTrue(runCatching { Provisioning.load(provDir(tls, false)) }.isFailure)
        assertTrue(runCatching { Provisioning.load(provDir(tls.replace("test-pass", "wrong"), true)) }.isFailure)
        assertTrue(runCatching { Provisioning.load(provDir("""{"mqtt_uri":"tcp://x:1883"}""", false)) }.isFailure)
    }
}
