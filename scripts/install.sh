#!/bin/bash
# android-webcam — robust fresh-install for Arch / Omarchy / CachyOS
# Idempotent: safe to re-run. Handles v4l2loopback DKMS, model rebuild,
# perms, pip, desktop, Hyprland, Omarchy, and null-sink audio.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
log()  { printf "\033[36m→ %s\033[0m\n" "$*"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$*"; }
warn() { printf "\033[33m⚠ %s\033[0m\n" "$*"; }
fail() { printf "\033[31m✗ %s\033[0m\n" "$*"; }

echo "== android-webcam install =="
echo "   Root: $ROOT"
echo "   User: $USER  Kernel: $(uname -r)  Arch: $(uname -m)"

# ── 0. Preflight: must be Arch with pacman ──────────────────────────
if ! command -v pacman >/dev/null 2>&1; then
  fail "pacman not found — this installer targets Arch / Omarchy / CachyOS"
  echo "  On other distros install manually: scrcpy, android-tools, v4l2loopback, ffmpeg, gtk4, libadwaita, then pip install -e ."
  exit 1
fi

# ── 1. System deps ──────────────────────────────────────────────────
# linux-headers is a meta that pulls headers for the running kernel flavour
# (linux-headers, linux-cachyos-headers, linux-lts-headers, etc.).
# We install the generic + the specific one for the running kernel.
log "Installing system deps (scrcpy, android-tools, v4l2loopback-dkms, ffmpeg, headers, gtk)…"
# Detect required header package for current kernel
KREL="$(uname -r)"
HDR_PKGS=(linux-headers)
case "$KREL" in
  *cachyos*) HDR_PKGS+=(linux-cachyos-headers) ;;
  *lts*)     HDR_PKGS+=(linux-lts-headers) ;;
  *zen*)     HDR_PKGS+=(linux-zen-headers) ;;
esac
# Filter to packages that actually exist in repos
HDR_INSTALL=()
for p in "${HDR_PKGS[@]}"; do
  if pacman -Si "$p" >/dev/null 2>&1; then HDR_INSTALL+=("$p"); fi
done

SYS_PKGS=(
  scrcpy android-tools
  v4l2loopback-dkms
  ffmpeg
  gtk4 libadwaita
  pipewire pipewire-pulse wireplumber
  "${HDR_INSTALL[@]}"
)
# shellcheck disable=SC2086
if ! sudo pacman -S --needed --noconfirm "${SYS_PKGS[@]}" 2>&1 | tail -n 20; then
  warn "pacman install had warnings — continuing, will verify later"
fi
# Critical: keep v4l2loopback even if nothing else depends on it.
# Without this, `pacman -Rns iriunwebcam-bin` removes it (was --asdeps).
if pacman -Q v4l2loopback-dkms >/dev/null 2>&1; then
  sudo pacman -D --asexplicit v4l2loopback-dkms >/dev/null 2>&1 || true
  ok "v4l2loopback-dkms marked explicit (won’t autoremove)"
else
  fail "v4l2loopback-dkms still not installed — check pacman output above"
fi

# ── 2. DKMS wait — v4l2loopback must be built for current kernel ────
log "Waiting for DKMS (v4l2loopback)…"
for _ in {1..12}; do
  if dkms status 2>&1 | grep -q "v4l2loopback.*$(uname -r).*installed"; then
    ok "DKMS built for $(uname -r)"
    break
  fi
  sleep 2
done
if ! dkms status 2>&1 | grep -q "v4l2loopback.*$(uname -r).*installed"; then
  warn "DKMS not yet built — forcing rebuild"
  sudo dkms install --no-depmod v4l2loopback/0.15.4 -k "$(uname -r)" 2>&1 | tail -n 10 || true
  sudo depmod -a 2>&1 | head -n 5 || true
fi

