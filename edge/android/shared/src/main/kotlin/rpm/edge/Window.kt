package rpm.edge

import kotlin.math.abs
import kotlin.math.sqrt

enum class Metric { HEART_RATE, IBI_MS, SPO2, SKIN_TEMP, STEPS, MOTION }

/** One raw sensor reading. `quality` is the sensor's own confidence 0..1 (null = unknown). */
data class Sample(val tMillis: Long, val metric: Metric, val value: Double, val quality: Double? = null,
                  val spotCheck: Boolean = false)

enum class Profile(val windowS: Int) { NORMAL(30), ELEVATED(15), CRITICAL(5) }

data class WindowSummary(
    val startMillis: Long, val endMillis: Long,
    val hrMean: Double?, val hrMin: Double?, val hrMax: Double?, val hrCount: Int, val hrQuality: Double?,
    val hrvRmssd: Double?, val spo2: Double?, val spo2Quality: Double?, val spo2Spot: Boolean,
    val skinTemp: Double?, val steps: Double, val activity: String, val motionIndex: Double,
)

/**
 * Collapses 1 Hz sensor readings into one summary per window. Signal quality combines the sensor's own accuracy with
 * wrist motion (accelerometer), so motion artefacts reach the cloud as low-quality readings the server won't score.
 */
class WindowAggregator(var windowS: Int, startMillis: Long) {
    private var start = startMillis
    private val hr = mutableListOf<Pair<Double, Double?>>()
    private val ibi = mutableListOf<Double>()
    private val motion = mutableListOf<Double>()
    private var spo2: Sample? = null
    private var skin: Double? = null
    private var steps = 0.0

    fun add(s: Sample) {
        when (s.metric) {
            Metric.HEART_RATE -> if (s.value in 25.0..240.0) hr += s.value to s.quality
            Metric.IBI_MS -> if (s.value in 250.0..2000.0) ibi += s.value
            Metric.SPO2 -> if (s.value in 50.0..100.0) spo2 = s
            Metric.SKIN_TEMP -> skin = s.value
            Metric.STEPS -> steps += s.value
            Metric.MOTION -> motion += s.value
        }
    }

    fun due(now: Long) = now - start >= windowS * 1000L

    fun close(now: Long): WindowSummary? {
        val motionIdx = if (motion.size >= 2) stdDev(motion) else 0.0
        val activity = when {
            motionIdx < 0.15 -> "resting"
            motionIdx < 0.8 -> "light"
            motionIdx < 2.5 -> "moderate"
            else -> "vigorous"
        }
        val motionPenalty = (motionIdx / 3.0).coerceIn(0.0, 1.0) * 0.7
        val sensorQ = hr.mapNotNull { it.second }.takeIf { it.isNotEmpty() }?.average() ?: 0.9
        val hasData = hr.isNotEmpty() || spo2 != null || skin != null || steps > 0
        val summary = if (!hasData) null else WindowSummary(
            startMillis = start, endMillis = now,
            hrMean = hr.map { it.first }.takeIf { it.isNotEmpty() }?.average(),
            hrMin = hr.minOfOrNull { it.first }, hrMax = hr.maxOfOrNull { it.first }, hrCount = hr.size,
            hrQuality = if (hr.isEmpty()) null else (sensorQ * (1 - motionPenalty)).coerceIn(0.0, 1.0),
            hrvRmssd = rmssd(ibi), spo2 = spo2?.value, spo2Quality = spo2?.quality,
            spo2Spot = spo2?.spotCheck ?: false, skinTemp = skin, steps = steps, activity = activity,
            motionIndex = motionIdx,
        )
        start = now
        hr.clear(); ibi.clear(); motion.clear(); spo2 = null; skin = null; steps = 0.0
        return summary
    }

    companion object {
        fun stdDev(v: List<Double>): Double {
            val m = v.average()
            return sqrt(v.sumOf { (it - m) * (it - m) } / v.size)
        }

        fun rmssd(ibi: List<Double>): Double? {
            if (ibi.size < 3) return null
            val diffs = ibi.zipWithNext { a, b -> b - a }.filter { abs(it) < 300 }  // drop ectopic jumps
            return if (diffs.isEmpty()) null else sqrt(diffs.sumOf { it * it } / diffs.size)
        }
    }
}

fun WindowSummary.toMetrics(): Metrics = Metrics(
    heartRate = hrMean?.let { Aggregate(r1(it), hrMin?.let(::r1), hrMax?.let(::r1), hrCount, hrQuality?.let(::r2)) },
    hrvRmssd = hrvRmssd?.let { Scalar(r1(it), hrQuality?.let(::r2)) },
    spo2 = spo2?.let { SpO2(r1(it), spo2Quality?.let(::r2), spo2Spot.takeIf { s -> s }) },
    skinTemp = skinTemp?.let { Scalar(r2(it)) },
    steps = Scalar(steps),
    activity = Activity(activity),
)

private fun r1(v: Double) = Math.round(v * 10) / 10.0
private fun r2(v: Double) = Math.round(v * 100) / 100.0
