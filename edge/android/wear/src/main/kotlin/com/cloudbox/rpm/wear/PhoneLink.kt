package com.cloudbox.rpm.wear

import android.content.Context
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.Wearable
import com.google.android.gms.wearable.WearableListenerService
import kotlinx.coroutines.tasks.await
import rpm.edge.Outbox

/** Companion mode: drafts go to the paired phone gateway over the Wearable Data Layer (Bluetooth / Wi-Fi). */
class PhoneLink(context: Context) {
    private val messages = Wearable.getMessageClient(context)
    private val nodes = Wearable.getNodeClient(context)
    private var nodeId: String? = null
    private var lastLookup = 0L
    var connected = false; private set
    var sent = 0L; private set

    suspend fun forward(outbox: Outbox) {
        val now = System.currentTimeMillis()
        if (nodeId == null || now - lastLookup > 30_000) {
            lastLookup = now
            nodeId = runCatching { nodes.connectedNodes.await().firstOrNull { it.isNearby }?.id }.getOrNull()
        }
        val node = nodeId ?: run { connected = false; return }
        for (item in outbox.peek(20)) {
            val ok = runCatching {
                messages.sendMessage(node, "/rpm/draft/${item.kind}", item.draft.toByteArray()).await()
            }.isSuccess
            connected = ok
            if (!ok) return
            outbox.remove(item.id)
            sent++
        }
        connected = true
    }
}

/** Receives cloud commands relayed by the phone (path /rpm/cmd). */
class PhoneCommandListener : WearableListenerService() {
    override fun onMessageReceived(event: MessageEvent) {
        if (event.path == "/rpm/cmd") MonitorState.actions.tryEmit(Action.Command(String(event.data)))
    }
}
