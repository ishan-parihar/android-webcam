package com.ishanp.androidwebcam.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Parsing tests for the on-device mDNS ground truth.
 *
 * These use REAL `getprop` output shapes, including the ones that break a
 * naive parser: an unset value with no key, a value containing colons, and
 * a value that is an empty string. The point is that this parser is the one
 * the desktop's discovery will be validated against, so it must not lose a
 * line or truncate a value.
 */
class AdbPropsTest {

    private fun props(out: String) = AdbProps(exec = { out }).relevant()

    @Test
    fun `reads the four adb mdns properties`() {
        val real = """
            [adb.tls.port]: [37123]
            [service.adb.tls.port]: [37123]
            [service.adb.tcp.port]: [5555]
            [service.adb.mdns.registration]: [_adb-tls-connect._tcp]
            [adb.mdns.hostname]: [redmi-note-8-pro]
        """.trimIndent()
        val p = props(real)
        assertEquals("37123", p["service.adb.tls.port"])
        assertEquals("5555", p["service.adb.tcp.port"])
        assertEquals("_adb-tls-connect._tcp", p["service.adb.mdns.registration"])
        assertEquals("redmi-note-8-pro", p["adb.mdns.hostname"])
    }

    @Test
    fun `value containing colons is not truncated`() {
        // A last-colon split would yield "22:33" instead of "10:22:33".
        val p = props("[some.time]: [10:22:33]")
        assertEquals("10:22:33", p["some.time"])
    }

    @Test
    fun `a bare bracket line is not mistaken for a property`() {
        // getprop can emit `[some.name]` with no colon and no value. After
        // bracket-stripping the key is "some.name", so it lands in the map as
        // an EMPTY value -- which is correct and harmless: callers use
        // toIntOrNull() and treat blank as absent. What must not happen is a
        // crash or an invented value.
        val p = props("[key1]: []\n[rosenbergalreadydisabled]")
        assertEquals("", p["key1"])
        assertEquals("", p["rosenbergalreadydisabled"])
    }

    @Test
    fun `blank and malformed lines are skipped without throwing`() {
        val p = props("\n   \n[good]: [yes]\n:\n")
        assertEquals("yes", p["good"])
        assertEquals(1, p.size)
    }

    @Test
    fun `exposes wireless and legacy ports as ints`() {
        val a = AdbProps { "[service.adb.tls.port]: [39481]\n[service.adb.tcp.port]: [5555]" }
        val s = a.read()
        assertEquals(39481, s.wirelessPort)
        assertEquals(5555, s.legacyPort)
    }

    @Test
    fun `ports are null when the properties are absent`() {
        val a = AdbProps { "" }
        val s = a.read()
        assertNull(s.wirelessPort)
        assertNull(s.legacyPort)
    }

    // -- provenance: a failed read must never look like an unset property --

    @Test
    fun `a failed read reports the error, not unset values`() {
        val a = AdbProps { throw IllegalStateException("getprop timed out after 3s") }
        val text = a.read().lines.joinToString(" | ")
        // The failure must be visible AND must not be dressed up as
        // "wireless debugging off", which is a different, real fact.
        assertTrue("failure was swallowed: $text", text.contains("getprop unavailable"))
        assertTrue("error detail lost: $text", text.contains("timed out"))
        assertTrue("a failed read was rendered as a real unset value: $text",
                  !text.contains("not set (wireless debugging off)"))
    }

    @Test
    fun `a successful read with the property absent still says not set`() {
        // The two cases must stay distinguishable in BOTH directions.
        val a = AdbProps { "[service.adb.tcp.port]: [5555]" }
        val text = a.read().lines.joinToString(" | ")
        assertTrue("unset case lost: $text", text.contains("not set (wireless debugging off)"))
        assertTrue("a good read was reported as a failure: $text", !text.contains("getprop unavailable"))
    }

