package com.cloudbox.rpm.wear

import android.content.Context

/** Built without the Samsung Health Sensor SDK (.aar not present in wear/libs/). */
object SamsungSupport {
    const val AVAILABLE = false
    suspend fun connect(@Suppress("UNUSED_PARAMETER") context: Context): VitalsSource? = null
}
