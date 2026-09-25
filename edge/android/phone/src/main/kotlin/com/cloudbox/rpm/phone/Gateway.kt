package com.cloudbox.rpm.phone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.Wearable
import com.google.android.gms.wearable.WearableListenerService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import rpm.edge.MqttTransport
import rpm.edge.Outbox
import rpm.edge.OutboxItem
import rpm.edge.RpmJson
import rpm.edge.SeqStore
import rpm.edge.Provisioning
import rpm.edge.Uplinker
import javax.net.SocketFactory

data class GatewayState(
    val connected: Boolean = false, val queued: Int = 0, val sent: Long = 0, val deviceId: String = "-",
    val endpoint: String = "-", val watchSeen: Long? = null, val heartRate: Double? = null, val spo2: Double? = null,
    val checkin: Triple<String, String, List<String>>? = null, val notice: String? = null,
    val log: List<String> = emptyList(),
)

object Gateway {
    val state = MutableStateFlow(GatewayState())
    val drafts = MutableSharedFlow<Pair<String, String>>(extraBufferCapacity = 256)
    fun log(line: String) = state.update { it.copy(log = (listOf(line) + it.log).take(10)) }
}

/** Durable store-and-forward queue: survives app restarts and phone reboots. */
class SqliteOutbox(context: Context) : Outbox,
    SQLiteOpenHelper(context, "outbox.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) = db.execSQL(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, draft TEXT NOT NULL, " +
            "priority INTEGER NOT NULL, created INTEGER NOT NULL)")
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    @Synchronized override fun add(kind: String, draft: String, priority: Int, createdMillis: Long) {
        writableDatabase.insert("outbox", null, ContentValues().apply {
            put("kind", kind); put("draft", draft); put("priority", priority); put("created", createdMillis)
        })
    }

    @Synchronized override fun peek(limit: Int): List<OutboxItem> = readableDatabase.rawQuery(
        "SELECT id, kind, draft, priority, created FROM outbox ORDER BY priority DESC, id LIMIT $limit", null
    ).use { c ->
        buildList { while (c.moveToNext()) add(OutboxItem(c.getLong(0), c.getString(1), c.getString(2), c.getInt(3), c.getLong(4))) }
    }

    @Synchronized override fun remove(id: Long) { writableDatabase.delete("outbox", "id=?", arrayOf(id.toString())) }
    @Synchronized override fun size(): Int =
        readableDatabase.rawQuery("SELECT count(*) FROM outbox", null).use { it.moveToFirst(); it.getInt(0) }
}

class PrefsSeq(context: Context) : SeqStore {
    private val prefs = context.getSharedPreferences("rpm.seq", Context.MODE_PRIVATE)
    @Synchronized override fun next(): Long {
        val v = prefs.getLong("seq", 0) + 1
        prefs.edit().putLong("seq", v).commit()
        return v
    }
}

/**
 * The phone is the IoT identity (thing `gw-demo-01`): it stamps sequence numbers, holds the durable outbox, keeps the
 * MQTT session to IoT Core (mutual TLS) and relays commands to the watch.
 * Provisioning (edge/android/provision.sh) writes provisioning.json [+ device.p12, ca.pem] to the app's external
 * files dir.
 */
class GatewayService : LifecycleService() {
    private lateinit var uplinker: Uplinker
    private lateinit var transport: MqttTransport
    private lateinit var outbox: SqliteOutbox

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java)
            .createNotificationChannel(NotificationChannel("gw", "Gateway", NotificationManager.IMPORTANCE_LOW))
        val n = NotificationCompat.Builder(this, "gw").setContentTitle("Forwarding your watch readings to your care team")
            .setSmallIcon(R.drawable.ic_heart).setOngoing(true).build()
        ServiceCompat.startForeground(this, 1, n,
            if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0)

        val prov = runCatching { Provisioning.load(getExternalFilesDir(null) ?: filesDir) }
            .onFailure { Gateway.log("provisioning invalid: ${it.message}") }.getOrNull()
        val uri = prov?.mqttUri ?: BuildConfig.DEFAULT_MQTT_URI
        val deviceId = prov?.deviceId ?: BuildConfig.DEFAULT_DEVICE_ID
        val tls: SocketFactory? = prov?.socketFactory
        outbox = SqliteOutbox(this)
        transport = MqttTransport(uri, deviceId, tls, onCommand = ::onCommand, log = Gateway::log)
        uplinker = Uplinker(deviceId, outbox, PrefsSeq(this), transport)
        Gateway.state.update { it.copy(deviceId = deviceId, endpoint = uri) }
        transport.start()

        lifecycleScope.launch { Gateway.drafts.collect { (kind, draft) -> accept(kind, draft) } }
        lifecycleScope.launch(Dispatchers.IO) {
            while (isActive) {
                uplinker.drain()
                Gateway.state.update { it.copy(connected = transport.connected, queued = outbox.size(), sent = uplinker.sent) }
                delay(1_000)
            }
        }
    }

    private fun accept(kind: String, draft: String) {
        uplinker.enqueue(kind, draft)
        if (kind == "telemetry") runCatching {
            val m = RpmJson.parseToJsonElement(draft).jsonObject["metrics"]!!.jsonObject
            Gateway.state.update {
                it.copy(watchSeen = System.currentTimeMillis(),
                        heartRate = m["heart_rate"]?.jsonObject?.get("mean")?.jsonPrimitive?.content?.toDouble() ?: it.heartRate,
                        spo2 = m["spo2"]?.jsonObject?.get("value")?.jsonPrimitive?.content?.toDouble() ?: it.spo2)
            }
        }
    }

    /** Cloud command: show patient-facing ones on the phone too, and relay everything to the watch. */
    private fun onCommand(json: String) {
        runCatching {
            val cmd = RpmJson.parseToJsonElement(json).jsonObject
            val args = cmd["args"] as? JsonObject
            when (cmd["cmd"]?.jsonPrimitive?.content) {
                "CHECKIN" -> Gateway.state.update { it.copy(checkin = Triple(
                    args?.get("question_id")?.jsonPrimitive?.content ?: "q",
                    args?.get("text")?.jsonPrimitive?.content ?: "How are you feeling?",
                    args?.get("options")?.jsonArray?.map { o -> o.jsonPrimitive.content } ?: listOf("Yes", "No"))) }
                "NOTIFY" -> Gateway.state.update { it.copy(notice = args?.get("text")?.jsonPrimitive?.content) }
            }
        }
        lifecycleScope.launch(Dispatchers.IO) {
            runCatching {
                Wearable.getNodeClient(this@GatewayService).connectedNodes.await().forEach { node ->
                    Wearable.getMessageClient(this@GatewayService).sendMessage(node.id, "/rpm/cmd", json.toByteArray()).await()
                }
            }.onFailure { Gateway.log("relay to watch failed: ${it.message}") }
        }
    }

    override fun onDestroy() { transport.stop(); super.onDestroy() }

    companion object {
        fun start(context: Context) = context.startForegroundService(Intent(context, GatewayService::class.java))
    }
}

/** Drafts arriving from the watch over the Data Layer. */
class WatchListener : WearableListenerService() {
    override fun onMessageReceived(event: MessageEvent) {
        val kind = event.path.removePrefix("/rpm/draft/")
        if (kind == "telemetry" || kind == "events") {
            GatewayService.start(this)
            Gateway.drafts.tryEmit(kind to String(event.data))
        }
    }
}
