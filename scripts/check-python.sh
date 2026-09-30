#!/usr/bin/env bash
# Byte-compile every host-side module before anything is committed.
#
# Why this exists: commit 32904a6 re-indented serve_key and broke
# backend.py, and it reached a commit because nothing compiled the file. An
# IndentationError there kills the WHOLE host app on import - "Let phone in"
# included - so it must fail loudly at the point of the edit, not silently at
# the point of use. py_compile also will not catch a bad runtime path (a
# float compared against None), which is why tests/test_serve_key_modes.py
# exercises the None path for real.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

status=0
for f in $(find src -name '*.py' | sort); do
    if ! out=$(python3 -m py_compile "$f" 2>&1); then
        echo "FAIL  $f"
        echo "$out" | sed 's/^/      /'
        status=1
    else
        echo "ok    $f"
    fi
done < <(find src -name '*.py' | sort)

exit "$status"
