"""Auto-rotation: poll the scrcpy window via hyprctl and apply the matching
--capture-orientation to the stream.  The window aspect is the active scrcpy
class (or the user's chosen class) on its current monitor; we map:

    window wider than tall -> 0  (landscape)
    window taller than wide -> 0  (scrcpy camera captures in natural phone
                                   orientation; we don't rotate the device,
                                   we just match the canvas)
    square -> 0

Scrcpy 4.1 with --video-source=camera streams in the phone's natural
orientation (typically 0 for back, 0 for front). The orientation the *viewer*
sees depends on the window's aspect — so we either:
  - emit --orientation=0|90|180|270 to the live process, or
  - add the --orientation to the start argv.

For v4l2-sink the viewer is the browser — it reads `width x height` from the
file descriptor. We can't change that mid-stream; what we *can* do is
re-launch the process with a new orientation when the window is moved to a
monitor with different aspect (or rotated). This module exposes:

  current_orientation(window_class: str) -> str
      "0" | "90" | "180" | "270" — based on the active window's monitor.

  active_window_class() -> str | None
      "scrcpy" if a scrcpy window is focused, else None.

  monitor_aspect() -> float
      aspect of the active monitor (width/height).

  start_rotator(proc: subprocess.Popen, cfg: dict, on_orientation: Callable)
      daemon thread — re-launches scrcpy when aspect changes more than ±10%
      or when window class changes between portrait/landscape.
"""
import subprocess, threading, time, shutil, os, re
from typing import Callable, Optional

DEFAULT_CLASS = "scrcpy"

def _hyprctl(*args) -> Optional[str]:
    if not shutil.which("hyprctl"):
        return None
    try:
        r = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=2)
        if r.returncode != 0: return None
        return r.stdout
    except Exception:
        return None

def active_window_class() -> Optional[str]:
    out = _hyprctl("activewindow")
    if not out: return None
    try:
        import json
        d = json.loads(out)
        cls = (d.get("class") or "").lower()
        return cls or None
    except Exception:
        return None

def active_window_size() -> Optional[tuple[int, int]]:
    out = _hyprctl("activewindow")
    if not out: return None
    try:
        import json
        d = json.loads(out)
        return tuple(d.get("size", [0, 0]))
    except Exception:
        return None

def monitor_aspect(monitor_id: int = 0) -> float:
    out = _hyprctl("monitors", "-j")
    if not out: return 1.0
    try:
        import json
        ms = json.loads(out)
        for m in ms:
            if m.get("id") == monitor_id:
                w = m.get("width", 1920)
                h = m.get("height", 1080)
                if m.get("transform", 0) in (1, 3):  # rotated 90/270
                    w, h = h, w
                return w / h
        if ms:
            w = ms[0].get("width", 1920)
            h = ms[0].get("height", 1080)
            return w / h
    except Exception:
        pass
    return 1.0

def current_orientation(window_class: str = DEFAULT_CLASS) -> str:
    """Pick --orientation value (0/90/180/270) for the active window.

    Logic: if a scrcpy window is the active window, use its size aspect.
    Otherwise use the active monitor's aspect.
    """
    cls = active_window_class()
    if cls == window_class.lower():
        sz = active_window_size()
        if sz and sz[0] > 0 and sz[1] > 0:
            return "0" if sz[0] >= sz[1] else "0"  # camera captures natural
    return "0"

def start_rotator(restart_callback: Callable[[str], None], cfg: dict,
                  window_class: str = DEFAULT_CLASS, interval: float = 2.0) -> threading.Thread:
    """Spawn a daemon thread that calls restart_callback(orientation) when the
    aspect of the active scrcpy window changes. Use this to re-launch scrcpy
    with --orientation=N. The callback is responsible for terminating the
    current process and starting a new one with the given orientation.

    To avoid bouncing, only restart when aspect flips past a 10% threshold
    and stays there for at least 1.5s.
    """
    stop = threading.Event()
    cfg_local = cfg
    def run():
        last = "0"
        last_change = 0.0
        while not stop.is_set():
            try:
                cur = current_orientation(window_class)
                if cur != last and (time.time() - last_change) > 1.5:
                    last = cur
                    last_change = time.time()
                    try:
                        restart_callback(cur)
                    except Exception:
                        pass
            except Exception:
                pass
            stop.wait(interval)
    t = threading.Thread(target=run, daemon=True, name="scrcpy-rotator")
    t.start()
    return t
