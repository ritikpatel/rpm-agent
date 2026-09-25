package com.cloudbox.rpm.wear

import android.Manifest
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.wear.compose.foundation.lazy.AutoCenteringParams
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.ChipDefaults
import androidx.wear.compose.material.CompactChip
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.PositionIndicator
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText

class MainActivity : ComponentActivity() {
    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        startMonitoring()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WearConfig(this).applyExtras(intent)
        permissions.launch(arrayOf(Manifest.permission.BODY_SENSORS, Manifest.permission.POST_NOTIFICATIONS,
                                   Manifest.permission.ACTIVITY_RECOGNITION))
        setContent { MaterialTheme { MonitorScreen() } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        WearConfig(this).applyExtras(intent)
        debugHooks(intent)
    }

    /** Debug builds only: drive the watch from adb for automated demos/tests. Release builds ignore these extras so
     *  no other app can inject a fake SOS or answer. */
    private fun debugHooks(intent: Intent) {
        if (!BuildConfig.DEBUG) return
        intent.getStringExtra("scenario")?.let { MonitorState.actions.tryEmit(Action.Scenario(it)) }
        if (intent.getStringExtra("action") == "sos") MonitorState.actions.tryEmit(Action.Sos)
        intent.getStringExtra("answer")?.let { a ->
            MonitorState.ui.value.checkin?.let { MonitorState.actions.tryEmit(Action.Answer(it.questionId, a)) }
        }
    }

    private fun startMonitoring() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.BODY_SENSORS) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED) {
            ContextCompat.startForegroundService(this, Intent(this, MonitoringService::class.java))
        } else MonitorState.log("body sensor permission denied")
    }
}

@Composable
private fun MonitorScreen() {
    val s by MonitorState.ui.collectAsState()
    val listState = rememberScalingLazyListState(initialCenterItemIndex = 0)
    val sosArmed = remember { mutableStateOf(false) }
    // A new question or alert lands at item 0: bring it on screen even if the patient had scrolled away.
    LaunchedEffect(s.checkin?.questionId, s.localAlert, s.notice) { listState.scrollToItem(0) }
    val wide = Modifier.fillMaxWidth().padding(horizontal = 10.dp)
    Scaffold(timeText = { TimeText() }, positionIndicator = { PositionIndicator(listState) }) {
        ScalingLazyColumn(state = listState, modifier = Modifier.fillMaxWidth(),
                          autoCentering = AutoCenteringParams(itemIndex = 0)) {
            // Anything that needs the patient's attention comes first, so it is on screen without scrolling.
            s.checkin?.let { q ->
                item {
                    Text(q.text, fontSize = 15.sp, fontWeight = FontWeight.Bold, textAlign = TextAlign.Center,
                         modifier = wide)
                }
                item {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        q.options.forEach { opt ->
                            Button(onClick = { MonitorState.actions.tryEmit(Action.Answer(q.questionId, opt)) }) {
                                Text(opt)
                            }
                        }
                    }
                }
            }
            s.localAlert?.let { msg ->
                item { Text(msg, color = Color(0xFFFAB219), textAlign = TextAlign.Center, modifier = wide) }
            }
            s.notice?.let { (title, text) ->
                item {
                    Chip(onClick = { MonitorState.actions.tryEmit(Action.DismissNotice) },
                         label = { Text(title) }, secondaryLabel = { Text(text, maxLines = 4) },
                         colors = ChipDefaults.secondaryChipColors(), modifier = Modifier.fillMaxWidth())
                }
            }
            item {
                Text(s.heartRate?.let { "${it.toInt()} bpm" } ?: "-- bpm", fontSize = 30.sp,
                     fontWeight = FontWeight.Bold, color = Color(0xFFE34948))
            }
            item {
                Text("SpO₂ ${s.spo2?.let { "%.0f%%".format(it) } ?: "--"}" +
                     if (s.spotCheckRunning) " · measuring…" else "", textAlign = TextAlign.Center, modifier = wide)
            }
            item {
                Text((if (s.cloudConnected) "● connected" else "○ offline") + "\n${s.profile} · ${s.queued} queued",
                     fontSize = 12.sp, textAlign = TextAlign.Center, modifier = wide,
                     color = if (s.cloudConnected) Color(0xFF5FD35F) else Color(0xFFFAB219))
            }
            item {
                Button(onClick = {
                    if (sosArmed.value) { MonitorState.actions.tryEmit(Action.Sos); sosArmed.value = false }
                    else sosArmed.value = true
                }, colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFFD03B3B))) {
                    Text(if (sosArmed.value) "Send" else "SOS")
                }
            }
            if (s.source == "wearos_emulator") {
                listOf("normal", "deteriorate", "hypoxia").forEach { sc ->
                    item {
                        CompactChip(onClick = { MonitorState.actions.tryEmit(Action.Scenario(sc)) },
                                    label = { Text("scenario: $sc", fontSize = 11.sp) },
                                    colors = ChipDefaults.secondaryChipColors())
                    }
                }
            }
            item {
                Text("${s.source} · ${s.transport} · sent ${s.sent}", fontSize = 10.sp, textAlign = TextAlign.Center,
                     modifier = wide)
            }
            s.log.forEach { line ->
                item { Text(line, fontSize = 10.sp, color = Color.Gray, textAlign = TextAlign.Center, modifier = wide) }
            }
        }
    }
}

private val Int.dp get() = androidx.compose.ui.unit.Dp(this.toFloat())
