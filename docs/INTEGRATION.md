# Integration record — `android-webcam-suite` and `android-webcam` are one project

Recorded 2026-09-26 when `android-webcam-suite` was created, and rewritten
2026-10-20 when the two were conjoined.

## What happened

On 2026-09-26 a second repository, `android-webcam-suite`, was created as "the
development home", with the plan that `android-webcam` would be retired into
it. **That plan was never executed, and it was overtaken by events.** The
suite's product code froze on 2026-09-27; `android-webcam`'s kept moving and
reached 2026-10-01.

The result was two projects, one of them carrying the older copy of the
plugin, with the suite's own `README.md` claiming the direction of travel
that had, by then, reversed.

| | `android-webcam` | `android-webcam-suite` |
|---|---|---|
| product code last touched | 2026-10-01 | 2026-09-27 |
| commits | 114 | 48 |
| `Panel.qml` | 257 lines | 65 lines |
| `setsid -f` detach | present | absent |
| remote | `ishan-parihar/android-webcam` | none — 48 commits local only |

## Which one survived, and why

`android-webcam`. Its host is a strict superset of the suite's: `app.py` is
329 lines larger, `backend.py` 440, and the extra code is features —
audio-session ownership of the mic, brightness, torch, rotation persistence,
a status refresh.

The suite's `discovery.py` was also upstreamed into the webcam's `backend.py`
and *improved* on the way: the suite's module scanned for the phone, and the
webcam's `_known_adb_endpoints()` scans the same three sources (adb mDNS,
avahi, the kernel neighbour cache) with measured fallbacks, because
`avahi-browse` is long-running by design and a single shot is a coin flip on
a busy LAN.

## What the suite had that the webcam did not

Three things, and all three are now here:

1. **`android-companion/`** — a Kotlin app that serves the phone's camera
   over a JSON-lines protocol on an `adb forward`, instead of asking
   scrcpy's screen-mirroring server for a different video source. It is
   **optional**: the scrcpy path needs no APK and is the supported one.
2. **`companion.py`** — the host-side client, and the `--status` JSON
   contract the panel reads.
3. **The plugin's newer QML.** The installed plugin had fixes that were in
   *neither* repository — the `Commons.Color` form, which is the current
   Quickshell idiom. Those were adopted here, and the installed copy and the
   repository are byte-identical again.

## What the two repos disagreed about, and how it was settled

| | `android-webcam` | `android-webcam-suite` | settled as |
|---|---|---|---|
| plugin location | repo root | `omarchy-plugin/` | **repo root** — that is where `omarchy plugin add` looks, and it means one copy of each file |
| discovery | `_known_adb_endpoints()` in `backend.py` | `discovery.py` | **`backend.py`** — the evolved version, and the suite's module was its ancestor |
| config write | `update()` only; `save()` is a hard error | `save()` | **`update()`** — a whole-dict save from a snapshot reverts the port the phone pushes at runtime |
| camera facing | `camera_facing` + `camera_id` | same | unchanged |

The companion's `sync` and `Session` were ported to `update()` for the same
reason the GUI's own writer was: writing the whole dict from a snapshot is how
the phone's pushed endpoint gets lost.

## The other thing the integration found

Two defects that were not about the split at all:

- **The gate was broken.** `genome.toml` named `pytest`, but eleven of the
  thirteen test files are standalone scripts that call `sys.exit()` at the
  end. pytest's collector imported the module, the module exited, and the gate
  exited 3 on a green tree. `scripts/run-tests.sh` runs both kinds the way
  each was written to be run.
- **The installed command was broken.** The editable install pointed at the
  pre-reorganisation path, so `android-webcam is-streaming` — the command the
  Omarchy panel runs every three seconds — failed with
  `ModuleNotFoundError`. The path was corrected; the command works.

And one gap neither repo had: there was no persistent udev rule, so
`/dev/video0`'s permissions were fixed once by the installer and reset on the
next reboot, and the webcam silently vanished from every app that opens a
camera. `udev/rules.d/99-android-webcam.rules` is that missing half, and the
README says so.

## What this repo is now

One repository, one project, one plugin. `android-webcam-suite` is retired:
its history and its remote-free 48 commits are preserved here, and the
repository is kept as a tombstone rather than deleted.
