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
| 3 | Trust-store writer behind a button | **done** — not yet run on a device |
| 4 | Fixed-port setter | **done** — not yet run on a device |
| 5 | Boot persistence with framework detection | **done** — not yet run on a device |
| 6 | Host-side: promote `--pair` + `ensure_connected` to GUI controls | **partial** — logic built and on the start path; the GTK controls are not drawn yet |
| 7 | Remove `scripts/*.sh` once the APK is the only entry point | pending — kept deliberately as the no-APK path and for recovery |

## 6. Verification that must happen before calling it done

Nothing in the phone-side chain has been executed yet — there is no adb device
and no root on the phone. These are the proofs owed:

| Claim | How it is proven |
|---|---|
| APK installs and launches | sideload, open, no crash |
| trust store works | `adb devices -l` lists the device, no pairing screen used |
| port pinned | `/proc/net/tcp` shows `15B3` in `LISTEN`, survives reboot |
| survives DHCP change | change the phone's IP, press start, still connects |
| survives auto-off | toggle wireless debugging off, press start, still connects |
| safety | `adb_keys.bak` exists; a second run is a no-op; key never truncated; hook file lands in the detected framework's dir |

## 7. Non-goals

- Not replacing adb with a custom transport — adbd's TLS + auth is correct and
  already solved.
- Not automating the RSA dialog. It is in System UI; no sanctioned API reaches
  it. Root injection is the only route, and it needs one human "Allow".
- Not supporting non-root phones automatically. They get the pairing screen,
  which is a deliberate, visible, one-time human step.
