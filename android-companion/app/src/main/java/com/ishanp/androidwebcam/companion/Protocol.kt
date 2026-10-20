package com.ishanp.androidwebcam.companion

import org.json.JSONArray
import org.json.JSONObject

/**
 * Wire types + request/response encoding for the companion protocol.
 *
 * Mirrors docs/companion-protocol.md. Deliberately uses org.json (built into
 * Android) so the app carries zero third-party dependencies.
 */
object Protocol {
    const val SOCKET_NAME = "android-webcam-companion"
    const val PROTO = 1
    const val APP = "android-webcam-companion"
    const val VERSION = 1

    /** Watchdog bounds the phone accepts from session.begin. */
    const val WATCHDOG_MIN_SEC = 15
    const val WATCHDOG_MAX_SEC = 300
}

class CompanionException(message: String) : Exception(message)

object Commands {
    fun ping() = JSONObject().put("cmd", "ping")

    fun sessionBegin(client: String, heartbeatInterval: Long, watchdogTimeout: Long): JSONObject =
        JSONObject()
            .put("cmd", "session.begin")
            .put("client", client)
            .put("heartbeat_interval", heartbeatInterval)
            .put("watchdog_timeout", watchdogTimeout)

    fun sessionHeartbeat(): JSONObject = JSONObject().put("cmd", "session.heartbeat")

    fun sessionEnd(): JSONObject = JSONObject().put("cmd", "session.end")

    fun keepAlive(on: Boolean): JSONObject =
        JSONObject().put("cmd", if (on) "keepalive.on" else "keepalive.off")

    fun configSet(cfg: CompanionConfig): JSONObject =
        JSONObject().put("cmd", "config.set").put("config", cfg.toJson())

    fun configGet(): JSONObject = JSONObject().put("cmd", "config.get")

    fun cameras(): JSONObject = JSONObject().put("cmd", "cameras")

    fun status(): JSONObject = JSONObject().put("cmd", "status")
}

/** The single-owner config shared with the desktop's config.json. */
data class CompanionConfig(
    val cameraId: String? = null,
    val size: Pair<Int, Int>? = null,
    val fps: Int? = null,
    val torch: Boolean = false,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("camera_id", cameraId ?: JSONObject.NULL)
        if (size != null) put("size", JSONArray().put(size.first).put(size.second)) else put("size", JSONObject.NULL)
        put("fps", fps ?: JSONObject.NULL)
        put("torch", torch)
    }

    companion object {
        fun fromJson(o: JSONObject): CompanionConfig = CompanionConfig(
            cameraId = if (o.isNull("camera_id")) null else o.optString("camera_id"),
            size = if (o.isNull("size")) null else {
                val a = o.getJSONArray("size")
                if (a.length() == 2) a.getInt(0) to a.getInt(1) else null
            },
            fps = if (o.isNull("fps")) null else o.optInt("fps"),
            torch = o.optBoolean("torch", false),
        )
    }
}

/** One camera from CameraManager, flattened for the wire. */
data class CameraInfo(
    val id: String,
    val facing: String,           // back | front | external | unknown
    val maxWidth: Int,
    val maxHeight: Int,
    val sizes: List<SizeFps>,     // [w, h, maxFps] the phone actually supports
    val torch: Boolean,
    val orientation: Int,
    val zoomMin: Float?,
    val zoomMax: Float?,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id)
        put("facing", facing)
        put("max_size", JSONArray().put(maxWidth).put(maxHeight))
        put("sizes", JSONArray().apply {
            sizes.forEach { put(JSONArray().put(it.w).put(it.h).put(it.maxFps)) }
        })
        put("torch", torch)
        put("orientation", orientation)
        if (zoomMin != null && zoomMax != null) {
            put("zoom", JSONArray().put(zoomMin.toDouble()).put(zoomMax.toDouble()))
        } else {
            put("zoom", JSONObject.NULL)
        }
    }
}

data class SizeFps(val w: Int, val h: Int, val maxFps: Int)

/**
 * Pure validation of a config against a camera list — same rules as the
 * desktop's companion.validate(). Fails loudly: returns every problem, never
 * clamps silently.
 */
object ConfigValidator {
    fun validate(cfg: CompanionConfig, cams: List<CameraInfo>): List<String> {
        if (cams.isEmpty()) return listOf("phone reports no cameras")
        val problems = mutableListOf<String>()

        val target: CameraInfo? = when {
            cfg.cameraId != null -> {
                val m = cams.firstOrNull { it.id == cfg.cameraId }
                if (m == null) {
                    problems.add("camera_id ${cfg.cameraId} not on this phone (available: ${cams.joinToString { it.id }})")
                }
                m
            }
            else -> cams.firstOrNull { it.facing == "back" } ?: cams.firstOrNull { it.facing == "front" }
        }
        if (target == null || cfg.cameraId != null && problems.isNotEmpty()) return problems

        cfg.size?.let { (w, h) ->
            val size = SizeFps(w, h, 0)
            if (target.sizes.none { it.w == w && it.h == h }) {
                problems.add(
                    "size ${w}x$h unsupported by camera ${target.id} " +
                        "(supported: ${target.sizes.joinToString { "${it.w}x${it.h}" }.ifEmpty { "none reported" }})"
                )
            } else {
                cfg.fps?.let { fps ->
                    val cap = target.sizes.filter { it.w == w && it.h == h }.maxOf { it.maxFps }
                    if (cap > 0 && fps > cap) {
                        problems.add("fps $fps exceeds max $cap for ${w}x$h on camera ${target.id}")
                    }
                }
            }
        }
        if (cfg.torch && !target.torch) {
            problems.add("camera ${target.id} has no torch — cannot enable --camera-torch")
        }
        return problems
    }
}