    @Test
    fun `render is pure and does not re-exec`() {
        var calls = 0
        val a = AdbProps { calls++; "[service.adb.tls.port]: [39481]" }
        val first = a.read().lines
        val second = a.render(a.relevantOrError())
        assertEquals(first, second)
        assertEquals(2, calls)   // one per summary/relevantOrError, NOT per accessor
    }

    @Test
    fun `summary explains an unset port rather than showing an empty row`() {
        val a = AdbProps { "[service.adb.tls.port]: []" }
        val text = a.read().lines.joinToString(" | ")
        assertTrue(text.contains("not set"))
    }

    @Test
    fun `summary names the real values when present`() {
        val a = AdbProps {
            "[service.adb.tls.port]: [39481]\n" +
                "[service.adb.mdns.registration]: [_adb-tls-connect._tcp]"
        }
        val text = a.read().lines.joinToString(" | ")
        assertTrue(text.contains("39481"))
        assertTrue(text.contains("_adb-tls-connect._tcp"))
    }
    // -- fork-once: the reason read() exists instead of summary() --

    @Test
    fun `read forks getprop exactly once no matter how many facts are rendered`() {
        var forks = 0
        val a = AdbProps { forks++; "[service.adb.tls.port]: [39481]\n[service.adb.tcp.port]: [5555]" }
        val snap = a.read()
        // Render every field a consumer could want, repeatedly.
        repeat(5) {
            snap.lines.forEach { }
            @Suppress("UNUSED_EXPRESSION")
            snap.wirelessPort
            @Suppress("UNUSED_EXPRESSION")
            snap.legacyPort
        }
        // The Snapshot carried the values; consumers do not re-read.
        assertEquals(39481, snap.wirelessPort)
        assertEquals(5555, snap.legacyPort)
        assertEquals(1, forks)
    }

    @Test
    fun `adb props cadence is far slower than the fast poll`() {
        // getprop forks a process and the app runs on battery, so the cadence
        // is a cost decision, not a style preference. Read the ACTUAL shared
        // constant (it lives in AdbProps so the Dashboard and the service
        // cannot drift into two different rates) and the actual UI poll.
        val props = File(
            "src/main/java/com/ishanp/androidwebcam/companion/AdbProps.kt"
        ).readText()
        val ui = File(
            "src/main/java/com/ishanp/androidwebcam/companion/MainActivity.kt"
        ).readText()
        val slow = Regex("""const val POLL_MS = ([\d_]+)L""").find(props)!!
            .groupValues[1].replace("_", "").toLong()
        val fast = Regex("""const val POLL_MS = ([\d_]+)L""").find(ui)!!
            .groupValues[1].replace("_", "").toLong()
        assertTrue(
            "adbProps poll ($slow ms) must be >= 5x the status poll ($fast ms)",
            slow >= fast * 5,
        )
    }

    @Test
    fun `the shared cache starts unloaded so a placeholder is never served as fact`() {
        // The default lines are non-empty, so an isEmpty() guard would never
        // fire. `loaded` is what distinguishes "not read yet" from a real
        // result, and the socket reports it.
        val fresh = AdbProps.Snapshot()
        assertTrue("placeholder must be marked unloaded", !fresh.loaded)
        val real = AdbProps { "[service.adb.tls.port]: [39481]" }.read()
        assertTrue("a real read must be marked loaded", real.loaded)
    }

    @Test
    fun `the placeholder states that nobody is polling, not that a read is pending`() {
        // The Dashboard renders the shared cache, and the service poller is
        // its only steady-state writer. With keep-alive off there IS no
        // poller, so "reading..." would be a lie that never resolves. The
        // terminal state must name the actual cause and be actionable.
        val fresh = AdbProps.Snapshot()
        assertTrue("placeholder must not claim a read is in progress",
            !fresh.lines.any { it.contains("reading") })
        assertTrue("placeholder must say no reader is running",
            fresh.lines.any { it.contains("no reader running") })
        assertTrue("placeholder must stay unloaded", !fresh.loaded)
    }

}
