"""GTK4+Adw GUI — full app with 720/1080, front/back, audio, flash, preview."""
import gi
gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")
from gi.repository import Gtk, Adw, GLib, Gio
import subprocess, threading, shlex, os, time
from pathlib import Path
from .config import load, save, SIZES, FPS_CHOICES
from .backend import build_argv, adb_ping, connect_adb

APP_ID = "io.github.ishanp.android-webcam"

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.cfg = load()
        self.proc: subprocess.Popen | None = None
        self.log_lines: list[str] = []
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        self.win = Adw.ApplicationWindow(application=app, title="Android Webcam", default_width=560, default_height=720)
        # Make the window content scrollable so all options are reachable on a
        # cramped screen.  Adwaita PreferencesGroup works inside Gtk.ScrolledWindow.
        self.toast_overlay = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        tv.add_top_bar(header)
        tv.set_content(self.toast_overlay)
        self.win.set_content(tv)
        self.header = header

        # Outer: scroller + scrollable content so the page works on small windows
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      vexpand=True, hexpand=True)
        scroller.set_propagate_natural_height(True)
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(12); content_box.set_margin_bottom(12)
        content_box.set_margin_start(12); content_box.set_margin_end(12)
        scroller.set_child(content_box)
        self.toast_overlay.set_child(scroller)
        box = content_box

        # Status row
        self.status_row = Adw.ActionRow(title="Status", subtitle="Idle — /dev/video0 free")
        self.status_icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        self.status_row.add_prefix(self.status_icon)
        self.ping_btn = Gtk.Button(label="Ping"); self.ping_btn.connect("clicked", self.on_ping)
        self.connect_btn = Gtk.Button(label="Connect"); self.connect_btn.connect("clicked", self.on_connect)
        h = Gtk.Box(spacing=6); h.append(self.ping_btn); h.append(self.connect_btn)
        self.status_row.add_suffix(h)
        grp_status = Adw.PreferencesGroup(title="Device", description="Redmi Note 8 Pro @ 192.168.1.12:5555 via adb TCP 5555")
        grp_status.add(self.status_row)
        box.append(grp_status)

        # Target row (editable IP)
        self.ip_row = Adw.EntryRow(title="Android IP:Port")
        self.ip_row.set_text(f"{self.cfg['android_ip']}:{self.cfg['android_port']}")
        self.ip_row.connect("changed", self.on_ip_changed)
        grp_status.add(self.ip_row)

        # Camera group
        grp_cam = Adw.PreferencesGroup(title="Camera", description="scrcpy 4.1 — Android 12+ — back 4640x3472 / front 2592x1940")
        # facing
        self.facing_row = Adw.ComboRow(title="Camera", model=Gtk.StringList.new(["Back (id 0)","Front (id 1)"]),
                                       selected=0 if self.cfg["camera_facing"]=="back" else 1)
        self.facing_row.connect("notify::selected", self.on_facing)
        grp_cam.add(self.facing_row)
        # resolution
        self.res_row = Adw.ComboRow(title="Resolution", model=Gtk.StringList.new(["720p — 1280×720","1080p — 1920×1080","Custom"]),
                                    selected={"720p":0,"1080p":1}.get(self.cfg["resolution"],0) if not self.cfg.get("custom_size") else 2)
        self.res_row.connect("notify::selected", self.on_res)
        grp_cam.add(self.res_row)
        self.custom_size_row = Adw.EntryRow(title="Custom size (WxH)")
        self.custom_size_row.set_text(self.cfg.get("custom_size") or "")
        self.custom_size_row.set_visible(self.cfg.get("custom_size") is not None)
        self.custom_size_row.connect("changed", self.on_custom_size)
        grp_cam.add(self.custom_size_row)
        # fps
        fps_strings = [str(x) for x in FPS_CHOICES]
        fps_idx = fps_strings.index(str(self.cfg.get("fps",30))) if str(self.cfg.get("fps",30)) in fps_strings else 1
        self.fps_row = Adw.ComboRow(title="FPS", model=Gtk.StringList.new(fps_strings), selected=fps_idx)
        self.fps_row.connect("notify::selected", self.on_fps)
        grp_cam.add(self.fps_row)
        # preview toggle
        self.preview_row = Adw.SwitchRow(title="Show preview window", subtitle="Also feeds /dev/video0")
        self.preview_row.set_active(bool(self.cfg.get("with_preview")))
        self.preview_row.connect("notify::active", self.on_preview)
        grp_cam.add(self.preview_row)
        box.append(grp_cam)

        # Extras group: torch, audio
        grp_extra = Adw.PreferencesGroup(title="Extras")
        self.torch_row = Adw.SwitchRow(title="Flash / Torch", subtitle="Back camera only — MOD+Shift+t toggles in preview")
        self.torch_row.set_active(bool(self.cfg.get("torch")))
        self.torch_row.connect("notify::active", self.on_torch)
        grp_extra.add(self.torch_row)

        # Audio: two independent switches
        self.mic_row = Adw.SwitchRow(title="Microphone (mic → host)", subtitle="PipeWire source 'scrcpy' carries phone mic. Muted by default to avoid echo.")
        self.mic_row.set_active(bool(self.cfg.get("mic_to_host", True)))
        self.mic_row.connect("notify::active", self.on_mic)
        grp_extra.add(self.mic_row)
        self.mic_mute_row = Adw.SwitchRow(title="Unmute mic on host", subtitle="When mic is on, leave it unmuted. Default OFF (echo protection).")
        self.mic_mute_row.set_active(not bool(self.cfg.get("mic_mute", True)))
        self.mic_mute_row.connect("notify::active", self.on_mic_mute)
        grp_extra.add(self.mic_mute_row)
        self.speaker_row = Adw.SwitchRow(title="Host mic → phone speakers", subtitle="Forward host microphone to phone audio output. Use phone as a speaker for host audio.")
        self.speaker_row.set_active(bool(self.cfg.get("mic_to_phone", False)))
        self.speaker_row.connect("notify::active", self.on_speaker)
        grp_extra.add(self.speaker_row)
        self.audio_info = Gtk.Label(label="Combinations:  mic only → safe (muted);  mic+unmute → enable per-app in pavucontrol;  speaker only → host mic piped to phone (--audio-source=playback --audio-dup);  mic+speaker → full duplex (feedback risk).")
        self.audio_info.set_wrap(True); self.audio_info.set_xalign(0)
        grp_extra.add(self.audio_info)
        # v4l2 sink + buffer
        self.v4l2_row = Adw.EntryRow(title="v4l2 sink")
        self.v4l2_row.set_text(self.cfg.get("v4l2_sink","/dev/video0"))
        self.v4l2_row.connect("changed", self.on_v4l2)
        grp_extra.add(self.v4l2_row)
        # brightness low
        self.bright_row = Adw.SwitchRow(title="Keep brightness lowest (1/0.001)", subtitle="Saves battery, keeps screen ON for webcam")
        self.bright_row.set_active(bool(self.cfg.get("stay_brightness_low",True)))
        self.bright_row.connect("notify::active", self.on_bright)
        grp_extra.add(self.bright_row)
        # auto-rotate
        self.rotate_row = Adw.SwitchRow(title="Auto-rotate to match window", subtitle="Re-launches scrcpy with --orientation=0|90|180|270 when the active window aspect flips")
        self.rotate_row.set_active(bool(self.cfg.get("auto_rotate",True)))
        self.rotate_row.connect("notify::active", self.on_rotate)
        grp_extra.add(self.rotate_row)
        box.append(grp_extra)

        # Command preview
        self.cmd_label = Gtk.Label(label=""); self.cmd_label.set_selectable(True); self.cmd_label.set_wrap(True)
        self.cmd_label.add_css_class("monospace")
        grp_cmd = Adw.PreferencesGroup(title="Command")
        grp_cmd.add(Adw.ActionRow(title="Preview", subtitle="Copied from backend.build_argv"))
        box.append(grp_cmd)
        box.append(self.cmd_label)
        self.refresh_cmd()

        # Start/Stop + log
        self.start_btn = Gtk.Button(label="Start webcam → /dev/video0")
        self.start_btn.add_css_class("suggested-action"); self.start_btn.set_hexpand(True)
        self.start_btn.connect("clicked", self.on_start_stop)
        self.stop_btn = Gtk.Button(label="Stop"); self.stop_btn.set_visible(False)
        self.stop_btn.connect("clicked", self.on_start_stop)
        btn_box = Gtk.Box(spacing=8); btn_box.append(self.start_btn); btn_box.append(self.stop_btn)
        box.append(btn_box)

        # Log expander
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_size_request(-1, 140)
        scrolled = Gtk.ScrolledWindow(child=self.log_view, vexpand=False); scrolled.set_min_content_height(140)
        exp = Adw.ExpanderRow(title="Log", subtitle="scrcpy stdout")
        exp.add_row(Adw.ActionRow(title="Tip: test in https://webcamtests.com while running")); # placeholder row
        # Use manual scrolled below expander for real log
        box.append(exp)
        box.append(scrolled)
        self.log_buf = self.log_view.get_buffer()

        # About
        about = Gtk.Button.new_from_icon_name("help-about-symbolic")
        about.connect("clicked", lambda *_: self.show_about())
        self.header.pack_end(about)
        self.win.present()
        # initial ping + auto-start: run on the GLib main loop, no user click
        GLib.idle_add(self._on_startup)

    # handlers
    def toast(self, msg: str):
        self.toast_overlay.add_toast(Adw.Toast.new(msg))

    def _on_startup(self):
        """Initial boot: ping phone → if unreachable, run --discover → re-ping
        → if reachable, immediately auto-start streaming.  Runs once on
        startup, no user click required.
        """
        def bg():
            from .backend import adb_ping, discover_phone
            ok, msg = adb_ping(self.cfg)
            if not ok:
                GLib.idle_add(self.status_row.set_subtitle, f"✗ {msg[:60]} — searching…")
                ok2, msg2 = discover_phone(self.cfg)
                if ok2:
                    self.refresh_cmd()
                    GLib.idle_add(self.status_row.set_subtitle, f"✓ {msg2[:80]}")
                    ok, msg = adb_ping(self.cfg)
            if ok:
                GLib.idle_add(self.status_row.set_subtitle, f"✓ {msg[:60]} — auto-start")
                GLib.idle_add(self.toast, "Auto-starting webcam…")
                # small delay so the user sees the status transition
                def go():
                    time.sleep(0.5)
                    GLib.idle_add(self._start_proc)
                threading.Thread(target=go, daemon=True).start()
            else:
                GLib.idle_add(self.status_row.set_subtitle, f"✗ {msg[:80]}")
                GLib.idle_add(self.toast, "Phone unreachable. Check WiFi / USB debugging.")
        threading.Thread(target=bg, daemon=True).start()
        return False

    def refresh_cmd(self):
        argv = build_argv(self.cfg)
        self.cmd_label.set_label(" ".join(shlex.quote(a) for a in argv))

    def on_ip_changed(self, row):
        txt = row.get_text().strip()
        if ":" in txt:
            ip, port = txt.rsplit(":",1)
            try: self.cfg["android_port"]=int(port); self.cfg["android_ip"]=ip
            except: pass
        else:
            self.cfg["android_ip"]=txt
        save(self.cfg); self.refresh_cmd()

    def on_facing(self, row, _):
        self.cfg["camera_facing"]="back" if row.get_selected()==0 else "front"
        self.cfg["camera_id"]=None; save(self.cfg); self.refresh_cmd()

    def on_res(self, row,_):
        sel=row.get_selected()
        if sel==0: self.cfg["resolution"]="720p"; self.cfg["custom_size"]=None
        elif sel==1: self.cfg["resolution"]="1080p"; self.cfg["custom_size"]=None
        else: self.cfg["custom_size"]= self.custom_size_row.get_text() or "1280x720"
        self.custom_size_row.set_visible(sel==2)
        save(self.cfg); self.refresh_cmd()

    def on_custom_size(self,row):
        self.cfg["custom_size"]=row.get_text().strip() or None; save(self.cfg); self.refresh_cmd()

    def on_fps(self,row,_):
        self.cfg["fps"]=FPS_CHOICES[row.get_selected()]; save(self.cfg); self.refresh_cmd()

    def on_preview(self,row,_): self.cfg["with_preview"]=row.get_active(); save(self.cfg); self.refresh_cmd()

    def on_torch(self,row,_): self.cfg["torch"]=row.get_active(); save(self.cfg); self.refresh_cmd()

    def on_mic(self,row,_):
        self.cfg["mic_to_host"]=row.get_active(); save(self.cfg); self.refresh_cmd()
        if row.get_active(): self.toast("Phone mic → host (muted). Unmute in pavucontrol if you want it.")

    def on_mic_mute(self,row,_):
        self.cfg["mic_mute"]=not row.get_active(); save(self.cfg); self.refresh_cmd()
        if not row.get_active(): self.toast("Echo warning: speakers → phone mic can feedback. Use headphones.")

    def on_speaker(self,row,_):
        self.cfg["mic_to_phone"]=row.get_active(); save(self.cfg); self.refresh_cmd()
        if row.get_active(): self.toast("Host mic → phone speakers. Useful for using phone as a speaker.")

    def on_rotate(self,row,_): self.cfg["auto_rotate"]=row.get_active(); save(self.cfg); self.refresh_cmd()

    def on_v4l2(self,row): self.cfg["v4l2_sink"]=row.get_text().strip() or "/dev/video0"; save(self.cfg); self.refresh_cmd()

    def on_bright(self,row,_): self.cfg["stay_brightness_low"]=row.get_active(); save(self.cfg)

    def on_ping(self,_):
        def bg():
            from .backend import adb_ping, discover_phone
            ok,msg = adb_ping(self.cfg)
            if not ok and "no route" in (msg or "").lower() or "not found" in (msg or "").lower() or "refused" in (msg or "").lower():
                GLib.idle_add(lambda: self.status_row.set_subtitle(f"✗ {msg[:60]} — searching…"))
                ok2,msg2 = discover_phone(self.cfg)
                GLib.idle_add(lambda: self.status_row.set_subtitle(
                    f"{'✓ found ' if ok2 else '✗ not found'} {msg2[:60]}"))
                if ok2: self.refresh_cmd()
            else:
                GLib.idle_add(lambda: self.status_row.set_subtitle(f"{'✓ reachable' if ok else '✗ unreachable'} — {msg[:80]}"))
        threading.Thread(target=bg,daemon=True).start()

    def on_connect(self,_):
        def bg():
            from .backend import adb_ping, discover_phone
            ok,msg=connect_adb(self.cfg)
            if not ok:
                ok2,msg2 = discover_phone(self.cfg)
                if ok2: msg = f"discovered: {msg2}"
            GLib.idle_add(lambda: self.toast(f"{'Connected' if ok else 'Connect failed'}: {msg[:80]}"))
            GLib.idle_add(lambda: self.on_ping(None))
        threading.Thread(target=bg,daemon=True).start()

    def on_start_stop(self,_):
        if self.proc and self.proc.poll() is None:
            self._stop_proc()
            return
        self._start_proc()

    def _start_proc(self):
        from . import backend as _b
        argv = _b.build_argv(self.cfg)
        try:
            # ensure connection before launching
            try: subprocess.run(["adb","connect",f"{self.cfg['android_ip']}:{self.cfg['android_port']}"], capture_output=True, text=True, timeout=5)
            except Exception: pass
            self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            self.toast("scrcpy not found — pacman -S scrcpy"); return
        self.start_btn.set_label("Running…"); self.start_btn.set_visible(False)
        self.stop_btn.set_visible(True)
        self.status_row.set_subtitle(f"● Streaming {self.cfg['camera_facing']} {self.cfg['resolution']} → {self.cfg['v4l2_sink']}")
        self.status_icon.set_from_icon_name("media-record-symbolic")
        self.log_buf.set_text("")
        def reader():
            for line in self.proc.stdout:  # type: ignore
                GLib.idle_add(lambda l=line: self.log_buf.insert(self.log_buf.get_end_iter(), l))
            GLib.idle_add(lambda: self.on_proc_exit())
        threading.Thread(target=reader,daemon=True).start()
        # mic mute after 2s (PipeWire source needs to register)
        if self.cfg.get("mic_to_host", True) and self.cfg.get("mic_mute", True):
            def _mute():
                time.sleep(2)
                ok = _b.apply_mic_mute(self.cfg)
                if ok: GLib.idle_add(lambda: self.log_buf.insert(self.log_buf.get_end_iter(), "[mic-mute] scrcpy source muted\n"))
            threading.Thread(target=_mute, daemon=True).start()
        # auto-rotate watcher
        if self.cfg.get("auto_rotate", True):
            self._start_rotator()
        self.toast(f"Started {self.cfg['camera_facing']} {self.cfg.get('custom_size') or SIZES[self.cfg['resolution']]} → {self.cfg['v4l2_sink']}")

    def _start_rotator(self):
        from . import rotation
        def restart(new_orientation):
            GLib.idle_add(self.log_buf.insert, self.log_buf.get_end_iter(), f"[rotate] → {new_orientation}°\n")
            self._stop_proc()
            self.cfg["auto_rotate_orientation"] = int(new_orientation)
            save(self.cfg)
            time.sleep(0.5)
            self._start_proc()
        self._rotator = rotation.start_rotator(restart, self.cfg, window_class="scrcpy", interval=2.0)

    def _stop_proc(self):
        if not (self.proc and self.proc.poll() is None): return
        try: self.proc.terminate()
        except Exception: pass
        try: self.proc.wait(timeout=3)
        except Exception:
            try: self.proc.kill()
            except Exception: pass
        self.proc=None
        self.start_btn.set_label("Start webcam → /dev/video0")
        self.start_btn.set_visible(True); self.stop_btn.set_visible(False)
        self.status_row.set_subtitle("Stopped — /dev/video0 free")
        self.status_icon.set_from_icon_name("camera-web-symbolic")

    def on_proc_exit(self):
        if self.proc and self.proc.poll() is not None:
            code=self.proc.poll(); self.proc=None
            self.start_btn.set_label("Start webcam → /dev/video0"); self.start_btn.set_visible(True); self.stop_btn.set_visible(False)
            self.status_row.set_subtitle(f"Exited {code} — /dev/video0 free"); self.status_icon.set_from_icon_name("camera-web-symbolic")

    def show_about(self):
        dlg = Adw.AboutWindow(transient_for=self.win, application_name="Android Webcam",
                              version="1.0.0", developer_name="ishanp",
                              website="https://github.com/ishanp/android-webcam",
                              comments="Native scrcpy camera → v4l2loopback. Replaces Iriun.",
                              license_type=Gtk.License.MIT_X11)
        dlg.present()

def main():
    import sys
    # CLI passthrough: android-webcam --front --720 etc without GUI
    if any(a in sys.argv for a in ("--help","-h","--dry-run","--front","--back","--720","--1080","--torch","--with-audio","--no-window","--with-preview")) and "--gui" not in sys.argv:
        # if any CLI flag, run headless backend.cli instead of GUI — unless --gui forced
        if "--help" in sys.argv or "-h" in sys.argv:
            from .backend import cli as backend_cli
            backend_cli(); return 0
        if "--dry-run" in sys.argv or any(a.startswith("--") for a in sys.argv[1:]):
            # let backend handle headless; detect GUI request via --gui
            from .backend import cli as backend_cli
            backend_cli(); return 0
    # strip --gui before GTK parses argv (it's our custom flag)
    argv = [a for a in sys.argv if a != "--gui"]
    app = App()
    return app.run(argv)

if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
