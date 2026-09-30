"""The fast path must recognise OUR key, not merely A key.

countKeys() is `grep -c .` on the trust file: it counts LINES. So a file
holding only some other host's key satisfied keys > 0, the fast path took the
"already trusted" branch, and the app advertised readiness while adbd was
refusing us. Observed 13:15: the log said keys=1 and "manager key: 1/1
present", yet `adb devices` said `unauthorized` and the host's 7911 server
logged zero requests. A count cannot tell us whose key it is.

This mirrors the phone's own gate - trustFileHasHostKey() does an exact
`grep -qxF` against our key. Uses the real ~/.android/adbkey.pub, because a
stub hides length sensitivity: an early run of this test with a 40-char stub
FAILED its own positive control.
"""
import base64
import pathlib
import sys


def _load_host_key() -> str:
    """Our host key, or a synthetic stand-in if this machine has none.

    Reading it at module level raises FileNotFoundError during collection on
    any clone without an adb key - the portability bug already fixed in the
    sibling test. A ~700-char base64 blob stands in: length is what the
    stub-vs-real failure was about, and no assertion depends on the bytes.
    """
    pub = pathlib.Path.home() / ".android" / "adbkey.pub"
    if pub.exists():
        return pub.read_text().split()[0]
    return base64.b64encode(b"0" * 520).decode()


OUR = _load_host_key()
OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIGotherHostKey000000000000000000"


def exact_gate(keys_file: str, ours: str) -> bool:
    """Mirror of trustFileHasHostKey(): exact key match, not a count."""
    return any(line.split()[0] == ours
               for line in keys_file.splitlines() if line.strip())


def old_gate(keys_file: str) -> bool:
    """Mirror of the bug: countKeys() > 0."""
    return len([l for l in keys_file.splitlines() if l.strip()]) > 0


def test_foreign_key_is_not_our_key():
    """The 13:15 case: a non-empty file that does NOT contain our key."""
    foreign = OTHER + " someuser@host"
    assert old_gate(foreign) is True, "old gate accepts this - that IS the bug"
    assert exact_gate(foreign, OUR) is False, \
        "exact gate must reject a file holding only another host's key"


def test_our_key_among_others_is_accepted():
    """Positive control: the real key really is in the file."""
    mine = OUR + " me@host\n" + OTHER + " other@host"
    assert exact_gate(mine, OUR) is True


def test_empty_file_rejected_by_both():
    assert exact_gate("", OUR) is False
    assert old_gate("") is False


def test_near_miss_rejected():
    """One character different must not pass."""
    assert exact_gate(OUR[:-1] + "X me@host", OUR) is False


def test_substring_rejected():
    """Our key embedded in a longer line must not match."""
    assert exact_gate("X" + OUR + " me@host", OUR) is False


def test_commented_adb_keys_line_matches():
    """A real adb_keys line carries a "user@host" comment.

    This is the 13:54/15:59 case: the line is 715 chars where the bare key is
    700, so a whole-line comparison could never succeed and the gate reported
    absent on a store adbd was accepting. The first-field form must match it.
    """
    commented = OUR + " ishanp@omarchy"
    assert len(commented) > len(OUR), "the fixture must actually differ from the bare key"
    assert exact_gate(commented, OUR) is True, \
        "a commented adb_keys line must still match our bare key"


def test_whole_line_form_would_fail_on_a_commented_line():
    """Why first-field matching was needed: the whole-line form fails here.

    Modelled explicitly rather than via exact_gate, which splits on whitespace
    and so could never reproduce the failure.
    """
    commented = OUR + " ishanp@omarchy"

    def whole_line(keys_file: str, ours: str) -> bool:
        return any(line == ours for line in keys_file.splitlines() if line.strip())

    assert whole_line(commented, OUR) is False, \
        "this is the bug: whole-line matching cannot see a commented key"
    assert exact_gate(commented, OUR) is True, "first-field matching can"


if __name__ == "__main__":
    failures = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures += 1
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(tests) - failures} passed")
    sys.exit(1 if failures else 0)
