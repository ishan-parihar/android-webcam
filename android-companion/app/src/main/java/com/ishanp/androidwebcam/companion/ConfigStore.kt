package com.ishanp.androidwebcam.companion

import android.content.Context
import android.content.SharedPreferences

/**
 * Persisted companion config (camera_id / size / fps / torch) + the manual
 * keep-alive toggle. SharedPreferences + org.json; no extra deps.
 */
class ConfigStore(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("companion", Context.MODE_PRIVATE)

    fun getConfig(): CompanionConfig = CompanionConfig(
        cameraId = prefs.getString(KEY_CAMERA_ID, null),
        size = prefs.getString(KEY_SIZE, null)?.let {
            val p = it.split("x")
            if (p.size == 2) p[0].toIntOrNull()?.let { w -> p[1].toIntOrNull()?.let { h -> w to h } }
            else null
        },
        fps = prefs.getInt(KEY_FPS, 30),
        torch = prefs.getBoolean(KEY_TORCH, false),
    )

    /**
     * Validate-then-save. Returns the list of problems; an empty list means the
     * config was accepted and persisted. Fails loud, never clamps.
     */
    fun setConfig(cfg: CompanionConfig, cams: List<CameraInfo>): List<String> {
        val problems = ConfigValidator.validate(cfg, cams)
        if (problems.isNotEmpty()) return problems
        prefs.edit()
            .putString(KEY_CAMERA_ID, cfg.cameraId)
            .putString(KEY_SIZE, cfg.size?.let { "${it.first}x${it.second}" })
            .putInt(KEY_FPS, cfg.fps ?: 30)
            .putBoolean(KEY_TORCH, cfg.torch)
            .apply()
        return emptyList()
    }

    var manualKeepAlive: Boolean
        get() = prefs.getBoolean(KEY_MANUAL, false)
        set(v) = prefs.edit().putBoolean(KEY_MANUAL, v).apply()

    companion object {
        private const val KEY_CAMERA_ID = "camera_id"
        private const val KEY_SIZE = "size"
        private const val KEY_FPS = "fps"
        private const val KEY_TORCH = "torch"
        private const val KEY_MANUAL = "manual_keepalive"
    }
}
