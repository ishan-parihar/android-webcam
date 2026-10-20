"""Config + constants — mirrors ~/.config/android-screen/config and scrcpy --list-cameras output."""
from pathlib import Path
import json

CONFIG_DIR = Path.home() / ".config" / "android-webcam"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG = Path.home() / ".config" / "android-screen" / "config"

DEFAULTS = {
    "android_ip": "192.168.1.12",
    "android_port": 0,
    "camera_facing": "back",  # back/front
    "camera_id": None,        # overrides facing if set
    "resolution": "1080p",    # 720p/1080p (default = 1080p)
    "custom_size": None,      # "1920x1080" etc
    "fps": 30,
    "torch": False,
    # Two independent audio flags (replaces single with_audio):
    #   mic_to_host  : forward phone mic to host as PipeWire source "scrcpy".
    #                   Default ON, UNMUTED — phone acts as a real mic for the
    #                   system.  Apps (browser, zoom, meet, etc.) just pick
    #                   "scrcpy" in their input picker and it works.
    #   mic_mute     : force-mute the forwarded mic on host at startup.  OFF
    #                   by default.  Enable if you hear echo/feedback (e.g.
    #                   your phone is sitting near your speakers and you have
    #                   a mic-monitoring app open).
    #   mic_to_phone : capture the PHONE'S OWN audio playback back to the host
    #                   (scrcpy --audio-source=playback --audio-dup), which
    #                   keeps playing on the device.  This is NOT host -> phone
    #                   audio: scrcpy has no host-to-phone audio path, so the
    #                   earlier "phone-as-speaker" description was wrong.
    #                   OFF by default.
    #   mic_default  : promote the forwarded phone mic to the system default
    #                   input source at startup.  ON by default.  Apps
    #                   (browser, zoom, etc.) pick it up without any manual
    #                   configuration.  Restored on stop.
    "mic_to_host": True,
    "mic_mute": False,        # unmuted by default — phone IS the system mic
    "mic_to_phone": False,
    "mic_default": True,      # phone mic becomes system default source on start
    "audio_source": "mic",
    "with_preview": False,
    "v4l2_sink": "/dev/video0",
    "v4l2_buffer": 120,
    "video_bit_rate": "8M",
    "stay_brightness_low": True,
    "auto_rotate": True,      # match scrcpy window to window aspect
    # Private state, not a setting: the PipeWire source this app displaced
    # when it promoted the phone mic, so it can be restored on stop. Persisted
    # through update() like any other key, which is why it must be declared
    # here - persist() diffs self.cfg wholesale and would otherwise raise
    # KeyError on it, and the mic-restore call site swallows exceptions, so
    # the breakage would show up only as a mic that never comes back.
    "_mic_prev": None,

    # Companion app (android-companion/) integration. companion_host_port is
    # the host-side adb forward port; see docs/companion-protocol.md. The
    # companion is OPTIONAL - the scrcpy path needs none of this.
    "companion_host_port": 7878,
    "companion_keepalive": True,   # ask the phone to hold a wake lock while streaming
}

SIZES = {"720p": "1280x720", "1080p": "1920x1080"}
FPS_CHOICES = [15, 30, 60, 120]


def update(**changes) -> dict:
    """Merge `changes` into the config on disk and return what was written.

    The phone PUSHES its real adb endpoint into this file while the app runs,
    so a whole-dict save from a stale snapshot silently reverts the port. Every
    writer must therefore merge, never overwrite. This is the one place that
    knows how, so the GUI and `android-webcam set` cannot drift apart and
    reintroduce that bug through a second code path.

    `load()` fresh; overlay only the named keys; write back. Keys the caller
    does not name are left exactly as they are on disk - including
    android_ip/android_port, which no settings screen should ever be writing.

    An unknown key raises KeyError. `update` is driven by a QML panel and a CLI,
    and a typo'd key would otherwise be written to disk forever: no effect, no
    error, and nothing to grep for later. Failing loudly at the write is the
    only place the mistake is still visible.

    The write is atomic (temp file in the same directory + os.replace), so a
    concurrent reader sees either the old file or the new one, never a truncated
    one. Atomic is not sufficient on its own: that makes each WRITE indivisible,
    not the load-then-merge-then-replace sequence around it. Two writers could
    both read android_port=0, both merge, and the second replace would discard
    the first - which is how the phone's pushed endpoint gets lost. The lock
    covers the whole read-modify-write, and it is a real one: a sibling lockfile
    held across the read AND the replace, because the processes involved are
    separate (the GUI, a panel-driven `set`, and the /phone handler serving an
    HTTP request) and none of them can coordinate in memory.

    Readers do NOT take the lock. os.replace is atomic, so a lock-free read sees
    a complete file - it may see the previous one for a moment, which is
    exactly what a read-modify-write retry under the lock then corrects.
    """
    import fcntl, os, tempfile
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise KeyError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    cfg: dict = {}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(str(CONFIG_DIR / "config.lock"), "w") as lk:
        # Blocking, with a ceiling. A writer that never exits would otherwise
        # wedge every settings change, including the phone's own endpoint push.
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            # Load HERE, inside the lock, and nowhere else. A read taken before
            # the lock is acquired can already be stale: another writer may
            # commit in between, and merging onto that snapshot is the lost
            # update the lock exists to prevent.
            cfg = load()
            cfg.update(changes)
            fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps(cfg, indent=2))
                os.replace(tmp, CONFIG_FILE)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    return cfg

def load() -> dict:
    cfg = DEFAULTS.copy()
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
    # legacy fallback (shell source)
    elif LEGACY_CONFIG.exists():
        try:
            txt = LEGACY_CONFIG.read_text()
            for line in txt.splitlines():
                if "ANDROID_IP" in line and "=" in line:
                    cfg["android_ip"] = line.split("=")[1].strip().strip('"').strip("'")
                if "ANDROID_PORT" in line and "=" in line:
                    cfg["android_port"] = int(line.split("=")[1].strip().strip('"').strip("'"))
        except Exception:
            pass
    return cfg

def save(cfg: dict) -> None:
    """REMOVED - use update(**changes).

    This was an unlocked whole-dict write, and every caller of it was a
    read-modify-write: it loaded a snapshot, changed two keys, and wrote the
    lot back. That is a lost update waiting to happen - the phone pushes its
    endpoint at runtime, and a snapshot taken before the push reverts it. It had
    four call sites, all of which now use update(), which merges under a lock.

    It is kept only as a hard error so an old caller fails loudly rather than
    silently writing a stale snapshot.
    """
    raise NotImplementedError(
        "config.save() is gone: it wrote the whole dict from a snapshot, which "
        "reverts the endpoint the phone pushes at runtime. Use update(**changes), "
        "which merges under a lock."
    )

def target(cfg: dict) -> str:
    return f"{cfg['android_ip']}:{cfg['android_port']}"
