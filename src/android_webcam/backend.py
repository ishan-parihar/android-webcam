"""scrcpy ↔ v4l2 backend. Pure argv builder + lifecycle; no GTK."""
import subprocess, shlex, shutil, time, json, os
from pathlib import Path
from .config import SIZES, load, save, target

def scrcpy_exists() -> bool: return shutil.which("scrcpy") is not None
def adb_exists() -> bool: return shutil.which("adb") is not None

def build_argv(cfg: dict, extra: list | None = None) -> list[str]:
    """Compose scrcpy argv.

    Audio is composed of two independent flags:
      mic_to_host  (default ON,  muted by mic_mute) -> --audio-source=mic
      mic_to_phone (default OFF)                    -> --audio-dup + output
      mic_mute     (default True)                   -> -Pmute=1 pipewire flag
    """
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    size = cfg.get("custom_size") or SIZES.get(cfg.get("resolution","1080p"), "1920x1080")
    facing = cfg.get("camera_facing","back")
    cam_id = cfg.get("camera_id")
    argv = ["scrcpy", f"--tcpip={tgt}", "--video-source=camera"]
    if cam_id is not None and cam_id != "":
        argv += [f"--camera-id={cam_id}"]
    else:
        argv += [f"--camera-facing={facing}"]
    argv += [f"--camera-size={size}", f"--camera-fps={cfg.get('fps',30)}"]
    if cfg.get("torch"): argv.append("--camera-torch")
    if cfg.get("auto_rotate"):
        # user-controlled orientation; can be overridden by start() with live
        # rotation from the rotator thread.
        argv += ["--orientation=0"]
    argv += [f"--v4l2-sink={cfg.get('v4l2_sink','/dev/video0')}", f"--v4l2-buffer={cfg.get('v4l2_buffer',120)}"]
    if cfg.get("video_bit_rate"): argv += [f"--video-bit-rate={cfg['video_bit_rate']}"]
    if not cfg.get("with_preview"): argv.append("--no-window")

    # Audio: two-flag composition
    mic_to_host = bool(cfg.get("mic_to_host", True))
    mic_to_phone = bool(cfg.get("mic_to_phone", False))
    mic_mute = bool(cfg.get("mic_mute", True))
    if mic_to_host and not mic_to_phone:
        # forward mic to host only (recommended; default)
        argv.append(f"--audio-source={cfg.get('audio_source','mic')}")
    elif mic_to_host and mic_to_phone:
        # mic + duplex audio to phone (full audio loop — risk of feedback)
        argv += [f"--audio-source={cfg.get('audio_source','mic')}", "--audio-dup"]
    elif not mic_to_host and mic_to_phone:
        # host audio -> phone speakers, no mic forwarding
        argv += ["--audio-source=playback", "--audio-dup"]
    else:
        # neither
        argv.append("--no-audio")

    # Mute the forwarded mic on host (PipeWire/Pulse) to avoid echo unless
    # the user has explicitly turned mute off. We use the scrcpy-internal
    # route via a Pulse/PipeWire mute. The standard CLI doesn't expose a
    # mute switch, so we set the source's mute property via pactl/wpctl
    # after start().  Here we just tag the config; the muting is done
    # by start() via apply_mic_mute().
    if extra: argv += extra
    return argv

