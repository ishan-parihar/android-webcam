"""GTK4+Adw GUI — full app with 720/1080, front/back, audio, flash, preview."""
import gi
gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")
from gi.repository import Gtk, Adw, GLib, Gio
import subprocess, threading, shlex, os
from pathlib import Path
from .config import load, save, SIZES, FPS_CHOICES
from .backend import build_argv, adb_ping, connect_adb, list_cameras

APP_ID = "io.github.ishanp.android-webcam"

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.cfg = load()
        self.proc: subprocess.Popen | None = None
        self.log_lines: list[str] = []
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        self.win = Adw.ApplicationWindow(application=app, title="Android Webcam", default_width=520, default_height=680)
        # Toast overlay + ToolbarView for Adw header
        self.toast_overlay = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        tv.add_top_bar(header)
        tv.set_content(self.toast_overlay)
        self.win.set_content(tv)
        self.header = header

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12); box.set_margin_bottom(12); box.set_margin_start(12); box.set_margin_end(12)
        self.toast_overlay.set_child(box)

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
        self.audio_row = Adw.SwitchRow(title="Microphone (with audio)", subtitle="⚠️ Speakers → phone mic causes echo. Off by default.")
        self.audio_row.set_active(bool(self.cfg.get("with_audio")))
        self.audio_row.connect("notify::active", self.on_audio)
        grp_extra.add(self.audio_row)
        self.audio_info = Gtk.Label(label="When ON: --audio-source=mic → PulseAudio 'scrcpy' source. Keep speakers low or use headphones.")
        self.audio_info.set_wrap(True); self.audio_info.set_xalign(0)
        self.audio_info.set_visible(bool(self.cfg.get("with_audio")))
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
        # initial ping
        GLib.idle_add(self.on_ping, None)

    # handlers
    def toast(self, msg: str):
        self.toast_overlay.add_toast(Adw.Toast.new(msg))

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

    def on_audio(self,row,_):
        self.cfg["with_audio"]=row.get_active(); self.audio_info.set_visible(row.get_active())
        save(self.cfg); self.refresh_cmd()
        if row.get_active(): self.toast("Mic ON — lower speakers / use headphones to avoid echo")

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
            # stop
            try: self.proc.terminate()
            except: pass
            try: self.proc.wait(timeout=3)
            except: self.proc.kill()
            self.proc=None
            self.start_btn.set_label("Start webcam → /dev/video0")
            self.start_btn.set_visible(True); self.stop_btn.set_visible(False)
            self.status_row.set_subtitle("Stopped — /dev/video0 free")
            self.status_icon.set_from_icon_name("camera-web-symbolic")
            return
        # start
        argv = build_argv(self.cfg)
        try:
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
        self.toast(f"Started {self.cfg['camera_facing']} {self.cfg.get('custom_size') or SIZES[self.cfg['resolution']]} → {self.cfg['v4l2_sink']}")

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
