"""serve_key must survive BOTH of its timeout policies.

max_wait=None is the mode the operator is told to use, and the failure it
guards against is a runtime TypeError, not an import error: the wait loop
compares time.monotonic() < deadline, so a None deadline raises AFTER the
socket is bound. py_compile passes on exactly that code, which is how commit
32904a6 shipped a host app that raised on import and then would have shipped
one that dies on start.

Runs both modes for real. Loopback and a throwaway port: this asserts the
lifetime policy, not LAN reachability.
"""
import socket
import sys
import threading
import time

sys.path.insert(0, "/home/ishanp/Documents/github/my-projects/android-webcam/src")
from android_webcam.backend import serve_key  # noqa: E402

HOST = "127.0.0.1"
PORT = 7977  # throwaway: never collides with the real 7911


def run(max_wait, settle=0.4):
    """Serve in a thread; return (thread, listening, raised_exception)."""
    err: dict = {}

    def go():
        try:
            serve_key(host=HOST, port=PORT, advertise=False,
                      once=False, grace=0, max_wait=max_wait)
        except BaseException as e:  # noqa: BLE001 - we assert on this
            err["e"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(settle)
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((HOST, PORT))
        up = True
    except OSError:
        up = False
    finally:
        s.close()
    return t, up, err.get("e")


def test_bounded_mode_listens_then_stops():
    """A finite deadline must actually bound the server."""
    t, up, err = run(2)
    assert up, "bounded mode must listen before its deadline"
    assert err is None, f"bounded mode raised {err!r}"
    time.sleep(3.0)
    assert not t.is_alive(), "bounded mode must stop at its deadline"


def test_forever_mode_never_evaluates_float_lt_none():
    """The TypeError guard: with deadline None, float < None must not happen."""
    t, up, err = run(None)
    assert up, "forever mode must listen"
    assert err is None, f"forever mode raised {err!r} - this is the guard's whole point"
    time.sleep(3.0)
    assert t.is_alive(), "forever mode must still be alive with no deadline"
    # Leave nothing bound to PORT for a later real run.
    _force_close(t)


def _force_close(t: threading.Thread) -> None:
    """serve_key has no stop handle; interrupt the thread's wait."""
    try:
        import ctypes
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(t.ident), ctypes.py_object(SystemExit))
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass
    t.join(timeout=3)
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((HOST, PORT))
        listening = True
    except OSError:
        listening = False
    finally:
        s.close()
    assert not listening, f"test left a server bound to {PORT}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted((k, v) for k, v in globals().items()
                           if k.startswith("test_")):
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures += 1
    print(f"\n{'FAILED' if failures else 'PASSED'}: "
          f"{len([k for k in globals() if k.startswith('test_')]) - failures} passed")
    sys.exit(1 if failures else 0)