def apply_mic_mute(cfg: dict) -> bool:
    """After the scrcpy process registers the PipeWire/Pulse source for the
    forwarded mic, mute it. Returns True on success. No-op if mic_to_host is
    off. Skipped silently if `pactl`/`wpctl` are unavailable.
    """
    if not cfg.get("mic_to_host", True): return True
    if not cfg.get("mic_mute", True): return True
    # Find the "scrcpy" source via pactl or wpctl and mute it.
    name = "scrcpy"
    for cmd in (
        ["pactl", "set-source-mute", name, "1"],
        ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode == 0: return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False

def adb_ping(cfg: dict, timeout=3) -> tuple[bool,str]:
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    try:
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"], capture_output=True, text=True, timeout=timeout)
        if "ok" in r.stdout:
            return (True, r.stdout.strip())
        rc,msg = connect_adb(cfg)
        if not rc: return (False, msg)
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"], capture_output=True, text=True, timeout=timeout)
        return ("ok" in r.stdout, r.stdout.strip() or r.stderr.strip() or msg)
    except Exception as e:
        rc,msg = connect_adb(cfg)
        if not rc: return (False, f"ping fail: {e}; connect: {msg}")
        try:
            r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"], capture_output=True, text=True, timeout=timeout)
            return ("ok" in r.stdout, r.stdout.strip() or r.stderr.strip())
        except Exception as e2:
            return (False, f"retry fail: {e2}")

def connect_adb(cfg: dict) -> tuple[bool,str]:
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    try:
        r = subprocess.run(["adb","connect",tgt], capture_output=True, text=True, timeout=5)
        msg = (r.stdout+r.stderr).strip()
        if "connected" in msg or r.returncode==0:
            return (True, msg)
        return (False, msg)
    except Exception as e:
        return (False, str(e))

def discover_phone(cfg: dict, gateway: str = "192.168.1.1") -> tuple[bool,str]:
    """Scan the local /24 for an open 5555 adb port; if found, update cfg and return new target.
    Tries 5555, 5554, 5556-5558 (Termux default + adb pair fallback).
    """
    import re as _re
    from .config import save
    def _subnet():
        try:
            with open("/proc/net/route") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if parts[1] != "00000000" or not int(parts[3],16) & 2: continue
                    iface = parts[0]
                    r = subprocess.run(["ip","-4","addr","show",iface], capture_output=True, text=True, timeout=2)
                    m = _re.search(r"inet (\d+\.\d+\.\d+)\/(\d+)", r.stdout)
                    if m:
                        import ipaddress
                        net = ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}", strict=False)
                        return str(net.network_address), int(m.group(2)), iface
        except Exception: pass
        return ("192.168.1.0", 24, "wlan0")
    base,prefix,iface = _subnet()
    if prefix != 24:
        return (False, f"non-/24 subnet ({prefix}) - manual IP needed")
    import concurrent.futures, socket
    found = []
    ports = (5555, 5554, 5556, 5557, 5558)
    def _probe(ip_port):
        ip, port = ip_port
        try:
            s = socket.socket(); s.settimeout(0.4)
            s.connect((ip, port)); s.close()
            return (ip, port)
        except Exception: return None
    targets = [(f"{base.rsplit('.',1)[0]}.{i}", p) for i in range(1,255) for p in ports]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for r in ex.map(_probe, targets):
            if r: found.append(r)
    if not found:
        return (False, f"no 5555/5554/5556-5558 open in {base}/24")
    for ip, port in found:
        try:
            cr = subprocess.run(["adb","connect",f"{ip}:{port}"], capture_output=True, text=True, timeout=4)
            if "connected" not in cr.stdout + cr.stderr: continue
            sr = subprocess.run(["adb","-s",f"{ip}:{port}","shell","getprop ro.product.model"], capture_output=True, text=True, timeout=4)
            model = sr.stdout.strip()
            if model:
                cfg["android_ip"] = ip
                cfg["android_port"] = port
                save(cfg)
                return (True, f"found {model} at {ip}:{port}")
            subprocess.run(["adb","disconnect",f"{ip}:{port}"], capture_output=True, timeout=2)
        except Exception: continue
    return (False, f"found adb on {found} but none is Android")

