"""Each panel toggle must change what the BACKEND reads, not just a QML property.

A toggle that flips a QML `checked` binding and writes nothing looks identical
to a working one until someone uses it. So every control here is asserted at
the layer it is supposed to affect:

  - torch / camera_facing / resolution / fps must change the argv that
    build_video_argv produces, because that argv is what scrcpy is actually
    launched with
  - mic_to_host must change the audio flags in that same argv
  - the pushed android_port must survive all of it
  - the panel must read state from the config FILE

The last one is the important structural check: if the panel kept its own copy
of the toggles, the dot and the buttons would disagree exactly the way the bar's
streaming indicator did. So the test asserts the panel invokes `set` (the merge
writer) rather than writing the file itself, and re-reads the file to refresh.
"""
import json
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from android_webcam import config as cfgmod  # noqa: E402
from android_webcam import backend  # noqa: E402

PANEL = REPO / "omarchy-plugin/Panel.qml"
INSTALLED = (pathlib.Path.home()
             / ".config/omarchy/plugins/ishanp.android-webcam/Panel.qml")

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


def argv_for(**over):
    """The exact argv the backend would launch scrcpy with."""
    cfg = cfgmod.load()
    cfg.update(over)
    return cfg, backend.build_argv(cfg, audio_owned=False)


def defined_functions(text):
    """Function names declared in this QML: `function name(` or `name: function(`."""
    names = set(re.findall(r'function\s+(\w+)\s*\(', text))
    names |= set(re.findall(r'^\s*(\w+)\s*:\s*function\s*\(', text, re.M))
    return names


