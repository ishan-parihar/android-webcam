"""The bar widget's streaming indicator must not be fooled by a text match.

Measured 17:55 on the INSTALLED plugin: with no scrcpy process alive at all -
confirmed by walking /proc and comparing each /proc/<pid>/exe - the widget
reported "on". The cause was `pgrep -f 'scrcpy.*camera.*v4l2-sink'`.

`-f` matches the whole command line, and the shell that RUNS the check is itself
a process whose command line contains those words. So the check is true inside
any script, terminal or test that mentions scrcpy. That is not a contrived
decoy: it is the widget's own polling loop, which lives inside a shell running
a command containing the pattern.

This test runs the command AS SHIPPED - it is extracted from the QML with a
regex, not retyped here. An earlier version reimplemented the check in Python
(over `pgrep -f v4l2` plus a /proc/<pid>/exe scan) and passed while the shipped
command was still the broken one: two different mechanisms, so the test proved
nothing about the file it claimed to guard. Reimplementing the thing under test
is how a suite ends up green on a broken command.

Both directions are asserted, because a check that can only say "off" would
look identical to a working fix until someone actually starts a stream.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

HOME = pathlib.Path.home()
PLUGIN = HOME / ".config/omarchy/plugins/ishanp.android-webcam/BarWidget.qml"
SRC = pathlib.Path(__file__).resolve().parents[1] / "omarchy-plugin/BarWidget.qml"

# Pull the statusProc command out of the QML. Parse the bracketed argv as JSON
# rather than picking out quoted strings: a token regex captured only the FIRST
# element ("android-webcam") and dropped the subcommand, so the harness exec'd a
# bare `android-webcam` - which matches none of main()'s flags and therefore
# opened the GTK window and blocked forever. Same trap as the panel button had,
# now inside the test that is supposed to catch it.
CMD_RE = re.compile(
    r'id:\s*statusProc.*?command:\s*(\[[^\]]*\])', re.S)

# A stand-in named "scrcpy". It must be a COMPILED binary: `comm` is derived
# from the executable IMAGE by the kernel, so a copy of /bin/sleep reads
# comm "sleep" no matter what argv[0] says, and a `#!/bin/sh` wrapper reports
# dash. Two dead ends, both worth remembering because each failed silently:
#   - a comment carrying the words, which is not in argv
#   - `exec -a`, which REPLACES the image, so /proc/pid/exe became sleep
#   - `executable=FAKE_BIN` with argv[0]="scrcpy": argv[0] resolves through PATH,
#     so it exec'd the REAL /usr/bin/scrcpy, which rejects --v4l2-sink and exits
#     at once, leaving a zombie whose cmdline is empty
# A binary compiled to the name scrcpy has comm "scrcpy" AND keeps its argv,
# which is what the detector reads.
BIN_DIR = pathlib.Path(tempfile.mkdtemp())
FAKE_SRC = BIN_DIR / "scrcpy.c"
FAKE_BIN = BIN_DIR / "scrcpy"
FAKE_SRC.write_text("#include <unistd.h>\n"
                    "int main(int argc, char **argv) { sleep(30); return 0; }\n")
_built = subprocess.run(["cc", "-O0", "-o", str(FAKE_BIN), str(FAKE_SRC)],
                        capture_output=True, text=True)
if _built.returncode != 0 or not FAKE_BIN.exists():
    # No compiler: fall back, and say so. The positive control will then fail
    # loudly (comm reads "sleep"), which is correct - a silent pass is worse.
    print(f"warn  no compiler ({_built.stderr.strip()[:60]}); "
          f"the positive control will fail rather than pass silently")
    shutil.copy("/bin/sleep", FAKE_BIN)
FAKE_BIN.chmod(0o755)

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


def shipped_status_command(path):
    """The exact argv the widget's statusProc runs, as a list, or None."""
    m = CMD_RE.search(path.read_text())
    if not m:
        return None
    argv = json.loads(m.group(1))
    # Quickshell Process.command may be a flat list or nested; normalise it.
    if argv and isinstance(argv[0], list):
        argv = argv[0]
    return argv


def run_shipped(path) -> str:
    cmd = shipped_status_command(path)
    if cmd is None:
        return "<no statusProc command found>"
    # Run the shipped argv DIRECTLY - never through a shell, which would
    # re-introduce the self-match the widget exists to avoid.
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=30).stdout.strip()


