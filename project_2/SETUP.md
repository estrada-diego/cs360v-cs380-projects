# Project 2: Build, Run & Test

You implement **`runtime/container.c`** (eleven functions); everything else is
provided. Read [SPEC.md](SPEC.md) first.

You do all of Project 2 inside the Ubuntu VM from Project 0, over SSH. Run everything
as **root** in the VM: namespaces and cgroup v2 delegation need it. The container
itself is still rootless, through its user namespace.

If you haven't set that VM up, do it now: follow the QEMU-VM part of
[Project 0's SETUP.md](../project_0/SETUP.md), then clone this repo in the VM if it
isn't there already and run `sudo project_0/setup/setup-vm.sh` once to install the
compiler, busybox and iproute2. Work from `<repo>/project_2`.

## Building

Everything below runs from `<repo>/project_2`.

```bash
make -C runtime             # -> runtime/container
```

`make -C runtime clean` removes the build. To build against a `container.c`
elsewhere, use `make -C runtime clean && make -C runtime SRC=/path/to/container.c`.

## The root filesystem

The container pivots into a **root filesystem** you point it at with `--rootfs`.
Build one from the VM's static busybox:

```bash
./make-rootfs.sh ./rootfs   # a bin/busybox tree with /proc /dev /tmp mount points
```

The command you run is a path **inside** that rootfs (for example `/bin/busybox`
or `/bin/sh`), not a host path.

## Running

```text
container [--name N] [--hostname H] [--rootfs DIR] [--pids-max P]
          [--mem-max BYTES] [--cgroup-base DIR] [--net] -- <command> [args...]
```

The command runs inside the container and the runtime exits with the command's
exit status.

```bash
sudo ./runtime/container --hostname box --pids-max 64 --mem-max 128M \
     --rootfs ./rootfs -- /bin/busybox sh
echo $?     # the command's exit status
```

That command gives you an interactive shell inside the container; type `exit` to
leave it.

`--mem-max` accepts a plain byte count or a `k`/`m`/`g` suffix (or `max` for no
limit); `--pids-max` is a count (or `max`).

When a check fails, run `/bin/busybox sh` as the command to see what the
container looks like from the inside. Each row holds once the feature it names
works, so the rows that fail tell you what is still missing:

| Run this inside | Expect | Feature |
|---|---|---|
| `echo $$` | `2`: your init is PID 1, the command is PID 2 | the init |
| `id` | `uid=0`, mapped to your real user outside | user namespace |
| `hostname` | `box` | UTS namespace |
| `ls /` | `bin dev etc proc tmp` and nothing else | the pivot |
| `ps` | only the container's own processes | your own `/proc` |
| `ls /dev` | `null` and `zero` | `/dev` |
| `touch /foo` | fails: read-only file system | read-only root |
| `touch /tmp/foo` | succeeds | writable `/tmp` |
| `grep Cap /proc/self/status` | `CapBnd` and `CapEff` all zeros | capabilities |

Once you have dropped capabilities and installed the seccomp filter, this shell
inherits both, so commands that need privilege start failing inside it (
`mount` returns `EPERM` for example). That is the sandbox working, not a broken shell.

### Networking with `--net`

By default the container has only loopback. With `--net` the runtime attaches it
to a host bridge over a veth pair, so the host can reach a server inside it. The
host side needs iproute2, since `net.c` builds the bridge and veth by calling
`ip`.

`make-rootfs.sh` also builds `runner/runner.c` into `rootfs/bin/runner`, a small
server for checking it. Start it in a `--net` container and
reach it from the host at `10.44.0.2`:

```bash
sudo ./runtime/container --rootfs ./rootfs --net -- /bin/runner 8080 &
printf 'PING\n' | nc 10.44.0.2 8080     # PONG
```

## Testing

```bash
sudo ./tests/run_tests.sh
```

It builds your runtime, builds a rootfs, runs the provided `observer` payload
inside a container, and prints PASS or FAIL for each property. These are the
same checks the grader uses; there are no hidden tests.

The shipped stub's `container_run()` returns 1, so nothing runs yet. Start by
getting the command to execute. That takes `container_run` and `container_init`
to spawn it, plus `container_setup` to pivot into the rootfs, because the command
lives inside the rootfs. Then add the remaining features in SPEC order; each one
turns on more checks.
