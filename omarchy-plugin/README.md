# Android Webcam — Omarchy Plugin

Native `scrcpy --video-source=camera --v4l2-sink=/dev/video0` bar widget + panel. Replaces Iriun.

## Install

```bash
omarchy plugin add https://github.com/ishanp/android-webcam.git
omarchy plugin enable ishanp.android-webcam
# add to bar: edit ~/.config/omarchy/shell.json layout.center add { "id": "ishanp.android-webcam" }
```

Or via this repo:

```bash
./scripts/install.sh  # installs GTK app + copies plugin to ~/.config/omarchy/plugins/ishanp.android-webcam
omarchy plugin enable ishanp.android-webcam
```

## Removal

```bash
omarchy plugin remove ishanp.android-webcam
```

## Usage

- Bar icon: `󰕧` idle, `󰕧●` streaming (`pgrep scrcpy.*camera`). Click → panel, Right-click → toggle.
- Panel: Open GUI, Quick start back 720p, Front 720p, Back 1080p, Torch, Mic ON.
- GUI: `android-webcam` — full controls (720p/1080p, front/back, FPS, torch, mic, preview, v4l2 sink, log, `pgrep` status).
- CLI: `android-webcam --back --720 --dry-run` prints argv; without `--dry-run` streams headless.
- Keys: `SUPER+SHIFT+W` (webcam), `SUPER+SHIFT+I` alias, `SUPER+SHIFT+A` (screen).

## License

MIT — depends on scrcpy (GPLv2), v4l2loopback (GPLv2), ffmpeg, GTK4/Adw.
