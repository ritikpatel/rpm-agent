package com.cloudbox.rpm.wear

import android.content.Context
import com.samsung.android.service.health.tracking.ConnectionListener
import com.samsung.android.service.health.tracking.HealthTracker
import com.samsung.android.service.health.tracking.HealthTrackerException
import com.samsung.android.service.health.tracking.HealthTrackingService
import com.samsung.android.service.health.tracking.data.DataPoint
import com.samsung.android.service.health.tracking.data.HealthTrackerType
import com.samsung.android.service.health.tracking.data.ValueKey
import kotlinx.coroutines.suspendCancellableCoroutine
import rpm.edge.Metric
import rpm.edge.Sample
import kotlin.coroutines.resume

/**
 * Galaxy Watch source via the Samsung Health Sensor SDK (compiled only when the SDK .aar is in wear/libs/).
 * Continuous HR + IBI (-> HRV), on-demand SpO2, continuous skin temperature. Requires Samsung Health developer mode
 * on the watch during development. Written against SDK 1.3.x type/key names; adjust here if your SDK version differs.
 */
object SamsungSupport {
    const val AVAILABLE = true

    suspend fun connect(context: Context): VitalsSource? = suspendCancellableCoroutine { cont ->
        var service: HealthTrackingService? = null
        service = HealthTrackingService(object : ConnectionListener {
            override fun onConnectionSuccess() {
                val supported = service!!.trackingCapability.supportHealthTrackerTypes
                if (cont.isActive) cont.resume(
                    if (HealthTrackerType.HEART_RATE_CONTINUOUS in supported) SamsungSource(service!!, supported) else null)
            }
            override fun onConnectionEnded() = Unit
            override fun onConnectionFailed(e: HealthTrackerException) { if (cont.isActive) cont.resume(null) }
        }, context)
        service.connectService()
    }
}

private class SamsungSource(private val service: HealthTrackingService,
                            private val supported: List<HealthTrackerType>) : VitalsSource {
    override val name = "galaxy_watch"
    override val supportsSpo2 = HealthTrackerType.SPO2_ON_DEMAND in supported
    private val trackers = mutableListOf<HealthTracker>()

    private fun listen(type: HealthTrackerType, onData: (List<DataPoint>) -> Unit): HealthTracker =
        service.getHealthTracker(type).also { tracker ->
            tracker.setEventListener(object : HealthTracker.TrackerEventListener {
                override fun onDataReceived(points: List<DataPoint>) = onData(points)
                override fun onFlushCompleted() = Unit
                override fun onError(error: HealthTracker.TrackerError) = Unit
            })
            trackers += tracker
        }

    override fun start(onSample: (Sample) -> Unit) {
        listen(HealthTrackerType.HEART_RATE_CONTINUOUS) { points ->
            points.forEach { p ->
                val now = System.currentTimeMillis()
                val status = p.getValue(ValueKey.HeartRateSet.HEART_RATE_STATUS)
                val hr = p.getValue(ValueKey.HeartRateSet.HEART_RATE)
                if (hr > 0) onSample(Sample(now, Metric.HEART_RATE, hr.toDouble(), if (status == 1) 0.95 else 0.3))
                val ibis = p.getValue(ValueKey.HeartRateSet.IBI_LIST)
                val ibiStatus = p.getValue(ValueKey.HeartRateSet.IBI_STATUS_LIST)
                ibis?.forEachIndexed { i, v -> if (ibiStatus?.getOrNull(i) == 0) onSample(Sample(now, Metric.IBI_MS, v.toDouble())) }
            }
        }
        if (HealthTrackerType.SKIN_TEMPERATURE_CONTINUOUS in supported) {
            listen(HealthTrackerType.SKIN_TEMPERATURE_CONTINUOUS) { points ->
                points.forEach { p ->
                    if (p.getValue(ValueKey.SkinTemperatureSet.STATUS) == 0) onSample(Sample(System.currentTimeMillis(),
                        Metric.SKIN_TEMP, p.getValue(ValueKey.SkinTemperatureSet.OBJECT_TEMPERATURE).toDouble()))
                }
            }
        }
    }

    override fun requestSpo2(onResult: (Double, Double?) -> Unit) {
        if (!supportsSpo2) return
        var tracker: HealthTracker? = null
        tracker = listen(HealthTrackerType.SPO2_ON_DEMAND) { points ->
            points.firstOrNull { it.getValue(ValueKey.SpO2Set.STATUS) == 2 }?.let {  // 2 = measurement completed
                onResult(it.getValue(ValueKey.SpO2Set.SPO2).toDouble(), 0.9)
                tracker?.unsetEventListener()
            }
        }
    }

    override fun stop() {
        trackers.forEach { runCatching { it.unsetEventListener() } }
        trackers.clear()
        service.disconnectService()
    }
}
