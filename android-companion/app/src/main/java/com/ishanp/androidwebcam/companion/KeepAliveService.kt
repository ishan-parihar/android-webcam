package com.ishanp.androidwebcam.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import org.json.JSONObject

/**
 * Foreground service holding a PARTIAL_WAKE_LOCK so the desktop's scrcpy
 * camera stream survives the display being off (CPU awake, screen dark).
 *
 * Two independent reasons to run (docs/companion-protocol.md v2):
 *   manual  — the user toggled keep-alive on the Dashboard.
 *   session — the desktop sent session.begin and is heart-beating.
 * The session watchdog disarms the session reason if heartbeats stop (e.g.
 * the panel pkills scrcpy), so a wake lock never outlives the stream.
 */
class KeepAliveService : Service() {

    lateinit var store: ConfigStore; private set
    private lateinit var statusReader: StatusReader
    private lateinit var cameraCatalog: CameraCatalog
    private lateinit var server: CompanionServer
    private lateinit var watchdog: Watchdog

    private var powerManager: PowerManager? = null
    private var wakeLock: PowerManager.WakeLock? = null

    @Volatile private var sessionActive = false
    @Volatile private var manualKeepAlive = false
    @Volatile private var foreground = false

    override fun onCreate() {
        super.onCreate()
        store = ConfigStore(this)
        statusReader = StatusReader(this)
        cameraCatalog = CameraCatalog(getSystemService(Context.CAMERA_SERVICE) as CameraManager)
        powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        manualKeepAlive = store.manualKeepAlive
        watchdog = Watchdog { onSessionExpired() }
        server = CompanionServer(this)
        // Start the property poller here, not lazily: statusJson runs on the
        // accept thread and must not block on a process fork.
        startAdbPropsPolling()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SET_MANUAL -> setManualKeepAlive(intent.getBooleanExtra(EXTRA_VALUE, false))
            ACTION_START -> if (keepAliveShouldRun()) ensureRunning()
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        watchdog.disarm()
        stopAdbPropsPolling()
        server.stop()
        releaseWakeLock()
        super.onDestroy()
    }

    /**
     * Stop the poller and clear the handle.
     *
     * Both halves matter. Interrupting alone leaves a non-null field, and the
     * start guard is a non-null check -- so the next service instance would
     * see a "running" poller, skip starting one, and serve a cache that
     * nothing is updating any more.
     */
    private fun stopAdbPropsPolling() {
        adbPropsThread?.interrupt()
        adbPropsThread = null
    }

    // -- wake lock / foreground lifecycle ------------------------------------
    private fun ensureRunning() {
        acquireWakeLock()
        if (!foreground) {
            startForeground(NOTIF_ID, buildNotification(source()))
            foreground = true
        }
        server.start()
    }

