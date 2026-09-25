package com.cloudbox.rpm.phone

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.activity.result.contract.ActivityResultContracts
import kotlinx.coroutines.flow.update
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import rpm.edge.DeviceEvent
import rpm.edge.DeviceEventTs
import rpm.edge.Metrics
import rpm.edge.RpmJson
import rpm.edge.Scalar
import rpm.edge.Telemetry
import rpm.edge.TelemetryTs
import rpm.edge.iso

class MainActivity : ComponentActivity() {
    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        GatewayService.start(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        setContent { MaterialTheme { CompanionScreen() } }
    }
}

private fun now() = System.currentTimeMillis()

/** Patient-entered home readings fill the NEWS2 parameters a wrist wearable cannot measure. */
private fun manualReading(sbp: Double?, dbp: Double?, temp: Double?): String {
    val t = now()
    return RpmJson.encodeToString(Telemetry(deviceId = "pending", seq = 0, source = "galaxy_watch",
        ts = TelemetryTs(iso(t), iso(t), iso(t)),
        metrics = Metrics(sbp = sbp?.let { Scalar(it) }, dbp = dbp?.let { Scalar(it) }, temp = temp?.let { Scalar(it) })))
}

private fun checkinReply(questionId: String, answer: String): String {
    val t = now()
    return RpmJson.encodeToString(DeviceEvent(deviceId = "pending", seq = 0, source = "galaxy_watch",
        type = "CHECKIN_REPLY", ts = DeviceEventTs(iso(t), iso(t)),
        data = buildJsonObject { put("question_id", questionId); put("answer", answer) }))
}

@Composable
private fun CompanionScreen() {
    val s by Gateway.state.collectAsState()
    val sbp = remember { mutableStateOf("") }
    val dbp = remember { mutableStateOf("") }
    val temp = remember { mutableStateOf("") }
    Scaffold { pad ->
        Column(Modifier.fillMaxSize().padding(pad).padding(16.dp).verticalScroll(rememberScrollState()),
               verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("RPM Companion", fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
            Text((if (s.connected) "Connected to care team" else "Offline — readings are saved and sent later") +
                 " · ${s.queued} waiting · ${s.sent} sent")
            Text("Watch: ${s.heartRate?.let { "${it.toInt()} bpm" } ?: "--"} · SpO₂ ${s.spo2?.let { "%.0f%%".format(it) } ?: "--"}",
                 fontSize = 18.sp)
            s.checkin?.let { (qid, text, options) ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("Your care team asks:", fontWeight = FontWeight.SemiBold)
                        Text(text)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            options.forEach { opt ->
                                Button(onClick = {
                                    Gateway.drafts.tryEmit("events" to checkinReply(qid, opt))
                                    Gateway.state.update { it.copy(checkin = null) }
                                }) { Text(opt) }
                            }
                        }
                    }
                }
            }
            s.notice?.let { Card(Modifier.fillMaxWidth()) { Text(it, Modifier.padding(12.dp)) } }
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Enter a home reading", fontWeight = FontWeight.SemiBold)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(sbp.value, { sbp.value = it }, label = { Text("Systolic") }, modifier = Modifier.weight(1f))
                        OutlinedTextField(dbp.value, { dbp.value = it }, label = { Text("Diastolic") }, modifier = Modifier.weight(1f))
                    }
                    OutlinedTextField(temp.value, { temp.value = it }, label = { Text("Temperature °C") })
                    OutlinedButton(onClick = {
                        val (a, b, c) = Triple(sbp.value.toDoubleOrNull(), dbp.value.toDoubleOrNull(), temp.value.toDoubleOrNull())
                        if (a != null || c != null) {
                            Gateway.drafts.tryEmit("telemetry" to manualReading(a, b, c))
                            sbp.value = ""; dbp.value = ""; temp.value = ""
                        }
                    }) { Text("Send reading") }
                }
            }
            Text("Device ${s.deviceId} · ${s.endpoint}", fontSize = 12.sp)
            s.log.forEach { Text(it, fontSize = 11.sp) }
        }
    }
}
