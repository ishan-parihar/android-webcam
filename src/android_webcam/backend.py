"""scrcpy ↔ v4l2 backend. Pure argv builder + lifecycle; no GTK."""
import subprocess, shlex, shutil, time, json
from pathlib import Path
from .config import SIZES, load

def scrcpy_exists() -> bool: return shutil.which("scrcpy") is not None
def adb_exists() -> bool: return shutil.which("adb") is not None

def build_argv(cfg: dict, extra: list | None = None) -> list[str]:
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    size = cfg.get("custom_size") or SIZES.get(cfg.get("resolution","720p"), "1280x720")
    facing = cfg.get("camera_facing","back")
    cam_id = cfg.get("camera_id")
    argv = ["scrcpy", f"--tcpip={tgt}", "--video-source=camera"]
    if cam_id is not None and cam_id != "":
        argv += [f"--camera-id={cam_id}"]
    else:
        argv += [f"--camera-facing={facing}"]
    argv += [f"--camera-size={size}", f"--camera-fps={cfg.get('fps',30)}"]
    if cfg.get("torch"): argv.append("--camera-torch")
    argv += [f"--v4l2-sink={cfg.get('v4l2_sink','/dev/video0')}", f"--v4l2-buffer={cfg.get('v4l2_buffer',120)}"]
    if cfg.get("video_bit_rate"): argv += [f"--video-bit-rate={cfg['video_bit_rate']}"]
    if not cfg.get("with_preview"): argv.append("--no-window")
    if cfg.get("with_audio"):
        argv += [f"--audio-source={cfg.get('audio_source','mic')}"]  # mic/mic-unprocessed/camcorder etc; warns echo if speakers
    else:
        argv.append("--no-audio")
    if extra: argv += extra
    return argv

def adb_ping(cfg: dict, timeout=3) -> tuple[bool,str]:
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    try:
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"], capture_output=True, text=True, timeout=timeout)
        return ("ok" in r.stdout, r.stdout.strip() or r.stderr.strip())
    except Exception as e:
        return (False, str(e))

def connect_adb(cfg: dict) -> tuple[bool,str]:
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    try:
        r = subprocess.run(["adb","connect",tgt], capture_output=True, text=True, timeout=5)
        return (r.returncode==0, (r.stdout+r.stderr).strip())
    except Exception as e:
        return (False, str(e))

def list_cameras(cfg: dict) -> str:
    try:
        r = subprocess.run(["scrcpy","--list-cameras"], capture_output=True, text=True, timeout=8)
        return r.stdout + r.stderr
    except Exception as e:
        return str(e)

def start(cfg: dict) -> subprocess.Popen:
    argv = build_argv(cfg)
    # brightness low hook (best-effort, no root needed for settings/cmd)
    if cfg.get("stay_brightness_low"):
        tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
        subprocess.Popen(["adb","-s",tgt,"shell",
                          "settings put system screen_brightness_mode 0; settings put system screen_brightness 1; cmd display set-brightness 0.001"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

def cli():
    """android-webcam-cli — headless for scripts/Hyprland binds."""
    import argparse
    p = argparse.ArgumentParser(description="android-webcam native (scrcpy camera → v4l2)")
    p.add_argument("--front", action="store_true"); p.add_argument("--back", action="store_true")
    p.add_argument("--720", dest="r720", action="store_true"); p.add_argument("--1080", dest="r1080", action="store_true")
    p.add_argument("--size", help="WxH custom"); p.add_argument("--fps", type=int)
    p.add_argument("--torch", action="store_true"); p.add_argument("--no-torch", dest="no_torch", action="store_true")
    p.add_argument("--with-audio", action="store_true"); p.add_argument("--no-audio", dest="no_audio", action="store_true")
    p.add_argument("--with-preview", action="store_true"); p.add_argument("--no-window", action="store_true")
    p.add_argument("--v4l2-sink", default=None); p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    cfg = load()
    if a.back: cfg["camera_facing"]="back"
    if a.front: cfg["camera_facing"]="front"
    if a.r720: cfg["resolution"]="720p"
    if a.r1080: cfg["resolution"]="1080p"
    if a.size: cfg["custom_size"]=a.size
    if a.fps: cfg["fps"]=a.fps
    if a.torch: cfg["torch"]=True
    if a.no_torch: cfg["torch"]=False
    if a.with_audio: cfg["with_audio"]=True
    if a.no_audio: cfg["with_audio"]=False
    if a.with_preview: cfg["with_preview"]=True
    if a.no_window: cfg["with_preview"]=False
    if a.v4l2_sink: cfg["v4l2_sink"]=a.v4l2_sink
    argv = build_argv(cfg)
    print(" ".join(shlex.quote(s) for s in argv))
    if a.dry_run: return
    if not scrcpy_exists(): print("scrcpy not found — pacman -S scrcpy"); return
    subprocess.run(argv)

if __name__ == "__main__":
    cli()