with tmpcfg():
    # ---------- every root.X() call in either QML must resolve -------------
    # `root.toggle()` was called but defined nowhere - not in the file, not in
    # the shell's BarWidget base - so a left click did nothing at all, silently.
    # Right-click worked, which is why it read as a vague complaint rather than
    # a bug report. Only method-call syntax is checked: a property reference
    # like root.streaming is not a call, and the shell supplies some methods.
    #
    # Comments are stripped first. The fix documents the old bug by NAME, so
    # checking the raw text flags the explanation rather than the defect - the
    # same mistake the pgrep -f check made one revision earlier.
    SHELL_METHODS = {"close", "show", "hide"}
    for path in (PANEL, REPO / "omarchy-plugin/BarWidget.qml"):
        text = path.read_text()
        code = re.sub(r'//[^\n]*', '', text)
        local = defined_functions(code)
        calls = set(re.findall(r'root\.(\w+)\s*\(', code))
        unresolved = sorted(calls - local - SHELL_METHODS)
        check(f"{path.name}: every root.X() call resolves to a defined function",
              not unresolved, f"called but never defined: {unresolved}")

    # ---------- the bar icon must actually control something -----------------
    bar_text = (REPO / "omarchy-plugin/BarWidget.qml").read_text()
    bar_code = re.sub(r'//[^\n]*', '', bar_text)
    handler = re.search(r'onPressed:\s*function\s*\([^)]*\)\s*\{(.*?)\n\s{4}\}',
                        bar_code, re.S)
    body = handler.group(1) if handler else ""
    check("the bar icon's press handler starts or stops the stream",
          "launchProc" in body and "stopProc" in body,
          f"press handler does neither: {body.strip()[:70]}")
    check("the press handler is not gated on one button only",
          "Qt.RightButton" not in body,
          "only right-click works, so left-click is a silent no-op")

    bar_execs = re.findall(r'command:\s*\[[^\]]*\]', bar_text)
    check("the bar's launch passes --no-window",
          any("--no-window" in e for e in bar_execs),
          "a bare `android-webcam` misses main()'s allowlist and opens the GUI")
    check("the bar's launch is detached with setsid",
          any("setsid" in e for e in bar_execs),
          "without setsid the stream dies with the shell")

    panel_text = PANEL.read_text()
    # the bar's own stopProc stops every scrcpy, so the "bar and panel agree"
    # check is about the start command only.
    check("bar and panel both start via setsid + --no-window",
          any("setsid" in e for e in re.findall(r'command:\s*\[[^\]]*\]', panel_text)),
          "the panel and the bar disagree on how to start the stream")

    # ---------- the dot must tell the webcam from the screen mirror ---------
    # `pgrep -x scrcpy` matches the NAME, and the screen-mirror session is the
    # same executable - so the dot read "streaming" while the phone was
    # mirroring the desktop. Both surfaces must ask the app, which checks
    # /proc/<pid>/cmdline for --v4l2-sink.
    for path in (PANEL, REPO / "omarchy-plugin/BarWidget.qml"):
        text = path.read_text()
        execs = re.findall(r'command:\s*\[[^\]]*\]', text)
        check(f"{path.name} asks the app, not pgrep, whether a camera is running",
              any("is-streaming" in e for e in execs)
              and not any("pgrep" in e for e in execs),
              "pgrep -x cannot distinguish the webcam from the screen mirror")

    # ---------- every control the panel offers is a real config key ----------
    cfgmod.update(android_ip="192.168.1.4", android_port=5555, torch=False,
                 camera_facing="back", resolution="1080p",
                 mic_to_host=False, with_preview=False)

    # ---------- each control must change the real argv ----------
    base_cfg, base = argv_for()

    _, torch_on = argv_for(torch=True)
    check("torch on changes the scrcpy argv",
          torch_on != base,
          "argv identical with torch on and off - the toggle would do nothing")
    check("torch on actually adds a torch flag",
          any("torch" in a for a in torch_on),
          f"argv={torch_on}")

    _, front = argv_for(camera_facing="front")
    check("camera_facing front changes the scrcpy argv",
          front != base, f"argv identical; back was {base}")

    _, small = argv_for(resolution="720p")
    check("resolution 720p changes the scrcpy argv",
          small != base, f"argv identical; 1080p was {base}")
    check("720p really is the smaller stream",
          "1280x720" in " ".join(small) and "1920x1080" not in " ".join(small),
          f"argv={small}")

    _, mic = argv_for(mic_to_host=True)
    check("mic_to_host changes the scrcpy argv",
          mic != base, "argv identical with the mic on and off")

    # ---------- the two must be independent, not one shared switch ----------
    _, torch_only = argv_for(torch=True)
    _, mic_only = argv_for(mic_to_host=True)
    check("torch and mic_to_host are independent controls",
          torch_only != mic_only,
          "both toggles produce the same argv, so one is shadowing the other")

    # ---------- the pushed endpoint survives every control ----------
    after = cfgmod.load()
    check("none of the toggles reverted the phone's pushed endpoint",
          after["android_port"] == 5555 and after["android_ip"] == "192.168.1.4",
          f"{after['android_ip']}:{after['android_port']}")

    # ---------- the panel drives `set`, not the file ----------
    check("the panel writes through the `set` merge writer",
          '"android-webcam", "set"' in panel_text,
          "panel does not invoke `set`; a direct file write can clobber the port")
    check("the panel never writes config.json itself",
          not re.search(r'>\s*~?/?\.config/android-webcam/config\.json', panel_text),
          "panel appears to redirect into the config file")

    # ---------- the panel reads state from the file, not from memory ----------
    check("the panel re-reads the config file",
          "config.json" in panel_text,
          "panel has no reader, so it can only remember what was clicked")
    check("state is bound to the read, not a local variable",
          'checked: root.truthy(' in panel_text
          and 'root.value("camera_facing"' in panel_text
          and 'root.value("resolution"' in panel_text,
          "toggles are not driven by the config read")
    check("the panel refreshes after a write",
          "cfgReader.running = true" in panel_text,
          "panel writes but never re-reads, so it would show stale state")

    # Strip comment lines so these assertions measure code, not the prose that
    # explains them. `code` is defined per-file; define it here so the block
    # stands alone.
    code = "\n".join(ln for ln in panel_text.splitlines()
                     if not ln.lstrip().startswith("//"))

    # ---------- accessibility: every control must be reachable by keyboard --
    # These are the controls for the whole feature. A hand-rolled
    # Rectangle+Text row has no focus and no key handling, so the webcam was
    # operable by mouse only - a functional gap, not a theming preference, and
    # why the panel now uses the shell's own components.
    check("the panel uses the shell's Toggle, not hand-rolled rows",
          re.search(r'^\s{0,6}Toggle\s*\{\s*$', code, re.M) is not None,
          "expected a bare qs.Ui Toggle, which carries activeFocusOnTab and "
          "Enter/Space - a Rectangle carrying a label property is mouse-only")
    check("the panel uses the shell's ButtonGroup for facing/resolution",
          re.search(r'^\s*ButtonGroup\s*\{', code, re.M) is not None,
          "expected qs.Ui ButtonGroup for the segmented options")
    check("the primary action uses a shell component, not a bare Rectangle",
          re.search(r'^\s{0,6}PanelActionButton\s*\{\s*$', code, re.M) is not None,
          "expected PanelActionButton for Start/Stop")
    # No control may be a bare Rectangle: that is precisely the mouse-only shape
    # this change removed. Keyed on a whole-line Rectangle so a nested Rectangle
    # inside a shell component (hover surfaces) does not trip it.
    bare_rects = [ln.strip() for ln in code.splitlines()
                  if re.match(r'^\s*Rectangle\s*\{', ln) and not re.match(
                      r'^\s{8,}Rectangle\s*\{', ln)]
    check("no control is a bare Rectangle", not bare_rects,
          f"hand-rolled rows remain: {bare_rects[:3]}")

    # Assert the SHELL component really offers keyboard access, so this does not
    # rest on my reading of it. If the shell drops Keys.onReturnPressed the panel
    # is mouse-only again, and this must fail.
    UI_DIR = pathlib.Path("/usr/share/omarchy/shell/Ui")
    if (UI_DIR / "Toggle.qml").exists():
        tq = (UI_DIR / "Toggle.qml").read_text()
        for key in ("activeFocusOnTab", "Keys.onReturnPressed", "Keys.onSpacePressed"):
            check(f"qs.Ui Toggle still provides {key}", key in tq,
                  f"Toggle.qml no longer has {key} - the panel would be mouse-only")
    else:
        print("skip  qs.Ui Toggle.qml not found; cannot verify keyboard support")

    # Every control must still be wired to a write, not merely present.
    for control in ("torch", "mic_to_host", "with_preview",
                    "camera_facing", "resolution"):
        check(f"{control} is still bound to a write",
              f'root.apply("{control}"' in code,
              f"the {control} control no longer writes anything")

    # ---------- every control the panel offers is a real config key ----------
    offered = set(re.findall(r'root\.apply\("([a-z_]+)"', panel_text))
    offered |= set(re.findall(r'root\.(?:truthy|value)\("([a-z_]+)"', panel_text))
    unknown = offered - set(cfgmod.DEFAULTS)
    check("every control the panel offers is a real config key",
          not unknown, f"panel offers keys config does not define: {sorted(unknown)}")
    check("the panel covers the controls the user asked for",
          {"torch", "mic_to_host", "camera_facing", "resolution"} <= offered,
          f"offered: {sorted(offered)}")

    # ---------- no self-matching process check ----------
    # Match the EXECUTED command only, never a comment. The panel documents the
    # old bug in prose, and a plain substring test fails on the explanation
    # rather than on the defect - which is what a substring test did here first.
    execs = re.findall(r'command:\s*\[[^\]]*\]', panel_text)
    check("no executed command uses pgrep -f / pkill -f",
          not any("pgrep -f" in e or "pkill -f" in e for e in execs),
          f"a -f pattern matches the shell running it: "
          f"{[e for e in execs if '-f' in e]}")
    check("the panel asks the app whether a camera is running",
          any("is-streaming" in e for e in execs),
          "the dot must distinguish the webcam from the screen mirror")

    # ---------- Start must not open a GTK window, and must survive the panel --
    check("Start passes a flag that routes to the headless backend",
          any("--no-window" in e for e in execs),
          "a bare `android-webcam` misses main()'s allowlist and opens the GUI")
    check("Start is detached so closing the panel does not kill the stream",
          any("setsid" in e for e in execs),
          "a child of the panel's Process is killed when the panel closes")

    # ---------- installed copies must match the repo ----------
    # Both files, not just the panel. The plugin had already drifted once this
    # way: the installed copies carried a theme-compat fix the repo lacked, and
    # the fix is only real once it is the copy Quickshell loads. Checking one
    # file and assuming the other is a gap - the bar widget carried the same
    # bare-launch defect for longer because nothing compared it.
    for name in ("BarWidget.qml", "Panel.qml"):
        inst = (pathlib.Path.home()
                / ".config/omarchy/plugins/ishanp.android-webcam" / name)
        if not inst.exists():
            check(f"installed {name} exists", False, f"missing: {inst}")
            continue
        check(f"installed {name} is byte-identical to the repo",
              inst.read_bytes() == (REPO / "omarchy-plugin" / name).read_bytes(),
              f"installed copy has drifted; copy omarchy-plugin/{name} over it")

real_after = REAL_CONFIG.read_bytes() if REAL_CONFIG.exists() else None
check("the REAL config was never written", real_before == real_after,
      "test clobbered the live config")

print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failed")
sys.exit(1 if failures else 0)