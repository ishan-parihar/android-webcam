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
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HOME = pathlib.Path.home()
PLUGIN = HOME / ".config/omarchy/plugins/ishanp.android-webcam/BarWidget.qml"
SRC = pathlib.Path(__file__).resolve().parents[1] / "omarchy-plugin/BarWidget.qml"

# Pulls the shell script out of: command: ["bash","-c","<here>"]
CMD_RE = re.compile(r'id:\s*statusProc.*?command:\s*\["bash","-c","([^"]+)"\]', re.S)

# A stand-in named "scrcpy". It must be a real native binary, not a shell
# script: `pgrep -x` matches comm, which the kernel derives from the executable
# image, and a `#!/bin/sh` wrapper reports dash as its image. Two earlier
# stand-ins passed for the wrong reason and both are worth remembering:
#   - a comment carrying the words, which is not in argv
#   - `exec -a`, which REPLACES the image, so /proc/pid/exe became sleep
# A plain copy of /bin/sleep under the name scrcpy has comm "scrcpy" and needs
# no argv trickery at all.
BIN_DIR = pathlib.Path(tempfile.mkdtemp())
FAKE_BIN = BIN_DIR / "scrcpy"
shutil.copy("/bin/sleep", FAKE_BIN)
FAKE_BIN.chmod(0o755)

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures += 1


def shipped_status_command(path):
    """The exact shell script the widget's statusProc runs, or None."""
    m = CMD_RE.search(path.read_text())
    return m.group(1) if m else None


def run_shipped(path) -> str:
    script = shipped_status_command(path)
    if script is None:
        return "<no statusProc command found>"
    return subprocess.run(["/bin/bash", "-c", script],
                          capture_output=True, text=True).stdout.strip()


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
    check(f"{label} status check does not use pgrep -f", "pgrep -f" not in script,
          f"still greps command-line text: {script}")
    check(f"{label} reports NOT streaming with no scrcpy alive",
          run_shipped(path) == "off",
          f"said {run_shipped(path)!r} with zero scrcpy processes running")

# Positive control: a real process whose name is scrcpy MUST be reported on.
# Without this, "always off" would pass the negative above vacuously.
proc = subprocess.Popen([str(FAKE_BIN), "30"])
try:
    for path, label in ((PLUGIN, "installed"), (SRC, "repo")):
        if not path.exists() or shipped_status_command(path) is None:
            continue
        check(f"{label} reports streaming for a real scrcpy process",
              run_shipped(path) == "on",
              f"said {run_shipped(path)!r} while a live scrcpy was running")
finally:
    proc.kill(); proc.wait()

# The stop button shares the hazard: pkill -f could kill the shell running it.
BAD_STOP = 'command: ["bash","-c","pkill -f'
for path, label in ((PLUGIN, "installed"), (SRC, "repo")):
    if not path.exists():
        continue
    text = path.read_text()
    check(f"{label} stop check is not pkill -f", BAD_STOP not in text,
          "stopProc could pkill the shell running it, or any editor naming scrcpy")
    check(f"{label} stop check uses pkill -x", "pkill -x scrcpy" in text,
          "expected pkill -x scrcpy")

FAKE_BIN.unlink()
BIN_DIR.rmdir()
print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)