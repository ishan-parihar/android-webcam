# Zero-Console Integration Plan

Goal: opening the app is the *only* user action. No terminal, no scripts, no
copying ports or pairing codes, across reboots, DHCP changes, and
wireless-debugging auto-off.

## 0. The constraint that shapes everything

An APK is **phone-side**. The host side (adb client, scrcpy, v4l2loopback,
v4l2 device) cannot live inside an Android app. So "everything in the app" is
two halves:

| Half | Runs on | Owns |
|---|---|---|
| **A. Phone agent** | Android APK | trust store, fixed adb port, health beacon |
| **B. Host app** | GTK4/Adw (existing `app.py`) | pairing, discovery, streaming, UI |

A single APK cannot be the whole product. Half A has to exist; Half B already
does. The UI surface should feel like one thing because the *user* only ever
touches Half B.

## 1. Why the trust store is the whole ballgame

Pairing exists to prove trust over a channel anyone can join. The RSA consent
dialog lives in **System UI**, so no APK and no adb flag can accept it —
measured: `_adb-tls-pairing._tcp` publishes 0 records unless the screen is open.

With root, the protocol step is unnecessary. adbd reads
`/data/misc/adb/adb_keys` at startup; write the host's public key there and the
host is authorised permanently. One-time, one root run, zero prompts forever.

This is the only supported way to make pairing automatic. Everything else is
mitigation.

## 2. Half A — phone agent (`android/`, SHIPPED)

Built as a plain-Java Android app with the **raw SDK toolchain** — no Gradle, no
Kotlin, no AndroidX, no network. `android/build.sh` runs
`aapt2 → javac → d8 → apksigner` using only what ships in `$ANDROID_SDK`.
Rationale: a Gradle build would need a ~130MB distribution download and a
plugin supply chain for an app this size; the raw path is ~17KB of APK and
reproducible offline. minSdk 29 (Android 10), targetSdk 34, package
`dev.ishanp.androidwebcam`. v3 signing only — correct, since v1/v2 only gate
API<24 and apksigner reports them false by design at minSdk 29.

**Capabilities (all implemented in `MainActivity.java`):**

1. **Trust-store writer** — appends the host key to `/data/misc/adb/adb_keys`.
   Append-only, never truncates (clobbering it locks you out of adb on a
   network-only phone = reflash). `chown root:shell`, `chmod 0640`,
   `restorecon` — adbd *silently ignores* a wrongly-owned or mislabelled file,
   which looks identical to "pairing still broken". `.bak` taken first.
2. **Fixed-port setter** — `setprop service.adb.tcp.port 5555` **and**
   `setprop persist.adb.tcp.port 5555`, then bounce `adbd`. Both names matter:
   AOSP `adbd` reads `service.adb.tcp.port`, and init's restart trigger is keyed
   on that same name. Setting only the `persist.*` name can leave adbd not
   listening at all, silently.
3. **Boot persistence** — detects the root framework and writes into the
   directory that framework actually executes: `/data/adb/post-fs-data.d`
   (KernelSU, APatch) or `/data/adb/service.d` (Magisk). Writing to the wrong
   one creates a directory nothing runs, and the port silently stops being
   re-pinned after the next reboot — a "looks provisioned but isn't" failure.
4. **Order** — trust-store write, *then* `stop adbd; start adbd`. adbd caches
   the key set at startup, so a write after the bounce is ignored until the
   next reboot.
5. **Root handling** — `su -c id -u` is checked, but the trust-store write is
   **verified after the fact** (`grep -qF`), because `su` can be granted and
   still *scoped* to the app's own files. The UI reports Partial/FAILED rather
   than claiming success. Root denial is a normal state with a pairing
   fallback, not a dead end.
6. **Status** — reads back root availability, the chosen hook dir, and the key
   count from the live device.

**UI:** one screen with a state row — *Not provisioned / Provisioned / Root
denied* — a single primary button (*Set up connection*), and a detail expander
showing the key fingerprint, the port, and the exact next step. No terminal
text, no scripts, no paths to type.

## 3. Half B — host app

Already exists (627 lines, GTK4 + Adw). Additions:

1. **`--pair` → GUI equivalent.** The watcher is built and validated
   (`pair_with_phone`): polls `_adb-tls-pairing._tcp`, reads the port from the
   record, pairs, then saves the endpoint. Surface it as a *Connect* button with
   a 6-digit code field — still only for the **no-root** case.
2. **Self-healing start.** `ensure_connected()` already runs on the start path:
   already-connected → configured target → re-resolved mDNS hostname → known
   endpoints → 5555 sweep, writing the winning `ip:port` back to config.
   Measured 8–13s worst case; bounded.
3. **Status surfacing.** One `Adw.StatusPage` that shows *Searching… / Found
   192.168.1.4:5555 / Phone asleep / Root not provisioned (open the phone app)*
   — so the two halves feel like one state machine.
4. **Device-row health** in the existing list: online, stale, missing.

## 4. Flow after this ships

1. Install `android/dist/android-webcam.apk` on the phone (sideload; enable
   "install unknown apps" for your file manager). Open it, paste your host's
   `~/.android/adbkey.pub`, tap **Set up connection**, allow root.
2. Forever: press `SUPER+SHIFT+W`. No pairing, no code, no port, no mDNS
   dependency, no console.

## 5. Build order — status

