# Companion protocol v2 — JSON lines over adb forward

Single contract shared by the Android companion app (`android-app/`) and the
desktop backend (`desktop/src/android_webcam/companion.py`). Both sides
implement exactly this document; no schema forks. v1 commands are unchanged;
v2 adds the session lifecycle that makes screen-off keep-alive leak-free.

## Transport

- Phone: `LocalServerSocket("android-webcam-companion")` (abstract Unix
  domain socket, no permission required), served by `KeepAliveService`.
- Desktop: `adb -s <ip:5555> forward tcp:7878 localabstract:android-webcam-companion`,
  then a TCP client to `127.0.0.1:7878`.
- Framing: one request per line, one response per line (JSON). Client closes
  after each exchange; server serves sequential clients.
- Timeouts: client 2 s connect / 2 s read. Failure is a loud
  `CompanionError` (no silent defaults); callers that may degrade render
  "companion unreachable".
- Clock: no time sync needed. The phone tracks *received* heartbeats.

## Common envelope

Every response echoes `"cmd"`, sets `"ok": true|false`, carries
`"proto": 1`, and on failure carries `"error": "<reason>"`.

```json
{"cmd":"ping","ok":true,"proto":1,"app":"android-webcam-companion","version":1}
```

## v1 commands (unchanged)

### `status`
```json
→ {"cmd":"status"}
← {"cmd":"status","ok":true,"proto":1,
   "adb_enabled":true,            // Settings.Global.ADB_ENABLED, read-only
   "model":"Redmi Note 8 Pro","sdk_int":36,
   "ips":["192.168.1.12"],        // non-loopback NetworkInterface addresses
   "battery":{"level":82,"charging":true},
   "ignoring_battery_optimizations":true,
   "keepalive":{"running":true,"wakelock_held":true,"source":"session"},
   "session":{"active":true,"last_heartbeat_ms_ago":3000},
   "config":{ …same shape as config.get… }}
```

### `cameras`
One entry per `CameraManager.getCameraIdList()`:
```json
→ {"cmd":"cameras"}
← {"cmd":"cameras","ok":true,"proto":1,"cameras":[
  {"id":"0","facing":"back","max_size":[4640,3472],
   "sizes":[[1920,1080,30],[1280,720,120],…],   // [w,h,maxFps]
   "torch":true,"orientation":90,"zoom":[1.0,10.0] }]}
```

### `config.get` / `config.set`
Single-owner keys, mirrored into desktop `config.json`:
`camera_id` → `camera_id`; `size` → `custom_size`; `fps` → `fps`;
`torch` → `torch`.
```json
→ {"cmd":"config.get"}
← {"cmd":"config.get","ok":true,"proto":1,
   "config":{"camera_id":"0","size":[1920,1080],"fps":30,"torch":false}}

→ {"cmd":"config.set","config":{"camera_id":"0","size":[1920,1080],"fps":30,"torch":false}}
← {"cmd":"config.set","ok":true,"proto":1}
```
`config.set` validates against the live camera list and fails loudly
(`ok:false,"error":"size 9999x9999 unsupported for camera 0"`) — never clamps.
Unknown keys in `config` are ignored (forward compatibility).

### `keepalive.on` / `keepalive.off`
```json
→ {"cmd":"keepalive.on"}
← {"cmd":"keepalive.on","ok":true,"proto":1,"running":true,"source":"manual"}
```
Starts/stops `KeepAliveService` (FGS + `PARTIAL_WAKE_LOCK`).
`source` is `manual` (Dashboard toggle) or `session` (driven by v2).

## v2 commands — session lifecycle

Purpose: the phone must hold a wake lock *only* while the host is actually
streaming, and must never keep one when the host dies unexpectedly (the
Omarchy bar widget stops scrcpy with `pkill -f 'scrcpy.*camera.*v4l2-sink'`,
which bypasses every graceful code path on the desktop).

### `session.begin`
```json
→ {"cmd":"session.begin","client":"android-webcam",
   "heartbeat_interval":10,"watchdog_timeout":45}
← {"cmd":"session.begin","ok":true,"proto":1,"session_active":true,
   "watchdog_timeout":45}
```
The phone stores the negotiated `watchdog_timeout` (clamped to
`[15, 300]` seconds) and arms a watchdog. Any active session causes
`KeepAliveService` to run with `source=session` (session outranks manual in
`status.keepalive.source`).

### `session.heartbeat`
```json
→ {"cmd":"session.heartbeat"}
← {"cmd":"session.heartbeat","ok":true,"proto":1,"ms_ago":0}
```
Resets the watchdog timer. The desktop sends this every `heartbeat_interval`
(10 s) in a daemon thread for the lifetime of the scrcpy process.

### `session.end`
```json
→ {"cmd":"session.end"}
← {"cmd":"session.end","ok":true,"proto":1,"session_active":false}
```
Ends the session immediately and releases the wake lock (unless a *manual*
keep-alive toggle is still on, in which case the service stays with
`source=manual`).

### Watchdog (phone-side, no command)
If no `session.heartbeat` arrives within `watchdog_timeout` of the last one
(or of `session.begin`), the phone ends the session itself and stops the
service. This is the backstop that makes the `pkill` stop path safe: a
killed host stops heart-beating, the phone releases within 45 s.

**Invariant:** at most one of {session, manual} holds the wake lock; ending
the session always leaves no wake lock held unless manual is on.

## Desktop usage sketch

```
adb connect 192.168.1.12:5555                       # backend.start()
adb -s 192.168.1.12:5555 forward tcp:7878 localabstract:android-webcam-companion
{"cmd":"session.begin","heartbeat_interval":10,"watchdog_timeout":45}   # → keep-alive on
{"cmd":"cameras"} / {"cmd":"status"} / {"cmd":"config.set",…}          # --sync
scrcpy --tcpip=… --video-source=camera --camera-id=0 --camera-size=1920x1080
       --camera-fps=30 --stay-awake --v4l2-sink=/dev/video0 --no-window …
…scrcpy exits (graceful) or is pkilled (panel)…
{"cmd":"session.end"}   # graceful: immediate;  pkill: phone watchdog ≤45 s
```
