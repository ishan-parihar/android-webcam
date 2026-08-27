# Launcher dedup — keep one Android Webcam

The Omarchy shell merges your custom menu (`~/.config/omarchy/extensions/omarchy-menu.jsonc`)
with the preinstalled `apps` provider (which lists every `.desktop` file in
`~/.local/share/applications/`). If you define `android.webcam` as a menu
action AND have `android-webcam.desktop` installed, the launcher shows both.

Fix: declare a parent menu item with `provider:"apps"` and let the provider
list the .desktop entries:

```jsonc
{
  "android": {"icon":"󰀲","label":"Android","provider":"apps",
              "description":"Screen Mirror, Android Webcam"}
}
```

That single parent now lists `Android Screen` and `Android Webcam` once each
under "Android" → apps.

# Auto port discovery

Phone IP changes when the router reassigns DHCP. Both `android-screen` (legacy)
and `android-webcam` (canonical) ship `--discover` to find the phone on the
LAN and update config:

```bash
android-screen --discover       # scans 5555/5554/5556-5558, updates config
android-webcam-cli --discover   # parallel TCP scan /24, saves IP
```

Both update `ANDROID_IP`/`android_ip` to the first reachable Android
(`getprop ro.product.model` non-empty).

# Why two sources of truth

`android-screen` uses `~/.config/android-screen/config` (shell `source`).
`android-webcam` uses `~/.config/android-webcam/config.json` (Python JSON).
They share the phone but their files differ. After running `--discover` on
either, the other will still use its stale config — run both.

Future: unify into a single shared config (e.g. `~/.config/android-bridge/state.json`).
