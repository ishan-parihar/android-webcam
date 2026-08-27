#!/bin/bash
set -e
echo "== android-webcam uninstall =="
pip uninstall -y android-webcam 2>&1 | tail -n 5 || true
rm -f ~/.local/bin/android-webcam ~/.local/bin/android-webcam-gui 2>&1 | head -n 5 || true
rm -f ~/.local/share/applications/android-webcam.desktop 2>&1 | head -n 5 || true
update-desktop-database ~/.local/share/applications 2>&1 | head -n 5 || true
echo "Keep: /etc/modules-load.d/v4l2loopback.conf and modprobe (remove manually if needed)"
echo "Keep: ~/.config/android-webcam/config.json"
echo "done"
