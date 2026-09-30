"""The trust-file gate must match the FIRST FIELD of each line, not the whole line.

adb_keys lines carry a "user@host" comment. Ours is 715 chars where the bare key
is 700. So `grep -qxF '<700ch>'` is a whole-line match against a 715-char line
and can never succeed.

Measured on the phone at 13:54:
    gate: cachedKey=700ch fileBytes=715ch cachedHasSpace=false -> NO
while adb devices said `device` and our key was in authorized_adb_keys.

This asserts the two shell forms against a real adbkey.pub line: the old
whole-line form must fail, the awk first-field form must match. Same idiom the
settings store already used at MainActivity.java:852 (`tr ' ' '\\n' | grep -qxF`).
"""
import pathlib
import subprocess
import sys
import tempfile

pub = pathlib.Path.home().joinpath(".android/adbkey.pub")
if not pub.exists():
    print("SKIP: no ~/.android/adbkey.pub on this machine")
    sys.exit(0)

full = pub.read_text().strip()          # what provision() receives: 715 chars
bare = full.split()[0]                  # what it caches: 700 chars
print(f"adbkey.pub line = {len(full)} chars; bare key = {len(bare)} chars; "
      f"comment = {full.split()[1]!r}")

with tempfile.NamedTemporaryFile("w", suffix=".adbkeys", delete=False) as fh:
    fh.write(full + "\n")
    keys = fh.name

try:
    def sh(cmd):
        return subprocess.run(["bash", "-c", cmd], capture_output=True,
                              text=True).stdout.strip()

    old_whole_line = sh(f"grep -qxF '{bare}' {keys} && echo YES || echo NO")
    new_first_field = sh(f"awk '{{print $1}}' {keys} | grep -qxF '{bare}' && echo YES || echo NO")

    print(f"  whole-line  grep -qxF '700ch' -> {old_whole_line}   <- the bug")
    print(f"  first-field awk | grep -qxF    -> {new_first_field}   <- the fix")

    assert old_whole_line == "NO", \
        "if the whole-line form matches, the 13:54 diagnosis was wrong"
    assert new_first_field == "YES", "first-field form must match a commented line"

    # negative control: a foreign key must still be rejected
    other = "AAAAC3NzaC1lZDI1NTE5AAAAIGotherHostKey000000000000000000"
    reject = sh(f"awk '{{print $1}}' {keys} | grep -qxF '{other}' && echo YES || echo NO")
    assert reject == "NO", "must not match a key that is not ours"
    print(f"  first-field, foreign key      -> {reject}   (negative control)")

    print("\nGATE OK: first-field matching succeeds on a commented line where "
          "whole-line matching cannot, and still rejects other hosts' keys.")
finally:
    pathlib.Path(keys).unlink(missing_ok=True)
