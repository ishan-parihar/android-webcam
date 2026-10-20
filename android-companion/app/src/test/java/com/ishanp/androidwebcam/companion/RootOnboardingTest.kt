package com.ishanp.androidwebcam.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The root-onboarding batch is a security boundary as much as a convenience:
 * it shells out as root, and the obvious "helpful" extra step — enabling
 * unauthenticated network ADB — would expose a pocketed device to any host on
 * the Wi-Fi. These tests pin that exclusion so a later "improvement" cannot
 * add it silently.
 */
class RootOnboardingTest {

    private fun allCommands(steps: List<RootOnboarding.Step>): String =
        (steps.flatMap { it.cmds } + steps.flatMap { it.probe }).joinToString("\n")

    @Test
    fun `batch never enables remote or unauthenticated adb`() {
        val text = allCommands(RootOnboarding.steps())
        // These are the writes that would expose the device; none may appear.
        listOf(
            "adb_enabled",
            "adb_tcp_port",
            "service.adb.tcp.port",
            "adbd.tcp.port",
            "settings put global adb",
            "setprop service.adb",
        ).forEach { forbidden ->
            assertFalse(
                "root batch must not contain '$forbidden':\n$text",
                text.contains(forbidden),
            )
        }
    }

    @Test
    fun `every step is verified by read-back, not by write exit status`() {
        for (step in RootOnboarding.steps()) {
            assertTrue("${step.id} has no read-back probe", step.probe.isNotEmpty())
            // The probe must read the system state back, not re-run the write.
            step.probe.forEach { probe ->
                assertTrue(
                    "${step.id} probe does not read state: $probe",
                    probe.startsWith("dumpsys") || probe.startsWith("appops get") ||
                        probe.startsWith("settings get"),
                )
            }
        }
    }

    @Test
    fun `steps are independent and self-describing`() {
        val steps = RootOnboarding.steps()
        assertTrue("expected at least the Doze + appops fixes", steps.size >= 2)
        assertEquals("step ids must be unique", steps.size, steps.map { it.id }.toSet().size)
        steps.forEach {
            assertTrue("${it.id} missing title", it.title.isNotBlank())
            assertTrue("${it.id} missing rationale", it.why.isNotBlank())
            assertTrue("${it.id} has no command", it.cmds.isNotEmpty())
        }
    }

    @Test
    fun `an unconfirmed oem autostart key is never applied`() {
        // MIUI's autostart key is ROM-private; until verified on a device it
        // must not be scripted, or it would read as "applied" while doing
        // nothing and invalidate the screen-off matrix.
        assertEquals(
            null,
            RootOnboarding.confirmedAutostart,
        )
        assertTrue(
            "no unconfirmed autostart command may be present",
            allCommands(RootOnboarding.steps()).contains("autostart").not(),
        )
    }

    @Test
    fun `runner reports not-applied when a successful write changes nothing`() {
        // The contract that matters: exit 0 from the write means nothing if the
        // read-back shows no change. Driven through the injected `su` seam —
        // under isReturnDefaultValues=true a real exec would not run at all.
        val su: (String) -> Su.Result = { Su.Result(ok = true, out = "", err = "") }
        val results = RootOnboardingRunner.applyAll(RootOnboarding.steps(), su)
        assertEquals(RootOnboarding.steps().size, results.size)
        assertTrue(
            "a write with no verifiable effect must not report applied",
            results.none { it.applied },
        )
    }

    @Test
    fun `a write that exits zero but changes nothing is not applied`() {
        // Distinguishes read-back trust from exit-code trust: the write SUCCEEDS
        // (exit 0, no stderr) and the read-back afterwards is still empty. An
        // implementation that trusted the exit code would report applied here.
        var whitelisted = false
        val su: (String) -> Su.Result = { cmd ->
            if (cmd == "id -u") {
                Su.Result(true, "0", "")
            } else if (cmd.contains("deviceidle whitelist +")) {
                whitelisted = true
                Su.Result(true, "", "")          // write "succeeds"...
            } else if (cmd.startsWith("dumpsys deviceidle whitelist")) {
                // ...but the read-back never reflects it (ROM ignored it).
                Su.Result(true, "", "")
            } else {
                Su.Result(true, "", "")
            }
        }
        val results = RootOnboardingRunner.applyAll(RootOnboarding.steps(), su)
        val step = results.first { it.id == "deviceidle-whitelist" }
        assertFalse(
            "exit 0 with no verifiable effect must not count as applied",
            step.applied,
        )
    }

    @Test
    fun `already-satisfied steps are reported and never written`() {
        // Read-before-write: a fix already in place must not shell root again
        // or mutate state, and must be distinguishable from one we applied.
        val writes = mutableListOf<String>()
        val su: (String) -> Su.Result = { cmd ->
            if (cmd.startsWith("dumpsys deviceidle whitelist")) {
                Su.Result(true, RootOnboarding.PKG, "")
            } else {
                writes += cmd
                Su.Result(true, "", "")
            }
        }
        val results = RootOnboardingRunner.applyAll(RootOnboarding.steps(), su)
        val satisfied = results.first { it.id == "deviceidle-whitelist" }
        assertTrue("whitelisted step should report applied", satisfied.applied)
        assertTrue("should be flagged already-satisfied", satisfied.alreadySatisfied)
        assertTrue(
            "an already-satisfied step must not be written: $writes",
            writes.none { it.contains("deviceidle whitelist +") },
        )
    }

    @Test
    fun `no root shell is reported per-step without aborting the batch`() {
        val su: (String) -> Su.Result = { cmd ->
            if (cmd == "id -u") Su.Result(false, "", "permission denied")
            else Su.Result(true, "", "")
        }
        val results = RootOnboardingRunner.applyAll(RootOnboarding.steps(), su)
        assertEquals(RootOnboarding.steps().size, results.size)
        assertTrue(results.none { it.applied })
        assertTrue(results.all { it.detail.contains("no root shell") })
    }

    @Test
    fun `a verified write is applied and distinct from already-satisfied`() {
        var whitelisted = false
        val su: (String) -> Su.Result = { cmd ->
            if (cmd == "id -u") {
                Su.Result(true, "0", "")
            } else if (cmd.contains("deviceidle whitelist +")) {
                whitelisted = true
                Su.Result(true, "", "")
            } else if (cmd.startsWith("dumpsys deviceidle whitelist")) {
                Su.Result(true, if (whitelisted) RootOnboarding.PKG else "", "")
            } else {
                Su.Result(true, "", "")
            }
        }
        val results = RootOnboardingRunner.applyAll(RootOnboarding.steps(), su)
        val step = results.first { it.id == "deviceidle-whitelist" }
        assertTrue("write should verify", step.applied)
        assertFalse("freshly applied is not already-satisfied", step.alreadySatisfied)
    }

    @Test
    fun `steps stay JVM-safe regardless of manufacturer argument`() {
        // The OEM step is gated on the parameter, never on Build, so passing a
        // real manufacturer must not make steps() touch the Android SDK.
        val steps = RootOnboarding.steps(manufacturer = "Xiaomi")
        assertTrue(steps.isNotEmpty())
        assertTrue(allCommands(steps).contains("autostart").not())
    }
}
