package rpm.edge

import java.security.SecureRandom
import java.util.UUID

private val random = SecureRandom()

/** UUIDv7 (RFC 9562): time-ordered idempotency key, identical layout to the Python services. */
fun uuid7(nowMillis: Long = System.currentTimeMillis()): String {
    val rand = ByteArray(10).also(random::nextBytes)
    val msb = (nowMillis shl 16) or (0x7L shl 12) or ((rand[0].toLong() and 0x0F) shl 8) or (rand[1].toLong() and 0xFF)
    var lsb = 0L
    for (i in 2 until 10) lsb = (lsb shl 8) or (rand[i].toLong() and 0xFF)
    lsb = (lsb and 0x3FFFFFFFFFFFFFFFL) or Long.MIN_VALUE  // variant 10
    return UUID(msb, lsb).toString()
}
