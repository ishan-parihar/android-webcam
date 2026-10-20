"""Port negotiation and forward-ownership tests.

Split from test_companion.py so the negotiation contract is covered by its own
suite: a previous append to the shared file was rolled back by a failed sibling
edit, which left the negotiation code with zero coverage and made mutation
checks report a false green.

The central behaviour, and the reason it is subtle: `adb forward` does NOT fail
when the local port is already bound by another process.  adb records the
mapping, the connect succeeds, and the bytes go to the *foreign* listener.
Measured on this host with 7878 held by another process:

    adb forward tcp:7878 ...   # rc depends on the device, not the port
    connect(('127.0.0.1', 7878))  -> SUCCEEDED, reached the other process

So the retry must be driven by a failed protocol handshake, never by the
forward's exit code.  An exit-code-driven fallback would never trigger.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from android_webcam import companion  # noqa: E402
from android_webcam.companion import CompanionError  # noqa: E402


def _cfg():
    return {"android_ip": "192.168.1.12", "android_port": 5555,
            "companion_host_port": 7878}


class NegotiateTest(unittest.TestCase):

    def test_preferred_port_used_when_it_answers(self):
        calls = []
        winner = companion.negotiate(
            "tgt", 7878,
            forwarder=lambda t, p: calls.append(p),
            prober=lambda p: {"ok": True})
        self.assertEqual(winner, 7878)
        self.assertEqual(calls, [7878], "must not advance when the port works")

    def test_failed_ping_falls_back_to_the_next_port(self):
        """The core case: the forward SUCCEEDS but the port is not ours."""
        calls = []

        def probe(p):
            if p == 7878:
                raise CompanionError("malformed companion response")
            return {"ok": True}

        winner = companion.negotiate(
            "tgt", 7878, candidates=(7878, 7879, 7880),
            forwarder=lambda t, p: calls.append(p), prober=probe)
        self.assertEqual(winner, 7879)
        self.assertEqual(calls, [7878, 7879])

    def test_forward_failure_skips_that_port(self):
        def fwd(t, p):
            if p == 7878:
                raise CompanionError("adb forward failed")

        winner = companion.negotiate(
            "tgt", 7878, candidates=(7878, 7879),
            forwarder=fwd, prober=lambda p: {"ok": True})
        self.assertEqual(winner, 7879)

    def test_raises_naming_every_attempt(self):
        def probe(p):
            raise CompanionError(f"unreachable on {p}")

        with self.assertRaises(CompanionError) as cm:
            companion.negotiate("tgt", 7878, candidates=(7878, 7879),
                                forwarder=lambda t, p: None, prober=probe)
        # Fail loud, never silently default: every attempted port is named.
        self.assertIn("7878", str(cm.exception))
        self.assertIn("7879", str(cm.exception))

    def test_stale_forward_recovers_by_moving_port(self):
        """A crashed host leaves adbd holding the port with a dead service: the
        connect is refused, which is a PORT problem, not a device problem. This
        is the case the feature mainly exists for, so it must retry rather than
        give up on the first refusal."""
        attempts = []

        def probe(p):
            attempts.append(p)
            if p == 7878:
                # What call() actually raises when adbd holds the port with a
                # dead service behind it.
                raise companion.CompanionError("unreachable on 127.0.0.1:7878")
            return {"ok": True}

        self.assertEqual(
            companion.negotiate("tgt", 7878, candidates=(7878, 7879),
                                forwarder=lambda t, p: None, prober=probe),
            7879)
        self.assertEqual(attempts, [7878, 7879])

    def test_a_genuinely_missing_device_still_reports_every_attempt(self):
        """When no port works, the error names them all — a missing phone is
        reported as such rather than as a port problem."""
        def probe(p):
            raise companion.CompanionError("is the app installed?")

        with self.assertRaises(companion.CompanionError) as cm:
            companion.negotiate("tgt", 7878, candidates=(7878, 7879),
                                forwarder=lambda t, p: None, prober=probe)
        self.assertIn("7878", str(cm.exception))
        self.assertIn("7879", str(cm.exception))

    def test_port_never_repeats_across_attempts(self):
        seen = []

        def probe(p):
            seen.append(p)
            raise CompanionError("no")

        with self.assertRaises(CompanionError):
            companion.negotiate("tgt", 7878, candidates=(7878, 7879, 7878),
                                forwarder=lambda t, p: None, prober=probe)
        self.assertEqual(len(seen), len(set(seen)), f"retried a port: {seen}")

    def test_candidates_include_the_default(self):
        self.assertIn(companion.DEFAULT_HOST_PORT, companion.PORT_CANDIDATES)
        self.assertGreaterEqual(len(companion.PORT_CANDIDATES), 4)


class ForwardOwnershipTest(unittest.TestCase):
    """A forward is host-global; ownership is per-process.

    Removing one on teardown that another consumer created is the live-stream
    bug: the QML panel polls --status every 5s and each cycle used to yank the
    port a running stream was using.
    """

    def setUp(self):
        for port in (9990, 9991, 9992, 9993):
            with companion._fwd_lock:
                companion._owned_forwards.discard(port)

    def test_release_leaves_an_unowned_port_alone(self):
        removed = []
        with unittest.mock.patch.object(
                companion, "remove_forward",
                side_effect=lambda p, *a, **k: removed.append(p)):
            # Seed a DIFFERENT port as owned, simulating another process's
            # forward; 9991 must not be touched.
            with companion._fwd_lock:
                companion._owned_forwards.add(9990)
            companion.release_forward(9991)
            self.assertEqual(removed, [], "evicted a forward it did not own")
        with companion._fwd_lock:
            self.assertIn(9990, companion._owned_forwards,
                          "release must not disturb another port's ownership")

    def test_release_removes_a_forward_we_created_exactly_once(self):
        removed = []
        with unittest.mock.patch.object(
                companion, "remove_forward",
                side_effect=lambda p, *a, **k: removed.append(p)):
            with companion._fwd_lock:
                companion._owned_forwards.add(9992)
            companion.release_forward(9992)
            companion.release_forward(9992)
            self.assertEqual(removed, [9992], "released twice")

    def test_forward_owned_records_ownership(self):
        with companion._fwd_lock:
            companion._owned_forwards.discard(9993)
        with unittest.mock.patch.object(companion, "forward"):
            companion.forward_owned("tgt", 9993)
        with companion._fwd_lock:
            self.assertIn(9993, companion._owned_forwards)
            companion._owned_forwards.discard(9993)

    def test_session_stop_does_not_evict_a_foreign_forward(self):
        removed = []
        with unittest.mock.patch.object(
                companion, "remove_forward",
                side_effect=lambda p, *a, **k: removed.append(p)):
            s = companion.Session(_cfg(), negotiator=lambda t, p: p)
            s.started = True
            s.port = 7878
            with companion._fwd_lock:
                companion._owned_forwards.discard(7878)  # owned elsewhere
            s.stop()
            self.assertEqual(removed, [],
                             "stop() evicted a forward this process did not own")


class SessionNegotiationTest(unittest.TestCase):

    def test_session_persists_the_negotiated_port(self):
        cfg = _cfg()
        s = companion.Session(
            cfg, forwarder=lambda t, p: None, unforwarder=lambda *a: None,
            negotiator=lambda t, p: 7881)
        with unittest.mock.patch.object(
                companion, "call", side_effect=CompanionError("no device")):
            with unittest.mock.patch("android_webcam.config.update"):
                s.start()
        self.assertEqual(s.port, 7881, "session must use the winner")
        self.assertEqual(cfg["companion_host_port"], 7881,
                         "winner must persist for the Panel and CLI")

    def test_no_negotiation_keeps_the_preferred_port(self):
        cfg = _cfg()
        s = companion.Session(
            cfg, forwarder=lambda t, p: None, unforwarder=lambda *a: None,
            negotiator=lambda t, p: p)
        with unittest.mock.patch.object(
                companion, "call", side_effect=CompanionError("no device")):
            s.start()
        self.assertEqual(s.port, 7878)

    def test_session_has_a_negotiator_by_default(self):
        # start() reads _negotiator unconditionally; guard the AttributeError.
        self.assertTrue(callable(companion.Session(_cfg())._negotiator))

    def test_injected_forwarder_is_used_by_the_default_negotiator(self):
        # The seam must reach negotiate(): if it does not, tests shell out to
        # real adb and the whole suite turns into an integration test.
        seen = []
        s = companion.Session(
            _cfg(), forwarder=lambda t, p: seen.append(p),
            prober=lambda p: {"ok": True}, unforwarder=lambda *a: None)
        with unittest.mock.patch.object(
                companion, "call", side_effect=CompanionError("no device")):
            s.start()
        self.assertEqual(seen, [7878],
                         "default negotiator ignored the injected forwarder")

    def test_injected_prober_is_used_by_the_default_negotiator(self):
        probed = []
        s = companion.Session(
            _cfg(), forwarder=lambda t, p: None,
            prober=lambda p: probed.append(p) or {"ok": True},
            unforwarder=lambda *a: None)
        with unittest.mock.patch.object(
                companion, "call", side_effect=CompanionError("no device")):
            s.start()
        self.assertEqual(probed, [7878],
                         "default negotiator ignored the injected prober")


class SyncNegotiationTest(unittest.TestCase):
    """sync() is the --sync CLI path, run *while* the GUI may be streaming, so
    it must negotiate rather than assume 7878, record ownership, and never tear
    down a forward it did not create."""

    def setUp(self):
        with companion._fwd_lock:
            for p in (7878, 7879, 7880, 7881, 7882, 7883, 7884, 7885):
                companion._owned_forwards.discard(p)

    def _cfg(self):
        return {"android_ip": "192.168.1.12", "android_port": 5555,
                "companion_host_port": 7878}

    def test_sync_uses_the_negotiated_port(self):
        cfg = self._cfg()
        seen = {}

        def fake_call(cmd, **kw):
            seen.setdefault("ports", set()).add(kw.get("port"))
            raise companion.CompanionError("no device")

        with unittest.mock.patch.object(companion, "call", side_effect=fake_call):
            with unittest.mock.patch.object(
                    companion, "negotiate", return_value=7881) as neg:
                summary = companion.sync(cfg, apply=False)
        neg.assert_called_once()
        self.assertEqual(summary["port"], 7881)
        self.assertIn(7881, seen["ports"],
                      "sync must talk to the negotiated port, not 7878")
        self.assertEqual(cfg["companion_host_port"], 7881,
                         "winner must persist so the Panel stops re-negotiating")

    def test_sync_persists_the_winner_but_never_raises_on_unwritable_config(self):
        cfg = self._cfg()
        with unittest.mock.patch.object(companion, "call",
                                        side_effect=companion.CompanionError("x")):
            with unittest.mock.patch.object(companion, "negotiate", return_value=7882):
                with unittest.mock.patch(
                        "android_webcam.config.update",
                        side_effect=OSError("read-only file system")):
                    summary = companion.sync(cfg, apply=False)
        # Degrade, never raise: an unwritable config must not fail the sync.
        self.assertTrue(any("could not persist" in n for n in summary["notes"]),
                        f"persist warning lost: {summary}")
        self.assertEqual(cfg["companion_host_port"], 7882)

    def test_sync_does_not_evict_a_foreign_forward(self):
        cfg = self._cfg()
        removed = []
        with unittest.mock.patch.object(
                companion, "remove_forward",
                side_effect=lambda p, *a, **k: removed.append(p)):
            with unittest.mock.patch.object(companion, "negotiate", return_value=7878):
                with unittest.mock.patch.object(
                        companion, "call",
                        side_effect=companion.CompanionError("x")):
                    companion.sync(cfg, apply=False)
            with companion._fwd_lock:
                companion._owned_forwards.discard(7878)   # owned elsewhere
            with unittest.mock.patch.object(companion, "negotiate", return_value=7878):
                with unittest.mock.patch.object(
                        companion, "call",
                        side_effect=companion.CompanionError("x")):
                    companion.sync(cfg, apply=False)
            self.assertEqual(removed, [],
                             "sync evicted a forward it did not own")


    def test_successful_sync_does_not_evict_a_foreign_forward(self):
        """The tail release (after cameras/status succeed) is the line that
        runs on the NORMAL path. Forcing call to raise only exercised the
        early-return branch, so this drives a fully successful sync."""
        cfg = self._cfg()
        removed = []
        with companion._fwd_lock:
            companion._owned_forwards.discard(7878)   # owned by another process
        with unittest.mock.patch.object(
                companion, "remove_forward",
                side_effect=lambda p, *a, **k: removed.append(p)):
            with unittest.mock.patch.object(companion, "negotiate", return_value=7878):
                with unittest.mock.patch.object(
                        companion, "cameras", return_value=[{"id": "0"}]):
                    with unittest.mock.patch.object(
                            companion, "status", return_value={"model": "m"}):
                        summary = companion.sync(cfg, apply=False)
        self.assertTrue(summary["reachable"], "sync should have succeeded")
        self.assertEqual(removed, [],
                         "successful sync evicted a forward it did not own")

    def test_successful_sync_releases_a_forward_it_did_own(self):
        cfg = self._cfg()
        removed = []
        with unittest.mock.patch.object(
                companion, "remove_forward",
                side_effect=lambda p, *a, **k: removed.append(p)):
            with unittest.mock.patch.object(companion, "negotiate", return_value=7878):
                # negotiate -> forward_owned marks it ours.
                with unittest.mock.patch.object(companion, "forward_owned"):
                    with companion._fwd_lock:
                        companion._owned_forwards.add(7878)
                    with unittest.mock.patch.object(
                            companion, "cameras", return_value=[{"id": "0"}]):
                        with unittest.mock.patch.object(
                                companion, "status", return_value={"model": "m"}):
                            companion.sync(cfg, apply=False)
        self.assertEqual(removed, [7878], "must clean up its own forward")


if __name__ == "__main__":
    unittest.main(verbosity=2)
