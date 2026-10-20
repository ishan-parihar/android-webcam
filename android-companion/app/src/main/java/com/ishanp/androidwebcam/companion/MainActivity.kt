package com.ishanp.androidwebcam.companion

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.selection.selectable
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import androidx.compose.runtime.rememberCoroutineScope
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {
    private val notifPerm =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        setContent { App() }
    }
}

private const val POLL_MS = 2000L

private data class Snapshot(
    val adbEnabled: Boolean,
    val model: String,
    val sdk: Int,
    val ips: List<String>,
    val battery: Int,
    val charging: Boolean,
    val exempt: Boolean,
    val keepAlive: Boolean,
    val keepSource: String,
    val sessionActive: Boolean,
    val manual: Boolean,
    val cfg: CompanionConfig,
    val cameras: List<CameraInfo>,
    val socketBound: Boolean = false,
    val error: String? = null,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun App() {
    val context = LocalContext.current
    val activity = context as? Activity
    val store = remember { ConfigStore(context) }
    val statusReader = remember { StatusReader(context) }
    val catalog = remember {
        CameraCatalog(context.getSystemService(android.content.Context.CAMERA_SERVICE) as android.hardware.camera2.CameraManager)
    }

    var snap by remember { mutableStateOf<Snapshot?>(null) }
    var tab by remember { mutableIntStateOf(0) }
    var manual by remember { mutableStateOf(store.manualKeepAlive) }
    var lastSync by remember { mutableStateOf("never") }
    var rootResults by remember { mutableStateOf<List<StepResult>?>(null) }
    var rootBusy by remember { mutableStateOf(false) }

    /**
     * Build a snapshot OFF the main thread.
     *
     * `manualNow` and `prev` are passed in rather than read from the closure:
     * this runs on Dispatchers.IO, and reading Compose state from another
     * thread is a data race -- the poll could observe a half-updated object or
     * a torn `snap`. The caller captures both on Main at the moment the tick
     * starts, and the result is assigned back in one step.
     */
    fun buildSnapshot(manualNow: Boolean, prev: Snapshot?): Snapshot {
        val (level, charging) = statusReader.battery()
        val cams = runCatching { catalog.list() }.getOrDefault(emptyList())
        return Snapshot(
            adbEnabled = statusReader.adbEnabled(),
            model = statusReader.model(),
            sdk = statusReader.sdkInt(),
            ips = statusReader.ips(),
            battery = level,
            charging = charging,
            exempt = statusReader.ignoringBatteryOptimizations(),
            keepAlive = manualNow || (prev?.sessionActive ?: false),
            keepSource = if (prev?.sessionActive == true) "session" else if (manualNow) "manual" else "none",
            sessionActive = prev?.sessionActive ?: false,
            manual = manualNow,
            cfg = store.getConfig(),
            cameras = cams,
            socketBound = CompanionServer.isSocketBound(),
        )
    }

    var adbProps by remember { mutableStateOf(AdbProps.Snapshot()) }
    val scope = rememberCoroutineScope()

    // getprop is a process fork, and this app's whole job is to survive on
    // battery. 15s, and independent of the fast poll.
    /**
     * No read here on purpose.
     *
     * The service poller is the ONLY writer of the shared cache, and it runs
     * whether or not the UI is foregrounded -- which is the point, because the
     * phone is normally screen-off. If the Dashboard also read getprop, there
     * would be two writers, two fork rates, and two possible answers to "what
     * port is the phone advertising".
     */

    /**
     * One lifecycle-gated effect, two cadences.
     *
     * Both loops used to be separate, and the getprop one was NOT
     * lifecycle-gated. A stopped Activity still retains its composition, so
     * that loop kept forking getprop every 15s in the background -- which is
     * the exact thing the cadence change was meant to stop, and the more
     * expensive half of it besides. Gating the 2s poll alone would have left
     * the battery regression in place.
     *
     * Reading immediately on STARTED (not after a delay) means the card shows
     * a real result the moment the Dashboard appears, rather than an empty
     * list that reads as "nothing advertised".
     */
    // Renders the shared cache, filled by the service poller. This effect
    // only re-reads that cheap value on the slow cadence; it must never fork
    // getprop while the Activity is stopped, which is what an ungated
    // LaunchedEffect(Unit) here would do -- a stopped Activity still retains
    // its composition.
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(lifecycleOwner) {
        lifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                // One-shot bootstrap ONLY while the cache is empty. The
                // service poller remains the steady-state writer; this just
                // means the card is never permanently blank for a user who
                // has not turned keep-alive on yet. It is not a competing
                // poll loop -- it runs at most once per STARTED, and only
                // while `loaded` is false.
                if (!AdbProps.cached.loaded) {
                    adbProps = withContext(Dispatchers.IO) { AdbProps().readAndPublish() }
                } else {
                    adbProps = AdbProps.cached
                }
                val nextPropsAt = SystemClock.elapsedRealtime() + AdbProps.POLL_MS
                // ...and the cheap status poll at the fast cadence, reusing
                // the props read above rather than forking a second time.
                while (SystemClock.elapsedRealtime() < nextPropsAt) {
                    val manualNow = manual
                    val prev = snap
                    snap = withContext(Dispatchers.IO) { buildSnapshot(manualNow, prev) }
                    delay(POLL_MS)
                }
            }
        }
    }

    /** Immediate one-shot refresh from a user action; same off-thread build. */
    fun refreshNow() {
        val manualNow = manual
        val prev = snap
        scope.launch {
            val next = withContext(Dispatchers.IO) { buildSnapshot(manualNow, prev) }
            snap = next
        }
    }

    // The work runs on Dispatchers.IO, not the main thread. These reads
    // block: CameraCatalog.list() talks to CameraManager, battery() waits on
    // a sticky broadcast, and getprop is a process fork with a 3s bounded
    // wait. On Main that is a visible UI stall, and a wedged getprop would
    // freeze the app for 3s at a time. The snapshot is rebuilt off-thread and
    // assigned in one step, so Compose sees one consistent value rather than
    // a half-updated object.
    fun setConfig(cfg: CompanionConfig) {
        val cams = snap?.cameras ?: emptyList()
        val problems = store.setConfig(cfg, cams)
        if (problems.isNotEmpty()) {
            Toast.makeText(context, problems.first(), Toast.LENGTH_LONG).show()
        }
    }

    Scaffold(topBar = { TopAppBar(title = { Text("Webcam Companion") }) }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            Row(Modifier.fillMaxWidth().padding(8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("Dashboard", "Cameras", "Settings").forEachIndexed { i, label ->
                    FilterChip(selected = tab == i, onClick = { tab = i }, label = { Text(label) })
                }
            }
            when (tab) {
                0 -> Dashboard(
                    snap, manual,
                    adbProps = adbProps,
                    onToggleManual = {
                        manual = it
                        KeepAliveService.setManual(context, it)
                    },
                    onExempt = {
                        val i = statusReader.batteryExemptionIntent()
                        runCatching { activity?.startActivity(i) }
                            .onFailure { Toast.makeText(context, "no settings app", Toast.LENGTH_LONG).show() }
                    },
                    onDevSettings = {
                        runCatching { activity?.startActivity(statusReader.developerSettingsIntent()) }
                    },
                    onWireless = {
                        val i = statusReader.wirelessDebuggingIntent()
                        if (i == null) Toast.makeText(context, "needs Android 11+", Toast.LENGTH_LONG).show()
                        else runCatching { activity?.startActivity(i) }
                    },
                    rootResults = rootResults,
                    rootBusy = rootBusy,
                    onApplyRootFixes = {
                        if (rootBusy) return@Dashboard
                        rootBusy = true
                        Thread {
                            val res = RootOnboardingRunner.applyAll(
                                RootOnboarding.steps(android.os.Build.MANUFACTURER)
                            )
                            activity?.runOnUiThread {
                                rootResults = res
                                rootBusy = false
                                Toast.makeText(
                                    context,
                                    "Root fixes: ${res.count { it.applied }}/${res.size} verified",
                                    Toast.LENGTH_LONG,
                                ).show()
                            }
                        }.start()
                    },
                )
                1 -> {
                    val s = snap ?: return@Column
                    val target = s.cfg.cameraId ?: s.cameras.firstOrNull { it.facing == "back" }?.id
                    val cam = s.cameras.firstOrNull { it.id == target } ?: s.cameras.firstOrNull()
                    if (cam == null) {
                        Text("No cameras reported", Modifier.padding(16.dp))
                    } else {
                        CameraScreen(
                            s.cameras, cam, s.cfg,
                            onSelectCamera = { setConfig(s.cfg.copy(cameraId = it.id)) },
                            onSelectSize = { setConfig(s.cfg.copy(cameraId = cam.id, size = it)) },
                            onSelectFps = { setConfig(s.cfg.copy(fps = it)) },
                            onToggleTorch = { setConfig(s.cfg.copy(torch = it)) },
                        )
                    }
                }
                else -> SettingsScreen(
                    snap, lastSync,
                    onSync = {
                        lastSync = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
                        refreshNow()
                    },
                    onClear = {
                        store.setConfig(CompanionConfig(fps = 30), snap?.cameras ?: emptyList())
                        lastSync = "reset " + lastSync
                    },
                )
            }
        }
    }
}

