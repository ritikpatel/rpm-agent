package com.cloudbox.rpm.wear

import android.content.Context
import android.content.Intent
import android.os.Build

/** Persistent device configuration; overridable at launch for demos:
 *  adb shell am start -n com.cloudbox.rpm.wear/.MainActivity --es transport direct --es mqtt_uri tcp://10.0.2.2:1883
 *      --es device_id gw-demo-01 --es source scripted */
class WearConfig(context: Context) {
    private val prefs = context.getSharedPreferences("rpm", Context.MODE_PRIVATE)

    val isEmulator: Boolean get() = Build.HARDWARE.contains("ranchu") || Build.FINGERPRINT.contains("generic")
    var transport: String by pref("transport", "direct")          // direct | phone
    var mqttUri: String by pref("mqtt_uri", BuildConfig.DEFAULT_MQTT_URI)
    var deviceId: String by pref("device_id", BuildConfig.DEFAULT_DEVICE_ID)
    var source: String by pref("source", "auto")                  // auto | samsung | health_services | scripted
    var scenario: String by pref("scenario", "normal")            // scripted source: normal | deteriorate | hypoxia

    fun applyExtras(intent: Intent?) {
        intent?.extras ?: return
        listOf("transport", "mqtt_uri", "device_id", "source", "scenario").forEach { k ->
            intent.getStringExtra(k)?.let { prefs.edit().putString(k, it).apply() }
        }
    }

    private fun pref(key: String, default: String) = object : kotlin.properties.ReadWriteProperty<Any?, String> {
        override fun getValue(thisRef: Any?, property: kotlin.reflect.KProperty<*>) =
            prefs.getString(key, default) ?: default
        override fun setValue(thisRef: Any?, property: kotlin.reflect.KProperty<*>, value: String) =
            prefs.edit().putString(key, value).apply()
    }
}
