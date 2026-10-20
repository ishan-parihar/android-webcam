"""Mock-only, network-free tests for the companion protocol client.

These run a fake companion server on a real loopback socket so the actual
JSON-lines framing, framing errors, and session lifecycle are exercised —
no phone, no adb, no internet.  Label: mock-only.

Run:  python tests/test_companion.py
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from android_webcam import companion  # noqa: E402

REDDMI_CAMERAS = [
    {"id": "0", "facing": "back", "max_size": [4640, 3472],
     "sizes": [[1920, 1080, 30], [1280, 720, 120], [640, 480, 30]],
     "torch": True, "orientation": 90, "zoom": [1.0, 10.0]},
    {"id": "1", "facing": "front", "max_size": [2592, 1940],
     "sizes": [[1920, 1080, 30], [1280, 720, 60]],
     "torch": False, "orientation": 270, "zoom": [1.0, 4.0]},
]


class FakeCompanion(threading.Thread):
    """Fake phone companion: real socket, scripted responses, records calls."""

    daemon = True

    def __init__(self, cameras=None, status=None, config=None):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.cameras = REDDMI_CAMERAS if cameras is None else cameras
        self.status = status if status is not None else {
            "adb_enabled": True, "model": "Redmi Note 8 Pro", "sdk_int": 36,
            "ips": ["192.168.1.12"], "battery": {"level": 82, "charging": True},
            "ignoring_battery_optimizations": True,
            "keepalive": {"running": False, "wakelock_held": False, "source": "none"},
            "session": {"active": False, "last_heartbeat_ms_ago": None},
        }
        self.config = config if config is not None else {
            "camera_id": "0", "size": [1920, 1080], "fps": 30, "torch": False}
        self.received = []          # every request dict
        self.session_active = False
        self.watchdog_timeout = None
        self.keepalive_on = False
        self._stop = threading.Event()

    # -- helpers the tests assert against
    def request(self, cmd, timeout=2.0):
        return companion.call(cmd, port=self.port, timeout=timeout)

    def wait_for_session(self, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.session_active:
                return True
            time.sleep(0.02)
        return False

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # -- server loop
    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                buf = b""
                while b"\n" not in buf:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                if not buf.strip():
                    continue
                try:
                    req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                except ValueError:
                    conn.sendall(b'{"ok":false,"error":"malformed"}\n')
                    continue
                self.received.append(req)
                resp = self._respond(req)
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

    def _respond(self, req):
        cmd = req.get("cmd")
        base = {"cmd": cmd, "ok": True, "proto": 1}
        if cmd == "ping":
            return {**base, "app": "android-webcam-companion", "version": 1}
        if cmd == "status":
            st = json.loads(json.dumps(self.status))
            st["keepalive"] = {"running": self.keepalive_on,
                               "wakelock_held": self.keepalive_on,
                               "source": "session" if self.session_active else "none"}
            st["session"] = {"active": self.session_active}
            return {**base, **st}
        if cmd == "cameras":
            return {**base, "cameras": self.cameras}
        if cmd == "config.get":
            return {**base, "config": self.config}
        if cmd == "config.set":
            cfg = req.get("config") or {}
            problems = companion.validate(
                {"camera_id": cfg.get("camera_id"),
                 "custom_size": (f"{cfg['size'][0]}x{cfg['size'][1]}"
                                 if isinstance(cfg.get("size"), list) else None),
                 "fps": cfg.get("fps"), "torch": cfg.get("torch")},
                self.cameras)
            if problems:
                return {"cmd": cmd, "ok": False, "proto": 1,
                        "error": problems[0]}
            self.config = cfg
            return base
        if cmd == "session.begin":
            self.session_active = True
            self.watchdog_timeout = req.get("watchdog_timeout")
            return {**base, "session_active": True,
                    "watchdog_timeout": self.watchdog_timeout}
        if cmd == "session.heartbeat":
            return {**base, "ms_ago": 0}
        if cmd == "session.end":
            self.session_active = False
            return {**base, "session_active": False}
        if cmd == "keepalive.on":
            self.keepalive_on = True
            return {**base, "running": True, "source": "manual"}
        if cmd == "keepalive.off":
            self.keepalive_on = False
            return {**base, "running": False}
        return {"cmd": cmd, "ok": False, "proto": 1, "error": "unknown cmd"}


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.phone = FakeCompanion()
        self.phone.start()
        self.addCleanup(self.phone.stop)

    def test_ping(self):
        r = self.phone.request({"cmd": "ping"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["app"], "android-webcam-companion")

    def test_cameras(self):
        cams = companion.cameras(self.phone.port)
        self.assertEqual(len(cams), 2)
        self.assertEqual(cams[0]["id"], "0")

    def test_status(self):
        st = companion.status(self.phone.port)
        self.assertTrue(st["adb_enabled"])
        self.assertEqual(st["model"], "Redmi Note 8 Pro")

    def test_config_roundtrip(self):
        companion.set_config({"camera_id": "0", "size": [1920, 1080],
                              "fps": 30, "torch": False}, self.phone.port)
        got = companion.get_config(self.phone.port)
        self.assertEqual(got["camera_id"], "0")

    def test_config_set_rejects_unsupported_size(self):
        with self.assertRaises(companion.CompanionError) as cm:
            companion.set_config({"camera_id": "0", "size": [9999, 1],
                                  "fps": 30, "torch": False}, self.phone.port)
        self.assertIn("unsupported", str(cm.exception))

    def test_unknown_command_raises(self):
        with self.assertRaises(companion.CompanionError):
            self.phone.request({"cmd": "nonsense"})

    def test_unreachable_raises_loudly(self):
        with self.assertRaises(companion.CompanionError) as cm:
            companion.ping(port=1)  # nothing listens on port 1
        self.assertIn("unreachable", str(cm.exception))


class SessionTest(unittest.TestCase):
    """The advisory-2 contract: no wake-lock leak, pkill-safe."""

    # Real adb is never invoked: the forward is stubbed and the socket is the
    # fake phone's loopback listener.  Mock-only.
    @staticmethod
    def _no_forward(_tgt, _port):
        return ""

    def setUp(self):
        self.phone = FakeCompanion()
        self.phone.start()
        self.addCleanup(self.phone.stop)
        self.cfg = {"android_ip": "192.168.1.12", "android_port": 5555,
                    "companion_host_port": self.phone.port}

    def test_session_begin_arms_keepalive_and_heartbeat(self):
        s = companion.Session(self.cfg, forwarder=self._no_forward)
        s.start()
        self.assertTrue(s.started)
        self.assertTrue(self.phone.wait_for_session(), "session.begin not seen")
        self.assertTrue(self.phone.keepalive_on, "keepalive not armed on start")
        self.assertEqual(self.phone.watchdog_timeout, companion.WATCHDOG_TIMEOUT)
        s.stop()

    def test_graceful_stop_releases_everything(self):
        s = companion.Session(self.cfg, forwarder=self._no_forward)
        s.start()
        self.assertTrue(self.phone.wait_for_session())
        s.stop()
        self.assertFalse(self.phone.session_active, "session.end not sent")
        self.assertFalse(self.phone.keepalive_on, "wake lock leaked after stop")
        self.assertFalse(s.started)

    def test_killed_host_leaves_watchdog_to_release(self):
        """Simulate the panel's pkill: start a session, then abandon it
        WITHOUT stop().  The phone must still be able to release via the
        watchdog contract — here we assert the heartbeat/wiring that the
        phone watchdog depends on (session.begin armed, no stop called)."""
        s = companion.Session(self.cfg, forwarder=self._no_forward)
        s.start()
        self.assertTrue(self.phone.wait_for_session())
        # Deliberately do NOT call s.stop(); the phone watchdog (WATCHDOG_TIMEOUT)
        # is the release path.  Assert the phone has a bounded timeout armed.
        self.assertIn(self.phone.watchdog_timeout, (15, 30, 45, 60, 90, 120, 300))
        # A fresh session.begin restarts the timer cleanly (re-stream case).
        s2 = companion.Session(self.cfg, forwarder=self._no_forward)
        s2.start()
        self.assertTrue(self.phone.wait_for_session())
        s2.stop()

    def test_session_degrades_when_phone_absent(self):
        cfg = {"android_ip": "127.0.0.1", "android_port": 1,
               "companion_host_port": 1}
        s = companion.Session(cfg, forwarder=self._no_forward)
        self.assertFalse(s.start(), "session should not claim success")
        self.assertIsNotNone(s.reason)
        s.stop()  # must not raise


class ValidateTest(unittest.TestCase):
    def test_valid_config_no_problems(self):
        self.assertEqual(companion.validate({"camera_facing": "back"}, REDDMI_CAMERAS), [])

    def test_unknown_camera_id(self):
        p = companion.validate({"camera_id": "7"}, REDDMI_CAMERAS)
        self.assertTrue(any("not on this phone" in x for x in p))

    def test_missing_facing(self):
        p = companion.validate({"camera_facing": "front"}, [])
        self.assertTrue(any("no cameras" in x for x in p))

    def test_unsupported_size(self):
        p = companion.validate({"custom_size": "9999x1"}, REDDMI_CAMERAS)
        self.assertTrue(any("unsupported" in x for x in p))

    def test_fps_over_max(self):
        p = companion.validate({"camera_facing": "back",
                                "custom_size": "1280x720", "fps": 240},
                               REDDMI_CAMERAS)
        self.assertTrue(any("exceeds max" in x for x in p))

    def test_torch_on_front_camera_rejected(self):
        p = companion.validate({"camera_facing": "front", "torch": True},
                               REDDMI_CAMERAS)
        self.assertTrue(any("no torch" in x for x in p))

    def test_collects_multiple_problems(self):
        p = companion.validate({"camera_facing": "front", "torch": True,
                                "custom_size": "9999x1"}, REDDMI_CAMERAS)
        self.assertGreaterEqual(len(p), 2)

    def test_malformed_size_is_rejected_not_raised(self):
        """A phone- or config-supplied non-numeric size must not traceback:
        the parse sits on a trust boundary (docs: fail loud, never crash)."""
        for bad in ("1920xabc", "1920x1080x5", "abc", "1x", "1920X"):
            with self.subTest(bad=bad):
                p = companion.validate({"camera_facing": "back", "custom_size": bad},
                                       REDDMI_CAMERAS)
                self.assertTrue(any("unsupported" in x for x in p), p)

    def test_lowercase_and_uppercase_x_both_parse(self):
        self.assertEqual(companion.validate(
            {"camera_facing": "back", "custom_size": "1920X1080", "fps": 30},
            REDDMI_CAMERAS), [])

    def test_wire_malformed_sizes_are_skipped_not_raised(self):
        """sizes[] arrives over the wire from the phone app: a bad entry
        (string, None, wrong arity) must degrade to 'unsupported', never
        raise ValueError out of validate()."""
        wire_cases = [
            ["1920x1080x30"],        # strings, not triples
            [None],
            [["a", "b", "c"]],
            [[1920, 1080]],          # 2 elements, not 3
            [[[1, 2, 3]]],           # nested
        ]
        for bad in wire_cases:
            with self.subTest(sizes=bad):
                cam = {"id": "0", "facing": "back", "sizes": bad, "torch": True}
                p = companion.validate(
                    {"camera_facing": "back", "custom_size": "1920x1080"}, [cam])
                self.assertTrue(any("unsupported" in x for x in p), p)

    def test_mixed_good_and_garbage_sizes_still_resolve(self):
        cam = {"id": "0", "facing": "back",
               "sizes": [[1920, 1080, 30], "junk", None, ["a"]], "torch": True}
        self.assertEqual(companion.validate(
            {"camera_facing": "back", "custom_size": "1920x1080", "fps": 30},
            [cam]), [])
        self.assertTrue(companion.validate(
            {"camera_facing": "back", "custom_size": "1920x1080", "fps": 60},
            [cam]))

    def test_size_summary_never_raises_on_wire_garbage(self):
        cam = {"id": "0", "facing": "back", "sizes": ["x", None, [1, 2]], "torch": True}
        self.assertEqual(companion.size_summary(cam), "none")


class AdbMdnsReportingTest(unittest.TestCase):
    """The phone's own adb/mDNS advertisement, read over the socket.

    This is the ground truth for desktop-side discovery. The avahi record
    layout on this host came from a man page and was only ever checked against
    synthetic records, so when the desktop cannot find the phone, this block
    is where the answer actually is.
    """

    def _status(self, payload):
        import android_webcam.companion as C
        orig = C.call
        C.call = lambda p, port=None: payload
        self.addCleanup(setattr, C, "call", orig)

    def test_surfaces_the_phones_own_advertisement(self):
        self._status({"ok": True, "adb_mdns": {
            "loaded": True, "wireless_port": 39481, "legacy_port": 5555,
            "lines": ["wireless port (service.adb.tls.port): 39481",
                      "mDNS registration: _adb-tls-connect._tcp"]}})
        import android_webcam.companion as C
        m = C.adb_mdns()
        self.assertEqual(m["wireless_port"], 39481)
        self.assertEqual(m["legacy_port"], 5555)
        self.assertTrue(any("_adb-tls-connect._tcp" in l for l in m["lines"]))

    def test_no_reader_is_distinct_from_nothing_advertised(self):
        # keep-alive off means nobody is polling on the phone. That must NOT
        # read as "the phone advertises nothing" -- those are different facts
        # and conflating them is a fabricated answer.
        self._status({"ok": True, "adb_mdns": {
            "loaded": False, "wireless_port": None, "legacy_port": None,
            "lines": ["no reader running"]}})
        import android_webcam.companion as C
        m = C.adb_mdns()
        self.assertFalse(m["loaded"])
        self.assertIsNone(m["wireless_port"])

    def test_a_phone_without_the_block_fails_loudly(self):
        # An older companion build. Returning {} here would silently look
        # like a phone that advertises nothing.
        self._status({"ok": True, "model": "Redmi"})
        import android_webcam.companion as C
        with self.assertRaises(C.CompanionError):
            C.adb_mdns()


class AdvertisementCheckTest(unittest.TestCase):
    """Compare the host's mDISCOVERY findings against the phone's own.

    The avahi record layout on this host came from a man page and was only
    ever checked against synthetic records. This is the check that turns that
    assumption into a verified fact -- the only place the truth exists is the
    phone.

    It is a VERIFICATION step, not a discovery source: reading the phone needs
    an adb forward, which needs a target already known.
    """

    def _phone(self, payload):
        import android_webcam.companion as C
        orig = C.adb_mdns
        C.adb_mdns = lambda port=None: payload
        self.addCleanup(setattr, C, "adb_mdns", orig)

    def test_agreement_when_the_host_finds_the_phones_port(self):
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        import android_webcam.companion as C
        r = C.check_advertisement(sweep=lambda: [("192.168.1.12", 39481)])
        self.assertTrue(r["checked"])
        self.assertTrue(r["agrees"])
        self.assertIsNone(r["mismatch"])

    def test_mismatch_when_the_host_finds_a_different_port(self):
        # The case that matters: the man-page parser is wrong about the
        # record layout, and the host finds the legacy port instead.
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        import android_webcam.companion as C
        r = C.check_advertisement(sweep=lambda: [("192.168.1.12", 5555)])
        self.assertFalse(r["agrees"])
        self.assertIn("39481", r["mismatch"])
        self.assertIn("5555", r["mismatch"])

    def test_an_unreachable_phone_is_unknown_not_agreement(self):
        # Must never render as success: "we could not check" is not "it
        # checks out", and conflating them is a fabricated answer.
        import android_webcam.companion as C
        def boom(port=None):
            raise C.CompanionError("unreachable")
        orig = C.adb_mdns
        C.adb_mdns = boom
        self.addCleanup(setattr, C, "adb_mdns", orig)
        r = C.check_advertisement(sweep=lambda: [("192.168.1.12", 5555)])
        self.assertFalse(r["checked"])
        self.assertIsNone(r["agrees"])
        self.assertIsNone(r["mismatch"])

    def test_no_wireless_debugging_is_its_own_case(self):
        # A phone on adb with wireless debugging off is not a parser bug.
        self._phone({"loaded": True, "wireless_port": None, "lines": []})
        import android_webcam.companion as C
        r = C.check_advertisement(sweep=lambda: [("192.168.1.12", 5555)])
        self.assertTrue(r["checked"])
        self.assertIsNone(r["agrees"])
        self.assertIn("wireless debugging off", r["mismatch"])



    def test_host_finds_nothing_is_a_mismatch_not_a_verdict(self):
        # THE case on this network: avahi returns nothing while the phone
        # demonstrably publishes a port. That is a real failure -- the parser
        # or the responder is broken -- and it used to fall through to
        # agrees=None, mismatch=None, i.e. silence in every print branch.
        # Silence here reads as "no news", which is the opposite of true.
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        import android_webcam.companion as C
        r = C.check_advertisement(sweep=lambda: [])
        self.assertIs(r["agrees"], False)
        self.assertIn("39481", r["mismatch"])
        self.assertIn("NO advertisement", r["mismatch"])

    def test_not_swept_gives_no_verdict_and_says_so(self):
        # --status polls repeatedly and the avahi sweep is a ~1s subprocess,
        # so that path passes sweep=None. It must NOT accuse the parser of
        # being broken when the truth is that it was never asked -- that
        # would be a fabricated failure.
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        import android_webcam.companion as C
        r = C.check_advertisement(sweep=None)
        self.assertTrue(r["checked"])          # the phone WAS asked
        self.assertFalse(r["swept"])           # the host was NOT
        self.assertIsNone(r["agrees"])         # so no verdict either way
        self.assertIsNone(r["mismatch"])
        self.assertEqual(r["phone_port"], 39481)   # the real fact survives

    def test_swept_empty_and_not_swept_are_different_states(self):
        # Both have empty host_ports; only one of them is a real failure.
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        import android_webcam.companion as C
        swept = C.check_advertisement(sweep=lambda: [])
        unswept = C.check_advertisement(sweep=None)
        self.assertTrue(swept["swept"])
        self.assertFalse(unswept["swept"])
        self.assertIs(swept["agrees"], False)
        self.assertIsNone(unswept["agrees"])


    def test_status_verdict_does_not_accuse_an_unswept_parser(self):
        # The regression this pins: _print_advertisement_check defaulted
        # sweep=None AND fell back to _mdns_ports on None, so passing None
        # from --status ran avahi anyway and printed the empty-sweep mismatch
        # -- accusing a parser of being broken that was never asked.
        import android_webcam.backend as B
        import android_webcam.companion as C
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        calls = []
        orig = B._mdns_ports
        B._mdns_ports = lambda: calls.append(1) or []
        self.addCleanup(setattr, B, "_mdns_ports", orig)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            B._print_advertisement_check(to_stderr=False, sweep=None)
        out = buf.getvalue()
        self.assertEqual(calls, [], "--status must not run an avahi sweep")
        self.assertIn("not swept", out)
        self.assertNotIn("NO advertisement", out,
                         "an unswept host must not be reported as a broken parser")

    def test_the_default_means_sweep(self):
        # ...and the default must still sweep, or --sync --status would stop
        # doing the comparison it exists for.
        # The phone MUST be stubbed here too: with no stub, adb_mdns raises
        # and check_advertisement returns before the sweep ever runs, which
        # is a test-setup failure masquerading as a code failure.
        self._phone({"loaded": True, "wireless_port": 39481, "lines": []})
        import android_webcam.backend as B
        calls = []
        orig = B._mdns_ports
        B._mdns_ports = lambda: calls.append(1) or [("ip", 39481)]
        self.addCleanup(setattr, B, "_mdns_ports", orig)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            B._print_advertisement_check()
        self.assertEqual(len(calls), 1, "the default must run the avahi sweep")
        self.assertIn("agrees", buf.getvalue())


    def test_status_json_has_no_key_that_contradicts_checked(self):
        # A hardcoded adb_mdns_checked=True sat directly above a call that
        # returns checked=False for an unreachable or too-old phone. In a
        # machine-readable field nothing on screen catches that lie.
        import android_webcam.backend as B
        import android_webcam.companion as C
        import io, contextlib, json
        def boom(port=None):
            raise C.CompanionError("unreachable")
        self._phone  # keep the helper's intent clear
        orig_mdns, orig_sync, orig_pg = C.adb_mdns, B._companion.sync, B.pgrep_streaming
        self.addCleanup(setattr, C, "adb_mdns", orig_mdns)
        self.addCleanup(setattr, B._companion, "sync", orig_sync)
        self.addCleanup(setattr, B, "pgrep_streaming", orig_pg)
        C.adb_mdns = boom
        B.pgrep_streaming = lambda: False
        B._mdns_ports = lambda: []
        B._companion.sync = lambda cfg, apply=True: {
            "reachable": True, "port": 7878, "notes": [], "changes": {},
            "status": {"model": "Redmi", "adb_enabled": True,
                       "battery": {"level": 1, "charging": False},
                       "keepalive": {"running": True}, "adb_mdns": {}},
            "cameras": [], "problems": []}
        argv = sys.argv
        sys.argv = ["android-webcam", "--status"]
        self.addCleanup(setattr, sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                B.cli()
            except SystemExit:
                pass
        d = json.loads(out.getvalue())
        self.assertNotIn("adb_mdns_checked", d)
        self.assertIs(d["advertisement_check"]["checked"], False,
                      "an unreachable phone must report checked=False")


if __name__ == "__main__":
    unittest.main(verbosity=2)