# ── 3. v4l2loopback config + load ────────────────────────────────────
log "Configuring v4l2loopback…"
sudo mkdir -p /etc/modules-load.d /etc/modprobe.d
echo "v4l2loopback" | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null
echo 'options v4l2loopback card_label="Android Webcam" exclusive_caps=1 video_nr=0' | sudo tee /etc/modprobe.d/v4l2loopback.conf >/dev/null
# Unload wrong flavour first (old Iriun label)
if lsmod | grep -q v4l2loopback; then
  if v4l2-ctl --list-devices 2>&1 | grep -q "Iriun Webcam"; then
    log "Unloading stale Iriun v4l2loopback…"
    sudo modprobe -r v4l2loopback 2>&1 | head -n 5 || true
    sleep 1
  fi
fi
if ! lsmod | grep -q v4l2loopback; then
  log "Loading v4l2loopback…"
  sudo modprobe v4l2loopback card_label="Android Webcam" exclusive_caps=1 video_nr=0 2>&1 | head -n 5 || true
  sleep 0.8
fi
# Udev can be slow — trigger and wait for /dev/video0
sudo udevadm trigger --action=add --subsystem-match=video4linux 2>&1 || true
for _ in {1..10}; do
  [[ -e /dev/video0 ]] && break
  sleep 0.5
done
if [[ -e /dev/video0 ]]; then
  # Fix perms — should be crw-rw----+ root:video via udev, but be safe
  sudo chgrp video /dev/video0 2>&1 || true
  sudo chmod 660 /dev/video0 2>&1 || true
  # Ensure running user is in video+adbusers (needs logout to take effect)
  for g in video adbusers; do
    if ! id -nG "$USER" | grep -qw "$g"; then
      log "Adding $USER to group $g (log out/in after install)"
      sudo usermod -aG "$g" "$USER" 2>&1 | head -n 3 || true
    fi
  done
  ok "/dev/video0 ready: $(ls -la /dev/video0 | awk '{print $1, $3, $4}')  $(v4l2-ctl -d /dev/video0 --all 2>&1 | grep 'Card type' | head -n1)"
else
  fail "/dev/video0 missing after modprobe — check dmesg / kernel headers"
fi

# ── 4. Null sink for mic without echo ────────────────────────────────
# Phone mic → scrcpy (SDL/Pulse) → null sink scrcpy_mic → monitor scrcpy_mic.monitor
# is the system mic. Without it, mic plays through HDMI (echo).
log "Ensuring PipeWire null sink for mic (scrcpy_mic)…"
if pactl list short sinks 2>&1 | grep -q "scrcpy_mic"; then
  ok "scrcpy_mic sink exists"
else
  if pactl load-module module-null-sink sink_name=scrcpy_mic sink_properties=device.description="PhoneMic_scrcpy" 2>&1 | grep -q .; then
    ok "Created null sink scrcpy_mic"
  else
    # PipeWire may not have Pulse loaded yet — try via pw
    warn "pactl null sink failed — PipeWire will create it on first use"
  fi
  sleep 0.5
fi
# Default source will be set to scrcpy_mic.monitor at webcam start by backend;
# keep current default as fallback.
if pactl get-default-source 2>&1 | head -n1 | grep -q .; then
  ok "PipeWire ready: default source $(pactl get-default-source 2>&1 | head -n1)"
fi

# ── 5. Python package ────────────────────────────────────────────────
log "Installing Python package…"
# PEP 668 externally-managed — need --break-system-packages on Arch
PIP_FLAGS=(--break-system-packages)
if [[ -f "$ROOT/pyproject.toml" ]]; then
  # Prefer pip install -e . (editable) for dev; fallback to --user
  if pip install "${PIP_FLAGS[@]}" -e "$ROOT" 2>&1 | tail -n 8; then
    ok "pip install -e . succeeded"
  else
    warn "pip -e failed — trying pip install --user"
    pip install "${PIP_FLAGS[@]}" --user -e "$ROOT" 2>&1 | tail -n 8 || true
  fi
fi

# wrappers — pip provides android-webcam & android-webcam-cli
mkdir -p ~/.local/bin
cat > ~/.local/bin/android-webcam-gui <<'EOS'
#!/bin/bash
exec android-webcam --gui "$@"
EOS
chmod +x ~/.local/bin/android-webcam-gui
# Restore if overwritten by pip
if ! grep -q "from android_webcam.app import main" ~/.local/bin/android-webcam 2>/dev/null; then
  log "Restoring pip entry"
  pip install "${PIP_FLAGS[@]}" --force-reinstall --no-deps -e "$ROOT" 2>&1 | tail -n 5 || true
