"""The bar widget's streaming indicator must not be fooled by a text match.

Measured 17:55 on the INSTALLED plugin: with no scrcpy process alive at all -
confirmed by walking /proc and comparing each /proc/<pid>/exe - the widget
reported "on". The cause is `pgrep -f 'scrcpy.*camera.*v4l2-sink'`.

`-f` matches the whole command line, and the shell that RUNS the check is itself
a process whose command line contains those words. So the check is true inside
any script, terminal or test that mentions scrcpy. That is not a contrived
decoy: it is the widget's own polling loop, which lives inside a shell running
a command containing the pattern.

Two decoys were tried and both failed before this one, which is worth keeping in
the record because the first two passed for the wrong reason:
  - `sh -c 'sleep 20 # words'`   - a comment is not in argv, so pgrep never saw it
  - `sh -c 'sleep 20' 'words'`   - sh collapses its own argv, so the trailing
                                  argument never appeared in ps output either

The fix resolves each candidate's executable instead of trusting its text, so
only a real scrcpy binary counts. Both directions are asserted: a check that can
only ever say "off" would look identical to a working fix until someone actually
starts a stream.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

HOME = pathlib.Path.home()
PLUGIN = HOME / ".config/omarchy/plugins/ishanp.android-webcam/BarWidget.qml"
SRC = pathlib.Path(__file__).resolve().parents[1] / "omarchy-plugin/BarWidget.qml"

OLD_PATTERN = "scrcpy.*camera.*v4l2-sink"

# A stand-in binary named "scrcpy", so the positive control exercises the same
# exe-resolution path a real stream would.
#
# It has to BE a real compiled process, not a shell script. A shell wrapper
# with `exec -a` replaces its own image, so /proc/<pid>/exe pointed at sleep
# and the exe check could never pass - the test was wrong, not the fix. Real
# scrcpy is a native binary whose exe genuinely is scrcpy, so compile a copy of
# /bin/sleep under that name. The exe basename then matches, and the argv
# carries the v4l2 words so pgrep can find it.
BIN_DIR = pathlib.Path(tempfile.mkdtemp())
FAKE_BIN = BIN_DIR / "scrcpy"
FAKE_BIN.write_bytes(pathlib.Path("/bin/sleep").read_bytes())
FAKE_BIN.chmod(0o755)

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


def old_says_streaming() -> bool:
    """Exactly the widget's current check, run from a shell naming the words."""
    script = f"pgrep -f '{OLD_PATTERN}' >/dev/null && echo on || echo off"
    out = subprocess.run(["/bin/bash", "-c", script],
                         capture_output=True, text=True).stdout.strip()
    return out == "on"


def new_says_streaming() -> bool:
    """The proposed fix: only a process that IS scrcpy counts.

    Matches the BASENAME of the resolved executable, not the full path. A
    full-path compare is wrong for a wrapper: scrcpy is launched through
    setsid, and a process that execs anything else reports THAT as its exe.
    The basename survives exec, so `scrcpy` is still scrcpy.
    """
    pids = subprocess.run(["pgrep", "-f", "v4l2"],
                          capture_output=True, text=True).stdout.split()
    for pid in pids:
        try:
            if os.path.basename(os.path.realpath(f"/proc/{pid}/exe")) == FAKE_BIN.name:
                return True
        except (OSError, ValueError):
            pass
    return False


def pgrep_can_see_a_real_process() -> bool:
    """The candidate list must be non-empty when something real matches.

    Without this, a check hardcoded to return False would pass the positive
    control vacuously - the 'always off' failure mode.
    """
    proc = subprocess.Popen(["scrcpy --video-source=camera --v4l2-sink=/dev/video0",
                             "5"], executable=str(FAKE_BIN))
    try:
        return subprocess.run(["pgrep", "-f", "v4l2"],
                              capture_output=True).returncode == 0
    finally:
        proc.kill(); proc.wait()


# --- negative: nothing is streaming, but the shell names the pattern ---------
check("old check reports streaming with NO scrcpy running (the 17:55 bug)",
      old_says_streaming(),
      "expected pgrep -f to be fooled by the checking shell's own cmdline")
check("new check correctly reports NOT streaming",
      not new_says_streaming(),
      "the exe check claimed a stream that does not exist")

# --- positive: a real scrcpy binary must be detected ------------------------
# argv[0] carries the v4l2 words so `pgrep -f v4l2` can see it, exactly as a
# real scrcpy's command line does.
proc = subprocess.Popen(["scrcpy --video-source=camera --v4l2-sink=/dev/video0",
                         "30"], executable=str(FAKE_BIN))
try:
    check("pgrep -f 'v4l2' can see a genuinely running process",
          pgrep_can_see_a_real_process(),
          "candidate list was empty, so the fix could be 'always off'")
    check("new check reports streaming for a real scrcpy",
          new_says_streaming(),
          "exe check missed a genuine stream")
finally:
    proc.kill(); proc.wait()

# --- the shipped files must stop using the self-matching pattern -------------
# Match the EXECUTED command, not any mention: the fix's comment quotes the old
# pattern on purpose, so a plain substring test would fail on the explanation
# rather than on the bug.
BAD_STATUS = 'command: ["bash","-c","pgrep -f'
BAD_STOP = 'command: ["bash","-c","pkill -f'
for path, label in ((PLUGIN, "installed"), (SRC, "repo")):
    if not path.exists():
        print(f"skip  {label} BarWidget.qml not at {path}")
        continue
    text = path.read_text()
    check(f"{label} BarWidget.qml status check is not pgrep -f",
          BAD_STATUS not in text, "status check still greps command-line text")
    check(f"{label} BarWidget.qml stop check is not pkill -f",
          BAD_STOP not in text, "stop check could pkill the shell running it")
    check(f"{label} BarWidget.qml uses -x exactly once each",
          text.count("pgrep -x scrcpy >/dev/null") == 1
          and text.count("pkill -x scrcpy ;") == 1,
          "expected one pgrep -x and one pkill -x, got "
          f"pgrep={text.count('pgrep -x scrcpy >/dev/null')} "
          f"pkill={text.count('pkill -x scrcpy ;')}")

FAKE_BIN.unlink()
BIN_DIR.rmdir()
print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)