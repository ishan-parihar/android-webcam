"""scrcpy ↔ v4l2 backend. Pure argv builder + lifecycle; no GTK."""
import subprocess, shlex, shutil, time, json, os, logging, sys
from pathlib import Path
from .config import SIZES, load, save, target

def scrcpy_exists() -> bool: return shutil.which("scrcpy") is not None
def adb_exists() -> bool: return shutil.which("adb") is not None

NULL_SINK = "scrcpy_mic"
NULL_MONITOR = "scrcpy_mic.monitor"

def ensure_null_sink() -> bool:
    """Create a PipeWire/Pulse null sink for phone mic without echo.

    scrcpy --audio-source=mic sends mic audio to the default sink (HDMI)
    as a playback stream, causing echo.  Routing it to a null sink and
    exposing the null sink's monitor as a source gives us a real mic
    without speakers.  Returns True if sink is ready.
    """
    try:
        r = subprocess.run(["pactl","list","short","sinks"], capture_output=True, text=True, timeout=3)
        if NULL_SINK in r.stdout:
            return True
        r = subprocess.run(["pactl","load-module","module-null-sink",
                            f"sink_name={NULL_SINK}",
                            "sink_properties=device.description='PhoneMic_scrcpy'"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 or NULL_SINK in r.stdout
    except Exception:
        return False

def remove_null_sink() -> None:
    try:
        r = subprocess.run(["pactl","list","short","modules"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            if "module-null-sink" in line and NULL_SINK in line:
                mod_id = line.split()[0]
                subprocess.run(["pactl","unload-module",mod_id], capture_output=True, timeout=3)
    except Exception: pass

def ensure_v4l2loopback() -> bool:
    """Ensure /dev/video0 exists; try to load v4l2loopback if missing.
    Returns True if device exists after attempt.
    """
    if Path("/dev/video0").exists():
        return True
    try:
        # Mirror /etc/modprobe.d/v4l2loopback.conf. exclusive_caps MUST stay 0:
        # with 1 the node is output-only unless a reader holds it, so every
        # consumer app (browser, Zoom, Meet) reports "no camera" even though
        # the module is healthy.
        subprocess.run(["sudo","modprobe","v4l2loopback",
                        "card_label=Android Webcam","exclusive_caps=0","video_nr=0"],
                       capture_output=True, timeout=5)
        time.sleep(0.5)
        # Fix perms if needed (udev should do this, but be safe)
        if Path("/dev/video0").exists():
            try:
                subprocess.run(["sudo","chgrp","video","/dev/video0"], capture_output=True, timeout=2)
                subprocess.run(["sudo","chmod","660","/dev/video0"], capture_output=True, timeout=2)
            except Exception: pass
            return True
    except Exception: pass
    return Path("/dev/video0").exists()

def build_argv(cfg: dict, extra: list | None = None) -> list[str]:
    """Compose scrcpy argv.

    Audio model (two independent flags):
      mic_to_host  (default ON)  -> --audio-source=mic  (to null sink, no echo)
      mic_to_phone (default OFF) -> --audio-source=playback --audio-dup
                                     (host audio -> phone speakers; use phone
                                      as a remote speakerphone)
      mic_mute     (default False) -> pactl set-source-mute on null monitor
      mic_default  (default True)  -> pactl set-default-source null monitor

    Phone mic is routed to a null sink (scrcpy_mic) via PULSE_SINK env in
    start(), so it never hits HDMI speakers.  The null sink's monitor
    (scrcpy_mic.monitor) becomes the system mic.
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
        argv += ["--orientation=0"]
    argv += [f"--v4l2-sink={cfg.get('v4l2_sink','/dev/video0')}", f"--v4l2-buffer={cfg.get('v4l2_buffer',120)}"]
    if cfg.get("video_bit_rate"): argv += [f"--video-bit-rate={cfg['video_bit_rate']}"]
    if not cfg.get("with_preview"): argv.append("--no-window")

    mic_to_host = bool(cfg.get("mic_to_host", True))
    mic_to_phone = bool(cfg.get("mic_to_phone", False))
    if mic_to_host and not mic_to_phone:
        # Default: phone mic -> null sink (no echo), monitor becomes mic
        argv += [f"--audio-source={cfg.get('audio_source','mic')}"]
    elif mic_to_host and mic_to_phone:
        argv += [f"--audio-source={cfg.get('audio_source','mic')}", "--audio-dup"]
    elif not mic_to_host and mic_to_phone:
        argv += ["--audio-source=playback", "--audio-dup"]
    else:
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


def _move_scrcpy_to_null_sink() -> bool:
    """Find ALL scrcpy sink-inputs and move them to the null sink.
    This prevents echo: phone mic → HDMI speakers → host mic feedback.
    With null sink, phone mic goes to scrcpy_mic → monitor is system mic,
    HDMI stays silent. Returns True if at least one moved or was already there.
    """
    moved_any = False
    for _ in range(8):  # poll up to 4s for scrcpy to appear
        try:
            r = subprocess.run(["pactl","list","sink-inputs"], capture_output=True, text=True, timeout=3)
            out = r.stdout
            if "application.name = \"scrcpy\"" not in out:
                time.sleep(0.5)
                continue
            import re as _re
            # Collect all scrcpy sink-input indices
            indices = []
            current_idx = None
            for line in out.splitlines():
                m = _re.match(r"Sink Input #(\d+)", line)
                if m:
                    current_idx = m.group(1)
                if current_idx and "application.name = \"scrcpy\"" in line:
                    indices.append(current_idx)
                    current_idx = None  # avoid duplicate for same block
            if not indices:
                time.sleep(0.5)
                continue
            # For each scrcpy sink-input, check if already on null sink, else move
            rs = subprocess.run(["pactl","list","short","sink-inputs"], capture_output=True, text=True, timeout=3)
            rn = subprocess.run(["pactl","list","short","sinks"], capture_output=True, text=True, timeout=3)
            null_sink_id = None
            for sl in rn.stdout.splitlines():
                if NULL_SINK in sl:
                    null_sink_id = sl.split()[0]
                    break
            for idx in indices:
                # Find current sink for this idx
                cur_sink = None
                for l in rs.stdout.splitlines():
                    if l.startswith(idx + "\t") or l.startswith(idx + " "):
                        parts = l.split()
                        if len(parts) >= 2:
                            cur_sink = parts[1]
                        break
                if cur_sink == null_sink_id:
                    moved_any = True
                    continue
                mr = subprocess.run(["pactl","move-sink-input", idx, NULL_SINK], capture_output=True, text=True, timeout=3)
                if mr.returncode == 0:
                    moved_any = True
            if moved_any:
                return True
        except Exception: pass
        time.sleep(0.5)
    return moved_any

def _find_real_source() -> str | None:
    """Return first non-monitor, non-scrcpy input source name, or None."""
    try:
        r = subprocess.run(["pactl","list","short","sources"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2: continue
            name = parts[1]
            if ".monitor" in name: continue
            if "scrcpy" in name: continue
            return name
    except Exception: pass
    return None

def set_phone_as_system_mic(cfg: dict) -> dict:
    """Make the forwarded phone mic the system default input source.

    With the null-sink model, the mic lives at scrcpy_mic.monitor.
    Steps: 1) move scrcpy sink-input to null sink (no echo),
           2) set null monitor as default source.
    Guards: if prev is already scrcpy/null, or scrcpy not running, do not
    clobber real prev; this prevents the sticky scrcpy_mic.monitor loop.
    """
    info = {"prev_default_source": None, "prev_default_mute": None, "set": False}
    if not cfg.get("mic_to_host", True):
        return info
    if not cfg.get("mic_default", True):
        return info
    time.sleep(1.5)  # wait for scrcpy to create sink-input
    # 1) Move scrcpy audio from HDMI to null sink (prevents echo)
    _move_scrcpy_to_null_sink()
    time.sleep(0.5)
    # verify scrcpy actually produced a stream; if dead, don't hijack source
    try:
        r = subprocess.run(["pactl","list","sink-inputs"], capture_output=True, text=True, timeout=3)
        if "application.name = \"scrcpy\"" not in r.stdout:
            return info  # scrcpy not running/no stream -> leave source alone
    except Exception: pass
    try:
        r = subprocess.run(["pactl","get-default-source"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            cur = r.stdout.strip()
            # guard: don't capture scrcpy as prev (sticky loop bug)
            if "scrcpy" in cur:
                real = _find_real_source()
                if real:
                    cur = real
                else:
                    return info
            info["prev_default_source"] = cur
            r2 = subprocess.run(["pactl","get-source-mute", cur],
                                 capture_output=True, text=True, timeout=3)
            info["prev_default_mute"] = "yes" in r2.stdout.lower() if r2.returncode == 0 else None
    except Exception: pass
    want_mute = "1" if cfg.get("mic_mute", False) else "0"
    # Prefer null sink monitor, fall back to legacy "scrcpy" source
    for src in (NULL_MONITOR, "scrcpy"):
        try:
            r = subprocess.run(["pactl","set-default-source",src], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                subprocess.run(["pactl","set-source-mute",src,want_mute], capture_output=True, timeout=3)
                info["set"] = True
                info["mic_source"] = src
                return info
        except Exception: continue
    # wpctl fallback
    for src in (NULL_MONITOR, "scrcpy"):
        try:
            subprocess.run(["wpctl","set-default",src], capture_output=True, timeout=3)
            info["set"] = True
            info["mic_source"] = src
            return info
        except Exception: continue
    return info


def restore_system_audio(prev: dict) -> bool:
    """Restore the default audio source after scrcpy exits.
    If prev is missing/corrupted (points to scrcpy), falls back to first
    real source instead of leaving a dead monitor as default. Always cleans
    up the sticky scrcpy_mic.default leftover.
    """
    src = (prev or {}).get("prev_default_source")
    # guard corrupted prev
    if not src or "scrcpy" in src:
        src = _find_real_source()
        if not src:
            # last fallback: try to set via wpctl to real source id, then try pactl
            try:
                r = subprocess.run(["pactl","get-default-source"], capture_output=True, text=True, timeout=3)
                if "scrcpy" in r.stdout:
                    real = _find_real_source()
                    if real:
                        subprocess.run(["pactl","set-default-source",real], capture_output=True, timeout=3)
                        return True
            except Exception: pass
            return False
    try:
        # before restoring, ensure the target source still exists
        r = subprocess.run(["pactl","list","short","sources"], capture_output=True, text=True, timeout=3)
        if src not in r.stdout:
            alt = _find_real_source()
            if alt: src = alt
            else: return False
        subprocess.run(["pactl","set-default-source",src], capture_output=True, timeout=3)
        if prev and prev.get("prev_default_mute") is not None:
            want = "1" if prev["prev_default_mute"] else "0"
            subprocess.run(["pactl","set-source-mute",src,want], capture_output=True, timeout=3)
        # confirm it took; if still on scrcpy, force again
        r2 = subprocess.run(["pactl","get-default-source"], capture_output=True, text=True, timeout=3)
        if "scrcpy" in r2.stdout and src not in r2.stdout:
            subprocess.run(["pactl","set-default-source",src], capture_output=True, timeout=3)
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


import re as _re

def _known_adb_endpoints() -> list[tuple[str,int]]:
    """(ip, port) pairs the phone may currently be serving adb on.

    Android wireless debugging assigns a RANDOM high port per session, so a
    fixed 5555-only scan can never find a phone that was never rooted. Three
    free local sources, newest first:
      1. `adb mdns services`  - what wireless debugging advertises right now
         (silently skipped: adb builds without mdns support error out).
      2. `adb devices` - the target the plugin was last connected to.
      3. the kernel neigh cache - where a phone that changed address usually
         still shows up, on the Termux default port.
    Never raises: an absent phone must cost one cheap subprocess, not a
    traceback out of discovery.
    """
    out: list[tuple[str,int]] = []
    def _add(ip, port):
        try: port = int(port)
        except (TypeError, ValueError): return
        if 0 < port < 65536 and (ip, port) not in out: out.append((ip, port))

    # Wireless debugging advertises its adb endpoints over mDNS. `adb mdns
    # services` is the first choice but many adb builds (e.g. 37.0.0) lack mdns
    # support and only print an error, so fall back to avahi and take the host:port
    # out of the resolved service text.
    try:
        r = subprocess.run(["adb","mdns","services"], capture_output=True, text=True, timeout=4)
        for line in (r.stdout or "").splitlines():
            m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d+)", line)
            if m: _add(m.group(1), m.group(2))
    except Exception: pass
    try:
        # avahi-browse is long-running BY DESIGN: it streams records as they
        # arrive and only emits the resolved "=" line on a fresh announcement.
        # A single shot is a coin flip on a busy LAN - measured 2 hits, then
        # 0/4. Retry a few times so a miss is recoverable, not fatal.
        for _ in range(3):
            try:
                r = subprocess.run(["avahi-browse","-rpt","_adb-tls-connect._tcp"],
                                   capture_output=True, text=True, timeout=5)
                for line in (r.stdout or "").splitlines():
                    if not line.startswith("=;"): continue
                    f = line.split(";")
                    # f[0]='=' f[1]=iface f[2]=proto f[3]=name f[4]=_type
                    # f[5]=domain f[6]=host f[7]=ip f[8]=port f[9]=txt
                    if len(f) >= 9 and f[7].strip(): _add(f[7].strip(), f[8].strip())
            except Exception: pass
            if any(p != 5555 for _, p in out): break
            time.sleep(2)
    except Exception: pass

    # The phone app publishes its OWN _adb-tcp._tcp record once adbd is pinned.
    # That is the reliable source of a real port: _adb-tls-connect is only
    # advertised while Wireless debugging is actively running, and this adb
    # build has no mdns support of its own.
    try:
        r = subprocess.run(["avahi-browse","-rpt","_adb-tcp._tcp"],
                           capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            if not line.startswith("=;"): continue
            f = line.split(";")
            if len(f) >= 9 and f[7].strip(): _add(f[7].strip(), f[8].strip())
    except Exception: pass

    try:
        r = subprocess.run(["adb","devices"], capture_output=True, text=True, timeout=4)
        for line in (r.stdout or "").splitlines():
            m = _re.match(r"\s*(\d{1,3}(?:\.\d{1,3}){3}):(\d+)\s", line)
            if m and "unauthorized" not in line: _add(m.group(1), m.group(2))
    except Exception: pass

    # The phone's mDNS hostname is the ONLY stable identifier across DHCP
    # changes - observed live: 192.168.1.6:43671 one minute, 192.168.1.4:43203
    # the next. The adb service name (adb-*.local) does not resolve on this
    # LAN, but the device hostname (Android-2.local) does, reliably.
    #
    # The PORT still comes from the _adb-tls-connect._tcp record above. Pairing
    # it with a hardcoded 5555 here is what produced "Connection refused":
    # Android 13 wireless debugging binds a random high port, never 5555.
    # Fall back to 5555 only when mDNS gave us nothing at all.
    for _host in _phone_hostnames():
        try:
            r = subprocess.run(["avahi-resolve","-n",_host], capture_output=True, text=True, timeout=5)
            # Reuse only REAL ports mDNS already reported. Picking known[0]
            # blindly took a Docker container's address when the list had one.
            real = [p for _, p in out]
            for line in (r.stdout or "").splitlines():
                m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                if not m: continue
                if real:
                    _add(m.group(1), real[0])
                else:
                    _add(m.group(1), 5555)
        except Exception: pass

    # `ip neigh` is a LAST resort and carries NO port information - it just
    # lists who is on the LAN. Emitting _add(ip, 5555) for every neighbour
    # stamped a wrong port on every Docker container on this host (measured:
    # 15 endpoints, all :5555, mostly 172.2x container addresses) and made
    # discovery useless. Only offer these when mDNS told us nothing at all,
    # and only for the WiFi subnet, never Docker/veth bridges.
    _wifi = _lan_ip().rsplit(".", 1)[0] + "."
    try:
        r = subprocess.run(["ip","neigh","show"], capture_output=True, text=True, timeout=3)
        for line in (r.stdout or "").splitlines():
            # `ip neigh` prints "<ip> dev <iface> <state> [lladdr]" - iface
            # precedes the state, and FAILED/INCOMPLETE rows are stale noise.
            m = _re.match(r"(\d{1,3}(?:\.\d{1,3}){3})\s+dev\s+(\S+)\s+(\w+)", line)
            if not m: continue
            ip, iface, state = m.group(1), m.group(2), m.group(3)
            if state in ("FAILED", "INCOMPLETE"): continue
            # wlan only: br-*/veth*/docker are containers, never the phone.
            if not iface.startswith(("wl", "wlp", "wlan")): continue
            if not ip.startswith(_wifi): continue
            if ip.endswith(".1"): continue
            if not out:                       # no real port was observed
                _add(ip, 5555)
    except Exception: pass

    return out


def _phone_hostnames() -> list[str]:
    """mDNS names the phone answers to.

    Learned from _adb-tls-connect._tcp records (the adb service name) plus the
    device hostname, which survives the IP changes the service name does not.
    """
    names: list[str] = []
    def _add(n):
        n = (n or "").strip()
        if n and n not in names: names.append(n)
    try:
        r = subprocess.run(["avahi-browse","-rpt","_adb-tls-connect._tcp"],
                           capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            f = line.split(";")
            if len(f) >= 9 and f[6].strip(): _add(f[6].strip())
    except Exception: pass
    _add("Android-2.local")
    return names


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
    tcp_timeout = 0.25 if quick else 0.4
    my_ip_prefix = base.rsplit('.', 1)[0]
    GATEWAY_IPS = {"192.168.1.1", "10.0.0.1", "192.168.0.1", "10.0.1.1", "172.16.0.1"}

    def _probe(ip_port):
        ip, port = ip_port
        if ip in GATEWAY_IPS: return None
        try:
            s = socket.socket(); s.settimeout(tcp_timeout)
            s.connect((ip, port)); s.close()
            return (ip, port)
        except Exception: return None

    found = []
    # 0) The configured target. If config.json already holds a reachable phone
    # this is exact and free, and it is the ONLY source that works when the
    # phone is not advertising over mDNS (wireless debugging off). Probing it
    # first is what makes the plugin usable without discovery.
    try:
        from .config import target as _target
        _ip, _port = _target(cfg).rsplit(":", 1)
        r = _probe((_ip, int(_port)))
        if r: found.append(r)
    except Exception: pass
    # A configured IP goes stale the moment the phone re-joins DHCP - observed
    # live: .6 -> .4 mid-session. Re-resolve the hostname before falling back to
    # a 254-host sweep, so a moved phone costs one avahi call, not a scan.
    if not found:
        for _host in _phone_hostnames():
            try:
                rr = subprocess.run(["avahi-resolve","-n",_host], capture_output=True, text=True, timeout=5)
                for _line in (rr.stdout or "").splitlines():
                    m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", _line)
                    if not m: continue
                    for _p in sorted({int(cfg.get("android_port") or 0), 5555} - {0}):
                        r = _probe((m.group(1), _p))
                        if r: found.append(r)
            except Exception: pass
            if found: break
    # 1) Endpoints we already know (avahi mDNS / adb devices / neigh): ~free,
    # and finds a phone on the random port it actually publishes. Then fall
    # back to a 5555-only /24 sweep (the Termux/rooted path). Probing every
    # discovered port against all 254 hosts would be O(hosts x ports).
    if not found:
        for _ip, _port in _known_adb_endpoints():
            r = _probe((_ip, _port))
            if r: found.append(r)
    if not found:
        # Address-only candidates must be PROBED, not stamped with a literal
        # 5555: Android 13 wireless debugging binds a random high port, so a
        # 5555 guess is a guaranteed "Connection refused". The only trustworthy
        # source of a real port is the phone's own mDNS advertisement. When
        # that is unavailable (adb built without mdns support, and avahi
        # returning nothing) we genuinely do not know the port - say so rather
        # than sweeping 254 hosts against a guess.
        known = _known_adb_endpoints()
        real_ports = {p for _, p in known if p and p != 5555}
        if not real_ports:
            return (False, "cannot determine the phone's adb port: it advertises a "
                           "random port over mDNS and no advertisement was seen. "
                           "Press 'Let phone in' and open the phone app so it can "
                           "report its own address, or type ip:port in the host app.")
        cands = list(known) + [(_ip, p) for _ip, _p in known for p in real_ports]
        with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
            res = list(ex.map(_probe, cands))
        found = [r for r in res if r]
    if not found:
        targets = [(f"{my_ip_prefix}.{i}", 5555) for i in range(1, 255)]
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
        return (False, f"no adb endpoint found (tried {len(_known_adb_endpoints())} known "
                       f"endpoint(s) + {base}/24:5555 sweep; is wireless debugging on?)")
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

PROVISION_PORT = 7911
SERVICE_TYPE = "_androidwebcam._tcp"


def _is_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4: return False
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255): return False
    return True


def _lan_ip() -> str:
        """This host's address on the LAN, so we bind one interface, not 0.0.0.0.

        The route probe picks the interface the kernel would use to reach the
        outside world. A hardcoded gateway would pick the wrong address on any
        other subnet, and a multi-homed box would bind one arbitrary interface.
        """
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((_default_gateway(), 9))   # no packets sent; picks the route
            ip = s.getsockname()[0]
        except Exception:
            try:
                ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                ip = "0.0.0.0"
        finally:
            s.close()
        return ip


def _default_gateway() -> str:
    """The real default gateway, read from the kernel's routing table.

    Hardcoding 192.168.1.1 makes _lan_ip() return an address the phone cannot
    reach on any other network, which surfaces as 'Couldn't find your computer'.
    """
    import socket
    try:
        with open("/proc/net/route") as f:
            next(f, None)                       # header
            for line in f:
                p = line.split()
                # Destination 00000000 == default route; Gateway is little-endian
                if len(p) > 2 and p[1] == "00000000":
                    g = p[2]
                    b = bytes.fromhex(g)
                    return socket.inet_ntoa(b[::-1])
    except Exception:
        pass
    return "8.8.8.8"                           # routable; nothing is sent


def serve_key(host: str | None = None, port: int = PROVISION_PORT,
              advertise: bool = True, once: bool = True,
              grace: int | None = None) -> None:
    """Advertise this host and hand the phone its adb PUBLIC key over the LAN.

    This is what removes the last manual step: the key is 715 chars of base64
    that would otherwise be hand-copied between two machines.

    Safety properties, deliberately:
      * binds the SPECIFIC LAN address, not 0.0.0.0, so nothing off-LAN and no
        other service on this machine can reach it;
      * serves the key, then lingers ~45s so a phone retry that was already in
        flight is not left with a dead endpoint, then closes itself;
      * prints the peer address, so the pairing is visible to the operator.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import socketserver, threading

    key = Path.home() / ".android" / "adbkey.pub"
    if not key.exists():
        print(f"no host key at {key} - run: adb keygen ~/.android/adbkey", flush=True)
        return

    if host is None:
        host = _lan_ip()

    served = {"n": 0, "peer": "-"}
    deadline = time.monotonic() + 600
    GRACE = grace if grace is not None else 45   # outlives the phone's ~30s of retries

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            # The phone reports the ip:port it is ACTUALLY on. This is the
            # reliable way for the host to learn the endpoint: mDNS is
            # unreliable on many hosts (this one resolves nothing for either
            # adb service), and Android 13 wireless debugging binds a random
            # port that cannot be guessed. The phone already has an open HTTP
            # connection to us, so it costs no new machinery.
            if self.path.rstrip("/") != "/phone":
                self.send_error(404); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self.send_error(400); return
            ip = str(data.get("ip", "")).strip()
            try:
                port = int(data.get("port", 0))
            except (TypeError, ValueError):
                port = 0
            # Only the peer that just fetched the key may write config. The
            # server binds the LAN address, which is a weak guarantee for a
            # WRITE; without this any host on the subnet can repoint discovery.
            peer = self.client_address[0]
            if not (_is_ipv4(peer) and peer.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0]):
                logging.warning("ignoring /phone from off-subnet peer %s", peer)
                self.send_error(403); return
            # Validate the ADDRESS too, not just the port. Port-only checks let
            # "999.1.1.1" through and wrote it into config, where it became a
            # connect target the host would keep retrying.
            if not _is_ipv4(ip) or not (0 < port < 65536):
                self.send_error(400); return
            try:
                from .config import load as _load, save as _save
                cfg = _load()
                cfg["android_ip"] = ip
                cfg["android_port"] = port
                _save(cfg)
                logging.info("phone reported itself at %s:%s", ip, port)
                print(f"  phone registered at {ip}:{port}", flush=True)
            except Exception as e:
                logging.warning("could not save phone endpoint: %s", e)
                self.send_error(500); return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") not in ("/adbkey.pub", ""):
                self.send_error(404); return
            body = key.read_text().strip().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            served["n"] += 1
            served["peer"] = self.client_address[0]
            print(f"  served key to {served['peer']} - staying up briefly", flush=True)
            logging.info("served host key to %s", served["peer"])
            # Signal COMPLETION, not first contact, and never shut the listener
            # down from here. The phone's sweep opens a TCP socket and only then
            # issues the HTTP GET, and it may retry several times over ~30s; a
            # server that exits on first contact dies between the probe and the
            # fetch, and the phone stalls on "looking again" forever.
            # main() closes this after a grace period instead.

        def log_message(self, *a): pass

    class OneShot(socketserver.TCPServer):
        allow_reuse_address = True

    srv = OneShot((host, port), H)
    # serve_forever() MUST run for do_GET to ever execute; shutdown() from
    # another thread is then legal. A thread + Event is the only correct
    # "serve exactly one request" shape: waiting on the main thread without a
    # serving thread deadlocks the endpoint shut.
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    pub = None
    if advertise:
        # The phone browses for this with NsdManager. Without it the phone would
        # have to scan the subnet and could pick up an unrelated device.
        def _publish():
            # -s is required to register a service, and type/port are POSITIONAL
            # (there is no -t/-p). Omitting them makes avahi-publish exit at once
            # and leave a <defunct> process.
            return subprocess.Popen(
                ["avahi-publish", "-R", "-f", "-s", "android-webcam",
                 SERVICE_TYPE, str(port), "v=1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            pub = _publish()
            # Best-effort ONLY. On this host avahi-daemon serves WiFi (the
            # phone's own _adb-tls-connect record resolves to 192.168.1.4) but a
            # locally published service yields no resolvable record, so the
            # phone cannot rely on finding us this way. The scan for the
            # provisioning port is the mechanism; this is an optimisation that
            # must never block the handover.
            time.sleep(1.0)
            if pub.poll() is not None:
                pub = None
            else:
                print(f"advertising {SERVICE_TYPE} on port {port}", flush=True)
        except FileNotFoundError:
            print("  (avahi-publish missing; phone finds us by scanning)",
                  flush=True)

    print(f"serving host adb key on http://{host}:{port} — waiting for the phone",
          flush=True)
    logging.info("serve_key listening on %s:%s (serves key, then closes)", host, port)
    try:
        # Long enough that the user can install and open the app without racing
        # the server. It still closes by itself so the endpoint does not linger.
        # Poll rather than wait(): the handler no longer signals an Event, so
        # done.wait() would block for the full 10 minutes. Bounded by deadline.
        while served["n"] == 0 and time.monotonic() < deadline:
            time.sleep(0.5)
        if served["n"] > 0:
            print(f"  hand-over complete; staying up {int(GRACE)}s for retries",
                  flush=True)
            time.sleep(GRACE)
        else:
            logging.warning("serve_key: timed out, no phone asked for the key")
            print("  timed out; nothing asked for the key", flush=True)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        srv.shutdown()
        srv.server_close()
        if pub:
            pub.terminate()
        print("done", flush=True)


def pair_with_phone(code: str, cfg: dict | None = None, wait: int = 60) -> tuple[bool,str]:
    """Pair with the phone by watching for the adb pairing mDNS service.

    The phone advertises `_adb-tls-pairing._tcp` ONLY while the
    "Pair device with pairing code" screen is open, and that screen shows the
    port the user is staring at. This polls for the service, reads the port out
    of the record, and pairs - so the user's only action is opening that screen
    and reading us the 6-digit code. No port to copy, no port to retype.

    `code` is that 6-digit code. Returns (ok, message).
    """
    import time as _t
    deadline = _t.time() + max(1, wait)
    if not code or len(code.strip()) != 6 or not code.strip().isdigit():
        return (False, "pairing code must be the 6 digits currently shown on the "
                        "phone's pairing screen (codes are short-lived)")
    # Trust is one-time and the phone shows an RSA dialog for it; a host-side
    # loop cannot click that, so this can only ever get TO the prompt.
    print(f"watching for _adb-tls-pairing._tcp for {wait}s —")
    print("on the phone: Developer options → Wireless debugging → Pair device with pairing code")
    last = None
    while _t.time() < deadline:
        try:
            r = subprocess.run(["avahi-browse","-rpt","_adb-tls-pairing._tcp"],
                               capture_output=True, text=True, timeout=5)
            for line in (r.stdout or "").splitlines():
                if not line.startswith("=;"): continue
                f = line.split(";")
                if len(f) < 9 or not f[7].strip(): continue
                ip, port = f[7].strip(), f[8].strip()
                if last != (ip, port):
                    last = (ip, port)
                    print(f"  pairing service seen at {ip}:{port} — pairing…")
                pr = subprocess.run(["adb","pair",f"{ip}:{port}",code],
                                    capture_output=True, text=True, timeout=20)
                out = (pr.stdout + pr.stderr).strip()
                if "Successfully paired" in out or "successfully paired" in out.lower():
                    print(f"  paired: {out}")
                    return (True, f"paired with {ip}:{port}")
                return (False, out or "adb pair failed")
        except Exception as e:
            last = None
        _t.sleep(1.5)
    return (False, f"no _adb-tls-pairing._tcp seen within {wait}s "
                    f"(is the pairing screen open on the phone?)")


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

def ensure_connected(cfg: dict, deadline: float = 12.0) -> tuple[bool,str]:
    """Make cfg's target actually usable, in place, before streaming.

    Order matters and is cheapest-first:
      0. already-connected device?  -> use it, touch nothing
      1. configured target answers?  -> use it
      2. re-resolve the phone's mDNS hostname (it changes IP on DHCP)
      3. known adb endpoints / 5555 sweep

    `deadline` bounds the whole thing. Measured: ~8s when nothing answers,
    ~13s when a candidate exists but never completes the adb handshake - both
    are on the interactive start path, so it must not grow unbounded. On
    timeout we still return a usable message rather than hanging.

    Every success writes the working ip:port back to config, so the next start
    hits step 0. This is what makes "just open the app" work across reboots,
    IP changes, and wireless-debugging toggles without the user editing config.
    """
    tgt = target(cfg)
    # 0. already have it?  (sub-ms, and the overwhelmingly common case)
    try:
        r = subprocess.run(["adb","devices"], capture_output=True, text=True, timeout=4)
        for line in (r.stdout or "").splitlines():
            if line.startswith(tgt) and "\tdevice" in line:
                return (True, f"already connected: {tgt}")
    except Exception: pass
    # 1-3. one discovery pass, which prefers cfg, then hostname, then known
    ok, msg = discover_phone(cfg, quick=True)
    if ok:
        return (True, f"connected: {target(cfg)}")
    return (False, msg)


def start(cfg: dict) -> subprocess.Popen:
    """Start scrcpy with the given config. Returns the Popen."""
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    # A stale IP is the most common failure and scrcpy reports it as an opaque
    # stream error. Re-resolve first so the failure is ours, not scrcpy's.
    ok, why = ensure_connected(cfg)
    if not ok:
        raise RuntimeError(f"phone not reachable ({why}) — run: android-webcam --discover")
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    argv = build_argv(cfg)
    # Kill any stale scrcpy camera instances (IP hop, previous crash)
    try:
        subprocess.run(["pkill","-f","scrcpy.*camera.*v4l2-sink"], capture_output=True, timeout=3)
        time.sleep(0.5)
    except Exception: pass
    if not ensure_v4l2loopback():
        raise RuntimeError("/dev/video0 missing — v4l2loopback not loaded. Reinstall v4l2loopback-dkms and modprobe.")
    mic_to_host = bool(cfg.get("mic_to_host", True))
    mic_to_phone = bool(cfg.get("mic_to_phone", False))
    use_null_sink = mic_to_host and not mic_to_phone
    if use_null_sink:
        ensure_null_sink()
    try:
        subprocess.run(["adb","connect",tgt], capture_output=True, text=True, timeout=5)
    except Exception: pass
    if cfg.get("stay_brightness_low"):
        try:
            subprocess.run(["adb","-s",tgt,"shell",
                            "settings put system screen_brightness_mode 0; settings put system screen_brightness 1; cmd display set-brightness 0.001"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception: pass
    # Route via _move_scrcpy_to_null_sink after launch instead of PULSE_SINK
    # env. PULSE_SINK pollutes WirePlumber's default-nodes (sink.0=scrcpy_mic)
    # and makes the null sink appear as a permanent output choice, conflicting
    # with the real HDMI (SAMSUNG) sink. Moving the stream after it appears
    # is sufficient and leaves no sticky default.
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    if cfg.get("mic_to_host", True) and cfg.get("mic_default", True):
        def _delay_default():
            prev = set_phone_as_system_mic(cfg)
            cfg["_mic_prev"] = prev
        threading_mod = __import__("threading")
        threading_mod.Thread(target=_delay_default, daemon=True).start()
    elif cfg.get("mic_to_host", True) and cfg.get("mic_mute", True):
        def _delay_mute():
            time.sleep(2)
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
    p.add_argument("--pair", metavar="CODE",
                   help="watch for the adb pairing service and pair with the 6-digit code "
                        "from the phone's 'Pair device with pairing code' screen")
    p.add_argument("--pair-wait", type=int, default=60, metavar="SEC",
                   help="how long --pair watches for the pairing service (default 60)")
    p.add_argument("--serve", action="store_true",
                   help="serve this host's adb public key on the LAN so the phone app "
                        "can fetch it (no hand-copying)")
    p.add_argument("--phone-log", action="store_true",
                   help="print the phone app's discovery log, then exit")
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
    if a.phone_log:
        # The phone writes into its own external files dir, which needs no
        # storage permission and is adb-readable on every API level. A bare
        # /sdcard path does NOT work on API 29+ (scoped storage) and a
        # swallowed write error there looks identical to "no logs".
        paths = [
            "/sdcard/Android/data/dev.ishanp.androidwebcam/files/aw.log",
            "/data/data/dev.ishanp.androidwebcam/files/aw.log",
        ]
        for p in paths:
            try:
                out = subprocess.run(["adb", "shell", "cat", p],
                                     capture_output=True, text=True, timeout=20)
                if out.returncode == 0 and out.stdout.strip():
                    print(out.stdout)
                    break
            except Exception as e:
                print(f"  ({p}: {e})", file=sys.stderr)
        else:
            print("no phone log found; on the phone, tap 'Copy diagnostics' instead")
        return
    if a.serve:
        serve_key()
        return
    if a.pair:
        ok, msg = pair_with_phone(a.pair, cfg, wait=a.pair_wait)
        print(msg)
        if ok:
            ok2, msg2 = discover_phone(cfg)
            print(msg2)
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
