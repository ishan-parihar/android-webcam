package com.ishanp.androidwebcam.companion

import android.graphics.ImageFormat
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.params.StreamConfigurationMap
import android.util.Range
import android.util.Size

/**
 * Wraps CameraManager into the wire's flat camera list.
 *
 * Listing cameras and reading characteristics needs NO camera permission —
 * only *opening* a camera does, and this app never opens one (scrcpy-server,
 * running as shell on the desktop's command, owns the camera while streaming).
 *
 * Sizes come from the JPEG capture stream configuration map (that is what
 * scrcpy asks for). Per-size fps uses high-speed ranges when the device
 * publishes them for that exact size, otherwise the camera's advertised
 * maximum AE target fps.
 */
class CameraCatalog(private val manager: CameraManager) {

    fun list(): List<CameraInfo> =
        manager.cameraIdList.mapNotNull { id -> runCatching { describe(id) }.getOrNull() }
            .sortedWith(compareBy({ it.facing != "back" }, { it.id.toIntOrNull() ?: 99 }))

    private fun describe(id: String): CameraInfo {
        val ch = manager.getCameraCharacteristics(id)
        val facing = when (ch.get(CameraCharacteristics.LENS_FACING)) {
            CameraMetadata.LENS_FACING_BACK -> "back"
            CameraMetadata.LENS_FACING_FRONT -> "front"
            CameraMetadata.LENS_FACING_EXTERNAL -> "external"
            else -> "unknown"
        }
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val sizes = captureSizes(map, ch)

        val pixelArray = ch.get(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE)
        val maxW = pixelArray?.width ?: sizes.maxOfOrNull { it.w } ?: 0
        val maxH = pixelArray?.height ?: sizes.maxOfOrNull { it.h } ?: 0
        // FLASH_INFO_AVAILABLE is a Key<Boolean> in this SDK: true means the
        // camera has a flash unit (and therefore a torch the scrcpy server
        // can drive via --camera-torch).
        val flash: Boolean? = ch.get(CameraCharacteristics.FLASH_INFO_AVAILABLE)
        val maxZoom = ch.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM)

        return CameraInfo(
            id = id,
            facing = facing,
            maxWidth = maxW,
            maxHeight = maxH,
            sizes = sizes,
            torch = flash == true,
            orientation = ch.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0,
            zoomMin = 1f,
            zoomMax = maxZoom,
        )
    }

    private fun captureSizes(
        map: StreamConfigurationMap?,
        ch: CameraCharacteristics,
    ): List<SizeFps> {
        if (map == null) return emptyList()
        val output: Array<Size> = map.getOutputSizes(ImageFormat.JPEG) ?: emptyArray()
        val deviceMaxFps = (ch.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
            ?.maxOfOrNull { it.upper } ?: 0)
        return output.map { size ->
            val highSpeed = runCatching { map.getHighSpeedVideoFpsRangesFor(size) }
                .getOrNull()?.maxOfOrNull { it.upper } ?: 0
            SizeFps(size.width, size.height, maxOf(highSpeed, deviceMaxFps))
        }
            .distinctBy { it.w to it.h }
            .sortedWith(compareByDescending<SizeFps> { it.w.toLong() * it.h }
                .thenByDescending { it.maxFps })
    }
}
