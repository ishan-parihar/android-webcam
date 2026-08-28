"""GTK4+Adw GUI — full app with 720/1080, front/back, audio, flash, preview."""
import gi
gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")
from gi.repository import Gtk, Adw, GLib, Gio
import subprocess, threading, shlex, os, time, logging
from pathlib import Path
from .config import load, save, SIZES, FPS_CHOICES
from .backend import build_argv, adb_ping, connect_adb, discover_phone, quick_reachable, _wait_device_ready, _adb_shell_echo

# log to /tmp for diagnosis when the GUI is launched via desktop file (no
# visible terminal). The user can `tail -f /tmp/android-webcam-gui.log`.
LOG_FILE = "/tmp/android-webcam-gui.log"
try:
    logging.basicConfig(
        filename=LOG_FILE, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        filemode="w",  # overwrite on each launch
    )
    # also mirror to console for terminal launches
    _sh = logging.StreamHandler()
    _sh.setLevel(logging.INFO)
    logging.getLogger().addHandler(_sh)
except Exception:
    pass

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
        self.mic_row = Adw.SwitchRow(title="Phone mic → system (default)", subtitle="Forwards the phone's microphone to a PipeWire source called 'scrcpy' for apps to use.  ON by default — phone is your real system mic, no echo because scrcpy does NOT play the mic through your speakers.")
        self.mic_row.set_active(bool(self.cfg.get("mic_to_host", True)))
        self.mic_row.connect("notify::active", self.on_mic)
        grp_extra.add(self.mic_row)
        self.mic_default_row = Adw.SwitchRow(title="Set 'scrcpy' as system default mic", subtitle="ON by default — every app (browser, zoom, discord) uses the phone mic without any manual setup.  Restored on stop.")
        self.mic_default_row.set_active(bool(self.cfg.get("mic_default", True)))
        self.mic_default_row.connect("notify::active", self.on_mic_default)
        grp_extra.add(self.mic_default_row)
        self.speaker_row = Adw.SwitchRow(title="Speakerphone (host audio → phone)", subtitle="OFF by default.  Forwards your computer's audio output to the phone's speaker — use your phone as a remote speakerphone.  Disable 'Phone mic → system' to avoid feedback.")
        self.speaker_row.set_active(bool(self.cfg.get("mic_to_phone", False)))
        self.speaker_row.connect("notify::active", self.on_speaker)
        grp_extra.add(self.speaker_row)
        self.audio_info = Gtk.Label(label="Default: phone mic → system mic, no echo.  Speakerphone mode routes host audio → phone speakers (off by default).")
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
        startup, no user click required.  Logs to /tmp/android-webcam-gui.log.

        Runs on a background thread (this is a 5-30s operation) and posts
        status updates back to the main loop via GLib.idle_add so the window
        stays responsive and the user sees 'Searching...' / 'Connecting...'
        progress rather than a frozen UI.
        """
        logging.info("=== _on_startup (thread) === cfg=%s:%s facing=%s res=%s",
                      self.cfg.get("android_ip"), self.cfg.get("android_port"),
                      self.cfg.get("camera_facing"), self.cfg.get("resolution"))
        GLib.idle_add(self._set_status, "Scanning for phone…", "search")
        threading.Thread(target=self._startup_worker, daemon=True).start()
        return False

    def _set_status(self, subtitle: str, icon: str = "search"):
        """Thread-safe status update.  icon ∈ {search,ok,bad,camera}."""
        self.status_row.set_subtitle(subtitle)
        icon_names = {
            "search": "system-search-symbolic",
            "ok": "object-select-symbolic",
            "bad": "dialog-error-symbolic",
            "camera": "camera-web-symbolic",
        }
        self.status_icon.set_from_icon_name(icon_names.get(icon, "camera-web-symbolic"))
        logging.info("status: [%s] %s", icon, subtitle)

    def _startup_worker(self):
        """Background thread: probe → discover → auto-start.  Posts status
        updates back to the main loop.  Waits for the adb per-device auth
        handshake to finish before issuing `adb shell` (fixes the GUI-vs-CLI
        parity bug where the first shell call after connect always times out).
        """
        import time as _t
        tgt_ip = self.cfg.get("android_ip")
        tgt_port = int(self.cfg.get("android_port", 5555)) if self.cfg.get("android_port") else 5555
        shell_ok = False

        def try_target(ip, port, why):
            """Try to verify one IP:port is actually responsive.
            Returns True on success and updates cfg."""
            logging.info("trying %s:%d (%s)", ip, port, why)
            GLib.idle_add(self._set_status, f"Connecting to {ip}:{port}…", "search")
            try:
                subprocess.run(["adb","disconnect",f"{ip}:{port}"],
                               capture_output=True, timeout=2)
            except Exception: pass
            _t.sleep(0.3)
            try:
                cr = subprocess.run(["adb","connect",f"{ip}:{port}"],
                                    capture_output=True, text=True, timeout=8)
                logging.info("adb connect -> %r", (cr.stdout+cr.stderr).strip())
            except Exception as e:
                logging.warning("adb connect error: %s", e)
            # wait for auth handshake to complete — the difference between
            # CLI (which has human delay) and GUI (which fires shell
            # immediately and times out).
            ready = _wait_device_ready(ip, port, timeout=5.0)
            if not ready:
                logging.warning("device not ready after connect: %s:%d", ip, port)
                return False
            shell_ok = _adb_shell_echo(ip, port, timeout=4.0)
            if not shell_ok:
                logging.warning("shell echo failed: %s:%d", ip, port)
                return False
            self.cfg["android_ip"] = ip
            self.cfg["android_port"] = port
            from .config import save
            save(self.cfg)
            return True

        # 1) try the configured target
        if tgt_ip:
            shell_ok = try_target(tgt_ip, tgt_port, "configured")

        # 2) discover on failure
        if not shell_ok:
            logging.info("discover_phone (thread)…")
            GLib.idle_add(self._set_status, "Searching LAN for phone…", "search")
            ok, msg = discover_phone(self.cfg)
            logging.info("discover -> %s %s", ok, msg)
            if ok:
                tgt_ip = self.cfg["android_ip"]
                tgt_port = int(self.cfg["android_port"])
                GLib.idle_add(self.refresh_cmd)
                shell_ok = try_target(tgt_ip, tgt_port, "discovered")

        # 3) decide
        if shell_ok:
            logging.info("auto-start -> /dev/video0")
            GLib.idle_add(self._set_status,
                          f"✓ {tgt_ip}:{tgt_port} — auto-starting webcam…", "ok")
            GLib.idle_add(self.toast, "Auto-starting webcam…")
            # give the UI a beat to repaint
            _t.sleep(0.4)
            GLib.idle_add(self._start_proc)
        else:
            logging.warning("auto-start aborted: phone unreachable")
            GLib.idle_add(self._set_status,
                          f"✗ unreachable — press Connect or check WiFi", "bad")
            GLib.idle_add(self.toast, "Phone unreachable. Press Connect to retry.")

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
        if row.get_active():
            self.toast("Phone mic → system.  No echo — scrcpy will NOT play the mic through your speakers.")
        else:
            self.toast("Phone mic forwarding disabled.")

    def on_mic_default(self,row,_):
        # row active = promote scrcpy to system default source at start
        self.cfg["mic_default"]=row.get_active(); save(self.cfg); self.refresh_cmd()
        if row.get_active():
            self.toast("Phone mic will become system default mic on start.")
        else:
            self.toast("Phone mic available as a source, but NOT promoted to default.")

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
        # mic: promote scrcpy to system default source (so all apps pick it up)
        # and apply mute flag (echo protection).  Restore previous default on stop.
        if self.cfg.get("mic_to_host", True):
            def _mic_default():
                time.sleep(2)  # wait for scrcpy to register PipeWire source
                prev = _b.set_phone_as_system_mic(self.cfg)
                self.cfg["_mic_prev"] = prev
                if prev.get("set"):
                    state = "muted" if self.cfg.get("mic_mute") else "live"
                    GLib.idle_add(lambda: self.log_buf.insert(
                        self.log_buf.get_end_iter(),
                        f"[mic] scrcpy is system default mic ({state})\n"))
            threading.Thread(target=_mic_default, daemon=True).start()
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
        # restore previous system default mic before tearing down scrcpy
        from . import backend as _b
        prev = self.cfg.get("_mic_prev")
        if prev:
            _b.restore_system_audio(prev)
            self.cfg["_mic_prev"] = None
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
            # restore previous system default mic when scrcpy exits
            from . import backend as _b
            prev = self.cfg.get("_mic_prev")
            if prev:
                _b.restore_system_audio(prev)
                self.cfg["_mic_prev"] = None
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
