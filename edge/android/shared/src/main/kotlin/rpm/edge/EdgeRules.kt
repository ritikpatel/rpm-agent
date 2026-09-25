package rpm.edge

/** Thresholds pushed from the cloud via the device shadow / SET_PROFILE; defaults match the server's home rules. */
data class Thresholds(val hrHigh: Double = 120.0, val hrLow: Double = 45.0, val spo2Low: Double = 90.0,
                      val sustainS: Int = 120, val minQuality: Double = 0.6)

data class EdgeFinding(val rule: String, val value: Double, val severity: String = "medium")

/**
 * On-device safety rules: they still work with no connectivity (haptic warning to the patient) and are mirrored as
 * edge flags / EDGE_ALERT events. The cloud re-evaluates everything; the edge never suppresses anything.
 */
class EdgeRules(var thresholds: Thresholds = Thresholds(), private val repeatMillis: Long = 10 * 60_000L) {
    private var highSince: Long? = null
    private var lowSince: Long? = null
    private val lastFired = mutableMapOf<String, Long>()

    fun evaluate(w: WindowSummary): List<EdgeFinding> {
        val t = thresholds
        val out = mutableListOf<EdgeFinding>()
        val hr = w.hrMean
        if (hr != null && (w.hrQuality ?: 1.0) >= t.minQuality) {
            highSince = if (hr > t.hrHigh) highSince ?: w.startMillis else if (hr < t.hrHigh - 5) null else highSince
            lowSince = if (hr < t.hrLow) lowSince ?: w.startMillis else if (hr > t.hrLow + 5) null else lowSince
            highSince?.let { if (w.endMillis - it >= t.sustainS * 1000L) out += EdgeFinding("HR_SUSTAINED_HIGH", hr) }
            lowSince?.let { if (w.endMillis - it >= t.sustainS * 1000L) out += EdgeFinding("HR_SUSTAINED_LOW", hr) }
        }
        val s = w.spo2
        if (s != null && (w.spo2Quality ?: 1.0) >= t.minQuality && s < t.spo2Low) {
            out += EdgeFinding("SPO2_LOW", s, if (s < 85) "high" else "medium")
        }
        return out
    }

    /** Whether a finding should buzz the patient / raise an EDGE_ALERT now (rate-limited per rule). */
    fun shouldAlert(f: EdgeFinding, now: Long): Boolean {
        val last = lastFired[f.rule]
        if (last != null && now - last < repeatMillis) return false
        lastFired[f.rule] = now
        return true
    }
}
