"""config.update() must never clobber the endpoint the phone pushes.

The load-bearing failure this guards: `android_port` is written by the PHONE
(/phone handler) while the app runs, not by any settings screen. A settings save
that writes a whole dict from a startup snapshot reverts the port to whatever
it was at startup, and the next `adb connect` goes to port 0 or the wrong
phone. This is a silent regression - nothing errors, the webcam just stops
appearing - which is why it needed a test rather than a code review.

The GUI's persist() had this right already, but its merge lived inside the App
class. Adding a second writer (the panel's CLI) is exactly how it comes back,
so the merge now lives in one place and both callers share it.

Two directions are asserted:

  positive - a key the caller DID ask for lands on disk
  negative - a key it did NOT ask for survives untouched, which is the whole
             point. A naive whole-dict save fails this and passes everything else.

Also asserted: an unknown key is rejected rather than written. `set` is driven
by a QML panel, and a typo'd key would otherwise be persisted forever with no
effect and no error.

config.py resolves CONFIG_FILE from Path.home() at IMPORT time, so the globals
are rebound to a tmpdir here. Without that this test rewrites the real
~/.config/android-webcam/config.json - destroying android_port=5555, which the
phone pushed and which is needed for the stream to work at all. That makes the
test destructive rather than merely wrong, so the rebinding is load-bearing
and is checked by the final assertion that the real file was never touched.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from android_webcam import config as cfgmod  # noqa: E402

REAL_CONFIG = pathlib.Path.home() / ".config/android-webcam/config.json"
real_before = REAL_CONFIG.read_bytes() if REAL_CONFIG.exists() else None

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


class tmpcfg:
    """Rebind config.py's path globals to a throwaway directory."""

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
    # Seed what the PHONE would have pushed: a real endpoint, discovered live.
    pushed = dict(cfgmod.DEFAULTS)
    pushed.update({"android_ip": "192.168.1.4", "android_port": 5555})
    cfgmod.save(pushed)

    # --- the negative that matters: an unrelated edit keeps the pushed port ---
    cfgmod.update(torch=True)

    on_disk = json.loads(cfgmod.CONFIG_FILE.read_text())
    check("editing torch does NOT revert the phone's pushed android_port",
          on_disk["android_port"] == 5555,
          f"port is {on_disk['android_port']}, expected 5555 - this is the clobber")
    check("editing torch does NOT revert the pushed android_ip",
          on_disk["android_ip"] == "192.168.1.4",
          f"ip is {on_disk['android_ip']}")
    check("the requested key IS written",
          on_disk["torch"] is True,
          f"torch is {on_disk['torch']}")

    # --- the reverse direction: the phone's push survives our settings save ---
    cfgmod.save({**cfgmod.load(), "android_port": 7911})
    cfgmod.update(mic_to_host=False)
    after = cfgmod.load()
    check("a settings save does not revert a LATER push",
          after["android_port"] == 7911,
          f"port is {after['android_port']}, expected 7911")

    # --- unrelated keys are untouched, not reset to defaults ---
    cfgmod.update(camera_facing="front")
    both = cfgmod.load()
    check("settings from earlier edits survive the next edit",
          both["torch"] is True and both["mic_to_host"] is False,
          f"torch={both['torch']} mic_to_host={both['mic_to_host']}")

    # --- unknown keys are refused, not silently persisted ---
    try:
        cfgmod.update(torcht=True)
        raised = None
    except KeyError as e:
        raised = e
    check("an unknown key is rejected", isinstance(raised, KeyError),
          f"no error raised; wrote {json.loads(cfgmod.CONFIG_FILE.read_text()).get('torcht')}")
    check("the rejected key is not on disk",
          "torcht" not in json.loads(cfgmod.CONFIG_FILE.read_text()),
          "typo'd key was persisted with no effect and no error")

    # --- atomic write leaves no debris and never truncates ---
    check("no temp files left behind",
          not list(cfgmod.CONFIG_DIR.glob("*.tmp")),
          f"left {[p.name for p in cfgmod.CONFIG_DIR.glob('*.tmp')]}")
    check("config.json is valid JSON after all writes",
          isinstance(json.loads(cfgmod.CONFIG_FILE.read_text()), dict), "")

    # --- and the file is still parseable by a reader mid-write (atomicity) ---
    check("update() returns the full merged config, not just the changes",
          set(pushed) <= set(cfgmod.update(fps=30)),
          "returned config is missing keys a reader would expect")

# The real config must be byte-identical to how we found it.
real_after = REAL_CONFIG.read_bytes() if REAL_CONFIG.exists() else None
check("the REAL ~/.config/android-webcam/config.json was never written",
      real_before == real_after,
      "test clobbered the live config - rebind CONFIG_FILE to a tmpdir")

print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)