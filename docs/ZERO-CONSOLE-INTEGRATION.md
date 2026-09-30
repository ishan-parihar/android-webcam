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
| APK builds and installs | **proven** — `android/build.sh` uses bare `javac` and exits 0; the APK was installed from `~/Downloads/android-webcam.apk` and produced this session's device log | build exits non-zero on any syntax error; the device log proves the installed APK ran |
| app recognises an already-trusted phone without network work | **PROVEN 15:59** — `gate: cachedKey=700ch fileBytes=715ch cachedHasSpace=false -> YES`, then `trust store has 1 key(s); our host key present=true` -> `manager key: 1/1 present` -> `advertising adb on port 5555`. **No** `start discovery`, no `nsd`, no sweep, no 7911 | host side, same minute: `adb devices` = `device` and our key is in `settings get secure authorized_adb_keys`; the phone's `_androidwebcam-adb._tcp` record resolves via `avahi-browse`. negative: 13:43/13:49/13:54 read `present=false` on the SAME store and re-ran discovery every launch — the gate was matching a 700-char key against a 715-char commented line |
| `adb_keys` is never truncated | **partially verified** — the app's own gate logs `fileBytes=` on EVERY launch, and it read `715ch` at 13:54, 15:59 and 16:03: same single line, no growth across four launches including two that re-ran discovery. Full idempotency needs a `wrote=true` re-run, which the fast path now bypasses | `fileBytes=` in the gate line is the control, and needs no root from the host. Not yet shown: a second `wrote=true` appending nothing |
| host key handover needs only one click | **PROVEN 13:43** — `serve_key` bound 192.168.1.13:7911 and the PHONE fetched it: `nsd -> 192.168.1.13:7911`, `GET /adbkey.pub -> 200, 715 bytes, valid=true`, host logged `served key to 192.168.1.4`. The earlier half-proof used a local `curl`, which is not a consumer | negative: at 13:21 and 13:54, same host and same code, the identical log shape ended `live on 7911 = []` with 246 SocketTimeouts — the ufw rule was the only difference |
| real adb port is learned | **proven on device** — the phone log reports `actualAdbPort=5555 (pinned by getprop) of listening=[5555, 1716, 39829, 50037, 5060]`, i.e. the pinned port was confirmed present in the LISTEN set, and the host connected to 5555 | `actualAdbPort()` returns −1 when the pinned port is absent from the LISTEN set |
| webcam delivers live video | **proven** — 3 grabs 3s apart, 3 distinct md5s (337269/334586/347891 B) | after killing scrcpy the grab is 0 B; a retained still would still hash |
| phone mic reaches applications | **proven as delivery** — recording the literal `default` node: live mean −79.9 dB, dead mean −91.0 dB with `max == mean` (+12.2 dB) | dead run is true digital silence; a check that cannot read −91.0 measures nothing |
| webcam and mic run together | **proven** — one session, `scrcpy --video-source=camera --audio-source=mic`, both streams non-empty | the webcam's 3-distinct-md5 liveness and the mic's live/dead pair are measured in the SAME run |
| stopping the webcam leaves the mic running | **proven** — `_stop_proc` contains 0 audio references, so the rotation watcher cannot kill the mic | `audio_proc` initialised in `__init__`; `_stop_audio_proc()` on a fresh object is a clean no-op |
| the phone mic is opened exactly once | **was BROKEN, fixed in `3c5b2a1`** — webcam-then-mic-only put 2 scrcpy streams in `scrcpy_mic`, and the system mic carried the phone mic summed with itself (mean −70.0 dB one session, −54.7 dB two, +15.3 dB). The camera and microphone are one capture resource. Now 1 stream in both click orders, and the mic button is disabled while the webcam runs. | `pactl list sink-inputs` counts the scrcpy sink-inputs, and the level is compared across session counts — a dB reading alone cannot see a doubled source |
| `mic_to_phone` sends host audio to the phone | **RETRACTED — does not exist** | `scrcpy --help`: `--audio-dup` is "capture and keep playing on the device" and is only valid with `--audio-source=playback`. scrcpy has no host→phone audio path. |
| phone playback capture carries signal | **UNEXERCISED** — the path starts and registers a source, but no audio was playing on the phone during the test, so the −91.0 dB reading is a NULL result, not a pass or a fail | needs audio playing on the phone |
| zero-key bootstrap (no key of ours in the store) | **PROVEN 13:43** — `wrote=true hook=true listening=true`; host logged `served key to 192.168.1.4`; `adb devices` went `unauthorized` -> `device` | the store held 1 key, a FOREIGN one, so this is exactly the state that used to fake success. positive: our key is in `settings get secure authorized_adb_keys` (readable from the host) and adbd answers `device`, so the file is correct. It does NOT isolate that THIS app wrote it - on a rooted phone the platform's own approval writes that file, which is the counterexample from 11:29/12:12. negative: 13:15, the pre-fix gate, same store, `1/1 present` -> straight to `advertising adb` with ZERO server requests. The 13:15/13:21 pair isolates the trust gate; 13:21/13:43 isolates the ufw rule (13:21 already logged `present=false`, so it cannot be the gate pair) |
| mic level is usable for speech | **sink-volume fix proven; speech usability NOT ESTABLISHED** — `ensure_null_sink` now forces 100%, and the attenuation it caused is measured: same capture chain, one session, sink as created mean −75.1 dB vs forced to 100% mean −36.1 dB, against a −91.0 dB floor. With the fix in place and no manual intervention the shipped default reads **mean −52.9 dB**. That is the whole evidence. | the floor is `max == mean == −91.0 dB` with scrcpy killed; a fresh sink and a pre-existing 20% sink were both checked to come up at `vol 1.00`. What is NOT shown: anyone speaking. One quiet-room sample cannot separate speech from ambient, so no speech-level claim is made. |

