#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "== android-webcam install =="

# pip install
if [[ -f "$ROOT/pyproject.toml" ]]; then
  echo "→ pip install -e ."
  pip install -e "$ROOT" 2>&1 | tail -n 10
fi

# bin wrappers — pip provides android-webcam & android-webcam-cli; keep them
# android-webcam-gui is an alias that forces GUI even with CLI flags
mkdir -p ~/.local/bin
cat > ~/.local/bin/android-webcam-gui <<'EOS'
#!/bin/bash
exec android-webcam --gui "$@"
EOS
chmod +x ~/.local/bin/android-webcam-gui
# Ensure pip entry is intact (reinstall if overwritten)
if ! grep -q "from android_webcam.app import main" ~/.local/bin/android-webcam 2>/dev/null; then
  echo "→ restoring pip android-webcam entry"
  pip install --break-system-packages --force-reinstall --no-deps -e "$ROOT" 2>&1 | tail -n 5
fi

# desktop file
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/android-webcam.desktop <<'EOS'
[Desktop Entry]
Name=Android Webcam
Comment=Native scrcpy camera → v4l2loopback (720p/1080p, front/back, torch, mic)
Exec=android-webcam
Icon=camera-web
Terminal=false
Type=Application
Categories=Video;AudioVideo;
StartupNotify=true
Keywords=webcam;scrcpy;android;v4l2;
EOS
desktop-file-validate ~/.local/share/applications/android-webcam.desktop 2>&1 | head -n 10 || true
update-desktop-database ~/.local/share/applications 2>&1 | head -n 5 || true

# legacy iriun cleanup — archive, don't delete history
if [[ -f ~/.local/bin/iriun-webcam ]]; then
  mkdir -p ~/.local/share/android-webcam/archive
  mv ~/.local/bin/iriun-webcam ~/.local/share/android-webcam/archive/iriun-webcam.$(date +%s) 2>&1 | head -n 5 || true
  echo "→ archived iriun-webcam"
fi
if [[ -f ~/.local/share/applications/iriun-webcam-bridge.desktop ]]; then
  mkdir -p ~/.local/share/android-webcam/archive
  mv ~/.local/share/applications/iriun-webcam-bridge.desktop ~/.local/share/android-webcam/archive/ 2>&1 | head -n 5 || true
fi

# ensure v4l2loopback configs
if [[ ! -f /etc/modules-load.d/v4l2loopback.conf ]]; then
  echo 'v4l2loopback' | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null
fi
if [[ ! -f /etc/modprobe.d/v4l2loopback.conf ]]; then
  echo 'options v4l2loopback card_label="Android Webcam" exclusive_caps=1 video_nr=0' | sudo tee /etc/modprobe.d/v4l2loopback.conf >/dev/null
fi
sudo modprobe v4l2loopback 2>&1 | head -n 5 || true
sudo usermod -aG video "$USER" 2>&1 | head -n 5 || true

echo "→ updating Hyprland/Omarchy menu"
# Hyprland bind: SUPER+SHIFT+W for webcam (W=webcam), keep SUPER+SHIFT+A for screen
mkdir -p ~/.config/hypr
if grep -q "iriun" ~/.config/hypr/bindings.lua 2>/dev/null; then
  sed -i 's/iriun-webcam/android-webcam/g; s/Iriun Webcam/Android Webcam/g' ~/.config/hypr/bindings.lua 2>&1 | head -n 10 || true
fi
if ! grep -q "android-webcam" ~/.config/hypr/bindings.lua 2>/dev/null; then
  # append bind via lua helper if missing — fallback: tell user
  echo "  Note: add to ~/.config/hypr/bindings.lua: o.bind(\"SUPER + SHIFT + W\", \"Android Webcam\", \"android-webcam\")"
fi
# Omarchy menu — replace iriun with native
if [[ -f ~/.config/omarchy/extensions/omarchy-menu.jsonc ]]; then
  python3 - <<'PY' 2>&1 | head -n 20
import json, pathlib, re
p = pathlib.Path.home()/".config/omarchy/extensions/omarchy-menu.jsonc"
try:
    t = p.read_text()
    # keep comments, just replace iriun entry
    t = t.replace('"android.iriun"', '"android.webcam"').replace('"iriun-webcam"', '"android-webcam"').replace('Iriun Webcam','Android Webcam').replace('Host+phone webcam','Native scrcpy camera → v4l2')
    # ensure webcam entry exists
    if '"android.webcam"' not in t:
        t = t.replace('}', ', "android.webcam": {"icon":"󰕧","label":"Android Webcam","action":"android-webcam","description":"Native scrcpy camera → /dev/video0 — SUPER+SHIFT+W"}\n}')
    p.write_text(t)
    print("omarchy-menu.jsonc updated")
except Exception as e:
    print("menu patch failed:", e)
PY
fi

# Hypr window rule for webcam preview (scrcpy)
if ! grep -q 'Android Webcam' ~/.config/hypr/hyprland.lua 2>/dev/null; then
  echo '  Note: window rule scrcpy { float, center } already covers webcam preview (same class scrcpy)'
fi

echo "== done =="
echo "Launch: android-webcam   (GUI)  or  android-webcam --help  (CLI)"
echo "Test:   android-webcam --back --720 --dry-run; /tmp/native_webcam_manual_test.sh front 720"
