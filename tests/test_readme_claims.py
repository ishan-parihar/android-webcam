#!/usr/bin/env python3
"""Every command, path and flag the README tells a user to run must exist.

A README that documents a flag the CLI does not accept, or a script that is not
in the tree, is worse than no README: the user's first contact with the project
is a failure they will blame on the project. So each claim is checked against the
repo rather than trusted.

Every check below can FAIL. That is the point: a doc gate that only ever passes
measures nothing. Proven red by the `--mic-mute-offx` and `scripts/nope.sh`
injections at the bottom.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text()
PLUGIN_README = (REPO / "omarchy-plugin/README.md").read_text()
BOTH = README + PLUGIN_README

failures = []


def check(desc, ok, detail=""):
    if ok:
        print(f"  ok    {desc}")
    else:
        print(f"  FAIL  {desc}" + (f": {detail}" if detail else ""))
        failures.append(desc)


print("test_readme_claims: README statements match the repository")

# --- the repo URL is the one that actually resolves -------------------------
url = "https://github.com/ishan-parihar/android-webcam.git"
check("README uses the real remote owner/repo", url in README,
      "found a github URL that is not the configured origin")
check("no stale ishanp/ (not the owner) URL in either README",
      "github.com/ishanp/" not in BOTH,
      "README points at a repo slug that does not exist")

# --- every repo path mentioned must exist -----------------------------------
paths = set(re.findall(r"`(?:\./)?((?:scripts|src|tests|omarchy-plugin|android|assets|docs)/[\w./-]+)`", BOTH))
missing = sorted(p for p in paths if not (REPO / p).exists())
check(f"all {len(paths)} repo paths named in the READMEs exist", not missing,
      f"missing: {missing}")

# --- every flag mentioned must be a real flag -------------------------------
# Only flags inside an `android-webcam ...` span are this CLI's. Scanning the
# whole document picked up `--decode--` from the ASCII diagram and `--reload`
# from `udevadm control --reload`, which are not ours.
# The spans are matched across backtick fences, so a fenced block is flattened
# first: a naive `` `android-webcam ([^`]+)` `` sees nothing in a ```bash fence
# and the check silently passes on ZERO flags. Assert the count is non-zero.
spans = re.findall(r"android-webcam((?:[^`\n]|`[^`\n]*`)*)", BOTH)
# Drop a trailing `&& sudo udevadm control --reload ...` style continuation so
# another tool's flags are not attributed to this CLI.
flags = sorted({f for s in spans
                for f in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", s.split("&&")[0])})
check("the flag check actually collected flags (not a vacuous pass)",
      len(flags) >= 5, f"only found {flags}")
# subcommands are dispatched before argparse, so collect them too
subs = sorted(set(re.findall(r"android-webcam (set|is-streaming|stop)\b", BOTH)))

app = (REPO / "src/android_webcam/app.py").read_text()
backend = (REPO / "src/android_webcam/backend.py").read_text()
source = app + backend

unknown = [f for f in flags if f not in source]
check(f"all {len(flags)} flags in the READMEs are defined in the source",
      not unknown, f"documented but absent: {unknown}")

for s in subs:
    check(f"subcommand '{s}' is dispatched in main()",
          re.search(rf'sys\.argv\[1\]\s*==\s*"{s}"', app) is not None,
          f"'{s}' is documented but never dispatched")

# --- stated versions must be the real constraint ----------------------------
pyproject = (REPO / "pyproject.toml").read_text()
m = re.search(r'requires-python\s*=\s*">=([\d.]+)"', pyproject)
check("README states the real requires-python",
      m is not None and f"Python {m.group(1)}+" in README,
      f"pyproject says {m.group(1) if m else '?'}, README disagrees")

# --- the zero-dependency claim ----------------------------------------------
zero = re.search(r"dependencies\s*=\s*\[\s*\]", pyproject) is not None
check("README's 'no pip dependencies' claim matches pyproject", zero,
      "pyproject declares dependencies but README says none")

# --- install.sh behaviours the README describes -----------------------------
install = (REPO / "scripts/install.sh").read_text()
check("install.sh really adds the user to video/adbusers",
      "usermod" in install and "video" in install)
check("install.sh really touches /dev/video0 permissions",
      "/dev/video0" in install)
check("README does not claim install.sh writes a udev rule (it does not)",
      not re.search(r"install\.sh[^\n]*udev rule|udev rule[^\n]*install\.sh", README, re.I),
      "README credits install.sh with a udev rule; it only chgrp/chmod")
check("README documents the persistent udev rule as a manual fallback",
      "udev/rules.d" in README)
check("install.sh copies the plugin from omarchy-plugin/ (the fixed copy)",
      "omarchy-plugin" in install and "$ROOT/omarchy-plugin" in install)

# --- the plugin's own control path -----------------------------------------
panel = (REPO / "omarchy-plugin/Panel.qml").read_text()
check("README's claim that the panel uses setsid -f is true",
      "setsid -f android-webcam --no-window" in panel)
check("README's claim that Stop is camera-specific matches the panel",
      "android-webcam stop" in panel)

# --- no dead flags or TODOs leaking into user-facing docs -------------------
check("no TODO/FIXME/XXX placeholder in either README",
      not re.search(r"\b(TODO|FIXME|XXX)\b", BOTH),
      "user-facing README contains a placeholder")

# --- the negative controls: this gate CAN fail -----------------------------
for bogus in ("--mic-mute-offx",):
    check(f"the flag check would reject an invented flag ({bogus})",
          bogus not in source)
for bogus_path in ("scripts/definitely-not-here.sh",):
    check(f"the path check would reject a missing file ({bogus_path})",
          not (REPO / bogus_path).exists())

print()
if failures:
    print(f"FAILED: {len(failures)} failed")
    sys.exit(1)
print("PASSED: 0 failed")