    private fun maybeStop() {
        if (!keepAliveShouldRun()) {
            watchdog.disarm()
            // The poller must die with the service, or it outlives it: the
            // guard below is a non-null check, so a thread left running from
            // a previous service instance would block a fresh one from ever
            // starting, and the cache would silently freeze at its last value.
            stopAdbPropsPolling()
            server.stop()
            releaseWakeLock()
            if (foreground) {
                stopForeground(STOP_FOREGROUND_REMOVE)
                foreground = false
            }
            stopSelf()
        }
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        wakeLock = powerManager?.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK, "webcam-keepalive:companion"
        )?.apply {
            setReferenceCounted(false)
            // Timeout is the watchdog's job; the lock is always released in
            // pairs with the service. A generous ceiling is a safety net.
            acquire(60 * 60 * 1000L)
        }
    }

    private fun releaseWakeLock() {
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        wakeLock = null
    }

    // -- state ---------------------------------------------------------------
    fun keepAliveShouldRun(): Boolean = sessionActive || manualKeepAlive

    private fun source(): String = when {
        sessionActive -> "session"
        manualKeepAlive -> "manual"
        else -> "none"
    }

    fun setManualKeepAlive(on: Boolean) {
        manualKeepAlive = on
        store.manualKeepAlive = on
        if (on) ensureRunning() else maybeStop()
    }

    fun onSessionBegin(heartbeatIntervalSec: Long, watchdogTimeoutSec: Long) {
        sessionActive = true
        // Arm with max(negotiated timeout, 4 missed beats) so a host that
        // negotiates a timeout shorter than its own heartbeat cadence cannot
        // trip the watchdog against a healthy session.
        val effective = maxOf(watchdogTimeoutSec, heartbeatIntervalSec * 4)
        watchdog.arm(effective)
        ensureRunning()
    }

    fun onHeartbeat() {
        watchdog.beat()
    }

    fun onSessionEnd() {
        sessionActive = false
        watchdog.disarm()
        maybeStop()
    }

    /** Watchdog fired: heartbeats stopped (host killed or app backgrounded
     *  hard) → release the wake lock ourselves. This is what makes the
     *  bar-widget pkill stop path leak-free. */
    private fun onSessionExpired() {
        Log.i(TAG, "session watchdog expired — releasing wake lock")
        sessionActive = false
        maybeStop()
    }

    fun cameras(): List<CameraInfo> = cameraCatalog.list()

    // -- status command ------------------------------------------------------
    /**
     * Cached adb/mDNS ground truth, readable by the desktop over the socket.
     *
     * Why cached and in the service rather than in the Activity: the facts
     * the desktop needs (the wireless port, the mDNS registration and
     * hostname) live in Compose state, which dies with the Activity. The
     * device is normally screen-off with the service running, so a UI-only
     * value is missing exactly when the desktop needs it, and the user would
     * have to read the screen and transcribe it.
     *
     * getprop forks a process, so this is refreshed on a timer rather than
     * per request, and a failed read is cached WITH ITS REASON -- serving a
     * blank would render identically to "nothing advertised", which is the
     * fabricated answer this whole path exists to avoid.
     */
    private var adbPropsThread: Thread? = null

    private fun startAdbPropsPolling() {
        if (adbPropsThread != null) return
        adbPropsThread = Thread({
            while (true) {
                try {
                    AdbProps().readAndPublish()
                } catch (e: Exception) {
                    // Publish the FAILURE, not a blank. A blank reads
                    // identically to "nothing advertised".
                    AdbProps.update(
                        AdbProps.Snapshot(
                            lines = listOf("⚠ getprop unavailable: ${e.message}"),
                            loaded = true,
                        )
                    )
                }
                try {
                    Thread.sleep(AdbProps.POLL_MS)
                } catch (e: InterruptedException) {
                    return@Thread
                }
            }
        }, "adb-props").apply {
            isDaemon = true
            start()
        }
    }

    fun statusJson(cmd: String): JSONObject {
        val (level, charging) = statusReader.battery()
        // NEVER fork getprop here. This runs on the CompanionServer accept
        // thread, which is strictly sequential: a 3s getprop would stall the
        // next real request (a ping) behind it. The poller is started in
        // onCreate and owns every read; this only reads the cached value.
        val ka = JSONObject()
            .put("running", keepAliveShouldRun())
            .put("wakelock_held", wakeLock?.isHeld == true)
            .put("source", source())
        val session = JSONObject()
            .put("active", sessionActive)
            .put("last_heartbeat_ms_ago", if (sessionActive) watchdog.msSinceBeat() else JSONObject.NULL)
        val status = JSONObject()
            .put("cmd", cmd).put("ok", true).put("proto", Protocol.PROTO)
            .put("adb_enabled", statusReader.adbEnabled())
            .put("model", statusReader.model())
            .put("sdk_int", statusReader.sdkInt())
            .put("ips", org.json.JSONArray(statusReader.ips()))
            .put("battery", JSONObject().put("level", level).put("charging", charging))
            .put("ignoring_battery_optimizations", statusReader.ignoringBatteryOptimizations())
                .put("adb_mdns", JSONObject()
                    .put("wireless_port", AdbProps.cached.wirelessPort ?: JSONObject.NULL)
                    .put("legacy_port", AdbProps.cached.legacyPort ?: JSONObject.NULL)
                    .put("loaded", AdbProps.cached.loaded)
                    .put("lines", org.json.JSONArray(AdbProps.cached.lines))
                )
            .put("keepalive", ka)
            .put("session", session)
            .put("config", store.getConfig().toJson())
        return status
    }

    private fun buildNotification(src: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Webcam keep-alive", NotificationManager.IMPORTANCE_LOW)
                    .apply { setShowBadge(false) }
            )
        }
        val b = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Webcam keep-alive active")
            .setContentText("Holding CPU awake for the camera stream ($src)")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
        return b.build()
    }

    companion object {
        const val TAG = "KeepAlive"
        const val ACTION_SET_MANUAL = "com.ishanp.companion.SET_MANUAL"
        const val ACTION_START = "com.ishanp.companion.START"
        const val EXTRA_VALUE = "value"
        private const val CHANNEL_ID = "keepalive"
        private const val NOTIF_ID = 42

        fun setManual(context: Context, on: Boolean) {
            context.startForegroundService(
                Intent(context, KeepAliveService::class.java)
                    .setAction(ACTION_SET_MANUAL).putExtra(EXTRA_VALUE, on)
            )
        }
    }
}
