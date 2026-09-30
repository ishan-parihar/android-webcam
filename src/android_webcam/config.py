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
    concurrent reader sees either the old file or the new one, never a
    truncated one. There is no lock: config is a few hundred bytes, writes take
    microseconds, and flock would not help across the GUI and a CLI process on
    a config that is re-read wholesale.
    """
    import os, tempfile
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise KeyError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    cfg = load()
    cfg.update(changes)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def target(cfg: dict) -> str:
    return f"{cfg['android_ip']}:{cfg['android_port']}"
