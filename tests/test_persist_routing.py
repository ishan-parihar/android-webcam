"""App.persist() must write through update(), and this must not regress.

A commit claimed "App.persist() routes through update() instead of doing its
own load-merge-save" and "update() is now the single writer". Both were false -
the GUI still ran its own load/merge/save, and the claim sat in the repo for a
full commit cycle. It was caught by grepping the writers rather than by a test,
which is exactly the failure mode worth pinning.

Why it matters, not just for tidiness:

  - the atomic write and the unknown-key check did not apply to the GUI's own
    edits, which are the majority of them
  - the `_mic_prev` DEFAULTS guard protected a path the GUI never took

So this asserts the routing structurally, and then behaviourally: persist() is
driven directly with a fake phone-push landing on disk between two calls, and
the port the phone pushed must still be there afterwards. The structural half
alone would pass for a `save()` that happens to be imported; the behavioural
half alone would need a GTK import, so both are needed.

app.py imports gi at module load, so persist() is exercised by extracting the
method source and running its body against this module's namespace. That is
awkward by design - the alternative is a headless GTK stub, which would test the
stub. What is asserted is the arithmetic persist() performs: diff against
_baseline, merge onto disk, adopt the result, re-seed the baseline.
"""
import pathlib
import re
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from android_webcam import config as cfgmod  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


APP = REPO / "src/android_webcam/app.py"
app_text = APP.read_text()
persist_src = app_text.split("def persist(self, **changes):")[1].split("\n    def ")[0]

# --- structural: the routing must be there and save() must not be ---------
check("persist() calls update(**changed)",
      "update(**changed)" in persist_src,
      f"no update(**changed) in persist(): {persist_src.strip()[:90]}")
save_call = re.search(r"save\(", persist_src)
check("persist() does not call save()",
      save_call is None,
      detail=(f"still writes the whole dict itself at offset {save_call.start()}"
              if save_call else ""))
check("persist() does not re-read the file itself",
      "load()" not in persist_src,
      "a bare load() here is the second read-modify-write the guard was for")
check("save is no longer imported into app.py",
      not re.search(r'from \.config import [^\n]*\bsave\b', app_text),
      "save is imported but unused, so the call site can creep back unnoticed")
check("app.py has no whole-dict config write left",
      not re.search(r'^\s*save\(', app_text, re.M),
      "a bare save(cfg) at column 0 is a second writer")

# --- and the whole-dict writer is now a hard error, not a landmine --------
backend_text = (REPO / "src/android_webcam/backend.py").read_text()
check("backend.py has no save(cfg) whole-dict write left",
      not re.search(r'^\s*save\(cfg\)', backend_text, re.M),
      "discovery or --set-ip is still writing the whole dict")
check("backend.py routes its endpoint writes through update()",
      "update(android_ip=ip, android_port=int(port))" in backend_text,
      "the /phone push and discovery are not on the merge path")
check("config.save() refuses to run rather than writing a stale snapshot",
      "NotImplementedError" in (REPO / "src/android_webcam/config.py").read_text(),
      "save() still writes a whole dict from a snapshot")

# --- the camera/mirror discriminator, behaviourally -----------------------
# pgrep -x scrcpy matches the NAME, and the screen-mirror session is the same
# executable, so a name-only check reports "streaming" while the phone is
# mirroring the desktop. The dot must be able to tell them apart. Driven with
# /proc synthesised, so this exercises the actual discriminator rather than
# asserting on source text - which has fooled this session three times.
import pathlib as _pl  # noqa: E402
from android_webcam import backend as _b  # noqa: E402

_real_iterdir = _pl.Path.iterdir


def _fake_proc(name, comm, cmdline):
    class _E:
        def __init__(self):
            self.name = name

        def __truediv__(self, part):
            p = _pl.Path(f"/tmp/_fp_{name}_{part}")
            if part == "comm":
                p.write_text(comm if isinstance(comm, str) else comm.decode())
            else:
                p.write_bytes(cmdline)
            return p
    return _E()


