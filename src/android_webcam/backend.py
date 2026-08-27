"""scrcpy ↔ v4l2 backend. Pure argv builder + lifecycle; no GTK."""
import subprocess, shlex, shutil, time, json
from pathlib import Path
from .config import SIZES, load, save, target

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
        if "ok" in r.stdout:
            return (True, r.stdout.strip())
        # try connect first
        rc,msg = connect_adb(cfg)
        if not rc: return (False, msg)
        r = subprocess.run(["adb","-s",tgt,"shell","echo","ok"], capture_output=True, text=True, timeout=timeout)
        return ("ok" in r.stdout, r.stdout.strip() or r.stderr.strip() or msg)
    except Exception as e:
        # try connect
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
    Scans reachable hosts from arp cache first, then probes /24 in parallel batches.
    """
    import re as _re
    from .config import save
    # read /proc/net/route for local interface subnet
    def _subnet():
        try:
            with open("/proc/net/route") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if parts[1] != "00000000" or not int(parts[3],16) & 2: continue
                    iface = parts[0]
                    gw_hex = parts[2]
                    # parse via ip route (more reliable)
                    import subprocess as sp
                    r = sp.run(["ip","-4","addr","show",iface], capture_output=True, text=True, timeout=2)
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
    def _probe(ip):
        try:
            s = socket.socket(); s.settimeout(0.5)
            s.connect((ip, 5555)); s.close()
            return ip
        except Exception: return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        for r in ex.map(_probe, [f"{base.rsplit('.',1)[0]}.{i}" for i in range(1,255)]):
            if r: found.append(r)
    if not found:
        return (False, f"no 5555 open in {base}/24")
    # try each candidate — connect + verify it's a phone
    for ip in found:
        try:
            cr = subprocess.run(["adb","connect",f"{ip}:5555"], capture_output=True, text=True, timeout=4)
            if "connected" not in cr.stdout + cr.stderr: continue
            sr = subprocess.run(["adb","-s",f"{ip}:5555","shell","getprop ro.product.model"], capture_output=True, text=True, timeout=4)
            model = sr.stdout.strip()
            if model:
                cfg["android_ip"] = ip
                save(cfg)
                return (True, f"found {model} at {ip}:5555")
            subprocess.run(["adb","disconnect",f"{ip}:5555"], capture_output=True, timeout=2)
        except Exception: continue
    return (False, f"found 5555 open on {found} but none is Android")

def list_cameras(cfg: dict) -> str:
    try:
        r = subprocess.run(["scrcpy","--list-cameras"], capture_output=True, text=True, timeout=8)
        return r.stdout + r.stderr
    except Exception as e:
        return str(e)

def start(cfg: dict) -> subprocess.Popen:
    argv = build_argv(cfg)
    # ensure connection before launching scrcpy (avoids 'No route to host' / 'Connection refused')
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    try:
        subprocess.run(["adb","connect",tgt], capture_output=True, text=True, timeout=5)
    except Exception: pass
    # brightness low hook (best-effort, no root needed for settings/cmd)
    if cfg.get("stay_brightness_low"):
        try:
            subprocess.run(["adb","-s",tgt,"shell",
                            "settings put system screen_brightness_mode 0; settings put system screen_brightness 1; cmd display set-brightness 0.001"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception: pass
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
    p.add_argument("--discover", action="store_true", help="Scan LAN for phone adb (port 5555) and update config")
    p.add_argument("--set-ip", help="Set phone IP and save to config (like android-screen)")
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