fi
# Ensure ~/.local/bin is in PATH for future shells (idempotent)
if ! grep -q 'HOME/.local/bin' ~/.bashrc 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi
if command -v fish >/dev/null 2>&1 && ! grep -q '.local/bin' ~/.config/fish/config.fish 2>/dev/null; then
  mkdir -p ~/.config/fish
  echo 'set -q PATH; or set PATH $HOME/.local/bin $PATH' >> ~/.config/fish/config.fish || true
fi
ok "bin: $(command -v android-webcam 2>&1 || echo ~/.local/bin/android-webcam)"

# ── 6. Desktop file (setsid -f so GUI detaches from launcher) ───────
log "Desktop file…"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/android-webcam.desktop <<'EOS'
[Desktop Entry]
Name=Android Webcam
Comment=Native scrcpy camera → v4l2loopback (720p/1080p, front/back, torch, mic via null sink)
Exec=setsid -f android-webcam
Icon=camera-web
Terminal=false
Type=Application
Categories=Video;AudioVideo;
StartupNotify=true
Keywords=webcam;scrcpy;android;v4l2;camera;
EOS
desktop-file-validate ~/.local/share/applications/android-webcam.desktop 2>&1 | head -n 5 || true
update-desktop-database ~/.local/share/applications 2>&1 | head -n 5 || true
ok "desktop: ~/.local/share/applications/android-webcam.desktop"

# ── 7. Legacy Iriun cleanup ─────────────────────────────────────────
for p in ~/.local/bin/iriun-webcam ~/.local/share/applications/iriun-webcam-bridge.desktop; do
  if [[ -f "$p" ]]; then
    mkdir -p ~/.local/share/android-webcam/archive
    mv "$p" ~/.local/share/android-webcam/archive/iriun-webcam.$(date +%s) 2>&1 | head -n 3 || true
    log "Archived legacy $p"
  fi
done
# If iriun pacman package still installed, remove it (was pulling v4l2loopback as dep)
if pacman -Q iriunwebcam-bin >/dev/null 2>&1; then
  log "Removing stale iriunwebcam-bin (keeps v4l2loopback)…"
  sudo pacman -Rns --noconfirm iriunwebcam-bin 2>&1 | tail -n 5 || true
  # Re-mark v4l2loopback explicit after removal
  sudo pacman -D --asexplicit v4l2loopback-dkms >/dev/null 2>&1 || true
fi

# ── 8. Hyprland / Omarchy integration ─────────────────────────────────
log "Hyprland / Omarchy…"
mkdir -p ~/.config/hypr ~/.config/omarchy/extensions
# Bindings: keep SUPER+SHIFT+W for webcam, A for screen
if [[ -f ~/.config/hypr/bindings.lua ]] && grep -q "iriun" ~/.config/hypr/bindings.lua 2>/dev/null; then
  sed -i 's/iriun-webcam/android-webcam/g; s/Iriun Webcam/Android Webcam/g' ~/.config/hypr/bindings.lua 2>&1 | head -n 5 || true
  ok "Hypr bindings: Iriun → Android Webcam"
fi
if [[ -f ~/.config/hypr/bindings.lua ]] && ! grep -q "android-webcam" ~/.config/hypr/bindings.lua 2>/dev/null; then
  warn "Add to ~/.config/hypr/bindings.lua:  o.bind(\"SUPER + SHIFT + W\", \"Android Webcam\", \"setsid -f android-webcam\")"
fi
# Omarchy menu — single 'Android' parent via apps provider (avoids dupes)
if [[ -f ~/.config/omarchy/extensions/omarchy-menu.jsonc ]]; then
  python3 - <<'PY' 2>&1 | head -n 5
