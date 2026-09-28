#!/usr/bin/env bash
#
# run_tests.sh: the Project 2 test suite. It builds your runtime and checks every
# isolation property the autograder checks; there are no hidden tests. It prints
# PASS or FAIL per requirement (no points; the autograder assigns those).
#
# Run as ROOT (namespaces + cgroup v2 delegation need it):
#   sudo ./run_tests.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo ./run_tests.sh"; exit 2; }
exec python3 "$HERE/run_tests.py"
