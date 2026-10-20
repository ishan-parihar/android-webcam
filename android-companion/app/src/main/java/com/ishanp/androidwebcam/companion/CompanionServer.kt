package com.ishanp.androidwebcam.companion

import android.content.Intent
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.IBinder
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader

/**
 * Companion socket server. Serves JSON-lines requests from the desktop (via
 * `adb forward tcp:7878 localabstract:android-webcam-companion`).
 *
 * Runs inside KeepAliveService so the socket's lifetime == the wake lock's
 * lifetime: no socket without a wake lock, no wake lock without a socket.
 * Accepts sequential clients; one request per connection.
 */
class CompanionServer(
    private val service: KeepAliveService,
) {
    @Volatile private var running = false
    private var serverSocket: LocalServerSocket? = null
    private var thread: Thread? = null

    fun start() {
        if (running) return
        running = true
        serverSocket = runCatching { LocalServerSocket(Protocol.SOCKET_NAME) }
            .getOrElse {
                Log.e(TAG, "cannot bind ${Protocol.SOCKET_NAME}: ${it.message}")
                running = false
                bound = false
                return
            }
        bound = true
        thread = Thread({ acceptLoop() }, "companion-server").apply {
            isDaemon = true
            start()
        }
        Log.i(TAG, "companion socket listening on ${Protocol.SOCKET_NAME}")
    }

    fun stop() {
        running = false
        bound = false
        runCatching { serverSocket?.close() }
        serverSocket = null
        thread = null
    }

    private fun acceptLoop() {
        while (running) {
            val client: LocalSocket = try {
                serverSocket?.accept() ?: return
            } catch (e: Exception) {
                if (running) Log.w(TAG, "accept failed: ${e.message}")
                return
            }
            try {
                handle(client)
            } catch (e: Exception) {
                Log.w(TAG, "client error: ${e.message}")
            } finally {
                runCatching { client.close() }
            }
        }
    }

    private fun handle(client: LocalSocket) {
        client.soTimeout = 3000
        val reader = BufferedReader(InputStreamReader(client.inputStream))
        val line = reader.readLine() ?: return
        val resp = runCatching { dispatch(JSONObject(line)) }
            .getOrElse { e -> errResp("status", "malformed request: ${e.message}") }
        client.outputStream.write((resp.toString() + "\n").toByteArray())
        client.outputStream.flush()
    }

    /** Pure command routing — returns a response JSONObject, never throws. */
    fun dispatch(req: JSONObject): JSONObject {
        val cmd = req.optString("cmd")
        return try {
            when (cmd) {
                "ping" -> ok(cmd).put("app", Protocol.APP).put("version", Protocol.VERSION)
                "session.begin" -> {
                    val hb = req.optLong("heartbeat_interval", 10L)
                    val wd = req.optLong("watchdog_timeout", 45L)
                    service.onSessionBegin(hb, wd)
                    ok(cmd).put("session_active", true).put("watchdog_timeout", wd)
                }
                "session.heartbeat" -> {
                    service.onHeartbeat()
                    ok(cmd).put("ms_ago", 0)
                }
                "session.end" -> {
                    service.onSessionEnd()
                    ok(cmd).put("session_active", false)
                }
                "keepalive.on" -> {
                    service.setManualKeepAlive(true)
                    ok(cmd).put("running", true).put("source", "manual")
                }
                "keepalive.off" -> {
                    service.setManualKeepAlive(false)
                    ok(cmd).put("running", service.keepAliveShouldRun()).put("source", "none")
                }
                "config.get" -> ok(cmd).put("config", service.store.getConfig().toJson())
                "config.set" -> {
                    val cfg = CompanionConfig.fromJson(req.getJSONObject("config"))
                    val problems = service.store.setConfig(cfg, service.cameras())
                    if (problems.isNotEmpty()) {
                        JSONObject().put("cmd", cmd).put("ok", false)
                            .put("proto", Protocol.PROTO).put("error", problems.first())
                    } else {
                        ok(cmd)
                    }
                }
                "cameras" -> {
                    val arr = org.json.JSONArray()
                    service.cameras().forEach { arr.put(it.toJson()) }
                    ok(cmd).put("cameras", arr)
                }
                "status" -> service.statusJson(cmd)
                else -> JSONObject().put("cmd", cmd).put("ok", false)
                    .put("proto", Protocol.PROTO).put("error", "unknown cmd")
            }
        } catch (e: Exception) {
            errResp(cmd, e.message ?: "internal error")
        }
    }

    private fun ok(cmd: String) =
        JSONObject().put("cmd", cmd).put("ok", true).put("proto", Protocol.PROTO)

    private fun errResp(cmd: String, msg: String) =
        JSONObject().put("cmd", cmd).put("ok", false).put("proto", Protocol.PROTO)
            .put("error", msg)

    companion object {
        const val TAG = "CompanionServer"

        @Volatile
        private var bound = false

        /**
         * Whether the abstract socket is bound RIGHT NOW.
         *
         * The desktop reports "companion unreachable" for several very
         * different reasons -- server not started, bind refused, adb forward
         * pointing elsewhere -- and they look identical from the host. This
         * is the one that can only be answered on the device.
         */
        @JvmStatic
        fun isSocketBound(): Boolean = bound
    }
}
