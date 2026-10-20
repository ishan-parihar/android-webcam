#!/usr/bin/env python3
"""End-to-end smoke for the companion status/sync path with NO phone.

Stands up a fake companion on a loopback socket, points the desktop client at
it (no adb: the adb forward is stubbed exactly as Session does under test),
and exercises `status` + `cameras` + `config.set` the way the CLI would.
Mock-only.  Run:  python tests/smoke_companion_cli.py
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))  # reuse the test's FakeCompanion

from android_webcam import companion
from test_companion import FakeCompanion


def main() -> int:
    phone = FakeCompanion()
    phone.start()
    port = phone.port
    print(f"[smoke] fake companion on 127.0.0.1:{port}")

    # 1. ping
    r = companion.ping(port)
    print("[smoke] ping ->", r["app"], "v", r["version"])

    # 2. status (this is exactly what `android-webcam-cli --status` prints)
    st = companion.status(port)
    b = st["battery"]
    print(f"[smoke] status -> {st['model']} android {st['sdk_int']} "
          f"adb={'on' if st['adb_enabled'] else 'off'} "
          f"batt={b['level']}% charging={b['charging']} "
          f"keepalive={st['keepalive']['running']}")
    assert "model" in st and "keepalive" in st and "battery" in st, "flat status fields missing"

    # 3. cameras + validation
    cams = companion.cameras(port)
    print(f"[smoke] cameras -> {[c['id'] + ':' + c['facing'] for c in cams]}")
    probs = companion.validate({"camera_facing": "back", "custom_size": "1920x1080", "fps": 30}, cams)
    assert probs == [], probs

    # 4. config round-trip through the phone (validate-then-save)
    companion.set_config({"camera_id": "0", "size": [1280, 720], "fps": 30, "torch": True}, port)
    got = companion.get_config(port)
    assert got["size"] == [1280, 720] and got["torch"] is True, got
    print("[smoke] config.set/get ->", got)

    # 5. bad config must fail loud, not clamp
    try:
        companion.set_config({"camera_id": "0", "size": [1, 1], "fps": 30}, port)
        print("[smoke] FAIL: bad size accepted")
        return 1
    except companion.CompanionError as e:
        print("[smoke] bad size rejected ->", str(e)[:60])

    # 6. session lifecycle (advisory-2 contract)
    cfg = {"android_ip": "x", "android_port": 5555, "companion_host_port": port}
    s = companion.Session(cfg, forwarder=lambda *_: "")
    assert s.start()
    assert phone.wait_for_session()
    assert phone.keepalive_on
    print("[smoke] session armed, keepalive on")
    s.stop()
    assert not phone.session_active and not phone.keepalive_on
    print("[smoke] session ended, wake lock released")

    phone.stop()
    print("[smoke] OK — all steps passed (mock-only, no phone, no adb, no network)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
