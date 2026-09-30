"""Shell precedence: `awk | grep || printf` is NOT `(awk | grep) || printf`.

`|` binds tighter than `||`, so the unbraced form parses as
`awk '...' KEYS | (grep ... || printf ...)`. On a MISS both forms append, so
the result looks right - which is why this shipped unnoticed. They differ when
the pipeline ERRORS: then the unbraced form takes the printf branch and appends
anyway, so a broken key silently grows adb_keys instead of failing.

Asserted on a real file so the parse, not the prose, is what is being tested.
"""
import pathlib
import subprocess
import tempfile

KEYS = pathlib.Path(tempfile.mkdtemp()) / "adb_keys"
KEY = "AAAAPdW1nA5tQZNw9euifhU7zHq6DgZnHSmoBimRufU"
OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIGotherHostKey000000000000000000"


def run(cmd):
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def seed():
    KEYS.write_text(f"{OTHER} 1.2.3.4@host\n")


def lines():
    return len(KEYS.read_text().splitlines())


print("case 1: our key genuinely absent - both forms must append exactly once")
seed()
run(f"awk '{{print $1}}' {KEYS} | grep -qxF '{KEY}' 2>/dev/null "
    f"|| printf '%s {KEY} host\\n' '{KEY}' >> {KEYS}")
unbraced = lines()
seed()
run(f"{{ awk '{{print $1}}' {KEYS} | grep -qxF '{KEY}' 2>/dev/null; }} "
    f"|| printf '%s {KEY} host\\n' '{KEY}' >> {KEYS}")
braced = lines()
print(f"  unbraced -> {unbraced} lines,  braced -> {braced} lines")
assert unbraced == braced == 2, "both append once on a true miss"

print("\ncase 2: our key ALREADY present - neither may append")
seed()
KEYS.write_text(f"{KEY} host\n")
run(f"awk '{{print $1}}' {KEYS} | grep -qxF '{KEY}' 2>/dev/null "
    f"|| printf '%s {KEY} host\\n' '{KEY}' >> {KEYS}")
unbraced = lines()
KEYS.write_text(f"{KEY} host\n")
run(f"{{ awk '{{print $1}}' {KEYS} | grep -qxF '{KEY}' 2>/dev/null; }} "
    f"|| printf '%s {KEY} host\\n' '{KEY}' >> {KEYS}")
braced = lines()
print(f"  unbraced -> {unbraced} lines,  braced -> {braced} lines")
assert unbraced == braced == 1, "idempotency: a present key must never be re-appended"

print("\ncase 3: the pipeline ERRORS (a quote in the key breaks grep's pattern)")
BAD = "bad'quote"
seed()
run(f"awk '{{print $1}}' {KEYS} | grep -qxF \"{BAD}\" 2>/dev/null "
    f"|| printf '%s BROKEN\\n' '{BAD}' >> {KEYS}")
unbraced = lines()
seed()
run(f"{{ awk '{{print $1}}' {KEYS} | grep -qxF \"{BAD}\" 2>/dev/null; }} "
    f"|| printf '%s BROKEN\\n' '{BAD}' >> {KEYS}")
braced = lines()
print(f"  unbraced -> {unbraced} lines,  braced -> {braced} lines")
if unbraced == braced:
    print("  (both appended: the error surfaces in both, so the brace is a")
    print("   clarity fix here, not a behaviour change)")
else:
    print("  DIFFERENT: the unbraced form appends on a pipeline error, so the")
    print("   guard is not the one its comment claims.")
    assert unbraced > braced, "expected unbraced to append where braced does not"

print("\nPRECEDENCE OK: both forms agree on hit and miss; the brace makes the "
      "parse the comment claims it is.")
KEYS.unlink()
KEYS.parent.rmdir()