def real_camera_running() -> bool:
    """Is a REAL webcam stream up right now (not one of this file's stand-ins)?

    Uses the app's own detector rather than a heuristic. The point is to avoid
    touching a live stream: this test must pass whether or not the user happens
    to be using the webcam.
    """
    r = subprocess.run(["android-webcam", "is-streaming"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() == "on"


# The negative is the caller's OWN shell: this test's command line names
# scrcpy, exactly as the widget's polling shell does. A `pgrep -f` pattern
# therefore matches it. There is no decoy process to construct, and none is
# needed - this file is the decoy.
for path, label in ((PLUGIN, "installed"), (SRC, "repo")):
    if not path.exists():
        print(f"skip  {label} BarWidget.qml not at {path}")
        continue
    script = shipped_status_command(path)
    if script is None:
        check(f"{label} ships a parseable statusProc command", False,
              "regex found no command: [\"bash\",\"-c\",...] on statusProc")
        continue
    check(f"{label} status check does not use pgrep -f",
          not any("pgrep -f" in a for a in script),
          f"still greps command-line text: {script}")
    # The negative needs no scrcpy to be running, and this test must never make
    # that true by killing a real one. I did exactly that once - stopped the
    # user's live stream to satisfy this check - which is not a thing a test is
    # allowed to do. If a real camera is up, say so and skip: the positive
    # control below still proves the check is not stuck off.
    if real_camera_running():
        print(f"skip  {label} 'not streaming' check — a real camera stream is "
              f"running, and stopping the user's stream is not this test's call")
        continue
    check(f"{label} reports NOT streaming with no scrcpy alive",
          run_shipped(path) == "off",
          f"said {run_shipped(path)!r} with zero scrcpy processes running")

# Positive control: a real process that IS a webcam MUST be reported on.
# Without this, "always off" would pass the negative above vacuously.
#
# argv[0] carries the scrcpy name (so /proc/<pid>/comm is "scrcpy") and
# argv[1] carries --v4l2-sink (so it is the CAMERA, not the screen mirror).
proc = subprocess.Popen(
    ["scrcpy", "--v4l2-sink=/dev/video0", "30"], executable=str(FAKE_BIN))
try:
    for path, label in ((PLUGIN, "installed"), (SRC, "repo")):
        if not path.exists() or shipped_status_command(path) is None:
            continue
        check(f"{label} reports streaming for a real scrcpy process",
              run_shipped(path) == "on",
              f"said {run_shipped(path)!r} while a live scrcpy was running")
finally:
    proc.kill(); proc.wait()

# The companion: a stand-in that is a scrcpy but NOT a camera must report off.
# That is the screen-mirror case, and it is the whole reason the check changed -
# `pgrep -x scrcpy` cannot tell the two apart, so the dot used to read
# "streaming" while the phone was mirroring the desktop.
mirror = subprocess.Popen(
    ["scrcpy", "--display-id=0", "30"], executable=str(FAKE_BIN))
try:
    for path, label in ((PLUGIN, "installed"), (SRC, "repo")):
        if not path.exists() or shipped_status_command(path) is None:
            continue
        check(f"{label} reports NOT streaming for a screen-mirror scrcpy",
              run_shipped(path) == "off",
              f"said {run_shipped(path)!r} for a scrcpy with no --v4l2-sink - "
              f"the webcam and the mirror are the same executable")
finally:
    mirror.kill(); mirror.wait()

# The stop button shares the hazard: pkill -f could kill the shell running it.
# Loops over the PANEL too. Its toggleProc used to carry its own pkill, and a
# grep for pkill/pgrep in executed commands missed it - so the panel's stop path
# was uncovered at exactly the moment it mattered. Four call sites exist across
# two files and this loop now sees all of them.
BAD_STOP = 'command: ["bash","-c","pkill -f'
for path, label in ((PLUGIN, "installed bar"), (SRC, "repo bar"),
                    (PLUGIN.parent / "Panel.qml", "installed panel"),
                    (pathlib.Path(__file__).resolve().parents[1]
                     / "omarchy-plugin/Panel.qml", "repo panel")):
    if not path.exists():
        continue
    text = path.read_text()
    check(f"{label} stop check is not pkill -f", BAD_STOP not in text,
          "stopProc could pkill the shell running it, or any editor naming scrcpy")
    # pkill -x is no longer acceptable either: it matches the NAME, and the
    # screen-mirror session is the same executable, so stopping the webcam would
    # also stop a mirror (android-screen is a real scrcpy wrapper here).
    # Check the EXECUTED commands, not the file: these files document the old
    # bug in prose, and "pkill" not in text flags the explanation instead of
    # the defect. Fourth time in this work that a comment has defeated a
    # plain-text check.
    # Strip comment lines, then search the CODE. Two earlier attempts were too
    # blunt in opposite directions: matching the raw text flags this file's own
    # explanation of the old bug, while `command:\s*\[[^\]]*\]` truncates at the
    # FIRST `]` - which cuts off a multi-line concatenated `bash -c` string
    # before the verb inside it, and reported the panel as having no `stop` when
    # it does. This is the fourth and fifth time in this work that a check on
    # QML text measured the wrong thing.
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("//"))
    check(f"{label} stop is not a blanket pkill",
          "pkill" not in code,
          "pkill matches the NAME, which the screen mirror also has - stopping "
          "the webcam would take a mirror session down with it")
    # The verb appears two ways across these files: as the argv
    # ["android-webcam", "stop"] (the bar) and inside a bash -c string as the
    # words `android-webcam stop` (the panel). Match both literally. A regex
    # spelling it as `"android-webcam",\s*"stop"` matches the bar only, and one
    # searching for the bare words matches the panel only - so an earlier
    # version of each was wrong for half the call sites, which is how the
    # panel's stop path went uncovered in the first place.
    check(f"{label} stop check uses the camera-scoped verb",
          ("android-webcam stop" in code) or ('"android-webcam", "stop"' in code),
          "expected `android-webcam stop` (argv form or inside bash -c)")

FAKE_BIN.unlink(missing_ok=True)
FAKE_SRC.unlink(missing_ok=True)
BIN_DIR.rmdir()

# --- the KILL, not just the detection ------------------------------------
# Everything above proves the app can RECOGNISE the camera. Nothing proved that
# stopping it leaves the screen mirror alone - the change that motivated the
# `stop` verb. So run both for real: two compiled stand-ins, one carrying
# --v4l2-sink and one not, then stop_camera_stream().
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from android_webcam.backend import scrcpy_camera_pids, stop_camera_stream  # noqa: E402

KILL_DIR = pathlib.Path(tempfile.mkdtemp())
KILL_BIN = KILL_DIR / "scrcpy"
(KILL_DIR / "s.c").write_text("#include <unistd.h>\n"
                              "int main(int c,char**v){sleep(120);return 0;}\n")
subprocess.run(["cc", "-O0", "-o", str(KILL_BIN), str(KILL_DIR / "s.c")],
               capture_output=True, text=True)

if not KILL_BIN.exists():
    print("warn  no compiler; cannot test the kill behaviourally")
else:
    KILL_BIN.chmod(0o755)
    cam = subprocess.Popen([str(KILL_BIN), "--v4l2-sink=/dev/video0"])
    mir = subprocess.Popen([str(KILL_BIN), "--display-id=0"])
    try:
        # Both stand-ins must be ALIVE before the kill, and the camera scan must
        # name only the camera. Asking the camera scan to report the mirror was
        # nonsense - it is built to exclude exactly that.
        for _ in range(50):
            if cam.poll() is None and mir.poll() is None:
                break
            time.sleep(0.1)
        check("both stand-ins are alive before the kill",
              cam.poll() is None and mir.poll() is None,
              f"camera={cam.poll()} mirror={mir.poll()} (neither should be done)")
        check("the camera scan names the camera and NOT the mirror",
              scrcpy_camera_pids() == [cam.pid],
              f"scan saw {sorted(scrcpy_camera_pids())}, expected exactly [{cam.pid}]")

        killed = stop_camera_stream()
        for _ in range(50):
            if cam.poll() is not None:
                break
            time.sleep(0.1)
        check("stop_camera_stream signalled exactly the camera",
              killed == 1, f"returned {killed}, expected 1")
        check("the CAMERA process died", cam.poll() is not None,
              f"camera pid {cam.pid} still alive after stop")
        check("the SCREEN MIRROR survived",
              mir.poll() is None,
              f"mirror pid {mir.pid} was killed too - the whole point of the "
              f"stop verb is that it does not match on process name")
        check("nothing camera-like remains after the stop",
              scrcpy_camera_pids() == [],
              f"still see {scrcpy_camera_pids()}")
    finally:
        for p in (cam, mir):
            if p.poll() is None:
                p.kill()
            p.wait()
    KILL_BIN.unlink()
    (KILL_DIR / "s.c").unlink()
    KILL_DIR.rmdir()
print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)