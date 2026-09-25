package rpm.edge

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

// Kotlin mirror of /contracts/schemas (rpm.telemetry.v1, rpm.device_event.v1, rpm.command.v1).
// ContractsTest round-trips the shared example files so drift is caught in CI.

val RpmJson = Json {
    encodeDefaults = true
    explicitNulls = false
    ignoreUnknownKeys = true
}

@Serializable data class Scalar(val value: Double, val quality: Double? = null)
@Serializable data class SpO2(val value: Double, val quality: Double? = null,
                              @SerialName("spot_check") val spotCheck: Boolean? = null)
@Serializable data class Aggregate(val mean: Double, val min: Double? = null, val max: Double? = null,
                                   val n: Int? = null, val quality: Double? = null)
@Serializable data class Activity(val value: String)

@Serializable
data class Metrics(
    @SerialName("heart_rate") val heartRate: Aggregate? = null,
    @SerialName("hrv_rmssd") val hrvRmssd: Scalar? = null,
    val spo2: SpO2? = null,
    @SerialName("resp_rate") val respRate: Scalar? = null,
    val sbp: Scalar? = null,
    val dbp: Scalar? = null,
    val temp: Scalar? = null,
    @SerialName("skin_temp") val skinTemp: Scalar? = null,
    val gcs: Scalar? = null,
    val steps: Scalar? = null,
    val activity: Activity? = null,
)

@Serializable data class TelemetryTs(
    @SerialName("window_start") val windowStart: String,
    @SerialName("window_end") val windowEnd: String,
    @SerialName("edge_sent") val edgeSent: String,
)
@Serializable data class EdgeInfo(val flags: List<String> = emptyList(), val profile: String? = null,
                                  val buffered: Boolean = false)
@Serializable data class DeviceInfo(@SerialName("battery_pct") val batteryPct: Double? = null,
                                    @SerialName("on_wrist") val onWrist: Boolean? = null, val fw: String? = null)

@Serializable
data class Telemetry(
    @SerialName("schema") val schema: String = "rpm.telemetry.v1",
    @SerialName("msg_id") val msgId: String = uuid7(),
    @SerialName("device_id") val deviceId: String,
    val seq: Long,
    val source: String,
    val context: String = "home",
    val ts: TelemetryTs,
    val metrics: Metrics,
    val edge: EdgeInfo? = null,
    val device: DeviceInfo? = null,
)

@Serializable data class DeviceEventTs(val occurred: String, @SerialName("edge_sent") val edgeSent: String)

@Serializable
data class DeviceEvent(
    @SerialName("schema") val schema: String = "rpm.device_event.v1",
    @SerialName("msg_id") val msgId: String = uuid7(),
    @SerialName("device_id") val deviceId: String,
    val seq: Long,
    val source: String,
    val type: String,
    val ts: DeviceEventTs,
    val data: JsonObject? = null,
)

@Serializable
data class Command(
    @SerialName("schema") val schema: String = "rpm.command.v1",
    @SerialName("cmd_id") val cmdId: String,
    val cmd: String,
    @SerialName("issued_at") val issuedAt: String,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("issued_by") val issuedBy: String? = null,
    val args: JsonObject? = null,
) {
    fun expired(nowMillis: Long): Boolean =
        expiresAt != null && java.time.Instant.parse(expiresAt).toEpochMilli() < nowMillis
}

object Topics {
    fun uplink(deviceId: String, kind: String) = "rpm/v1/dev/$deviceId/$kind"
    fun commands(deviceId: String) = "rpm/v1/dev/$deviceId/cmd"
}

fun iso(millis: Long): String = java.time.Instant.ofEpochMilli(millis).toString()
