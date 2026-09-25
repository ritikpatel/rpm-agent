package com.cloudbox.rpm.wear

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import androidx.health.services.client.HealthServices
import androidx.health.services.client.MeasureCallback
import androidx.health.services.client.data.Availability
import androidx.health.services.client.data.DataPointContainer
import androidx.health.services.client.data.DataType
import androidx.health.services.client.data.DeltaDataType
import androidx.health.services.client.data.HeartRateAccuracy
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.guava.await
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import rpm.edge.Metric
import rpm.edge.Sample
import kotlin.math.sin
import kotlin.random.Random

/** A physiological sensor source. Implementations push samples; spot SpO2 is on demand where supported. */
interface VitalsSource {
    val name: String              // contract `source` value
    val supportsSpo2: Boolean
    fun start(onSample: (Sample) -> Unit)
    fun stop()
    fun requestSpo2(onResult: (Double, Double?) -> Unit) {}
}

/** Wear OS Health Services: continuous heart rate (any Wear OS watch; synthetic data on the emulator). */
class HealthServicesSource(context: Context) : VitalsSource {
    override val name = "wearos_hs"
    override val supportsSpo2 = false
    private val client = HealthServices.getClient(context).measureClient
    private var callback: MeasureCallback? = null

    suspend fun available(): Boolean = runCatching {
        DataType.HEART_RATE_BPM in client.getCapabilitiesAsync().await().supportedDataTypesMeasure
    }.getOrDefault(false)

    override fun start(onSample: (Sample) -> Unit) {
        val cb = object : MeasureCallback {
            override fun onAvailabilityChanged(dataType: DeltaDataType<*, *>, availability: Availability) = Unit
            override fun onDataReceived(data: DataPointContainer) {
                data.getData(DataType.HEART_RATE_BPM).forEach { dp ->
                    val q = when ((dp.accuracy as? HeartRateAccuracy)?.sensorStatus) {
                        HeartRateAccuracy.SensorStatus.ACCURACY_HIGH -> 0.95
                        HeartRateAccuracy.SensorStatus.ACCURACY_MEDIUM -> 0.8
                        HeartRateAccuracy.SensorStatus.ACCURACY_LOW -> 0.55
                        HeartRateAccuracy.SensorStatus.UNRELIABLE, HeartRateAccuracy.SensorStatus.NO_CONTACT -> 0.1
                        else -> 0.85
                    }
                    onSample(Sample(System.currentTimeMillis(), Metric.HEART_RATE, dp.value, q))
                }
            }
        }
        callback = cb
        client.registerMeasureCallback(DataType.HEART_RATE_BPM, cb)
    }

    override fun stop() {
        callback?.let { client.unregisterMeasureCallbackAsync(DataType.HEART_RATE_BPM, it) }
        callback = null
    }
}

/** Accelerometer magnitude feeds the signal-quality index and activity class (motion-artefact rejection). */
class MotionSource(context: Context) : SensorEventListener {
    private val sm = context.getSystemService(SensorManager::class.java)
    private var sink: ((Sample) -> Unit)? = null
    private var lastEmit = 0L

    fun start(onSample: (Sample) -> Unit) {
        sink = onSample
        sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)?.let { sm.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
    }

    fun stop() { sm.unregisterListener(this); sink = null }

    override fun onSensorChanged(e: SensorEvent) {
        val now = System.currentTimeMillis()
        if (now - lastEmit < 200) return  // 5 Hz is plenty for a motion index
        lastEmit = now
        val (x, y, z) = Triple(e.values[0], e.values[1], e.values[2])
        sink?.invoke(Sample(now, Metric.MOTION, kotlin.math.sqrt((x * x + y * y + z * z).toDouble())))
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
}

/**
 * Deterministic physiology for the emulator / demos (incl. SpO2, which Health Services does not expose):
 * `normal`, `deteriorate` (HR climbs, SpO2 falls over ~3 min) or `hypoxia`.
 */
class ScriptedSource(private val scope: CoroutineScope, var scenario: String) : VitalsSource {
    override val name = "wearos_emulator"
    override val supportsSpo2 = true
    private var job: Job? = null
    private var scenarioStart = System.currentTimeMillis()
    private val rnd = Random(42)

    fun switchScenario(s: String) { scenario = s; scenarioStart = System.currentTimeMillis() }

    private fun intensity(): Double = ((System.currentTimeMillis() - scenarioStart) / 180_000.0).coerceIn(0.0, 1.0)

    private fun hr(t: Long): Double {
        val base = 76 + 4 * sin(t / 60_000.0) + rnd.nextDouble(-2.0, 2.0)
        return base + when (scenario) { "deteriorate" -> 48 * intensity(); "hypoxia" -> 14 * intensity(); else -> 0.0 }
    }

    fun spo2(): Double = 97.0 + rnd.nextDouble(-0.6, 0.6) - when (scenario) {
        "deteriorate" -> 8 * intensity(); "hypoxia" -> 13 * intensity(); else -> 0.0
    }

    override fun start(onSample: (Sample) -> Unit) {
        job = scope.launch {
            var tick = 0
            while (isActive) {
                val now = System.currentTimeMillis()
                val h = hr(now)
                onSample(Sample(now, Metric.HEART_RATE, h, 0.93))
                onSample(Sample(now, Metric.IBI_MS, 60_000 / h + rnd.nextDouble(-25.0, 25.0)))
                if (tick % 300 == 0) onSample(Sample(now, Metric.SPO2, spo2(), 0.9, spotCheck = true))
                if (tick % 60 == 0) onSample(Sample(now, Metric.SKIN_TEMP, 33.4 + rnd.nextDouble(-0.2, 0.2)))
                tick++
                delay(1_000)
            }
        }
    }

    override fun stop() { job?.cancel() }

    override fun requestSpo2(onResult: (Double, Double?) -> Unit) {
        scope.launch { delay(15_000); onResult(spo2(), 0.92) }  // on-demand measurement takes ~15-30 s
    }
}
