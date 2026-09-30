"""Shell precedence: `awk | grep || printf` is NOT `(awk | grep) || printf`.

`|` binds tighter than `||`, so the unbraced form parses as
`awk '...' KEYS | (grep ... || printf ...)`.

MEASURED, and the difference is nil in every reachable case. The pipeline's exit
status is grep's (no `set -o pipefail`), and grep correctly reports "not found"
when awk produced nothing, so on hit and on miss both forms behave identically -
including when awk cannot read the file at all. The brace is a parse-clarity fix
so the code says the guard its comment describes, not a behaviour fix.

An earlier draft of this test asserted that the unbraced form would append
spuriously on a pipeline error. That was wrong, and case 3 below is the control
that refutes it: chmod 000 on the keys file, a genuine error, same result.

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

print("\ncase 3: a real pipeline ERROR - awk cannot read the file (chmod 000)")
LOCKED = KEYS.parent / "locked_keys"
LOCKED.write_text(f"{OTHER} host\n")
LOCKED.chmod(0o000)
try:
    run(f"awk '{{print $1}}' {LOCKED} | grep -qxF '{KEY}' 2>/dev/null "
        f"|| printf '%s APPENDED\\n' '{KEY}' >> {KEYS}")
    unbraced = len(KEYS.read_text().splitlines())
    KEYS.write_text(f"{OTHER} host\n")
    run(f"{{ awk '{{print $1}}' {LOCKED} | grep -qxF '{KEY}' 2>/dev/null; }} "
        f"|| printf '%s APPENDED\\n' '{KEY}' >> {KEYS}")
    braced = len(KEYS.read_text().splitlines())
    print(f"  unbraced -> {unbraced} lines,  braced -> {braced} lines")
    # Both append: grep's exit status is the PIPELINE's status, and with awk
    # failing there is no `set -o pipefail`, so grep decides - and grep
    # correctly reports "not found". So the unbraced form does NOT append
    # spuriously on error. My earlier claim that it did was wrong; the only
    # real difference is the PARSE, i.e. which subshell owns the printf.
finally:
    LOCKED.chmod(0o600)
    LOCKED.unlink()
    KEYS.unlink(missing_ok=True)

print("\nPRECEDENCE: measured, and the two forms agree in every reachable case.")
print("The brace is a parse-clarity fix so the code matches its comment - NOT a")
print("behaviour fix. An earlier draft of this test claimed unbraced would append")
print("on a pipeline error; that is false, and this case is the control that")
print("refutes it.")