@Composable
private fun Dashboard(
    s: Snapshot?, manual: Boolean,
    adbProps: AdbProps.Snapshot = AdbProps.Snapshot(),
    onToggleManual: (Boolean) -> Unit,
    onExempt: () -> Unit,
    onDevSettings: () -> Unit,
    onWireless: () -> Unit,
    rootResults: List<StepResult>?,
    rootBusy: Boolean,
    onApplyRootFixes: () -> Unit,
) {
    if (s == null) { Text("Reading…", Modifier.padding(16.dp)); return }
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item {
            // Ground truth for the desktop's mDNS discovery, which has never
            // been exercised against a real advertisement. If the desktop
            // cannot find this phone, the answer is in these four values.
            StatusCard(
                "Discovery (desktop reads this)",
                adbProps.lines + listOf(
                    if (s.socketBound) "✓ socket bound: ${Protocol.SOCKET_NAME}" else "✗ socket not bound: ${Protocol.SOCKET_NAME}",
                    "cameras seen: ${s.cameras.size}",
                ),
            )
        }
        item { StatusCard("Device", listOf("${s.model} · Android ${s.sdk}", "IPs: ${if (s.ips.isEmpty()) "none (no wifi?)" else s.ips.joinToString()}", "battery ${s.battery}% ${if (s.charging) "(charging)" else ""}")) }
        item {
            StatusCard("Debugging (ADB)", listOf(
                if (s.adbEnabled) "✓ USB debugging ON — desktop can adb connect" else "✗ ADB OFF — enable in Developer options",
                "Desktop target: ${s.ips.firstOrNull() ?: "<phone ip>"}:5555 (adb tcpip 5555)",
                "Wireless debugging (Android 11+) is pairing-authenticated and needs no setting changes.",
            ))
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onDevSettings) { Text("Dev options") }
                OutlinedButton(onClick = onWireless) { Text("Wireless debug") }
            }
        }
        item {
            // Root path: the screen-off goal fails on MIUI without these, and
            // they are the one part of onboarding an ordinary app cannot do.
            // Only power-management fixes — nothing here enables remote ADB.
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Root fixes (screen-off survival)", style = MaterialTheme.typography.titleSmall)
                    Text(
                        "Applies the Doze/appops whitelists that stop this device's " +
                            "battery manager from killing keep-alive. Does not touch ADB or networking.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Button(
                        onClick = onApplyRootFixes,
                        enabled = !rootBusy,
                        modifier = Modifier.padding(top = 4.dp),
                    ) { Text(if (rootBusy) "Applying…" else "Apply root fixes") }
                    rootResults?.forEach { r ->
                        Text(
                            (if (r.applied) "✓ " else "✗ ") + r.title + " — " + r.detail,
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                    // MIUI autostart is ROM-private and unconfirmed, so it is
                    // never auto-applied; say exactly what to do instead.
                    // Grouped deliberately: `a && b || c` would show this on
                    // every Xiaomi device regardless of SDK.
                    if (s.sdk >= 30 && listOf("Xiaomi", "Redmi", "POCO")
                            .any { s.model.contains(it, ignoreCase = true) }
                    ) {
                        Text(
                            "Still needed by hand on MIUI: Settings → Apps → Android Webcam " +
                                "Companion → Autostart ON, and Battery saver → No restrictions. " +
                                "The autostart key is ROM-private, so it is not scripted here.",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
        item {
            Card(Modifier.fillMaxWidth()) {
                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Keep CPU awake (screen off)", style = MaterialTheme.typography.titleSmall)
                        Text(
                            if (s.keepAlive) "Active via ${s.keepSource}" else "Off — screen-off streaming may doze",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    Switch(checked = manual, onCheckedChange = onToggleManual)
                }
            }
        }
        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text("Battery optimization", style = MaterialTheme.typography.titleSmall)
                    Text(
                        if (s.exempt) "✓ exempt — Doze won't freeze the service"
                        else "✗ not exempt — Doze can kill keep-alive (screen-off risk)",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (!s.exempt) {
                        Button(onClick = onExempt, modifier = Modifier.padding(top = 8.dp)) { Text("Grant exemption") }
                    }
                }
            }
        }
        item { StatusCard("Session", listOf(
            if (s.sessionActive) "Desktop session ACTIVE — heartbeat streaming" else "No desktop session",
            "Watchdog: if the desktop is killed (pkill), the phone releases the lock ≤45s.",
        )) }
    }
}

@Composable
private fun StatusCard(title: String, lines: List<String>) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            lines.forEach { Text(it, style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace) }
        }
    }
}

