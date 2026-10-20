#!/usr/bin/env bash
# The gate: every test in this repo, run the way each one was written to run.
#
# Most of these are STANDALONE scripts - they call sys.exit() at the end and
# print their own "ok" lines - so pytest cannot collect them: its collector
# imports the module, the module exits, and pytest reports an INTERNALERROR
# rather than a failure. That is why the gate used to be `pytest` and why it
# exited 3 on a green tree.
#
# The style is DETECTED, not hardcoded: a test file that declares
# unittest.TestCase classes goes to pytest, and one that runs itself goes to
# the interpreter. A new test file is therefore gated the moment it lands,
# whichever style it is written in - hardcoding the list meant a new
# pytest-style test was run as a standalone script and passed vacuously.
#
# Every one of them exits non-zero on failure, so the loop is the gate.
set -uo pipefail
cd "$(dirname "$0")/.."

failed=0
ran=0
pytest_style=()
standalone=()

for t in tests/test_*.py; do
  [[ -f "$t" ]] || continue
  if grep -qE 'unittest\.TestCase|^import pytest|@pytest' "$t"; then
    pytest_style+=("$t")
  else
    standalone+=("$t")
  fi
done

for t in "${pytest_style[@]:-}"; do
  [[ -f "$t" ]] || continue
  ran=$((ran + 1))
  # The pipeline's exit code is tail's, not pytest's: pipefail is what makes
  # the failure visible. Without it a failed pytest reads as a successful
  # tail, and the gate passes on a red tree.
  if ! python3 -m pytest "$t" -q 2>&1 | tail -3; then
    echo "  FAILED: $t"
    failed=$((failed + 1))
  fi
done

for t in "${standalone[@]:-}"; do
  [[ -f "$t" ]] || continue
  ran=$((ran + 1))
  if ! python3 "$t" >/tmp/android-webcam-test.log 2>&1; then
    echo "  FAILED: $t"
    grep -E 'FAIL' /tmp/android-webcam-test.log | head -3
    failed=$((failed + 1))
  fi
done

if [[ $failed -gt 0 ]]; then
  echo "$failed of $ran test file(s) FAILED"
  exit 1
fi
echo "all $ran test file(s) passed"