### Trust store: PROVEN at 13:43, and how it was nearly missed

The 13:43 log is the first and only run where the phone actually fetched a key:

    trust store has 1 key(s); our host key present=false
    start discovery; myIp=192.168.1.4 prefix=/24
    nsd -> 192.168.1.13:7911
    GET http://192.168.1.13:7911/adbkey.pub -> 200, 715 bytes, valid=true
    provisioning with key from 192.168.1.13:7911
    POST /phone ip=192.168.1.4 port=5555 -> 200
    provision wrote=true hook=true listening=true on port 5555

Host side, independently: the server logged `served key to 192.168.1.4`,
`adb devices` went from `unauthorized` to `device`, and
`adb shell echo HANDOVER_WORKS` returned `HANDOVER_WORKS`. Two independent
observers, same run.

Three separate causes stood between this and working, and the second was
invisible until the trust gate was fixed:

  1. **ufw dropped the packets.** ENABLED=yes, DEFAULT_INPUT_POLICY=DROP, and
     no rule for 22 or 7911. Fixed with
     `sudo ufw allow from 192.168.1.0/24 to any port 7911 proto tcp` and the
     same for 22. Confirmed against the ENFORCED rules
     (`^-A ufw-user-input`, not the comment lines).
  2. **countKeys() let a foreign key fake a trusted phone.** `grep -c .` counts
     LINES, so a file holding another host's key satisfied `keys > 0`, the fast
     path at :979 took the "already trusted" branch, and the app advertised
     readiness while adbd refused us. This is why 13:15 logged `keys=1` and
     `1/1 present` with the server logging zero requests: **the sweep never
     ran**. Fixed with trustFileHasHostKey(), an exact `grep -qxF`, gated on
     `keys > 0 && ours`. See tests/test_trust_store_gate.py.
  3. **serve_key self-terminated at 600s.** That deadline applied even with
     once=False, so a phone launching later met a closed port. max_wait=None
     now waits until Ctrl-C. This is a RUNTIME failure, not an import error:
     the wait loop compared `float < deadline`, and `float < None` raises after
     the socket binds while py_compile passes. See
     tests/test_serve_key_modes.py.

Causes 2 and the gate's second bug were both invisible until the app started
logging what it actually compared. `trustFileHasHostKey()` now logs the cached
key length, the byte length of what `cat KEYS` returned, and whether the cached
value carried a comment. Without that line, cause 4 below was a guess.

  4. **The gate matched the wrong field of the file.** See trap 11. The count
     bug (cause 2) hid this one: both made the fast path read as needed when it
     was not, and the count bug ran first.

