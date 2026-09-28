#!/usr/bin/env bash
#
# make-rootfs.sh — build a tiny root filesystem for the container to run in.
#
# Your runtime pivots into a root filesystem (the --rootfs directory) instead of
# an empty tmpfs, so the container needs a populated tree with at least a shell
# and the mount points your runtime expects (/proc, /dev, /tmp). This builds one
# from the VM's statically linked busybox: one binary, symlinked to
# the usual command names.
#
#   ./make-rootfs.sh [DIR]      # default DIR: ./rootfs
#
# Run your container against it:
#   sudo ./runtime/container --rootfs ./rootfs -- /bin/busybox sh
set -euo pipefail

ROOT="${1:-./rootfs}"
BB="$(command -v busybox || true)"
[ -n "$BB" ] || { echo "busybox not found (install busybox-static)"; exit 1; }

rm -rf "$ROOT"
mkdir -p "$ROOT"/bin "$ROOT"/proc "$ROOT"/dev "$ROOT"/tmp "$ROOT"/etc

cp "$BB" "$ROOT/bin/busybox"
# The usual command names, all served by the one static busybox.
for cmd in sh ls cat echo id hostname ps grep head printenv sleep mount env true false; do
    ln -sf busybox "$ROOT/bin/$cmd"
done

# Mount-point targets your runtime binds real device nodes onto (it cannot
# mknod(2) in a user namespace). Empty files are fine; the bind covers them.
: > "$ROOT/dev/null"
: > "$ROOT/dev/zero"

# The course function runner, if it is present. Build it STATICALLY (the rootfs
# has no shared libc) so the container can run it as a Project 4 backend: a server
# reachable over the network with `--net`. See SETUP.md.
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/runner/runner.c" ]; then
    if gcc -O2 -static -o "$ROOT/bin/runner" "$HERE/runner/runner.c" 2>/dev/null; then
        echo "  + built the function runner into $ROOT/bin/runner"
    else
        echo "  ! could not static-build the runner (need gcc + static libc); skipping"
    fi
fi

echo "built rootfs at $ROOT (busybox: $BB)"
