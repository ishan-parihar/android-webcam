package com.ishanp.androidwebcam.companion

import android.util.Log
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Runs a single command as root via `su -c` and captures its output.
 *
 * Every read is deadline-bounded and stdin is closed immediately after exec.
 * That combination is what makes this safe to call from the UI:
 *  - stdin closed  → a `su` that wants a password fails fast instead of
 *    blocking the app waiting for input nobody can type into it;
 *  - timeout       → a `su` that opens an on-screen root *prompt* (Magisk,
 *    KernelSU) still gets a bounded wait, and an unanswered prompt reads as
 *    "no root" rather than freezing the app.
 *
 * Streams are drained on separate threads: a full pipe buffer on either side
 * would otherwise deadlock against waitFor().
 */
object Su {

    data class Result(val ok: Boolean, val out: String, val err: String) {
        /** Both streams, for surfacing to the user when something fails. */
        val all: String get() = (out + "\n" + err).trim()
    }

    private const val TAG = "AndroidWebcam"

    /** Bound on every su invocation, in seconds. */
    const val PROMPT_TIMEOUT_S = 20

    /**
     * True if a root shell is actually available: `id -u` must return 0
     * within [PROMPT_TIMEOUT_S]. A denial or an unanswered prompt is a normal
     * outcome, not an error to propagate.
     */
    fun available(): Boolean {
        val r = run("id -u", PROMPT_TIMEOUT_S)
        return r.ok && r.out.trim() == "0"
    }

    fun run(cmd: String, timeoutS: Int = PROMPT_TIMEOUT_S): Result {
        var proc: Process? = null
        return try {
            proc = Runtime.getRuntime().exec(arrayOf("su", "-c", cmd))
            // Nothing is ever written to su's stdin; close it so a password
            // prompt fails fast rather than holding the timeout.
            runCatching { proc.outputStream.close() }

            val out = AtomicReference<String>()
            val err = AtomicReference<String>()
            val outT = Thread { out.set(proc!!.inputStream.bufferedReader().use { it.readText() }) }
            val errT = Thread { err.set(proc!!.errorStream.bufferedReader().use { it.readText() }) }
            outT.isDaemon = true
            errT.isDaemon = true
            outT.start()
            errT.start()

            if (!proc.waitFor(timeoutS.toLong(), TimeUnit.SECONDS)) {
                proc.destroyForcibly()
                return Result(false, "", "timed out after ${timeoutS}s (root prompt not answered)")
            }
            outT.join(2_000)
            errT.join(2_000)
            Result(
                ok = proc.exitValue() == 0,
                out = out.get().orEmpty().trim(),
                err = err.get().orEmpty().trim(),
            )
        } catch (e: Exception) {
            runCatching { proc?.destroyForcibly() }
            Log.i(TAG, "su '$cmd' failed: ${e.message}")
            Result(false, "", e.message ?: e.toString())
        }
    }
}
