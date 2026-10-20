package com.ishanp.androidwebcam.companion

import android.os.SystemClock
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Session watchdog — the phone-side half of the "no wake-lock leak" contract
 * (docs/companion-protocol.md, v2).
 *
 * The Omarchy bar widget stops scrcpy with `pkill -f 'scrcpy.*camera.*v4l2-sink'`,
 * which bypasses every graceful path on the desktop. If the phone only released
 * its wake lock when told, that stop would leak a held wakelock with nothing
 * streaming. So the phone ends the session itself when heartbeats stop.
 *
 * Timing uses SystemClock.elapsedRealtime (monotonic, immune to wall-clock
 * changes and timezone shifts), not System.currentTimeMillis.
 */
class Watchdog(
    private val onExpire: () -> Unit,
) {
    private val running = AtomicBoolean(false)
    private val lastBeatAt = AtomicLong(0L)
    private var timeoutMs = 45_000L
    private var thread: Thread? = null

    /** Starts (or re-arms) the watchdog with a clamped timeout. */
    fun arm(requestedTimeoutSec: Long) {
        timeoutMs = requestedTimeoutSec.coerceIn(
            Protocol.WATCHDOG_MIN_SEC.toLong(),
            Protocol.WATCHDOG_MAX_SEC.toLong(),
        ) * 1000L
        beat()
        if (running.compareAndSet(false, true)) {
            thread = Thread({ loop() }, "companion-watchdog").apply {
                isDaemon = true
                start()
            }
        }
    }

    fun beat() {
        lastBeatAt.set(SystemClock.elapsedRealtime())
    }

    fun disarm() {
        running.set(false)
        thread?.interrupt()
        thread = null
    }

    val isArmed: Boolean get() = running.get()

    fun msSinceBeat(): Long =
        if (lastBeatAt.get() == 0L) -1L else SystemClock.elapsedRealtime() - lastBeatAt.get()

    private fun loop() {
        while (running.get()) {
            try {
                Thread.sleep(1000L)
            } catch (e: InterruptedException) {
                return
            }
            val since = SystemClock.elapsedRealtime() - lastBeatAt.get()
            if (running.get() && since > timeoutMs) {
                // Expired: disarm first so onExpire can't re-enter the loop.
                running.set(false)
                lastBeatAt.set(0L)
                onExpire()
                return
            }
        }
    }
}
