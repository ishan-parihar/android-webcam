package com.ishanp.androidwebcam.companion

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Source-level guards for thread lifecycle, which no JVM test can observe.
 *
 * Removing stopAdbPropsPolling() from the teardown paths compiles cleanly and
 * passes every behavioural test in this suite: a leaked daemon thread is a
 * RUNTIME property, and a unit test that never starts an Android service will
 * never see it. Asserting against the source is the honest guard available
 * without an instrumented device.
 *
 * It is deliberately narrow: it checks the teardown CALL SITES exist and that
 * the helper both interrupts and nulls. It does not prove the code is correct
 * at runtime -- that still needs the device.
 */
class KeepAliveServiceTest {

    private val src = File("src/main/java/com/ishanp/androidwebcam/companion/KeepAliveService.kt").readText()

    @Test
    fun `both teardown paths stop the poller`() {
        // onDestroy AND the maybeStop path reached via onSessionExpired.
        // Missing either leaks a getprop fork per service instance, and the
        // stale non-null handle makes a recreated service skip its own start.
        assertTrue(
            "onDestroy must stop the poller",
            Regex("""override fun onDestroy\(\)[\s\S]{0,300}?stopAdbPropsPolling\(\)""").containsMatchIn(src),
        )
        assertTrue(
            "maybeStop must stop the poller",
            Regex("""fun maybeStop\(\)[\s\S]{0,400}?stopAdbPropsPolling\(\)""").containsMatchIn(src),
        )
    }

    @Test
    fun `the stop helper interrupts AND nulls the handle`() {
        // Nulling matters as much as interrupting: the start guard is a
        // non-null check, so a stale handle blocks the next start.
        val body = Regex("""fun stopAdbPropsPolling\(\)[\s\S]{0,300}?\n    \}""").find(src)
        assertTrue("stopAdbPropsPolling() not found", body != null)
        val b = body!!.value
        assertTrue("must interrupt the thread", b.contains("interrupt()"))
        assertTrue("must null the handle so a new instance can start", b.contains("adbPropsThread = null"))
    }

    @Test
    fun `the poller is started in onCreate, not lazily per request`() {
        // A lazy start on the accept thread would fork getprop inside
        // statusJson, stalling the next real request behind a 3s wait.
        assertTrue(
            "poller must start in onCreate",
            Regex("""override fun onCreate\(\)[\s\S]{0,800}?startAdbPropsPolling\(\)""").containsMatchIn(src),
        )
        // Anchor BOTH ends and keep the window bounded. A trailing-only
        // `[\s\S]*?` never matched its end anchor here, so the assertion was
        // effectively scanning an unbounded span -- it would have widened
        // silently the moment anything earlier in the file called
        // AdbProps().read(), and a guard that quietly stops guarding is worse
        // than no guard.
        val statusBody = Regex(
            """fun statusJson\s*\([^)]*\)\s*:\s*JSONObject\s*\{[\s\S]*?\n    \}"""
        ).find(src)
        assertTrue("statusJson body not found (pattern drifted from the source)", statusBody != null)
        assertTrue(
            "statusJson must NOT fork getprop on the accept thread",
            !statusBody!!.value.contains("AdbProps().read"),
        )
    }
}
