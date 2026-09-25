package rpm.edge

/** A message waiting to be published: "telemetry" or "events" plus a draft JSON (seq/edge_sent stamped at send). */
data class OutboxItem(val id: Long, val kind: String, val draft: String, val priority: Int, val createdMillis: Long)

/** Durable queue (SQLite on Android). Ordering: priority desc, then insertion order. */
interface Outbox {
    fun add(kind: String, draft: String, priority: Int, createdMillis: Long)
    fun peek(limit: Int): List<OutboxItem>
    fun remove(id: Long)
    fun size(): Int
}

class InMemoryOutbox(private val capacity: Int = 20_000) : Outbox {
    private val items = mutableListOf<OutboxItem>()
    private var nextId = 1L

    @Synchronized override fun add(kind: String, draft: String, priority: Int, createdMillis: Long) {
        if (items.size >= capacity) items.removeAt(items.indexOfFirst { it.priority == 0 }.coerceAtLeast(0))
        items += OutboxItem(nextId++, kind, draft, priority, createdMillis)
    }

    @Synchronized override fun peek(limit: Int) =
        items.sortedWith(compareByDescending<OutboxItem> { it.priority }.thenBy { it.id }).take(limit)

    @Synchronized override fun remove(id: Long) { items.removeAll { it.id == id } }
    @Synchronized override fun size() = items.size
}

/** Monotonic per-device sequence (SharedPreferences on Android). */
interface SeqStore { fun next(): Long }

class InMemorySeq(private var value: Long = 0) : SeqStore {
    @Synchronized override fun next() = ++value
}
