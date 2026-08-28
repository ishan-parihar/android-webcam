"""scrcpy ↔ v4l2 backend. Pure argv builder + lifecycle; no GTK."""
import subprocess, shlex, shutil, time, json, os
from pathlib import Path
from .config import SIZES, load, save, target

def scrcpy_exists() -> bool: return shutil.which("scrcpy") is not None
def adb_exists() -> bool: return shutil.which("adb") is not None

def build_argv(cfg: dict, extra: list | None = None) -> list[str]:
    """Compose scrcpy argv.

    Audio model (two independent flags):
      mic_to_host  (default ON)  -> --audio-source=mic
                                   + --no-audio-playback (no echo!)
      mic_to_phone (default OFF) -> --audio-source=playback --audio-dup
                                    (host audio -> phone speakers; use phone
                                     as a remote speakerphone)
      mic_mute     (default False) -> pactl set-source-mute scrcpy 1 after start
      mic_default  (default True)  -> pactl set-default-source scrcpy after start

    Critical: --no-audio-playback MUST be added when mic_to_host is on and
    mic_to_phone is off.  Without it, scrcpy plays the captured phone mic
    through the host's default sink (HDMI speakers) creating an infinite
    echo loop.  See issue: "My speakers are still routing the phone's mic".
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

    # Audio: two-flag composition (mutually exclusive combos)
    mic_to_host = bool(cfg.get("mic_to_host", True))
    mic_to_phone = bool(cfg.get("mic_to_phone", False))
    if mic_to_host and not mic_to_phone:
        # Default: phone mic -> host as PipeWire source for apps to use.
        # --no-audio-playback prevents scrcpy from playing the mic through
        # the host's default sink (HDMI speakers), which is what causes
        # the infinite echo.  Apps pull from the 'scrcpy' source as normal.
        argv += [
            f"--audio-source={cfg.get('audio_source','mic')}",
            "--no-audio-playback",
        ]
    elif mic_to_host and mic_to_phone:
        # phone mic -> host AND host audio -> phone (full duplex, echo risk)
        argv += [f"--audio-source={cfg.get('audio_source','mic')}", "--audio-dup"]
    elif not mic_to_host and mic_to_phone:
        # "Speakerphone" mode: host audio -> phone speakers, no mic forwarding
        argv += ["--audio-source=playback", "--audio-dup"]
    else:
        # neither
        argv.append("--no-audio")

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


def set_phone_as_system_mic(cfg: dict) -> dict:
    """Make the forwarded phone mic the system default input source.

    Returns a dict with the previous default source info so it can be
    restored on stop.  No-op if mic_to_host is off.

    Without this, the scrcpy source appears in apps' input picker but apps
    default to whatever the system default was before (often a monitor
    source or nothing).  This is the difference between "you can pick it
    manually in each app" and "your system mic is just the phone".
    """
    info = {"prev_default_source": None, "prev_default_mute": None, "set": False}
    if not cfg.get("mic_to_host", True):
        return info
    # Wait briefly for scrcpy to register the source
    time.sleep(2.0)
    # 1) capture previous default
    try:
        r = subprocess.run(["pactl","get-default-source"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            info["prev_default_source"] = r.stdout.strip()
            # also capture its mute state so we can restore it
            r2 = subprocess.run(["pactl","get-source-mute", info["prev_default_source"]],
                                 capture_output=True, text=True, timeout=3)
            info["prev_default_mute"] = "yes" in r2.stdout.lower() if r2.returncode == 0 else None
    except Exception: pass
    # 2) set scrcpy as default + unmuted (or muted if mic_mute flag is on)
    want_mute = "1" if cfg.get("mic_mute", False) else "0"
    for cmd in (
        ["pactl", "set-default-source", "scrcpy"],
        ["pactl", "set-source-mute", "scrcpy", want_mute],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                # 'No such entity' if the scrcpy source isn't yet registered.
                # wpctl fallback for WirePlumber:
                if "scrcpy" in cmd[1:]:
                    try:
                        subprocess.run(["wpctl","set-default","scrcpy"], capture_output=True, timeout=3)
                    except Exception: pass
        except FileNotFoundError: continue
        except Exception: continue
    info["set"] = True
    return info


def restore_system_audio(prev: dict) -> bool:
    """Restore the default audio source after scrcpy exits."""
    if not prev or not prev.get("prev_default_source"): return False
    src = prev["prev_default_source"]
    try:
        subprocess.run(["pactl","set-default-source",src], capture_output=True, timeout=3)
        if prev.get("prev_default_mute") is not None:
            want = "1" if prev["prev_default_mute"] else "0"
            subprocess.run(["pactl","set-source-mute",src,want], capture_output=True, timeout=3)
        return True
    except Exception:
        return False

def adb_ping(cfg: dict, timeout=4) -> tuple[bool,str]:
    """Probe the phone.  Two-stage: (1) adb shell echo, (2) fall back to
    TCP probe + reconnect, (3) auto-discover on failure.  Always returns
    (bool, message) and never raises.
    """
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    # 1. quick reachability
    try:
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"],
                          capture_output=True, text=True, timeout=timeout)
        if "ok" in r.stdout:
            return (True, r.stdout.strip())
    except Exception: pass
    # 2. reconnect then retry
    try: connect_adb(cfg)
    except Exception: pass
    try:
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"],
                          capture_output=True, text=True, timeout=timeout)
        if "ok" in r.stdout:
            return (True, r.stdout.strip())
    except Exception: pass
    # 3. last resort: quick_reachable (TCP + connect) without scanning
    if quick_reachable(cfg):
        try:
            r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"],
                              capture_output=True, text=True, timeout=timeout)
            if "ok" in r.stdout:
                return (True, r.stdout.strip())
        except Exception: pass
        return (True, f"reachable at {tgt}")
    return (False, f"unreachable at {tgt}")

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

def _wait_device_ready(ip: str, port: int, timeout: float = 6.0) -> bool:
    """After `adb connect` returns 'connected', the per-device auth handshake
    is still in progress for ~1-3s.  Issuing `adb shell` immediately times out
    because the device is in 'authorizing' / 'offline' state.

    This polls `adb -s IP:PORT get-state` every 200ms until it returns
    'device' or the timeout elapses.  Returns True iff ready.
    """
    tgt = f"{ip}:{port}"
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        try:
            r = subprocess.run(["adb","-s",tgt,"get-state"],
                               capture_output=True, text=True, timeout=2)
            if "device" in r.stdout:  # exactly 'device\n' (no 'offline' prefix)
                return True
        except Exception: pass
        _t.sleep(0.2)
    return False


def _adb_shell_echo(ip: str, port: int, timeout: float = 4.0) -> bool:
    """Issue `adb -s IP:PORT shell echo ok` and return True iff 'ok' returned.
    Caller should have called _wait_device_ready first.
    """
    tgt = f"{ip}:{port}"
    try:
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"],
                           capture_output=True, text=True, timeout=timeout)
        return "ok" in r.stdout
    except Exception:
        return False


def discover_phone(cfg: dict, gateway: str = "192.168.1.1", quick: bool = False) -> tuple[bool,str]:
    """Scan the local /24 for an open 5555 adb port; if found, update cfg
    and return new target. Tries 5555, 5554, 5556-5558 (Termux default + adb
    pair fallback).

    `quick=True` does a faster scan with a shorter timeout and falls back to
    the arp cache / recent adb devices for quick retries.

    Defensive: skips `192.168.1.1` (gateway's UPnP false positive), skips
    candidates that come back 'offline' from adb, retries 3x with backoff.
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
    tcp_timeout = 0.25 if quick else 0.4
    my_ip_prefix = base.rsplit('.', 1)[0]  # exclude ourselves + gateway
    GATEWAY_IPS = {"192.168.1.1", "10.0.0.1", "192.168.0.1", "10.0.1.1", "172.16.0.1"}  # common UPnP false positives

    def _probe(ip_port):
        ip, port = ip_port
        if ip in GATEWAY_IPS: return None
        try:
            s = socket.socket(); s.settimeout(tcp_timeout)
            s.connect((ip, port)); s.close()
            return (ip, port)
        except Exception: return None
    targets = [(f"{my_ip_prefix}.{i}", p) for i in range(1,255) for p in ports]
    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
        for r in ex.map(_probe, targets):
            if r: found.append(r)
    if not found:
        try:
            r = subprocess.run(["adb","devices"], capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if "device" in line and ":" in line and "offline" not in line:
                    ip_port = line.split()[0]
                    if ":" in ip_port:
                        ip, port = ip_port.split(":", 1)
                        if ip not in GATEWAY_IPS:
                            found.append((ip, port))
        except Exception: pass
    if not found:
        return (False, f"no 5555/5554/5556-5558 open in {base}/24 (excluding gateway)")
    # ONLY kill the adb server if no candidate responds to get-state, AND as a
    # last resort.  Killing adb-server drops ALL in-flight per-device auth
    # handshakes and closes the local TCP socket for the duration of
    # start-server, which causes our subsequent TCP scan to find nothing.
    cfg_ip = cfg.get("android_ip")
    cfg_port = int(cfg.get("android_port", 5555)) if cfg.get("android_port") else None
    # If the configured target is in our candidate list, try it FIRST (avoids
    # killing adb-server if the user is already pointed at the right place).
    if cfg_ip and cfg_port and (cfg_ip, cfg_port) in [(c[0], c[1]) for c in found]:
        found.remove((cfg_ip, cfg_port))
        found.insert(0, (cfg_ip, cfg_port))
    for ip, port in found:
        for attempt in range(3):
            try:
                subprocess.run(["adb","disconnect",f"{ip}:{port}"], capture_output=True, timeout=2)
                time.sleep(0.3)
                cr = subprocess.run(["adb","connect",f"{ip}:{port}"], capture_output=True, text=True, timeout=8)
                if "connected" not in (cr.stdout + cr.stderr):
                    time.sleep(0.5 * (attempt+1))
                    continue
                # wait for auth handshake to finish — without this, the next
                # `adb shell` times out because the device is still 'offline'
                # in the adb daemon's per-device state machine
                if not _wait_device_ready(ip, port, timeout=4.0):
                    time.sleep(0.5 * (attempt+1))
                    continue
                sr = subprocess.run(["adb","-s",f"{ip}:{port}","shell","getprop ro.product.model"],
                                     capture_output=True, text=True, timeout=8)
                model = sr.stdout.strip()
                if model and "error" not in model.lower():
                    cfg["android_ip"] = ip
                    cfg["android_port"] = int(port)
                    save(cfg)
                    return (True, f"found {model} at {ip}:{port}")
                time.sleep(0.5 * (attempt+1))
            except Exception as e:
                time.sleep(0.5 * (attempt+1))
                continue
    # last resort: reset adb server state and retry the first candidate
    logging.info("discover: candidates exhausted, trying kill+start+retry")
    try: subprocess.run(["adb","kill-server"], capture_output=True, timeout=3)
    except Exception: pass
    time.sleep(1.5)
    try: subprocess.run(["adb","start-server"], capture_output=True, timeout=3)
    except Exception: pass
    for ip, port in found[:5]:  # only retry a few, not all 254
        for attempt in range(2):
            try:
                cr = subprocess.run(["adb","connect",f"{ip}:{port}"], capture_output=True, text=True, timeout=8)
                if "connected" not in (cr.stdout + cr.stderr): continue
                if not _wait_device_ready(ip, port, timeout=5.0): continue
                sr = subprocess.run(["adb","-s",f"{ip}:{port}","shell","getprop ro.product.model"],
                                     capture_output=True, text=True, timeout=8)
                model = sr.stdout.strip()
                if model and "error" not in model.lower():
                    cfg["android_ip"] = ip
                    cfg["android_port"] = int(port)
                    save(cfg)
                    return (True, f"found {model} at {ip}:{port} (after kill+start)")
            except Exception: continue
    return (False, f"found adb on {found[:5]} but none responded to getprop after retries")

def quick_reachable(cfg: dict) -> bool:
    """Returns True if the configured target is reachable.  Tries a TCP
    probe, then adb connect, then a brief shell echo.  Tolerates slow
    connections (timeout 4s per step).
    """
    import socket
    tgt_ip = cfg.get("android_ip", "")
    tgt_port = int(cfg.get("android_port", 5555))
    if not tgt_ip: return False
    # 1. TCP probe (fast)
    try:
        s = socket.socket(); s.settimeout(1.5)
        s.connect((tgt_ip, tgt_port)); s.close()
    except Exception:
        # Try adb connect
        try:
            subprocess.run(["adb","connect",f"{tgt_ip}:{tgt_port}"], capture_output=True, text=True, timeout=4)
        except Exception: pass
    # 2. shell echo
    try:
        r = subprocess.run(["adb","-s",f"{tgt_ip}:{tgt_port}","shell","echo","ok"],
                          capture_output=True, text=True, timeout=4)
        return "ok" in r.stdout
    except Exception:
        return False

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
    # Promote phone mic to system default source (so all apps pick it up)
    if cfg.get("mic_to_host", True) and cfg.get("mic_default", True):
        def _delay_default():
            prev = set_phone_as_system_mic(cfg)
            cfg["_mic_prev"] = prev
        threading_mod = __import__("threading")
        threading_mod.Thread(target=_delay_default, daemon=True).start()
    elif cfg.get("mic_to_host", True) and cfg.get("mic_mute", True):
        # Legacy: only mute, don't promote (kept for compatibility)
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