def start(cfg: dict) -> subprocess.Popen:
    """Start scrcpy with the given config. Returns the Popen. Caller is
    responsible for the process lifecycle. If cfg['auto_rotate'] is True and
    Hyprland reports an active scrcpy window whose aspect changes, the
    caller can use rotation.start_rotator() to re-launch.
    """
    argv = build_argv(cfg)
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    try:
        subprocess.run(["adb","connect",tgt], capture_output=True, text=True, timeout=5)
    except Exception: pass
    if cfg.get("stay_brightness_low"):
        try:
            subprocess.run(["adb","-s",tgt,"shell",
                            "settings put system screen_brightness_mode 0; settings put system screen_brightness 1; cmd display set-brightness 0.001"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception: pass
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    # Try to mute the forwarded mic so the user doesn't get echo by default
    if cfg.get("mic_to_host", True) and cfg.get("mic_mute", True):
        def _delay_mute():
            time.sleep(2)  # wait for scrcpy to register the PipeWire source
            apply_mic_mute(cfg)
        threading_mod = __import__("threading")
        threading_mod.Thread(target=_delay_mute, daemon=True).start()
    return p

def cli():
    """android-webcam-cli — headless for scripts/Hyprland binds."""
    import argparse
    p = argparse.ArgumentParser(description="android-webcam native (scrcpy camera → v4l2)")
    p.add_argument("--front", action="store_true"); p.add_argument("--back", action="store_true")
    p.add_argument("--720", dest="r720", action="store_true"); p.add_argument("--1080", dest="r1080", action="store_true")
    p.add_argument("--size", help="WxH custom"); p.add_argument("--fps", type=int)
    p.add_argument("--torch", action="store_true"); p.add_argument("--no-torch", dest="no_torch", action="store_true")
    # audio flags: two independent switches
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-mic", action="store_true", help="disable mic forwarding (no --audio-source)")
    g.add_argument("--mic", action="store_true", help="forward phone mic to host (muted by default)")
    g.add_argument("--mic-mute-off", action="store_true", help="forward mic and unmute (no echo protection)")
    g.add_argument("--mic-to-speaker", action="store_true", help="forward host mic to phone speakers (--audio-source=playback --audio-dup)")
    g.add_argument("--mic-and-speaker", action="store_true", help="forward phone mic to host AND host mic to phone (full duplex)")
    p.add_argument("--with-preview", action="store_true"); p.add_argument("--no-window", action="store_true")
    p.add_argument("--v4l2-sink", default=None); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--discover", action="store_true", help="Scan LAN for phone adb and update config")
    p.add_argument("--set-ip", help="Set phone IP and save to config")
    p.add_argument("--no-rotate", action="store_true", help="disable auto-rotate")
    a = p.parse_args()
    cfg = load()
    if a.set_ip:
        if ":" in a.set_ip:
            ip,port = a.set_ip.rsplit(":",1)
            cfg["android_ip"]=ip; cfg["android_port"]=int(port)
        else:
            cfg["android_ip"]=a.set_ip
        save(cfg)
        print(f"Saved IP {cfg['android_ip']}:{cfg['android_port']} to config")
        return
    if a.discover:
        ok,msg = discover_phone(cfg)
        print(msg)
        return
    if a.back: cfg["camera_facing"]="back"
    if a.front: cfg["camera_facing"]="front"
    if a.r720: cfg["resolution"]="720p"
    if a.r1080: cfg["resolution"]="1080p"
    if a.size: cfg["custom_size"]=a.size
    if a.fps: cfg["fps"]=a.fps
    if a.torch: cfg["torch"]=True
    if a.no_torch: cfg["torch"]=False
    # audio
    if a.no_mic: cfg["mic_to_host"]=False; cfg["mic_to_phone"]=False
    if a.mic: cfg["mic_to_host"]=True; cfg["mic_to_phone"]=False; cfg["mic_mute"]=True
    if a.mic_mute_off: cfg["mic_to_host"]=True; cfg["mic_to_phone"]=False; cfg["mic_mute"]=False
    if a.mic_to_speaker: cfg["mic_to_host"]=False; cfg["mic_to_phone"]=True; cfg["mic_mute"]=True
    if a.mic_and_speaker: cfg["mic_to_host"]=True; cfg["mic_to_phone"]=True; cfg["mic_mute"]=False
    if a.with_preview: cfg["with_preview"]=True
    if a.no_window: cfg["with_preview"]=False
    if a.v4l2_sink: cfg["v4l2_sink"]=a.v4l2_sink
    if a.no_rotate: cfg["auto_rotate"]=False
    argv = build_argv(cfg)
    print(" ".join(shlex.quote(s) for s in argv))
    if a.dry_run: return
    if not scrcpy_exists(): print("scrcpy not found — pacman -S scrcpy"); return
    subprocess.run(argv)

if __name__ == "__main__":
    cli()
