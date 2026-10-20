package com.ishanp.androidwebcam.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM unit tests for the pure logic: config validation and the protocol
 * encoder/decoder. No Android framework, no network — runs on the desktop
 * JVM via `gradlew testDebugUnitTest`.
 */
class ConfigValidatorTest {

    private val back = CameraInfo(
        id = "0", facing = "back", maxWidth = 4640, maxHeight = 3472,
        sizes = listOf(
            SizeFps(1920, 1080, 30), SizeFps(1280, 720, 120), SizeFps(640, 480, 30),
        ),
        torch = true, orientation = 90, zoomMin = 1f, zoomMax = 10f,
    )
    private val front = CameraInfo(
        id = "1", facing = "front", maxWidth = 2592, maxHeight = 1940,
        sizes = listOf(SizeFps(1920, 1080, 30), SizeFps(1280, 720, 60)),
        torch = false, orientation = 270, zoomMin = 1f, zoomMax = 4f,
    )
    private val cams = listOf(back, front)

    @Test
    fun `valid config has no problems`() {
        val cfg = CompanionConfig(cameraId = "0", size = 1920 to 1080, fps = 30, torch = true)
        assertEquals(emptyList<String>(), ConfigValidator.validate(cfg, cams))
    }

    @Test
    fun `unknown camera id is rejected`() {
        val p = ConfigValidator.validate(CompanionConfig(cameraId = "7"), cams)
        assertTrue(p.any { it.contains("not on this phone") })
    }

    @Test
    fun `unsupported size is rejected with the supported list`() {
        val p = ConfigValidator.validate(CompanionConfig(cameraId = "0", size = 9999 to 1), cams)
        assertTrue(p.any { it.contains("unsupported") && it.contains("1920x1080") })
    }

    @Test
    fun `fps above the size maximum is rejected`() {
        val p = ConfigValidator.validate(
            CompanionConfig(cameraId = "0", size = 1280 to 720, fps = 240), cams)
        assertTrue(p.any { it.contains("exceeds max 120") })
    }

    @Test
    fun `torch on a camera without torch is rejected`() {
        val p = ConfigValidator.validate(
            CompanionConfig(cameraId = "1", size = 1920 to 1080, fps = 30, torch = true), cams)
        assertTrue(p.any { it.contains("no torch") })
    }

    @Test
    fun `no cameras reports a problem`() {
        val p = ConfigValidator.validate(CompanionConfig(cameraId = "0"), emptyList())
        assertTrue(p.any { it.contains("no cameras") })
    }

    @Test
    fun `multiple problems are collected, not just the first`() {
        val p = ConfigValidator.validate(
            CompanionConfig(cameraId = "1", size = 9999 to 1, fps = 240, torch = true), cams)
        assertTrue("expected >=2 problems, got $p", p.size >= 2)
    }

    @Test
    fun `facing fallback picks the back camera`() {
        val cfg = CompanionConfig(cameraId = null, size = 1920 to 1080, fps = 30, torch = true)
        assertEquals(emptyList<String>(), ConfigValidator.validate(cfg, cams))
    }
}

class ConfigJsonTest {

    @Test
    fun `config round-trips through json`() {
        val cfg = CompanionConfig(cameraId = "0", size = 1920 to 1080, fps = 30, torch = true)
        val back = CompanionConfig.fromJson(cfg.toJson())
        assertEquals(cfg, back)
    }

    @Test
    fun `null size survives round-trip`() {
        val cfg = CompanionConfig(cameraId = "1", size = null, fps = 30, torch = false)
        val back = CompanionConfig.fromJson(cfg.toJson())
        assertEquals(null, back.size)
        assertEquals("1", back.cameraId)
    }

    @Test
    fun `camera info json carries every wire field`() {
        val info = CameraInfo(
            id = "0", facing = "back", maxWidth = 4640, maxHeight = 3472,
            sizes = listOf(SizeFps(1920, 1080, 30)), torch = true,
            orientation = 90, zoomMin = 1f, zoomMax = 10f,
        )
        val j = info.toJson()
        assertEquals("0", j.getString("id"))
        assertEquals("back", j.getString("facing"))
        assertEquals(4640, j.getJSONArray("max_size").getInt(0))
        assertEquals(1, j.getJSONArray("sizes").length())
        assertTrue(j.getBoolean("torch"))
        assertEquals(2, j.getJSONArray("zoom").length())
    }
}
