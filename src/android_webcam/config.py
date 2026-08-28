"""Config + constants — mirrors ~/.config/android-screen/config and scrcpy --list-cameras output."""
from pathlib import Path
import json

CONFIG_DIR = Path.home() / ".config" / "android-webcam"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG = Path.home() / ".config" / "android-screen" / "config"

DEFAULTS = {
    "android_ip": "192.168.1.12",
    "android_port": 5555,
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
    #   mic_to_phone : also forward host microphone back to phone audio output.
    #                   Enables a phone-side mic that captures host audio
    #                   (e.g. for a phone-as-speaker use case).  OFF by
    #                   default to avoid feedback loops with phone speakers.
    "mic_to_host": True,
    "mic_mute": False,        # unmuted by default — phone IS the system mic
    "mic_to_phone": False,
    "audio_source": "mic",
    "with_preview": False,
    "v4l2_sink": "/dev/video0",
    "v4l2_buffer": 120,
    "video_bit_rate": "8M",
    "stay_brightness_low": True,
    "auto_rotate": True,      # match scrcpy window to window aspect
}

SIZES = {"720p": "1280x720", "1080p": "1920x1080"}
FPS_CHOICES = [15, 30, 60, 120]

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
