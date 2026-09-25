package rpm.edge

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/** Callbacks from the pipeline to the device UI / sensor layer. */
interface EdgeListener {
    fun onWindow(summary: WindowSummary) {}
    fun onLocalAlert(finding: EdgeFinding) {}
    fun onCheckin(questionId: String, text: String, options: List<String>) {}
    fun onNotify(title: String, text: String) {}
    fun onSpotCheckRequested(metric: String) {}
    fun onProfile(profile: Profile) {}
}

/**
 * Sensor samples -> windows -> edge rules -> draft messages -> sink. Runs on the watch (sink = phone link or direct
 * MQTT) and its drafts are completed by an Uplinker. Handles cloud commands (SET_PROFILE, SPOT_CHECK, CHECKIN, NOTIFY).
 */
class EdgePipeline(
    private val source: String,
    private val sink: (kind: String, draft: String) -> Unit,
    private val listener: EdgeListener,
    private val clock: () -> Long = System::currentTimeMillis,
    private val context: String = "home",
) {
    var profile = Profile.NORMAL; private set
    val rules = EdgeRules()
    private val window = WindowAggregator(profile.windowS, clock())
    var batteryPct: Double? = null
    var onWrist: Boolean = true
    private var lastWristCheck = true

    fun onSample(s: Sample) = synchronized(this) { window.add(s) }

    /** Call about once per second. */
    fun tick() {
        val now = clock()
        val summary = synchronized(this) { if (window.due(now)) window.close(now) else null } ?: return
        listener.onWindow(summary)
        val findings = rules.evaluate(summary)
        val telemetry = Telemetry(
            deviceId = "pending", seq = 0, source = source, context = context,
            ts = TelemetryTs(iso(summary.startMillis), iso(summary.endMillis), iso(summary.endMillis)),
            metrics = summary.toMetrics(),
            edge = EdgeInfo(flags = findings.map { it.rule }, profile = profile.name.lowercase()),
            device = DeviceInfo(batteryPct = batteryPct, onWrist = onWrist, fw = FIRMWARE),
        )
        sink("telemetry", RpmJson.encodeToString(telemetry))
        findings.filter { rules.shouldAlert(it, now) }.forEach { f ->
            listener.onLocalAlert(f)
            event("EDGE_ALERT", buildJsonObject {
                put("rule", f.rule); put("value", f.value); put("severity", f.severity)
            })
        }
        if (onWrist != lastWristCheck) {
            lastWristCheck = onWrist
            event("DEVICE_STATUS", buildJsonObject { put("reason", if (onWrist) "on_wrist" else "off_wrist") })
        }
    }

    fun event(type: String, data: JsonObject? = null) {
        val now = clock()
        val ev = DeviceEvent(deviceId = "pending", seq = 0, source = source, type = type,
                             ts = DeviceEventTs(iso(now), iso(now)), data = data)
        sink("events", RpmJson.encodeToString(ev))
    }

    fun sos() = event("SOS", buildJsonObject { put("trigger", "button") })
    fun fall() = event("FALL", buildJsonObject { put("trigger", "fall_detection") })

    fun checkinAnswer(questionId: String, answer: String) =
        event("CHECKIN_REPLY", buildJsonObject { put("question_id", questionId); put("answer", answer) })

    fun spotCheckResult(value: Double, quality: Double?) {
        onSample(Sample(clock(), Metric.SPO2, value, quality, spotCheck = true))
        event("SPOT_CHECK_RESULT", buildJsonObject {
            put("metric", "spo2"); put("value", value); quality?.let { put("quality", it) }
        })
    }

    fun setProfile(p: Profile) = synchronized(this) {
        profile = p
        window.windowS = p.windowS
        listener.onProfile(p)
    }

    /** Returns a short description of what was done (for logs), or null if the command was ignored. */
    fun onCommand(json: String): String? {
        val cmd = runCatching { RpmJson.decodeFromString<Command>(json) }.getOrNull() ?: return null
        if (cmd.expired(clock())) return "ignored expired ${cmd.cmd}"
        val args = cmd.args ?: JsonObject(emptyMap())
        fun arg(k: String) = args[k]?.jsonPrimitive?.contentOrNull
        return when (cmd.cmd) {
            "SET_PROFILE" -> Profile.entries.firstOrNull { it.name.equals(arg("profile"), true) }
                ?.let { setProfile(it); "profile ${it.name}" }
            "SPOT_CHECK" -> { listener.onSpotCheckRequested(arg("metric") ?: "spo2"); "spot check" }
            "CHECKIN" -> {
                val options = (args["options"] as? kotlinx.serialization.json.JsonArray)
                    ?.map { it.jsonPrimitive.content } ?: listOf("Yes", "No")
                listener.onCheckin(arg("question_id") ?: cmd.cmdId, arg("text") ?: "How are you feeling?", options)
                "check-in"
            }
            "NOTIFY" -> { listener.onNotify(arg("title") ?: "Care team", arg("text") ?: ""); "notify" }
            else -> null
        }
    }

    companion object {
        const val FIRMWARE = "rpm-wear-0.3.0"
    }
}
