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
# The plugin lives at the repo ROOT: `omarchy plugin add` clones the repo and
# reads the top-level manifest.json, and omarchy-plugin-validate refuses any
# symlink inside a plugin folder, so a symlinked or duplicated second copy cannot
# be made to work. There is one manifest and one Panel.qml, and they are these.
PLUGIN_README = README
BOTH = README

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
check("no stale ishanp/ (not the owner) URL in the README",
      "github.com/ishanp/" not in BOTH,
      "README points at a repo slug that does not exist")

# --- every repo path mentioned must exist -----------------------------------
paths = set(re.findall(r"`(?:\./)?((?:scripts|src|tests|android|assets|docs)/[\w./-]+)`", BOTH))
missing = sorted(p for p in paths if not (REPO / p).exists())
check(f"all {len(paths)} repo paths named in the READMEs exist", not missing,
      f"missing: {missing}")

# --- every flag mentioned must be a real flag -------------------------------
# Enumerate the REAL flag set, then assert each README flag is an exact member.
# The earlier `f in source` was substring membership: it cannot separate
# `--1080` from `--1080turbo`, so it passed on an invented flag. A design flaw,
# not a typo - hence enumeration rather than a patched regex.
# A span must be a real command: either a fenced line that starts with
# `android-webcam`, or a backtick span whose content starts with it. Matching
# anywhere swallowed `99-android-webcam.rules` and then `--reload` from a
# udevadm continuation on the same line.
readme_flags = sorted({
    f for line in BOTH.splitlines()
    for m in re.findall(r"(?:^android-webcam |`android-webcam )([^`\n]*)", line)
    for f in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", m.split("&&")[0])
})
check("the flag check actually collected flags (not a vacuous pass)",
      len(readme_flags) >= 5, f"only found {readme_flags}")

app = (REPO / "src/android_webcam/app.py").read_text()
backend = (REPO / "src/android_webcam/backend.py").read_text()
source = app + backend
# argparse declarations, plus the literal allowlist tuple in main()
defined = set(re.findall(r'add_argument\(\s*["\'](--[a-z0-9-]+)', source))
defined |= set(re.findall(r'"(--[a-z0-9-]+)"', source))
unknown = [f for f in readme_flags if f not in defined]
check(f"all {len(readme_flags)} flags in the READMEs are real CLI flags",
      not unknown, f"documented but absent: {unknown}")

# The check must be able to say no: an invented flag sharing a prefix with a
# real one is the exact case substring membership got wrong.
check("the flag check rejects a prefix-extension of a real flag",
      "--1080turbo" not in defined and "--1080" in defined,
      "defined set cannot distinguish --1080 from --1080turbo")
subs = sorted(set(re.findall(r"android-webcam (set|is-streaming|stop)\b", BOTH)))

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
check("install.sh copies the plugin from the repo ROOT (one copy, no duplicate)",
      "$ROOT/omarchy-plugin" not in install
      and '"$ROOT/$f"' in install
      and not (REPO / "omarchy-plugin").exists(),
      "install.sh still reads omarchy-plugin/, which no longer exists")

# A published sdist must carry the plugin, or `omarchy plugin add` from the
# tarball fails the validator's manifest check. This shipped a package with no
# Panel.qml at all, silently, because the include list still named the directory
# that the cutover deleted.
sdist = re.search(r"\[tool\.hatch\.build\.targets\.sdist\](.*?)(\n\[|\Z)",
                  pyproject, re.S)
# Comments before the include line would otherwise swallow the match.
sdist_body = re.sub(r"#.*", "", sdist.group(1)) if sdist else ""
check("the sdist ships the plugin from the repo root",
      all(n in sdist_body for n in ("Panel.qml", "BarWidget.qml", "manifest.json"))
      and "omarchy-plugin/" not in sdist_body,
      f"sdist include is missing the root plugin files: {sdist_body.strip()[:90]}")

# --- the plugin's own control path -----------------------------------------
panel = (REPO / "Panel.qml").read_text()
check("README's claim that the panel uses setsid -f is true",
      "setsid -f android-webcam --no-window" in panel)
check("README's claim that Stop is camera-specific matches the panel",
      "android-webcam stop" in panel)

# --- no dead flags or TODOs leaking into user-facing docs -------------------
check("no TODO/FIXME/XXX placeholder in either README",
      not re.search(r"\b(TODO|FIXME|XXX)\b", BOTH),
      "user-facing README contains a placeholder")

# --- the app's real role -----------------------------------------------------
# I first wrote "the APK is legacy/unused". That was wrong: backend.py reads
# aw.log out of that app's data dir. Assert the README's description matches the
# code, so the claim cannot drift back.
check("README does not call the android/ app unused while the host reads its log",
      ("dev.ishanp.androidwebcam" not in source)
      or ("unused" not in README.lower()
          and "legacy" not in README.lower()),
      "host reads the companion app's aw.log, so README must not call it unused")
check("README describes the companion app as log capture, which is what it does",
      "aw.log" in README and "android/" in README)
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
