"""`android-webcam set` must be a real, safe, no-GTK-launcher writer.

Three things this guards, each of which would be silent:

1. It must NOT open the GTK window. `main()` decides GUI-vs-CLI with a literal
   tuple of "--" strings, and `set` is a bare subcommand that matches none of
   them, so without an early dispatch it falls straight through to App().run()
   and a panel button spawns a window instead of writing a file. The
   positive/negative pair below proves which branch is taken, by observing the
   exit and the fact that nothing was displayed.

2. It must not clobber the endpoint the phone pushes. android_port is written
   by the phone at runtime; a whole-dict save from a stale snapshot reverts it
   and the webcam silently stops being found.

3. Values must be coerced to the type the backend expects, since the panel
   sends "on"/"off" and not booleans. A string "True" in a bool field reaches
   build_video_argv and produces a wrong argv.

The GUI cannot be imported here (no display, and gi is optional), so set_cmd is
driven directly - the same function main() dispatches to - and the dispatch
order it depends on is asserted against main()'s source text rather than by
launching a window.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from android_webcam import config as cfgmod  # noqa: E402
from android_webcam.app import set_cmd  # noqa: E402  (no GTK import at module load)

REAL_CONFIG = pathlib.Path.home() / ".config/android-webcam/config.json"
real_before = REAL_CONFIG.read_bytes() if REAL_CONFIG.exists() else None

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


class tmpcfg:
    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.saved = (cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE, cfgmod.LEGACY_CONFIG)
        cfgmod.CONFIG_DIR = self.dir
        cfgmod.CONFIG_FILE = self.dir / "config.json"
        cfgmod.LEGACY_CONFIG = self.dir / "legacy"
        return self

    def __exit__(self, *a):
        cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE, cfgmod.LEGACY_CONFIG = self.saved


with tmpcfg():
    # --- dispatch: `set` is handled BEFORE the flag tuple, or a panel button
    #     opens the GTK window instead of writing the file.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/android_webcam/app.py").read_text()
    # `.index` raises ValueError if the dispatch is gone, which aborts the whole
    # run with a traceback instead of naming the failed check. Report it as a
    # failure so the output says what broke.
    try:
        dispatch = src.index('sys.argv[1] == "set"')
        flagtest = src.index('any(a in sys.argv for a in')
        ordered = dispatch < flagtest
        detail = f"set at {dispatch}, flag test at {flagtest}"
    except ValueError as e:
        ordered, detail = False, f"dispatch block missing from main(): {e}"
    check("`set` is dispatched before the flag test that opens the GUI",
          ordered,
          f"{detail} - `set` would open a window instead of writing")

    # --- seed the phone's pushed endpoint
    cfgmod.update(android_ip="192.168.1.4", android_port=5555)

    # --- the basic panel toggle, as QML would send it
    rc = set_cmd(["torch", "on"])
    cfg = cfgmod.load()
    check("set torch on writes a real boolean", rc == 0 and cfg["torch"] is True,
          f"rc={rc} torch={cfg['torch']!r} type={type(cfg['torch']).__name__}")

    # --- the negative that matters: the pushed port survives
    check("set torch on did NOT revert the pushed android_port",
          cfg["android_port"] == 5555, f"port is {cfg['android_port']}")
    check("set torch on did NOT revert the pushed android_ip",
          cfg["android_ip"] == "192.168.1.4", f"ip is {cfg['android_ip']}")

    # --- off/false/on spellings all work (a panel sends any of them)
    for word, want in (("off", False), ("false", False), ("0", False),
                       ("on", True), ("1", True), ("true", True)):
        set_cmd(["torch", word])
        got = cfgmod.load()["torch"]
        check(f"`{word}` sets torch={want}", got is want, f"got {got!r}")

    # --- multiple keys in one call (a panel saves a row at a time)
    rc = set_cmd(["camera_facing", "front", "resolution", "720p", "fps", "15"])
    cfg = cfgmod.load()
    check("multiple keys in one invocation all land",
          rc == 0 and cfg["camera_facing"] == "front"
          and cfg["resolution"] == "720p" and cfg["fps"] == 15,
          f"facing={cfg['camera_facing']} res={cfg['resolution']} fps={cfg['fps']!r}")
    check("int coercion kept fps an int, not a string",
          isinstance(cfg["fps"], int), f"type is {type(cfg['fps']).__name__}")

    # --- dashed keys, because QML might send --mic-to-host
    rc = set_cmd(["--mic-to-host", "off"])
    check("dashed key form works", rc == 0 and cfgmod.load()["mic_to_host"] is False,
          f"rc={rc} mic_to_host={cfgmod.load()['mic_to_host']!r}")

    # --- rejections: each must be non-zero and must NOT write
    before = cfgmod.CONFIG_FILE.read_text()
    check("an unknown key exits non-zero", set_cmd(["nosuchkey", "1"]) == 2, "")
    check("a bad boolean exits non-zero", set_cmd(["torch", "maybe"]) == 2, "")
    check("a non-integer for an int key exits non-zero",
          set_cmd(["fps", "thirty"]) == 2, "")
    check("a missing value exits non-zero", set_cmd(["torch"]) == 2, "")
    check("no arguments exits non-zero", set_cmd([]) == 2, "")
    check("a rejected call left the file untouched",
          cfgmod.CONFIG_FILE.read_text() == before,
          "a failed `set` still wrote to disk")

    # --- the key is validated BEFORE coercion -----------------------------
    # What the reordering actually changes is the MESSAGE, not the exit code.
    # Both paths exit 2: the old one because coercion handed the string "on" to
    # update(), which raised KeyError, caught at the end of set_cmd. So asserting
    # the exit code alone pins nothing about the order - and it stayed green when
    # the validation was moved back. The observable difference is WHERE the key
    # is named: set_cmd's own check lists the known settings, update()'s late
    # raise does not. Assert the message.
    import io
    import contextlib

    def _set_capture(argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = set_cmd(argv)
        return rc, err.getvalue()

    before_typo = cfgmod.CONFIG_FILE.read_text()
    rc_typo, msg_typo = _set_capture(["torcht", "on"])
    check("a typo'd key exits non-zero", rc_typo == 2, f"returned {rc_typo}")
    check("the typo'd key is named by set_cmd's own check, not by a late raise",
          "torcht" in msg_typo and "known settings" in msg_typo.lower(),
          f"message was {msg_typo.strip()[:80]!r} - it fell through to "
          f"update()'s KeyError, so the reordering is not exercised")
    check("a typo'd key never reaches the file",
          cfgmod.CONFIG_FILE.read_text() == before_typo,
          "the mistyped key was written to disk")

    # --- private state is not a setting anyone can set ---------------------
    # _mic_prev holds the PipeWire source this app displaced when it promoted
    # the phone mic. Overwriting it means stop restores the wrong device.
    before_priv = cfgmod.CONFIG_FILE.read_text()
    rc_priv = set_cmd(["_mic_prev", "totally-not-a-sink"])
    check("a private key is refused",
          rc_priv == 2,
          f"returned {rc_priv} - internal state is writable from the CLI")
    check("the private key never reached the file",
          cfgmod.CONFIG_FILE.read_text() == before_priv,
          "_mic_prev was overwritten by a CLI caller")
    check("_mic_prev is still a real config key for the GUI",
          "_mic_prev" in cfgmod.DEFAULTS,
          "it must stay declared or persist() would raise on the GUI path")

    # --- and the good paths still work after the reordering ---------------
    check("a valid key still coerces after the reordering",
          set_cmd(["torch", "on"]) == 0 and cfgmod.load()["torch"] is True,
          "the reordering broke the normal path")

    # --- arity is reported before the key is validated --------------------
    # Both checks used to run in the other order, so `set torcht` with no value
    # dumped the entire known-settings list before saying what was actually
    # wrong. A typo that also omits its value should name that mistake.
    before_arity = cfgmod.CONFIG_FILE.read_text()
    rc_arity, msg_arity = _set_capture(["torcht"])
    check("a missing value is reported as a missing value",
          rc_arity == 2 and "needs a value" in msg_arity,
          f"rc={rc_arity}, message {msg_arity.strip()[:70]!r}")
    check("the arity error does not dump the settings list first",
          "known settings" not in msg_arity.lower(),
          f"message {msg_arity.strip()[:70]!r}")
    check("the arity error left the file untouched",
          cfgmod.CONFIG_FILE.read_text() == before_arity, "")

    # --- a real value, not a mock echo: the config the backend reads
    set_cmd(["camera_facing", "back", "resolution", "1080p"])
    final = json.loads(cfgmod.CONFIG_FILE.read_text())
    check("the written file is what the backend will load",
          final["camera_facing"] == "back" and final["resolution"] == "1080p",
          f"facing={final['camera_facing']} res={final['resolution']}")
    check("the pushed endpoint is still intact at the end",
          final["android_port"] == 5555 and final["android_ip"] == "192.168.1.4",
          f"{final['android_ip']}:{final['android_port']}")

real_after = REAL_CONFIG.read_bytes() if REAL_CONFIG.exists() else None
check("the REAL config was never written", real_before == real_after,
      "test clobbered the live config")

print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)