def _detect(comm, cmdline):
    _pl.Path.iterdir = lambda self: iter([_fake_proc("999", comm, cmdline)])
    try:
        return _b.scrcpy_is_camera()
    finally:
        _pl.Path.iterdir = _real_iterdir


for label, comm, cmd, want in (
    ("webcam (--v4l2-sink=/dev/video0)", b"scrcpy",
     b"scrcpy\x00--v4l2-sink=/dev/video0\x00", True),
    ("webcam (--v4l2-sink as a split arg)", b"scrcpy",
     b"scrcpy\x00--v4l2-sink\x00/dev/video0\x00", True),
    ("screen mirror (no v4l2 sink)", b"scrcpy",
     b"scrcpy\x00--display-id=0\x00", False),
    ("a shell merely mentioning scrcpy", b"bash",
     b"bash\x00-c\x00pgrep -x scrcpy\x00", False),
    ("an unrelated process", b"firefox", b"firefox\x00scrcpy\x00", False),
):
    check(f"camera detection: {label}",
          _detect(comm, cmd) is want,
          f"returned {_detect(comm, cmd)}, expected {want}")

# --- behavioural: drive the real arithmetic ------------------------------
class tmpcfg:
    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.saved = (cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE, cfgmod.LEGACY_CONFIG)
        cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE = self.dir, self.dir / "config.json"
        cfgmod.LEGACY_CONFIG = self.dir / "legacy"
        return self

    def __exit__(self, *a):
        cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE, cfgmod.LEGACY_CONFIG = self.saved


with tmpcfg():
    # Build a stand-in object exposing exactly what persist() touches, then run
    # persist()'s own source against it. gi is never imported this way.
    ns = {"update": cfgmod.update, "load": cfgmod.load}
    obj = types.SimpleNamespace()
    exec(compile("def persist(self, **changes):" + persist_src,
                 "<persist>", "exec"), ns)
    obj.persist = types.MethodType(ns["persist"], obj)

    # Startup state: the GUI loaded a config where the phone had not reported.
    obj.cfg = dict(cfgmod.DEFAULTS)
    obj.cfg.update({"android_ip": "192.168.1.12", "android_port": 0})
    obj._baseline = dict(obj.cfg)
    cfgmod.update(android_ip=obj.cfg["android_ip"], android_port=obj.cfg["android_port"])

    # The phone reports itself WHILE the GUI runs - the /phone push.
    cfgmod.update(android_ip="192.168.1.4", android_port=5555)

    # The user changes a setting. This is the call that used to revert the port.
    obj.cfg["torch"] = True
    obj.persist()

    after = cfgmod.load()
    check("persist() keeps the port the phone pushed while the GUI was running",
          after["android_port"] == 5555,
          f"port is {after['android_port']}, expected 5555 - this is the clobber")
    check("persist() keeps the pushed address",
          after["android_ip"] == "192.168.1.4", f"ip is {after['android_ip']}")
    check("persist() writes the setting it was asked to",
          after["torch"] is True, f"torch is {after['torch']!r}")

    # The second change is where the clobber used to land: after _baseline
    # moved, the port looked "changed" and got written straight back.
    obj.cfg["mic_to_host"] = False
    obj.persist()
    second = cfgmod.load()
    check("a SECOND persist() still keeps the pushed port",
          second["android_port"] == 5555,
          f"port is {second['android_port']} - the clobber landed on the second save")
    check("both settings persisted, neither reset",
          second["torch"] is True and second["mic_to_host"] is False,
          f"torch={second['torch']} mic_to_host={second['mic_to_host']}")

    # _baseline must be re-seeded from what update() returned, or the next
    # diff compares against a stale snapshot and rewrites untouched keys.
    check("persist() re-seeds _baseline from the merged config",
          obj._baseline.get("android_port") == 5555,
          f"_baseline port is {obj._baseline.get('android_port')}")
    check("persist() adopts the merged config in memory",
          obj.cfg.get("android_port") == 5555,
          f"in-memory port is {obj.cfg.get('android_port')}")

    # An unknown key must raise rather than land on disk.
    obj.cfg["torcht"] = True
    try:
        obj.persist()
        raised = None
    except KeyError as e:
        raised = e
    check("persist() refuses an unknown key", isinstance(raised, KeyError),
          f"no error; the guard that protects the GUI is not active")
    check("the refused key never reached disk",
          "torcht" not in cfgmod.CONFIG_FILE.read_text(),
          "an unrecognised key was written by the GUI")

