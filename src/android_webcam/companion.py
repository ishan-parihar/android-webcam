"""Android Webcam Companion client — adb forward + JSON-lines protocol.

The companion app (android-app/, phone) serves a LocalServerSocket on the
abstract name "android-webcam-companion".  The host reaches it through an adb
forward, so the protocol works identically over USB and over `adb tcpip 5555`
with no pairing, no wifi, and no permissions on either side.

Contract: docs/companion-protocol.md (v1).  Protocol v2 adds the session
lifecycle (session.begin / session.heartbeat / session.end) plus a
self-releasing keep-alive watchdog on the phone.

Fail loud, never silently default: every network/adb error propagates as
CompanionError with a readable message.  Callers that want a soft "companion
not installed" path catch the exception explicitly.
"""
import json
import logging
import socket
import subprocess
import threading
import time

SOCKET_NAME = "android-webcam-companion"   # abstract name on the phone
PROTO = 1
DEFAULT_HOST_PORT = 7878

# Session liveness: the phone releases its wake lock on its own when no
# heartbeat arrives for WATCHDOG_TIMEOUT.  Heartbeat every HB_INTERVAL.
HB_INTERVAL = 10.0
WATCHDOG_TIMEOUT = 45.0


class CompanionError(RuntimeError):
    """Any companion/adb/socket failure.  Never swallowed silently."""


# ── adb plumbing ────────────────────────────────────────────────────────────