TWO STORES, which conflating them produced several wrong conclusions:

  1. `/data/misc/adb/adb_keys` - the FILE adbd reads to authorize a host.
  2. `settings secure authorized_adb_keys` - AdbDebuggingManager's PROMPT list,
     which is what the Wireless-debugging UI shows as "paired devices".

`ensureManagerAuthorizedKey()` only ever touches (2). It reads keys FROM (1) and
publishes them to (2); it never writes (1). With (1) empty and no
`pendingHostKey` it logs `manager key: no host key available; leaving store
untouched` and returns - so the app CANNOT repopulate an empty store, and it
is not what re-added the paired entry. The `keys=N` on the launch line is a
count read from (1).

The store is written by `provision()` in the app, confirmed at 13:43 by
`wrote=true` and by adb going to `device`. Note the store was never EMPTY: it
held 1 key belonging to another host, and that foreign key WAS the bug - it
made `keys > 0` true and the app skipped discovery entirely.

The file's contents cannot be read from the host at all: `cat`, `wc -l` and
`wc -c` on /data/misc/adb/adb_keys all return `Permission denied`, and an
earlier `wc -l` that appeared to print `1` was that error text on stderr, not a
count. I briefly read it as "1 line present" and drew a verdict from it. The
readable surface is `settings get secure authorized_adb_keys`, which the app
populates but which is a different store; the phone's own log is the authority
on the file.

SEVEN TRAPS THAT PRODUCED WRONG CONCLUSIONS HERE, all mine:

  1. `Log2.resetForNewRun()` TRUNCATES the log every launch. A paste can never
     show an earlier run's fetch, so "not in this log" is NOT evidence it never
     happened - the inference runs the other way.
  2. `serve_key(once=True)` is one-shot and a local `curl` sets `served=1`. My
     own probe is what made commit 72d40b3 look like proof; retracted in
     647f935.
  3. Reading `settings get secure authorized_adb_keys` while adb was
     `unauthorized` returned the pairing dialog instead of a value. I read
     "host key NOT found" out of a check that had not run.
  4. Claiming the app repopulates the paired entry on revoke. It cannot - see
     the two-store analysis. The user was about to act on it.
  5. Treating `grep -E '22'` over /etc/ufw/user.rules as the enforced ruleset.
     Those are ufw's SOURCE file with matching comment lines. The live rules
     are the `^-A ufw-user-input` entries.
  6. A 40-char stub in the trust-gate test FAILED its own positive control,
     which is how the length sensitivity surfaced. It now reads the real
     ~/.android/adbkey.pub.
  7. Committing a syntax error. 32904a6 re-indented serve_key and broke
     backend.py on import, taking the whole host app down. scripts/
     check-python.sh now gates every commit, and is verified BY FAILURE:
     re-injecting that exact break exits 1 and names the file.
  8. Reading `cat /data/misc/adb/adb_keys` as empty and then concluding
     "whole line DIFFERS (this is the bug)". Both verdicts compared our key
     against the empty string - the read never ran. Same shape as trap 3.
  9. Claiming a comment-field mismatch on the CACHED side caused 13:49's
     `present=false`. provision() strips the comment at :1287 before caching,
     so the cached value was already bare. Refuted by reading the code. The
     comment in hostPublicKey() records that rather than repeating it.
  10. 13:49's firewall control is the LIVE one: `control probe 192.168.1.13:22
     (sshd, OPEN on this host) -> 0`, versus `-2` at 13:21. Same probe, same
     host, only the ufw rule changed.
  11. Attributing 13:49's `present=false` to the wrong store. It was the FILE
     side, not the cache, and only the gate's own instrumentation could say so:
     `gate: cachedKey=700ch fileBytes=715ch cachedHasSpace=false -> NO`. The
     cache was right; `adb_keys` lines carry a `user@host` comment
     (`ishanp@omarchy`, 715 chars against a 700-char key), so a whole-line
     `grep -qxF` could never match. Fixed at all three file-side call sites
     (:1179, :1303, :1342) by comparing the first field, as :852 already did
     for the settings store. See tests/test_adb_keys_first_field.py, which
     runs both forms against a real adbkey.pub line: whole-line NO, first-field
     YES, foreign key NO.

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