@Composable
private fun CameraScreen(
    cameras: List<CameraInfo>,
    current: CameraInfo,
    cfg: CompanionConfig,
    onSelectCamera: (CameraInfo) -> Unit,
    onSelectSize: (Pair<Int, Int>) -> Unit,
    onSelectFps: (Int) -> Unit,
    onToggleTorch: (Boolean) -> Unit,
) {
    Column(Modifier.fillMaxSize().padding(horizontal = 12.dp)) {
        Text("Camera", style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(vertical = 4.dp))
        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(cameras) { cam ->
                FilterChip(
                    selected = cam.id == current.id,
                    onClick = { onSelectCamera(cam) },
                    label = { Text("id ${cam.id} · ${cam.facing} · max ${cam.maxWidth}×${cam.maxHeight}") },
                )
            }
        }
        Spacer(Modifier.height(8.dp))
        Text("Resolution (supported)", style = MaterialTheme.typography.titleSmall)
        LazyRow(Modifier.padding(vertical = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(current.sizes) { s ->
                val rec = s.w == 1920 || s.h == 1080 || s.w == 1280 || s.h == 720
                FilterChip(
                    selected = cfg.size?.first == s.w && cfg.size?.second == s.h,
                    onClick = { onSelectSize(s.w to s.h) },
                    label = { Text("${s.w}×${s.h}${if (rec) " ★" else ""} @${s.maxFps}") },
                )
            }
        }
        Text("FPS", style = MaterialTheme.typography.titleSmall)
        val fpsChoices = current.sizes
            .filter { cfg.size == null || (it.w == cfg.size!!.first && it.h == cfg.size!!.second) }
            .map { it.maxFps }.distinct().sorted()
            .ifEmpty { listOf(15, 30, 60, 120) }
        LazyRow(Modifier.padding(vertical = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(fpsChoices) { f ->
                FilterChip(selected = cfg.fps == f, onClick = { onSelectFps(f) }, label = { Text("$f") })
            }
        }
        if (current.torch) {
            Row(Modifier.padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Switch(checked = cfg.torch, onCheckedChange = onToggleTorch)
                Spacer(Modifier.padding(4.dp))
                Text("Torch (applied on next stream start)")
            }
        }
        Text(
            "These mirror scrcpy --camera-id/--camera-size/--camera-fps. ★ = common webcam sizes.",
            style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 12.dp),
        )
    }
}

@Composable
private fun SettingsScreen(s: Snapshot?, lastSync: String, onSync: () -> Unit, onClear: () -> Unit) {
    if (s == null) { Text("…", Modifier.padding(16.dp)); return }
    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Shared config (mirrors desktop config.json)", style = MaterialTheme.typography.titleSmall)
        Text("camera_id=${s.cfg.cameraId ?: "(facing)"}  size=${s.cfg.size?.let { "${it.first}x${it.second}" } ?: "-"}  fps=${s.cfg.fps}  torch=${s.cfg.torch}",
            fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
        Text("Companion socket: ${Protocol.SOCKET_NAME} (adb forward tcp:7878 localabstract:…)", style = MaterialTheme.typography.bodySmall)
        Text("Last desktop sync pull: $lastSync", style = MaterialTheme.typography.bodySmall)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onSync) { Text("Refresh") }
            OutlinedButton(onClick = onClear) { Text("Reset to defaults") }
        }
        Text("Android Webcam Companion v${Protocol.VERSION} · proto ${Protocol.PROTO}", style = MaterialTheme.typography.bodySmall)
    }
}