import pathlib
p = pathlib.Path.home()/".config/omarchy/extensions/omarchy-menu.jsonc"
try:
    t = p.read_text()
    orig = t
    t = t.replace('"android.iriun"', '"android"').replace('"iriun-webcam"', '"android-webcam"').replace('Iriun Webcam','Android Webcam')
    if '"android"' not in t:
        # minimal parent that delegates to .desktop apps
        t = t.rstrip().rstrip('}').rstrip(',') + ', "android": {"icon":"󰀲","label":"Android","provider":"apps","description":"Rooted bridge: Screen Mirror, Android Webcam (from .desktop entries)"}\n}'
        if not t.strip().endswith('}'): t += '\n}'
    if t != orig:
        p.write_text(t); print("omarchy-menu.jsonc updated")
except Exception as e:
    print("menu patch failed:", e)
PY
fi
# Omarchy plugin (bar-widget + panel) — copy from repo if newer
if [[ -d "$ROOT/omarchy-plugin" ]]; then
  PLUG_DST="$HOME/.config/omarchy/plugins/ishanp.android-webcam"
  mkdir -p "$PLUG_DST"
  for f in BarWidget.qml Panel.qml manifest.json; do
    if [[ -f "$ROOT/omarchy-plugin/$f" ]]; then
      if ! cmp -s "$ROOT/omarchy-plugin/$f" "$PLUG_DST/$f" 2>/dev/null; then
        cp "$ROOT/omarchy-plugin/$f" "$PLUG_DST/$f" && echo "  plugin $f updated" || true
      fi
    fi
  done
  for f in LICENSE README.md; do [[ -f "$ROOT/$f" ]] && cp "$ROOT/$f" "$PLUG_DST/$f" 2>/dev/null || true; done
  ok "Omarchy plugin synced to $PLUG_DST"
fi
# Hypr window rule note (scrcpy class already floats)
if ! grep -q "scrcpy" ~/.config/hypr/hyprland.lua 2>/dev/null; then
  : # rule already in omarchy defaults
fi

# ── 9. Verify ────────────────────────────────────────────────────────
echo ""
echo "── Verify ──"
FAIL=0
for cmd in scrcpy adb ffmpeg v4l2-ctl pactl; do
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd $(command -v $cmd)"; else fail "$cmd missing"; FAIL=1; fi
done
if command -v android-webcam >/dev/null 2>&1; then ok "android-webcam $(android-webcam --help 2>&1 | head -n1)"; else fail "android-webcam not on PATH (add ~/.local/bin)"; FAIL=1; fi
if [[ -e /dev/video0 ]]; then ok "/dev/video0 $(ls -la /dev/video0 | awk '{print $1, $3, $4}')  $(v4l2-ctl -d /dev/video0 --all 2>&1 | grep 'Card type' | head -n1)"; else fail "/dev/video0 missing"; FAIL=1; fi
if dkms status 2>&1 | grep -q "v4l2loopback.*$(uname -r).*installed"; then ok "DKMS v4l2loopback for $(uname -r)"; else warn "DKMS not built for $(uname -r) — reboot after headers"; fi
if grep -q "card_label=\"Android Webcam\"" /etc/modprobe.d/v4l2loopback.conf 2>&1; then ok "modprobe card_label Android Webcam"; else warn "modprobe card_label not Android Webcam"; fi
if pactl list short sinks 2>&1 | grep -q scrcpy_mic; then ok "null sink scrcpy_mic"; else warn "null sink scrcpy_mic not yet (created on first webcam start)"; fi
if desktop-file-validate ~/.local/share/applications/android-webcam.desktop 2>&1 | grep -q .; then fail "desktop file invalid"; else ok "desktop file valid"; fi

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "== done — launch: android-webcam  (GUI)  |  android-webcam --help  (CLI) =="
  echo "   Test webcam:  ffmpeg -f v4l2 -video_size 1920x1080 -i /dev/video0 -frames:v 1 /tmp/test.jpg && xdg-open /tmp/test.jpg"
  echo "   Test mic:     parecord --device=scrcpy_mic.monitor /tmp/mic.wav  # after webcam start"
  echo "   Logs:         tail -f /tmp/android-webcam-gui.log"
  if ! id -nG "$USER" | grep -qw video; then
    echo ""
    warn "You were added to 'video' group — log out and back in for it to take effect."
  fi
else
  echo "== done with warnings — see ✗ above =="
fi
