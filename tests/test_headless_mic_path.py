"""The headless Start path must do what the GUI already does, or the mic leaks.

Found while building the panel: both Start surfaces (bar icon and panel button)
run `android-webcam --no-window`, which lands in backend.cli(), and cli() never
created the null sink. scrcpy's `--audio-source=mic` plays the phone's mic into
the DEFAULT SINK, so on this machine the microphone came out of the HDMI
speakers and fed back into the host mic. The panel's "Phone mic -> host" toggle
reported success while it happened.

The GUI has done this correctly all along (App._start_proc), so this asserts the
two paths AGREE rather than re-deriving the audio design.

The ORDER is the whole substance, and it is easy to get backwards:

  - ensure_null_sink() before the stream: it only creates a module.
  - set_phone_as_system_mic() on a daemon thread started before the run. It
    sleeps, moves the scrcpy sink-input, then verifies a stream exists - so
    called inline it finds nothing and returns set=False. subprocess.run blocks
    for the whole stream, so nothing can usefully run after it.
  - restore gated on .get("set"), not truthiness: a declined promotion still
    returns a POPULATED dict, and restoring that passes a None prev source.
  - remove_null_sink() in the finally, or the stale module makes
    ensure_null_sink a no-op and the mic arrives ~39 dB too quiet.

Why this EXECUTES cli() rather than reading its source: an earlier version of
this file asserted on substrings, and every one of those checks could be fooled -
by this file's own comments, which quote `ensure_null_sink()` and
`set_phone_as_system_mic()` by name, and by the two `[-1]` splits that reach past
cli() into the rest of a 1200-line module. A check that reads prose is not a
check on behaviour. Here every external call is stubbed and the ORDER of the
recorded calls is what is asserted, so no amount of commenting can pass it.

Stubs, not mocks of our own code: the point is what cli() asks the system to do.
"""
import pathlib
import sys
import threading

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import android_webcam.backend as backend  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


class Recorder:
    """Records the order in which cli() reaches out to the system."""

    def __init__(self):
        self.calls = []
        self.promoted = {"prev_default_source": "was:alsa", "prev_default_mute": False,
                         "set": True}

    def install(self, monkey_args, *, scrcpy_present=True, promote=True):
        rec = self
        saved = {n: getattr(backend, n) for n in
                 ("subprocess", "ensure_null_sink", "remove_null_sink",
                  "restore_system_audio", "set_phone_as_system_mic",
                  "scrcpy_exists", "logging", "threading", "build_argv", "load")}
        sys.argv = monkey_args

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                rec.calls.append("thread-start")
                rec.daemon = daemon
                self._t = target

            def start(self):
                # Run synchronously: the real thread races the stream, and this
                # test is about WHICH function runs, not about timing.
                rec.calls.append("promote")
                if self._t:
                    self._t()

        backend.threading = type("T", (), {"Thread": FakeThread})
        backend.subprocess = type("S", (), {"run": staticmethod(
            lambda *a, **k: rec.calls.append("scrcpy-run") or type("R", (), {"returncode": 0})())})
        backend.scrcpy_exists = lambda: True
        backend.ensure_null_sink = lambda: rec.calls.append("ensure-null-sink") or True
        backend.remove_null_sink = lambda: rec.calls.append("remove-null-sink")
        backend.restore_system_audio = lambda prev: rec.calls.append("restore") or True
        backend.set_phone_as_system_mic = (
            lambda cfg: rec.calls.append("promote") or dict(rec.promoted, set=promote))
        backend.build_argv = lambda cfg, **k: ["scrcpy", "--v4l2-sink=/dev/video0"]
        backend.load = lambda: {"mic_to_host": True, "mic_to_phone": False,
                                "mic_default": True, "android_ip": "192.168.1.4",
                                "android_port": 5555}
        return saved

    def restore(self, saved):
        for k, v in saved.items():
            setattr(backend, k, v)


# --- the happy path: mic forwarding on ------------------------------------
rec = Recorder()
saved = rec.install(["android-webcam", "--no-window"])
try:
    backend.cli()
finally:
    rec.restore(saved)
    sys.argv = sys.argv[:1]

order = rec.calls
check("cli() creates the null sink before starting scrcpy",
      "ensure-null-sink" in order
      and order.index("ensure-null-sink") < order.index("scrcpy-run"),
      f"order was {order}")
check("cli() promotes the phone mic on a daemon thread",
      "thread-start" in order and rec.daemon is True,
      f"daemon={getattr(rec, 'daemon', None)} - a non-daemon thread blocks exit")
check("promotion is dispatched before scrcpy blocks in subprocess.run",
      order.index("thread-start") < order.index("scrcpy-run"),
      f"order was {order}")
check("the null sink is removed when the stream ends",
      "remove-null-sink" in order
      and order.index("remove-null-sink") > order.index("scrcpy-run"),
      f"order was {order}")
check("the audio source is restored when the stream ends",
      "restore" in order and order.index("restore") > order.index("scrcpy-run"),
      f"order was {order}")

# --- the negative that matters: promotion DECLINED -------------------------
# set_phone_as_system_mic returns a POPULATED dict with set=False when scrcpy
# is not ready. Restoring that would call restore_system_audio with a None
# previous source and could clobber the user's real input device.
rec2 = Recorder()
saved = rec2.install(["android-webcam", "--no-window"], promote=False)
try:
    backend.cli()
finally:
    rec2.restore(saved)
    sys.argv = sys.argv[:1]

check("a DECLINED promotion does not restore a source it never took",
      "restore" not in rec2.calls,
      f"restore ran anyway with set=False: {rec2.calls}")
check("the null sink is still removed even when promotion was declined",
      "remove-null-sink" in rec2.calls, f"order was {rec2.calls}")

# --- mic forwarding OFF must not touch the audio system at all ------------
rec3 = Recorder()
saved = rec3.install(["android-webcam", "--no-window", "--no-mic"])
backend.load = lambda: {"mic_to_host": False, "mic_to_phone": False,
                        "mic_default": True, "android_ip": "192.168.1.4",
                        "android_port": 5555}
try:
    backend.cli()
finally:
    rec3.restore(saved)
    sys.argv = sys.argv[:1]

check("with the mic off, no sink is created and nothing is promoted",
      "ensure-null-sink" not in rec3.calls and "thread-start" not in rec3.calls,
      f"touched the audio system with mic_to_host off: {rec3.calls}")
check("with the mic off, scrcpy still starts",
      "scrcpy-run" in rec3.calls, f"order was {rec3.calls}")

# --- both Start surfaces must reach this path ----------------------------
for name in ("Panel.qml", "BarWidget.qml"):
    text = (REPO / name).read_text()
    check(f"{name} starts the stream through the headless path that is now fixed",
          "--no-window" in text,
          "without --no-window a bare `android-webcam` opens the GTK GUI instead")

print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)