def forward(tgt: str, host_port: int = DEFAULT_HOST_PORT,
            timeout: float = 5.0) -> str:
    """Point host localhost:host_port at the phone's companion socket.

    Idempotent (adb replaces an existing forward on the same port).
    Returns adb's combined stdout.  Raises CompanionError on failure.
    """
    if not shutil_available():
        raise CompanionError("adb not found in PATH — install android-tools")
    cmd = ["adb", "-s", tgt, "forward", f"tcp:{host_port}",
           f"localabstract:{SOCKET_NAME}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CompanionError("adb not found in PATH — install android-tools")
    except subprocess.TimeoutExpired:
        raise CompanionError(f"adb forward timed out after {timeout}s")
    if r.returncode != 0:
        raise CompanionError(
            f"adb forward failed (rc={r.returncode}): "
            f"{(r.stdout + r.stderr).strip() or '<no output>'}")
    return (r.stdout + r.stderr).strip()


def remove_forward(host_port: int = DEFAULT_HOST_PORT, timeout: float = 5.0) -> None:
    """Remove ONE host-side forward.  Best effort.  Never raises.

    Prefer [release_forward] in live code: removing a forward this process did
    not create is what makes a 5s status poll yank the port out from under a
    running stream.
    """
    try:
        subprocess.run(["adb", "forward", "--remove", f"tcp:{host_port}"],
                       capture_output=True, timeout=timeout)
    except Exception:
        pass


# Ports tried in order when the preferred one cannot reach the companion.
# Deliberately a small contiguous block rather than a random scan: the failure
# mode is a stale or foreign forward on a fixed port, so stepping to the next
# neighbour is both sufficient and predictable.
PORT_CANDIDATES = (7878, 7879, 7880, 7881, 7882, 7883, 7884, 7885)

# Ports this process created, so we only ever tear down our own.  adb forwards
# are host-global, so a per-process counter is what keeps one consumer's
# teardown from breaking another.
_owned_forwards: set[int] = set()
_fwd_lock = threading.Lock()


def forward_owned(tgt: str, host_port: int = DEFAULT_HOST_PORT,
                  timeout: float = 5.0) -> None:
    """[forward], recording that this process owns the resulting forward."""
    forward(tgt, host_port, timeout)
    with _fwd_lock:
        _owned_forwards.add(host_port)


def release_forward(host_port: int = DEFAULT_HOST_PORT,
                    timeout: float = 5.0) -> None:
    """Remove the forward only if THIS process created it.

    A forward is host-global but ownership is per-process, so a GUI session and
    a --status poll are independent.  Removing on every teardown was the bug:
    the poll's 5s cycle tore down the port the stream was using.
    """
    with _fwd_lock:
        owned = host_port in _owned_forwards
        if owned:
            _owned_forwards.discard(host_port)
    if owned:
        remove_forward(host_port, timeout)


def negotiate(tgt: str, preferred: int = DEFAULT_HOST_PORT,
              candidates: "tuple[int, ...]" = PORT_CANDIDATES,
              forwarder: "callable | None" = None,
              prober: "callable | None" = None) -> int:
    """Find a host port that actually reaches the companion, return it.

    The retry is driven by a failed PING, not by the forward's exit code.  This
    distinction is the whole design: `adb forward` does not fail when the port
    is already bound by something else — adb records the mapping and the
    connect lands on the *foreign* listener.  Measured on this host: with 7878
    held by another process, the connect succeeded and reached that process.
    So exit-code-driven fallback would never trigger, and the only honest
    signal is a protocol handshake that does not come back as our JSON.

    Port order: [preferred] first if it is a plausible base, then the
    candidates, never repeating.  The winner is returned for the caller to
    reuse for the whole session.
    """
    fwd = forwarder or forward_owned
    probe = prober or ping
    order: list[int] = []
    if preferred not in order:
        order.append(preferred)
    for c in candidates:
        if c not in order:
            order.append(c)
    errors: list[str] = []
    for port in order:
        try:
            fwd(tgt, port)
        except CompanionError as e:
            errors.append(f"{port}: forward failed ({e})")
            continue
        try:
            probe(port)
            return port
        except CompanionError as e:
            # Almost always "something else owns this port" or a stale forward
            # pointing at a phone that is gone.  Both mean: try the next one.
            errors.append(f"{port}: {e}")
            continue
    raise CompanionError(
        "no usable companion port; tried " + ", ".join(map(str, order)) + ":\n  " +
        "\n  ".join(errors))


def shutil_available() -> bool:
    return __import__("shutil").which("adb") is not None


# ── JSON-lines client ───────────────────────────────────────────────────────

def call(cmd: dict, host: str = "127.0.0.1", port: int = DEFAULT_HOST_PORT,
         timeout: float = 2.0) -> dict:
    """One request/response round-trip against the companion socket.

    Sends one JSON line, reads one JSON line, closes.  Raises
    CompanionError on connect/read/parse failure and on ok=false responses.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as e:
        raise CompanionError(
            f"companion unreachable on {host}:{port} ({e}) — "
            f"is the app installed and KeepAlive running?")
    if not buf.strip():
        raise CompanionError("companion closed the connection with no response")
    try:
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise CompanionError(f"malformed companion response: {e}")
    if not resp.get("ok"):
        raise CompanionError(f"companion error: {resp.get('error', '<no error field>')}")
    return resp


def check_advertisement(port: int = DEFAULT_HOST_PORT,
                        sweep=None) -> dict:
    """Compare the host's DISCOVERY findings against the phone's own.

    This is the whole reason the phone reports `service.adb.tls.port`. The
    avahi record layout on this host came from a man page and was only ever
    checked against synthetic records, so it is a standing assumption that
    may simply be wrong. The phone is the only place the truth exists.

    It is a VERIFICATION step, not a discovery source: reading the phone
    requires an adb forward, which requires a target to already be known --
    so it can never be how a target is found. It runs after acquire().

    Returns:
        {"checked": bool, "swept": bool, "agrees": bool|None,
         "mismatch": str|None, "phone_port": int|None, "host_ports": [int,...]}

    `checked` False means the phone could not be asked (unreachable, or an
    older build without the block). That is NOT agreement and NOT a
    mismatch -- it is unknown, and callers must not render it as success.

    `swept` False means no host-side sweep was run (avahi-browse is a ~1s
    subprocess and some callers poll). That is a THIRD distinct state from
    "the sweep ran and found nothing" -- conflating them would accuse the
    parser of being broken when the truth is that it was never asked.
    """
    out = {"checked": False, "swept": False, "agrees": None, "mismatch": None,
           "phone_port": None, "host_ports": []}
    try:
        mdns = adb_mdns(port)
    except CompanionError:
        return out  # older phone, or unreachable: unknown, not agreement

    out["checked"] = True
    raw_port = mdns.get("wireless_port")
    if isinstance(raw_port, int):
        out["phone_port"] = raw_port
    if sweep is not None:
        out["swept"] = True
        try:
            out["host_ports"] = sorted({p for _, p in sweep()})
        except Exception:
            out["host_ports"] = []
    if out["phone_port"] is None:
        out["agrees"] = None
        out["mismatch"] = "phone reports no wireless port (wireless debugging off?)"
    elif not out["swept"]:
        # Deliberately no verdict. The phone-side facts are real and useful,
        # but the host-side comparison was never attempted, so claiming
        # either agreement or a parser failure would be invented.
        out["agrees"] = None
        out["mismatch"] = None
    elif not out["host_ports"]:
        # The case that actually occurs on this network: avahi returns
        # nothing, so the host sees no advertisement at all while the phone
        # demonstrably publishes one. That is a REAL failure -- the parser or
        # the responder is not working -- and it must not fall through to
        # 'no verdict', which reads as good news.
        out["agrees"] = False
        out["mismatch"] = (
            f"phone advertises port {out['phone_port']}, but this host's mDNS "
            f"found NO advertisement at all -- the avahi parser or the "
            f"responder is not working"
        )
    elif out["phone_port"] not in out["host_ports"]:
        out["agrees"] = False
        out["mismatch"] = (
            f"phone advertises port {out['phone_port']}, but this host's mDNS "
            f"found {out['host_ports']} -- the avahi parser or the responder "
            f"is wrong"
        )
    elif out["host_ports"]:
        out["agrees"] = True
    return out


# ── typed accessors (all raise CompanionError) ──────────────────────────────

def ping(port: int = DEFAULT_HOST_PORT) -> dict:
    return call({"cmd": "ping"}, port=port)


def status(port: int = DEFAULT_HOST_PORT) -> dict:
    return call({"cmd": "status"}, port=port)


def adb_mdns(port: int = DEFAULT_HOST_PORT) -> dict:
    """The phone's own adb/mDNS advertisement, as the PHONE sees it.

    This is the ground truth the desktop's own discovery guesses at. The
    avahi record layout on this host came from a man page and was only ever
    checked against synthetic records; the phone reads its own adbd
    properties, so when the desktop cannot find the phone this is where the
    answer is.

    Returns a dict with at least `loaded` and `lines`. `loaded` is False when
    the phone has no poller running (keep-alive off), which is DIFFERENT from
    a read that found nothing -- see the phone's AdbProps for why that
    distinction is carried explicitly.
    """
    data = status(port)
    mdns = data.get("adb_mdns")
    if not isinstance(mdns, dict):
        raise CompanionError(
            "phone is too old to report adb_mdns (companion protocol upgrade required)"
        )
    return mdns


def cameras(port: int = DEFAULT_HOST_PORT) -> list[dict]:
    resp = call({"cmd": "cameras"}, port=port)
    cams = resp.get("cameras")
    if not isinstance(cams, list):
        raise CompanionError("cameras response has no 'cameras' list")
    return cams


def get_config(port: int = DEFAULT_HOST_PORT) -> dict:
    resp = call({"cmd": "config.get"}, port=port)
    cfg = resp.get("config")
    if not isinstance(cfg, dict):
        raise CompanionError("config.get response has no 'config' object")
    return cfg


def set_config(cfg: dict, port: int = DEFAULT_HOST_PORT) -> dict:
    return call({"cmd": "config.set", "config": cfg}, port=port)


def keepalive(on: bool, port: int = DEFAULT_HOST_PORT) -> dict:
    return call({"cmd": "keepalive.on" if on else "keepalive.off"}, port=port)


# ── config validation against the phone's real capabilities ─────────────────

class ValidationError(ValueError):
    """Requested camera/size/fps is not supported by the phone.

    Collects every problem (fail loud AND complete), like jev_decision.py's
    collect-all-errors parsing.
    """


def validate(cfg: dict, cams: list[dict]) -> list[str]:
    """Return a list of human-readable problems with cfg against cams.

    An empty list means cfg is valid for this phone.  Never clamps silently.
    """
    problems: list[str] = []
    if not cams:
        problems.append("phone reports no cameras")
        return problems

    cam_id = cfg.get("camera_id")
    by_id = {str(c.get("id")): c for c in cams}

    if cam_id not in (None, ""):
        if str(cam_id) not in by_id:
            ids = ", ".join(sorted(by_id))
            problems.append(
                f"camera_id {cam_id} not on this phone (available: {ids})")
            return problems
    else:
        # facing-based selection: make sure at least one camera matches
        facing = cfg.get("camera_facing", "back")
        if not any(c.get("facing") == facing for c in cams):
            faces = ", ".join(sorted({str(c.get("facing")) for c in cams}))
            problems.append(
                f"no {facing} camera on this phone (available: {faces})")
            return problems

    target_cam = (by_id.get(str(cam_id)) if cam_id not in (None, "")
                  else next(c for c in cams
                            if c.get("facing") == cfg.get("camera_facing", "back")))

    size = cfg.get("custom_size")
    if size not in (None, ""):
        supported = _norm_sizes(target_cam)
        if not _size_supported(target_cam, size):
            avail = ", ".join(f"{w}x{h}" for w, h, _ in supported)
            problems.append(
                f"size {size} unsupported by camera {target_cam.get('id')} "
                f"(supported: {avail or 'none reported'})")
        else:
            fps = cfg.get("fps")
            if fps is not None:
                cap = _max_fps_for(target_cam, size)
                if cap is not None and fps > cap:
                    problems.append(
                        f"fps {fps} exceeds max {cap} for {size} on camera "
                        f"{target_cam.get('id')}")

    torch = bool(cfg.get("torch"))
    if torch and not target_cam.get("torch"):
        problems.append(
            f"camera {target_cam.get('id')} has no torch — cannot enable --camera-torch")
    return problems


def _parse_size(size: str) -> tuple[int, int] | None:
    try:
        w, h = (int(x) for x in str(size).lower().split("x", 1))
        return (w, h)
    except (ValueError, AttributeError):
        return None   # malformed size from config or phone: treat as unsupported

def _norm_sizes(cam: dict) -> list[tuple[int, int, int]]:
    """Normalize the phone's sizes[] into (w, h, fps) int triples.

    This is a trust boundary: the entries arrive over the wire from the phone
    app, so they can be absent, non-numeric, or not 3-element lists.  Skip
    what we cannot read instead of raising out of validate().
    """
    out: list[tuple[int, int, int]] = []
    dropped = 0
    for entry in cam.get("sizes") or []:
        try:
            w, h, fps = entry
            out.append((int(w), int(h), int(fps)))
        except (TypeError, ValueError):
            dropped += 1
    if dropped:
        # A version-skewed or buggy phone would otherwise look like a camera
        # with no supported modes — indistinguishable from a real device.
        logging.warning("camera %s: dropped %d unparseable sizes entries (of %d)",
                        cam.get("id"), dropped, len(cam.get("sizes") or []))
    return out

def _size_supported(cam: dict, size: str) -> bool:
    wh = _parse_size(size)
    if wh is None:
        return False
    return any((sw, sh) == wh for sw, sh, _ in _norm_sizes(cam))

def _max_fps_for(cam: dict, size: str) -> int | None:
    wh = _parse_size(size)
    if wh is None:
        return None
    caps = [fps for (sw, sh, fps) in _norm_sizes(cam) if (sw, sh) == wh]
    return max(caps) if caps else None


# ── session manager: keeps the phone awake exactly while we stream ──────────

class Session:
    """Owns the companion session for one scrcpy run.

    start():  adb forward -> session.begin -> keepalive.on -> heartbeat thread
    stop():   session.end -> keepalive.off -> stop heartbeat -> remove forward

    The heartbeat is what makes the phone-side watchdog correct: if this
    process is SIGKILLed (the panel's pkill path), heartbeats stop and the
    phone releases its own wake lock after WATCHDOG_TIMEOUT.  Graceful stop
    ends the session immediately.  Either way the wake lock does not leak.
    """

    def __init__(self, cfg: dict, port: int | None = None,
                 forwarder: "callable | None" = None,
                 unforwarder: "callable | None" = None,
                 negotiator: "callable | None" = None,
                 prober: "callable | None" = None):
        self.cfg = cfg
        self.port = port if port is not None else int(
            cfg.get("companion_host_port", DEFAULT_HOST_PORT))
        self.tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
        # Injectable so tests (and any future non-adb transport) can swap the
        # adb forward for a direct socket path.  Cleanup goes through the same
        # seam, so an injected path is genuinely device-free end to end.
        self._forwarder = forwarder or forward_owned
        self._prober = prober or (lambda p: call({"cmd": "ping"}, port=p))
        # Teardown only ever removes a forward this process created; a poll
        # from another process must not break a running stream.
        self._unforwarder = unforwarder or release_forward
        # Default to the real negotiator so production gets the retry; tests
        # inject a stub instead.  Never left unset -- start() reads it.
        # Partial-applied so the negotiator reuses THIS session's forwarder
        # and probe: the default negotiate() ignores them and would shell out
        # to real adb, bypassing the injected seam entirely.
        if negotiator is not None:
            self._negotiator = negotiator
        else:
            self._negotiator = lambda tgt, port: negotiate(
                tgt, port, forwarder=self._forwarder, prober=self._prober)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started = False
        self.reason: str | None = None   # set when the companion is absent

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        """Begin a session.  Returns True if the companion answered.

        Never raises: a missing/unreachable companion degrades the webcam to
        plain scrcpy (which still works) but records self.reason.  The
        caller surfaces that reason in the UI — we do NOT fail the stream.
        """
        try:
            # Negotiate the port first: it forwards internally and returns the
            # port that actually reached the companion, which the rest of the
            # session (session.begin, keepalive, heartbeats) then reuses.
            # Deliberately no separate self._forwarder call -- that would
            # double-forward and make an injected forwarder dead code.
            winner = self._negotiator(self.tgt, self.port)
            if winner != self.port:
                # Persist the winner so the Panel poll and the CLI reuse it
                # instead of every consumer re-fighting over 7878.
                self.port = winner
                self.cfg["companion_host_port"] = winner
                try:
                    from . import config as _cfg_mod
                    # update(), not save(): the phone writes android_port at
                    # runtime, and a whole-dict write from a snapshot would
                    # revert it. config.save() is a hard error for that reason.
                    _cfg_mod.update(companion_host_port=winner)
                except Exception:
                    logging.warning("could not persist negotiated port %s", winner)
            call({"cmd": "session.begin",
                  "client": "android-webcam",
                  "heartbeat_interval": HB_INTERVAL,
                  "watchdog_timeout": WATCHDOG_TIMEOUT}, port=self.port)
            if self.cfg.get("companion_keepalive", True):
                keepalive(True, self.port)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="companion-hb")
            self._thread.start()
            self.started = True
            return True
        except CompanionError as e:
            self.reason = str(e)
            return False

    def stop(self) -> None:
        """End the session and release the phone's wake lock (best effort)."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if not self.started:
            return
        self.started = False
        for cmd in ({"cmd": "session.end"},
                    {"cmd": "keepalive.off"}):
            try:
                call(cmd, port=self.port, timeout=1.5)
            except CompanionError:
                pass   # phone may already be gone; forward removal still runs
        self._unforwarder(self.port)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- internals ----------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HB_INTERVAL):
            try:
                call({"cmd": "session.heartbeat"}, port=self.port, timeout=2.0)
            except CompanionError:
                pass   # transient; the phone watchdog is the backstop


# ── desktop-side entry points used by the CLI and the GUI ───────────────────

def size_summary(cam: dict) -> str:
    """Render a camera's sizes for CLI output without trusting their shape."""
    sizes = _norm_sizes(cam)
    return ", ".join(f"{w}x{h}@{fps}" for w, h, fps in sizes) or "none"


def sync(cfg: dict, apply: bool = True) -> dict:
    """Pull cameras + status from the phone, validate cfg, persist fixes.

    Returns a summary dict:
      {"reachable": bool, "reason": str|None, "cameras": [...],
       "problems": [...], "status": {...}, "changes": {key: (old, new)}}
    """
    tgt = f"{cfg['android_ip']}:{cfg['android_port']}"
    summary = {"reachable": False, "reason": None, "cameras": [],
               "problems": [], "status": {}, "changes": {}, "port": None,
               "notes": []}
    # Negotiate like Session.start does, so --sync gets the same fallback: a
    # stale/foreign forward on the preferred port must not look like a dead
    # phone.  Records ownership, so the release below actually cleans up.
    try:
        port = negotiate(tgt, int(cfg.get("companion_host_port", DEFAULT_HOST_PORT)))
    except CompanionError as e:
        summary["reason"] = str(e)
        return summary
    summary["port"] = port
    if port != int(cfg.get("companion_host_port", DEFAULT_HOST_PORT)):
        cfg["companion_host_port"] = port
        try:
            from . import config as _cfg_mod
            # update(), not save(): a whole-dict write from this snapshot
            # would revert the port the phone pushed at runtime.
            _cfg_mod.update(companion_host_port=port)
        except OSError as e:
            # Degrade, never raise: an unwritable config path must not turn a
            # successful sync into a failure.  Recorded in notes so a later
            # connection failure cannot silently overwrite the warning.
            summary["notes"].append(
                f"used port {port} but could not persist it: {e}")
    try:
        summary["cameras"] = cameras(port)
        summary["status"] = status(port)
        summary["reachable"] = True
    except CompanionError as e:
        summary["reason"] = str(e)
        # Release only a forward this process created.  The previous
        # unconditional remove_forward() here is what made a 5s --status poll
        # tear down the port a live stream was using.
        release_forward(port)
        return summary

    summary["problems"] = validate(cfg, summary["cameras"])
    if apply and not summary["problems"]:
        # Push the phone's preferred config when the host has no explicit
        # custom_size yet — the phone knows the real supported list.
        try:
            phone_cfg = get_config(port)
        except CompanionError:
            phone_cfg = {}
        for src_key, host_key, convert in (
            ("camera_id", "camera_id", lambda v: str(v) if v is not None else None),
            ("size", "custom_size",
             lambda v: f"{v[0]}x{v[1]}" if isinstance(v, list) and len(v) == 2 else None),
            ("fps", "fps", lambda v: int(v) if v is not None else None),
            ("torch", "torch", lambda v: bool(v)),
        ):
            if src_key not in phone_cfg or host_key == "custom_size" and cfg.get("custom_size"):
                continue
            new = convert(phone_cfg[src_key])
            if new is None:
                continue
            if cfg.get(host_key) != new:
                summary["changes"][host_key] = (cfg.get(host_key), new)
                cfg[host_key] = new
    release_forward(port)
    return summary