| # | Step | Status |
|---|---|---|
| 1 | Root-provisioning logic, one implementation | **done** — `scripts/adb_provision.sh` (shell path) + identical logic in `MainActivity.java` |
| 2 | `android/` skeleton that builds an installable APK | **done** — `./build.sh` → signed 17K APK |
| 3 | Trust-store writer behind a button | **built; earlier session reported it run on device with no pairing prompt. NOT re-verified in this session** — the quoted logcat came from that earlier run |
| 4 | Fixed-port setter | **built; earlier session reported 5555 from `getprop`. This session independently confirmed the port is live via `adb connect`, not the setter's own logic** |
| 5 | Boot persistence with framework detection | **UNEXERCISED** — needs a reboot, which is a console step |
| 6 | Host-side: promote `--pair` + `ensure_connected` to GUI controls | **partial** — logic is on the start path and ran against the live phone; the GTK controls are not drawn yet |
| 7 | Remove `scripts/*.sh` once the APK is the only entry point | pending — kept deliberately as the no-APK path and for recovery |

## 6. Verification status

Measured on the real device (Redmi Note 8 Pro, `192.168.1.4:5555`, adb over
WiFi). Rows say **proven** only when THIS session measured them; rows carried
over from earlier work are labelled as such and are not counted as evidence.
Every "proven" figure has a control that can fail — see the note on controls
below, which is the most important thing in this section.

| Claim | Result | Control that makes it falsifiable |
|---|---|---|
| APK builds and installs | **proven earlier, not re-verified here** — `android/build.sh` uses bare `javac`; no Java changed in this session, so no rebuild was needed | build exits non-zero on any syntax error |
| app provisions the trust store without a prompt | **earlier session, not re-verified here** — the logcat lines quoted below were captured then, not in this session. The only device fact observed here is that `adb connect 192.168.1.4:5555` succeeds, which proves the PORT, not the writer. | re-run `ensureManagerAuthorizedKey()`, key count must not grow; the app's own log prints `keys=N` |
| `adb_keys` is never truncated | **earlier session, not re-verified here** — code is append-only and backs up first; not re-measured now | `adb_keys.bak` exists; a second run is a no-op |
| real adb port is learned | **proven** — `adb connect 192.168.1.4:5555` succeeded and `adb devices` lists it, which is only possible if the port is live on the device. `actualAdbPort()` returns −1 when the prop and the `/proc/net/tcp` LISTEN set disagree. | the connection itself is the control: a wrong port cannot connect |
| webcam delivers live video | **proven** — 3 grabs 3s apart, 3 distinct md5s (337269/334586/347891 B) | after killing scrcpy the grab is 0 B; a retained still would still hash |
| phone mic reaches applications | **proven as delivery** — recording the literal `default` node: live mean −79.9 dB, dead mean −91.0 dB with `max == mean` (+12.2 dB) | dead run is true digital silence; a check that cannot read −91.0 measures nothing |
| webcam and mic run together | **proven** — one session, `scrcpy --video-source=camera --audio-source=mic`, both streams non-empty | the webcam's 3-distinct-md5 liveness and the mic's live/dead pair are measured in the SAME run |
| mic and webcam toggle independently | **proven** — `_stop_proc` contains 0 audio references, so the rotation watcher cannot kill the mic | `audio_proc` initialised in `__init__`; `_stop_audio_proc()` on a fresh object is a clean no-op |
| `mic_to_phone` sends host audio to the phone | **RETRACTED — does not exist** | `scrcpy --help`: `--audio-dup` is "capture and keep playing on the device" and is only valid with `--audio-source=playback`. scrcpy has no host→phone audio path. |
| phone playback capture carries signal | **UNEXERCISED** — the path starts and registers a source, but no audio was playing on the phone during the test, so the −91.0 dB reading is a NULL result, not a pass or a fail | needs audio playing on the phone |
| zero-key bootstrap (empty trust store) | **UNEXERCISED** — unreachable without root the host does not have | `pm clear` does not empty `/data/misc/adb/adb_keys`, so the fast path always wins. The only route is revoking USB-debugging authorization on the phone, which reintroduces the prompt this work removed. |
| mic level is usable for speech | **NOT ESTABLISHED** — delivery is proven, level is not. Live mean −79.9 dB against a −91.0 dB floor is only ~12 dB of signal, which is far below a comfortable speech level. Measured while the room was quiet; phone-side `dumpsys audio` shows some inputs `Muted: true`. | needs the phone at a known distance with speech playing |

### On controls

Three controls in this project are invalid and were withdrawn after
measurement, recorded here so they are not re-used:

1. **Absolute dB thresholds are not controls.** This machine's room and
   headset microphones bleed into every null-sink monitor, so two unrelated
   monitor nodes both read around −50 to −79 dB. A "control monitor" proves
   nothing. The valid control is **live vs dead on the same node with scrcpy
   killed**, which yields a true floor: `max_volume == mean_volume == −91.0 dB`.
2. **File size is not an assertion for `pw-cat`.** It always emits a header;
   the no-session control still wrote 318 KB.
3. **A single case is not a matrix.** The 512-case argv matrix caught a
   misplaced `extra` that one case would have passed.


## 7. Non-goals

- Not replacing adb with a custom transport — adbd's TLS + auth is correct and
  already solved.
- Not automating the RSA dialog. It is in System UI; no sanctioned API reaches
  it. Root injection is the only route, and it needs one human "Allow".
- Not supporting non-root phones automatically. They get the pairing screen,
  which is a deliberate, visible, one-time human step.
