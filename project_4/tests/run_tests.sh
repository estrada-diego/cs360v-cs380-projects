#!/usr/bin/env bash
#
# run_tests.sh: the Project 4 test suite. It starts runner backends and your
# platform and drives the control plane, running every check the autograder runs;
# there are no hidden tests. Prints PASS or FAIL per requirement (no points; the
# autograder assigns those). Needs gcc + python3.
#
#   ./run_tests.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/run_tests.py"
