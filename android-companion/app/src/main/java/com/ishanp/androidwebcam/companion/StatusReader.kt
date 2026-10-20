package com.ishanp.androidwebcam.companion

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.BatteryManager
import android.os.PowerManager
import android.provider.Settings
import java.net.NetworkInterface

/**
 * Device status for the Dashboard and the `status` command.
 *
 * Everything here is READ-ONLY and permission-free:
 *  - ADB state: Settings.Global.ADB_ENABLED is readable by any app.
 *  - IPs: NetworkInterface enumeration (no WifiManager/location permission).
 *  - Battery: sticky ACTION_BATTERY_CHANGED broadcast.
 *  - Battery-optimization exemption: PowerManager.isIgnoringBatteryOptimizations.
 *
 * The app deliberately CANNOT toggle USB/wireless debugging or read the
 * wireless-debugging pairing code — those are system/signature-only. It
 * detects and guides (deep links) instead; the desktop automates via adb.
 */
class StatusReader(private val context: Context) {

    fun adbEnabled(): Boolean =
        runCatching {
            Settings.Global.getInt(context.contentResolver, Settings.Global.ADB_ENABLED, 0) == 1
        }.getOrDefault(false)

    fun ips(): List<String> =
        runCatching {
            NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { nic -> nic.inetAddresses.toList() }
                .mapNotNull { addr ->
                    val s = addr.hostAddress ?: return@mapNotNull null
                    if (s.contains(':')) null else s   // skip IPv6 for the adb target
                }
                .distinct()
        }.getOrDefault(emptyList())

    fun battery(): Pair<Int, Boolean> {
        val intent: Intent? = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val pct = if (level >= 0 && scale > 0) level * 100 / scale else -1
        val charging = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ==
            BatteryManager.BATTERY_STATUS_CHARGING
        return pct to charging
    }

    fun ignoringBatteryOptimizations(): Boolean {
        val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return false
        return pm.isIgnoringBatteryOptimizations(context.packageName)
    }

    fun model(): String = "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}".trim()

    fun sdkInt(): Int = android.os.Build.VERSION.SDK_INT

    /** Intent for the system "ignore battery optimizations" dialog. */
    fun batteryExemptionIntent(): Intent =
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
            data = Uri.parse("package:${context.packageName}")
        }

    fun developerSettingsIntent(): Intent =
        Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)

    fun wirelessDebuggingIntent(): Intent? =
        // The platform constant Settings.ACTION_WIRELESS_DEBUGGING_SETTINGS is
        // @hide; the action string itself is public and documented on AOSP.
        if (sdkInt() >= 30) Intent("android.settings.WIRELESS_DEBUGGING_SETTINGS") else null

    fun isBackgroundRestricted(): Boolean {
        // Informational only: OEM battery managers (MIUI etc.) can still freeze
        // the service despite the exemption; the UI surfaces this as a warning.
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager
            ?: return false
        return am.isBackgroundRestricted
    }
}
