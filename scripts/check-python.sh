#!/usr/bin/env bash
# Byte-compile every host-side module, then run the test suites.
#
# Why this exists: commit 32904a6 re-indented serve_key and broke backend.py,
# and it reached a commit because nothing compiled the file. An IndentationError
# there kills the WHOLE host app on import - "Let phone in" included - so it
# must fail loudly at the point of the edit, not at the point of use.
#
# The tests are here for what py_compile structurally cannot see: a float
# compared against None (a runtime failure, after the socket has already bound),
# a whole-line match against a commented adb_keys line, and the 13:15 count-gate
# bug where a foreign key faked a trusted phone. They are cheap and need no
# fixtures, so a commit should not be able to skip them.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

status=0

while IFS= read -r f; do
    if out=$(python3 -m py_compile "$f" 2>&1); then
        echo "ok    $f"
    else
        echo "FAIL  $f"
        echo "$out" | sed 's/^/      /'
        status=1
    fi
done < <(find src -name '*.py' | sort)

shopt -s nullglob
for t in tests/test_*.py; do
    if out=$(timeout 300 python3 "$t" 2>&1); then
        echo "ok    $t"
    else
        echo "FAIL  $t"
        echo "$out" | tail -8 | sed 's/^/      /'
        status=1
    fi
done

exit "$status"