if failures:
    print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
    sys.exit(1)

# --- the lock must serialise concurrent writers --------------------------
# os.replace makes each WRITE atomic; it does not make the load-merge-replace
# around it atomic. Two writers that both read port=0, both merge, and both
# replace will lose one of the changes. This drives update() from real threads
# with a barrier, which is how the GUI and a panel `set` actually collide.
import threading  # noqa: E402

lost = 0
TRIALS = 20
for _ in range(TRIALS):
    with tmpcfg():
        cfgmod.update(android_port=0, torch=False, mic_to_host=False)
        barrier = threading.Barrier(4)

        def racer(key, val):
            barrier.wait()          # maximise the interleaving
            cfgmod.update(**{key: val})

        ts = [threading.Thread(target=racer, args=a) for a in
              (("torch", True), ("mic_to_host", True), ("android_port", 5555))]
        for t in ts:
            t.start()
        barrier.wait()
        for t in ts:
            t.join()
        final = cfgmod.load()
        if not (final["torch"] and final["mic_to_host"] and final["android_port"] == 5555):
            lost += 1

check("concurrent update() calls do not lose a write", lost == 0,
      f"lost an update in {lost}/{TRIALS} trials - the lock is not covering the "
      f"whole read-modify-write")

# The above is the load-bearing half. Five clean runs of 200 trials, faithful
# unlock (only the flock lines removed, the re-read still inside the try):
#
#   unlocked:  151/200  177/200  197/200  194/200  198/200 lost
#   locked:      0/200 lost
#
# An earlier note here claimed the race did NOT reproduce, citing 0/200 with the
# flock removed. That run did compile and did execute unlocked code, so the
# reason it lost nothing is not established - the likeliest explanation is that
# the machine was loaded enough that the three writers serialised by accident.
# Treat the five-run distribution as the measurement and the 0/200 as a single
# unexplained outlier; do not re-derive a conclusion from it.
#
# The second check pins that the lock is actually TAKEN: a writer holding
# config.lock must block update() for as long as it holds it. Removing the flock
# makes it return in ~0.001s instead of waiting out the hold.
import time  # noqa: E402

with tmpcfg():
    cfgmod.update(torch=True)
    lock = cfgmod.CONFIG_DIR / "config.lock"
    check("the lockfile lives beside the config it guards", lock.exists(),
          f"expected {lock}")

    holder_fh = []
    def hold():
        fh = open(str(lock), "w")
        import fcntl as _f
        _f.flock(fh, _f.LOCK_EX)
        holder_fh.append(fh)
        time.sleep(0.5)
        _f.flock(fh, _f.LOCK_UN)

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.1)                 # let the holder take the lock first
    started = time.monotonic()
    cfgmod.update(mic_to_host=True)
    waited = time.monotonic() - started
    t.join()

    check("update() blocks while another writer holds the lock", waited > 0.25,
          f"update() returned in {waited:.3f}s - the lock is not being taken, "
          f"so the read-modify-write is unprotected")
    check("the value written after the block still landed",
          cfgmod.load()["mic_to_host"] is True, "")
    for fh in holder_fh:
        fh.close()

print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)