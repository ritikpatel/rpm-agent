package com.cloudbox.rpm.wear

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.BatteryManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import rpm.edge.EdgeFinding
import rpm.edge.EdgeListener
import rpm.edge.EdgePipeline
import rpm.edge.InMemoryOutbox
import rpm.edge.MqttTransport
import rpm.edge.Profile
import rpm.edge.SeqStore
import rpm.edge.Provisioning
import rpm.edge.Uplinker
import rpm.edge.WindowSummary

/**
 * Foreground health service: sensors -> EdgePipeline (windows, signal quality, edge rules, haptics) -> either the
 * phone gateway (Data Layer) or directly to MQTT (standalone watch / emulator). Handles cloud commands.
 */
class MonitoringService : LifecycleService() {
    private lateinit var cfg: WearConfig
    private lateinit var pipeline: EdgePipeline
    private var source: VitalsSource? = null
    private var scripted: ScriptedSource? = null
    private val motion by lazy { MotionSource(this) }
    private val outbox = InMemoryOutbox(capacity = 5_000)
    private var mqtt: MqttTransport? = null
    private var uplinker: Uplinker? = null
    private var phone: PhoneLink? = null
    private var started = false

    override fun onCreate() {
        super.onCreate()
        cfg = WearConfig(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Monitoring", NotificationManager.IMPORTANCE_LOW))
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("Remote monitoring active").setSmallIcon(R.drawable.ic_heart).setOngoing(true).build()
        val type = if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH else 0
        ServiceCompat.startForeground(this, 1, notification, type)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        if (!started) { started = true; start() }
        return START_STICKY
    }

    private fun start() = lifecycleScope.launch {
        val src = chooseSource()
        source = src
        pipeline = EdgePipeline(src.name, ::onDraft, listener)
        var identity = cfg.deviceId
        if (cfg.transport == "direct") {
            // A provisioned identity (provision.sh) wins over the dev defaults; a broken one is not replaced by them.
            val prov = runCatching { Provisioning.load(filesDir) }
            prov.exceptionOrNull()?.let { MonitorState.log("provisioning invalid: ${it.message}") }
            val p = prov.getOrNull()
            identity = p?.deviceId ?: cfg.deviceId
            if (prov.isSuccess) {
                val transport = MqttTransport(p?.mqttUri ?: cfg.mqttUri, identity, p?.socketFactory,
                    onCommand = { MonitorState.actions.tryEmit(Action.Command(it)) }, log = MonitorState::log)
                mqtt = transport.also { it.start() }
                uplinker = Uplinker(identity, outbox, PrefsSeq(this@MonitoringService), transport)
            }
        } else {
            phone = PhoneLink(this@MonitoringService)
        }
        src.start(pipeline::onSample)
        motion.start(pipeline::onSample)
        MonitorState.ui.update { it.copy(running = true, source = src.name, transport = cfg.transport) }
        MonitorState.log("source ${src.name}, transport ${cfg.transport} as $identity")
        launch { MonitorState.actions.collect(::handle) }
        launch(Dispatchers.IO) {
            while (isActive) {
                pipeline.batteryPct = battery()
                pipeline.tick()
                val link = phone
                if (link != null) link.forward(outbox) else uplinker?.drain()
                MonitorState.ui.update {
                    it.copy(cloudConnected = mqtt?.connected ?: (phone?.connected == true), queued = outbox.size(),
                            sent = uplinker?.sent ?: (phone?.sent ?: 0))
                }
                delay(1_000)
            }
        }
    }

    private suspend fun chooseSource(): VitalsSource {
        fun scriptedSource() = ScriptedSource(lifecycleScope, cfg.scenario).also { scripted = it }
        return when (cfg.source) {
            "scripted" -> scriptedSource()
            "health_services" -> HealthServicesSource(this)
            "samsung" -> SamsungSupport.connect(this) ?: scriptedSource()
            else -> if (cfg.isEmulator) scriptedSource()
                    else SamsungSupport.connect(this)
                        ?: HealthServicesSource(this).takeIf { it.available() }
                        ?: scriptedSource()
        }
    }

    private fun onDraft(kind: String, draft: String) {
        val up = uplinker
        if (up != null) up.enqueue(kind, draft) else outbox.add(kind, draft, if (kind == "events") 1 else 0,
                                                                System.currentTimeMillis())
    }

    private val listener = object : EdgeListener {
        override fun onWindow(summary: WindowSummary) = MonitorState.ui.update {
            it.copy(heartRate = summary.hrMean ?: it.heartRate, hrQuality = summary.hrQuality ?: it.hrQuality,
                    spo2 = summary.spo2 ?: it.spo2)
        }

        override fun onLocalAlert(finding: EdgeFinding) {
            buzz(longArrayOf(0, 400, 200, 400))
            val msg = when (finding.rule) {
                "SPO2_LOW" -> "Low oxygen reading. Sit still and breathe slowly."
                "HR_SUSTAINED_HIGH" -> "Heart rate has been high. Please sit and rest."
                "HR_SUSTAINED_LOW" -> "Heart rate has been low. Please sit down."
                else -> "Please check how you feel."
            }
            MonitorState.ui.update { it.copy(localAlert = msg) }
        }

        override fun onCheckin(questionId: String, text: String, options: List<String>) {
            buzz(longArrayOf(0, 200, 150, 200))
            MonitorState.ui.update { it.copy(checkin = Checkin(questionId, text, options)) }
        }

        override fun onNotify(title: String, text: String) {
            buzz(longArrayOf(0, 300))
            MonitorState.ui.update { it.copy(notice = title to text) }
        }

        override fun onSpotCheckRequested(metric: String) {
            val src = source ?: return
            if (!src.supportsSpo2) { MonitorState.log("SpO2 spot check not supported by ${src.name}"); return }
            MonitorState.ui.update { it.copy(spotCheckRunning = true) }
            src.requestSpo2 { value, q ->
                pipeline.spotCheckResult(value, q)
                MonitorState.ui.update { it.copy(spotCheckRunning = false, spo2 = value) }
            }
        }

        override fun onProfile(profile: Profile) =
            MonitorState.ui.update { it.copy(profile = profile.name.lowercase()) }
    }

    private fun handle(a: Action) {
        when (a) {
            Action.Sos -> { pipeline.sos(); buzz(longArrayOf(0, 100)); MonitorState.log("SOS sent") }
            is Action.Answer -> { pipeline.checkinAnswer(a.questionId, a.answer); MonitorState.ui.update { it.copy(checkin = null) } }
            Action.DismissNotice -> MonitorState.ui.update { it.copy(notice = null, localAlert = null) }
            is Action.Command -> pipeline.onCommand(a.json)?.let { MonitorState.log("cmd: $it") }
            is Action.Scenario -> { cfg.scenario = a.name; scripted?.switchScenario(a.name); MonitorState.log("scenario ${a.name}") }
        }
    }

    private fun buzz(pattern: LongArray) {
        getSystemService(Vibrator::class.java)?.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }

    private fun battery(): Double =
        getSystemService(BatteryManager::class.java).getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).toDouble()

    override fun onDestroy() {
        source?.stop(); motion.stop(); mqtt?.stop()
        MonitorState.ui.update { it.copy(running = false) }
        super.onDestroy()
    }

    companion object { const val CHANNEL = "monitoring" }
}

/** Monotonic sequence that survives restarts (the server uses it for gap detection). */
class PrefsSeq(context: Context) : SeqStore {
    private val prefs = context.getSharedPreferences("rpm.seq", Context.MODE_PRIVATE)
    @Synchronized override fun next(): Long {
        val v = prefs.getLong("seq", 0) + 1
        prefs.edit().putLong("seq", v).commit()
        return v
    }
}
