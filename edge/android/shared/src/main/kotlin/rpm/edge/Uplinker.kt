package rpm.edge

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/**
 * Drains the outbox in order. Sequence numbers and `edge_sent` are stamped at publish time, so whichever component
 * publishes (the phone gateway, or the watch in direct mode) owns one monotonic sequence per IoT identity, and
 * store-and-forward delay is measurable. Telemetry older than `lateAfterMillis` is marked `edge.buffered`.
 */
class Uplinker(
    private val deviceId: String,
    private val outbox: Outbox,
    private val seq: SeqStore,
    private val transport: Transport,
    private val clock: () -> Long = System::currentTimeMillis,
    private val lateAfterMillis: Long = 60_000,
) {
    var sent = 0L; private set

    fun enqueue(kind: String, draft: String, priority: Int = if (kind == "events") 1 else 0) =
        outbox.add(kind, draft, priority, clock())

    /** Publishes as much as possible; returns the number of messages sent. Stops at the first failure. */
    fun drain(max: Int = 200): Int {
        var n = 0
        while (transport.connected && n < max) {
            val batch = outbox.peek(20)
            if (batch.isEmpty()) break
            for (item in batch) {
                val payload = stamp(item)
                if (!transport.publish(Topics.uplink(deviceId, item.kind), payload)) return n
                outbox.remove(item.id)
                n++; sent++
            }
        }
        return n
    }

    fun stamp(item: OutboxItem): String {
        val now = clock()
        val doc = RpmJson.parseToJsonElement(item.draft).jsonObject.toMutableMap()
        doc["device_id"] = JsonPrimitive(deviceId)
        doc["seq"] = JsonPrimitive(seq.next())
        val ts = doc["ts"]!!.jsonObject.toMutableMap().apply { put("edge_sent", JsonPrimitive(iso(now))) }
        doc["ts"] = JsonObject(ts)
        if (item.kind == "telemetry" && now - item.createdMillis > lateAfterMillis) {
            val edge = (doc["edge"]?.jsonObject ?: JsonObject(emptyMap())).toMutableMap()
            edge["buffered"] = JsonPrimitive(true)
            doc["edge"] = JsonObject(edge)
        }
        return JsonObject(doc).toString()
    }
}
