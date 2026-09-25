package com.cloudbox.rpm.wear

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update

data class UiState(
    val running: Boolean = false,
    val source: String = "-",
    val transport: String = "-",
    val cloudConnected: Boolean = false,
    val queued: Int = 0,
    val sent: Long = 0,
    val heartRate: Double? = null,
    val hrQuality: Double? = null,
    val spo2: Double? = null,
    val profile: String = "normal",
    val localAlert: String? = null,
    val checkin: Checkin? = null,
    val notice: Pair<String, String>? = null,
    val spotCheckRunning: Boolean = false,
    val log: List<String> = emptyList(),
)

data class Checkin(val questionId: String, val text: String, val options: List<String>)

/** Process-wide state shared by the service and the UI, plus a bus for UI actions and relayed commands. */
object MonitorState {
    val ui = MutableStateFlow(UiState())
    val actions = MutableSharedFlow<Action>(extraBufferCapacity = 32)

    fun log(line: String) = ui.update { it.copy(log = (listOf(line) + it.log).take(8)) }
}

sealed interface Action {
    data object Sos : Action
    data class Answer(val questionId: String, val answer: String) : Action
    data object DismissNotice : Action
    data class Command(val json: String) : Action
    data class Scenario(val name: String) : Action
}
