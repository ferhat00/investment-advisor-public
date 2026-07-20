#!/bin/sh
# Run the full test battery (POSIX). Pass --offline to skip the two live spot-checks.
here="$(cd "$(dirname "$0")" && pwd)"
export PYTHONIOENCODING=utf-8
fail=0
python3 "$here/test_math.py" || fail=1
python3 "$here/smoke_cli.py" "$@" || fail=1
[ "$fail" -eq 0 ] && echo "ALL TESTS PASSED" || { echo "TESTS FAILED"; exit 1; }
