package com.ishanp.androidwebcam.companion

/**
 * On-device ground truth for the desktop's mDNS discovery.
 *
 * WHY THIS EXISTS
 * The desktop parses `avahi-browse` records, and that field layout was taken
 * from a man page and checked against SYNTHETIC records only -- no phone was
 * ever on the network. This is the one piece of discovery that cannot be
 * verified from the desktop, and it is also the piece most likely to be
 * wrong. The phone is the only place the real advertisement exists.
 *
 * The four properties below are what adbd actually publishes:
 *   service.adb.tls.port        -- wireless-debugging port (random, high)
 *   service.adb.tcp.port        -- legacy tcpip port (usually 5555)
 *   service.adb.mdns.registration -- the mDNS service name being advertised
 *   adb.mdns.hostname           -- hostname in the advertisement
 *
 * Reading them is also how a user finds out WHY the desktop cannot see the
 * phone, without needing a terminal on the host.
 *
 * PERMISSION NOTE
 * `android.os.SystemProperties` is @hide and absent from the public SDK jar,
 * so the reflective call below is unavailable to a normal app. `getprop` via
 * Runtime.exec is the permission-free route and is what this uses. `getprop`
 * is readable by any app; it is not a privileged property.
 */
class AdbProps(
    /** Injected so the parsing is JVM-testable without a device. */
    private val exec: (String) -> String = ::runGetprop,
) {

    /**
     * The props that actually matter, in one `getprop` call.
     *
     * Single call, not four: `getprop` forks a process per invocation and the
     * Dashboard polls this every 2s, so four calls per tick would be 8 forks
     * per 2 seconds of pure waste.
     */
    fun relevant(): Map<String, String> = parseGetprop(exec(GETPROP_ALL))

    /**
     * Read getprop once and report WHY it failed when it did.
     *
     * The failure distinction matters and is not cosmetic: an empty string
     * from a blocked or hung getprop is indistinguishable from "the property
     * is not set", so a phone where wireless debugging is simply OFF would
     * render the same as a phone where reading failed. That is a fabricated
     * answer in exactly the place the user asked us to stop guessing, so the
     * error text is carried through instead of swallowed.
     */
    fun relevantOrError(): Result<Map<String, String>> =
        runCatching { parseGetprop(exec(GETPROP_ALL)) }

    /**
     * `getprop` prints `[key]: [value]` -- the KEY is bracketed too, which is
     * the detail that makes a naive parser silently return an empty map
     * rather than fail. Both sides are stripped.
     *
     * The split must be on the FIRST colon: a key never contains one, but a
     * value can (`[some.time]: [10:22:33]`), so splitting on the last colon
     * would truncate that value to `22:33`.
     */
    fun parseGetprop(out: String): Map<String, String> {
        val outMap = HashMap<String, String>()
        for (line in out.lineSequence()) {
            if (line.isBlank()) continue
            val k = line.substringBefore(':').trim().removeSurrounding("[", "]").trim()
            val v = line.substringAfter(':', "")
                .trim()
                .removeSurrounding("[", "]")
                .trim()
            if (k.isNotBlank()) outMap[k] = v
        }
        return outMap
    }

    /**
     * Ports are carried on Snapshot, not read via a map-taking accessor.
     *
     * An accessor shaped wirelessPort(map) invites a relevant() call at every
     * call site, which is how this class ended up forking getprop 11 times
     * per refresh before the fork-once test caught it. The values live on the
     * captured Snapshot and are read as fields.
     */
    private fun port(p: Map<String, String>, key: String): Int? = p[key]?.toIntOrNull()

    /**
     * A short human summary for the Dashboard, with explicit provenance.
     *
     * Each line says where its value came from, so an empty value is never
     * silently rendered as a real fact. Returns one line per fact.
     */
    /**
     * Render lines from an already-read result. Pure -- no exec, no fork.
     *
     * Callers read once and then format as many times as they need, so the
     * 2s Dashboard poll forks getprop exactly once per tick.
     */
    fun render(read: Result<Map<String, String>>): List<String> {
        if (read.isFailure) {
            return listOf(
                "⚠ getprop unavailable: ${read.exceptionOrNull()?.message ?: "unknown error"}",
                "mDNS values below are UNKNOWN, not unset — cannot verify discovery.",
            )
        }
        val p = read.getOrDefault(emptyMap())
        val tls = p["service.adb.tls.port"]
        val tcp = p["service.adb.tcp.port"]
        val reg = p["service.adb.mdns.registration"]
        val host = p["adb.mdns.hostname"]
        return listOf(
            "wireless port (service.adb.tls.port): ${tls?.takeIf { it.isNotBlank() } ?: "— not set (wireless debugging off)"}",
            "legacy port  (service.adb.tcp.port): ${tcp?.takeIf { it.isNotBlank() } ?: "— not set (no adb tcpip)"}",
            "mDNS registration: ${reg?.takeIf { it.isNotBlank() } ?: "— not advertising"}",
            "mDNS hostname:    ${host?.takeIf { it.isNotBlank() } ?: "—"}",
        )
    }

    /**
     * Deliberately NO summary() convenience method.
     *
     * It read as a one-liner but hid a process fork, and a caller rendering
     * one line per fact would fork getprop once per fact. read() returns a
     * captured Snapshot so the fork happens exactly once per refresh and the
     * card renders from that.
     */
    /** Read once; render as many times as you like from the result. */
    /** Read once, publish to the shared cache, and return it. */
    fun readAndPublish(): Snapshot {
        val snap = read()
        update(snap)
        return snap
    }

    /** Read once; render as many times as you like from the result. */
    fun read(): Snapshot {
        val result = relevantOrError()
        return Snapshot(
            lines = render(result),
            wirelessPort = result.getOrNull()?.let { port(it, "service.adb.tls.port") },
            legacyPort = result.getOrNull()?.let { port(it, "service.adb.tcp.port") },
            loaded = true,
        )
    }

    /**
     * One captured getprop read: the rendered lines plus the parsed values.
     *
     * Holding the result rather than re-reading means the Dashboard can show
     * the facts as many times as it likes without forking the process again.
     */
    data class Snapshot(
        val lines: List<String> = listOf(POLLER_NOT_RUNNING),
        val wirelessPort: Int? = null,
        val legacyPort: Int? = null,
        /**
         * False until a real read happens.
         *
         * A separate flag rather than testing `lines.isEmpty()`: the default
         * line is non-empty, so an emptiness check would never fire and the
         * placeholder would be served as fact forever.
         */
        val loaded: Boolean = false,
    )

    companion object {
        /**
         * getprop forks a process and the four properties it reads only change
         * when the user toggles wireless debugging. This app exists to run on
         * battery, so the read is on a slow timer, not per UI tick.
         *
         * Shared by the Dashboard and KeepAliveService so the two cannot
         * drift apart into two different polling rates.
         */
        const val POLL_MS = 15_000L

        /**
         * The terminal placeholder, which must never BE the answer.
         *
         * The Dashboard renders the shared cache and the service poller is its
         * only writer. If keep-alive is off, or the user has never toggled it,
         * no poller exists and the cache would sit at "reading…" forever --
         * indistinguishable from a real reading-in-progress. So the default
         * says what is actually true: nobody is polling.
         */
        const val POLLER_NOT_RUNNING =
            "⚠ no reader running — start Keep-alive to see these values"

        /**
         * The one cached read, shared by the Dashboard and the socket.
         *
         * Two independent pollers would mean two getprop rates that drift
         * apart, and two answers to "what port is the phone advertising". One
         * writer (the service poller), many readers.
         */
        @Volatile
        @JvmStatic
        var cached: Snapshot = Snapshot()

        /** Replace the cache only from the poller thread. */
        @JvmStatic
        fun update(next: Snapshot) {
            cached = next
        }

        const val GETPROP_ALL = "getprop"

        /**
         * Must never hang. A wedged `getprop` would freeze the Dashboard
         * poll loop, so the process is destroyed after a bounded wait.
         * This is the same rule as Su.kt: stdin closed, timeout enforced.
         */
        fun runGetprop(cmd: String): String = try {
            val p = Runtime.getRuntime().exec(cmd)
            p.outputStream.close()
            val text = p.inputStream.bufferedReader().readText()
            if (!p.waitFor(GETPROP_TIMEOUT_SEC, java.util.concurrent.TimeUnit.SECONDS)) {
                p.destroy()
                // THROW, do not return "". An empty string is what an
                // unconfigured property looks like, so returning it here
                // makes "getprop hung" and "wireless debugging off"
                // render identically -- the exact wrong answer this class
                // exists to prevent.
                throw IllegalStateException("getprop timed out after ${GETPROP_TIMEOUT_SEC}s")
            }
            text
        } catch (e: IllegalStateException) {
            throw e
        } catch (e: Exception) {
            throw IllegalStateException("getprop failed: ${e.javaClass.simpleName}: ${e.message}", e)
        }

        const val GETPROP_TIMEOUT_SEC = 3L
    }
}
