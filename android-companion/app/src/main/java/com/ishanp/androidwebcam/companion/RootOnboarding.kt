package com.ishanp.androidwebcam.companion

/**
 * Root onboarding: the power-management settings that stop MIUI/HyperOS from
 * killing the keep-alive foreground service when the screen is off.
 *
 * Everything here is a *power-management* fix. Nothing in this file touches
 * ADB, networking, or the pairing-port settings, and that is deliberate:
 *
 *  - `adb_tcp_port 5555` / `settings put global adb_enabled 1` open
 *    *unauthenticated* network ADB — any host on the same Wi-Fi gets a shell.
 *    A device carried in a pocket must not have that on by default, and it is
 *    not a power-management fix, so it is not here and must not be added.
 *  - Android 11+ already has Wireless debugging, which is pairing-authenticated
 *    and needs none of these settings. The app points at it instead.
 *
 * So the batch is limited to whitelisting this app in the OEM battery manager.
 * Each step is independently confirmed and independently revertible, and the
 * result is read back from the system rather than assumed from exit status —
 * an OEM ROM may ignore the write, and a silent no-op here looks exactly like
 * "the service was killed for no reason" later.
 */
object RootOnboarding {

    /**
     * One power-management fix. [cmds] are run in order via `su -c`; success is
     * decided by the read-back probe, not by exit status.
     */
    data class Step(
        val id: String,
        val title: String,
        val why: String,
        val cmds: List<String>,
        val probe: List<String>,
        /** Whether the probe's output counts as "applied". */
        val appliedWhen: (String) -> Boolean,
    )

    private fun deviceWhiteList(): String = "deviceidle whitelist +$PKG"

    const val PKG = "com.ishanp.androidwebcam.companion"

    /**
     * The batch, in dependency order. Deliberately excludes anything that
     * enables remote ADB — see the file header.
     *
     * [manufacturer] is a parameter rather than a `Build.MANUFACTURER` read so
     * this is pure and JVM-testable: the OEM step is gated on it, and touching
     * `Build` here would throw on the unit-test JVM the moment the OEM key is
     * ever confirmed. The UI passes the real value; tests pass null.
     */
    fun steps(manufacturer: String? = null): List<Step> = buildList {
        add(
            Step(
                id = "deviceidle-whitelist",
                title = "Whitelist from Doze",
                why = "Doze suspends the app between maintenance windows; a foreground " +
                    "service with a wake lock is still frozen without this.",
                cmds = listOf(deviceWhiteList()),
                probe = listOf("dumpsys deviceidle whitelist | grep -w $PKG"),
                appliedWhen = { it.isNotBlank() },
            )
        )
        add(
            Step(
                id = "appops-run-in-background",
                title = "Allow background start",
                why = "Run-anyway-on-background: Android blocks a background FGS start " +
                    "for apps MIUI considers idle.",
                cmds = listOf("appops set $PKG RUN_ANY_IN_BACKGROUND allow"),
                probe = listOf("appops get $PKG RUN_ANY_IN_BACKGROUND"),
                appliedWhen = { it.contains("allow", ignoreCase = true) },
            )
        )
        oemAutostartStep(manufacturer)?.let { add(it) }
    }

    /**
     * MIUI/HyperOS keeps an autostart allow-list that is separate from Doze and
     * from `appops`, and the service is killed minutes after the screen goes
     * off without it. This is the single most important setting for the
     * screen-off goal.
     *
     * It is DELIBERATELY NOT automated here. The key is ROM-private and
     * unversioned, and a write to a key that does not exist on a given build
     * either throws or silently no-ops while still reading "applied" — which
     * would invalidate the whole matrix. Until the real key is confirmed
     * against a device, [confirmedAutostart] is null and the step is simply
     * absent, so the batch is unaffected. Once it is filled in, [steps] must
     * be given the real manufacturer or the OEM step would silently vanish.
     *
     * To confirm on a device:
     *   adb shell settings list secure | grep -i auto
     *   adb shell dumpsys package com.miui.securitycenter | grep -i auto
     */
    private fun oemAutostartStep(manufacturer: String?): Step? {
        // Read the manufacturer from the parameter, never from Build, so this
        // stays JVM-testable with no Android framework and no Robolectric.
        if (manufacturer?.lowercase() !in setOf("xiaomi", "redmi", "poco")) return null
        val confirmed = confirmedAutostart ?: return null
        return Step(
            id = "oem-autostart",
            title = "OEM autostart allow",
            why = "MIUI/HyperOS autostart allow-list; without it the service is " +
                "killed shortly after the screen goes off.",
            cmds = listOf(confirmed.write),
            probe = listOf(confirmed.probe),
            appliedWhen = { it.isNotBlank() },
        )
    }

    /**
     * A verified autostart key/probe pair, or null while unconfirmed. Filled in
     * only against a real device — see [oemAutostartStep].
     */
    data class AutostartKey(val write: String, val probe: String)

    val confirmedAutostart: AutostartKey? = null
}
