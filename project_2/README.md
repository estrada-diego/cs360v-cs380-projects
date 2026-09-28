# Project 2: A Minimal Container Runtime

You write a program that runs a command inside a **container**, using the same
Linux features `docker run` uses. Linux has no single container feature. You
assemble a container from several:

- **namespaces** for the process's own view of PIDs, hostname, mounts, users, and network;
- an **init** process (PID 1) that launches the command and reaps orphans;
- a **root filesystem** you pivot into, read-only, with its own `/proc`, `/dev`,
  and a writable `/tmp`;
- a **cgroup** to cap its processes and memory;
- dropped **capabilities** and a **seccomp** filter, so its "root" is powerless;
- a clean **teardown** so nothing leaks.

`main.c` does almost nothing: it parses the command line and calls
`container_run()`. **You** drive the whole lifecycle in `runtime/container.c` --
the `clone()` and its stack, the parent/child sync pipe, the reaping init, the
cgroup, the pivot into the rootfs, and the teardown.

- **[SPEC.md](SPEC.md)**: the contract. Read it first; it covers the parts in
  the order to build them.
- **[SETUP.md](SETUP.md)**: how to build, run, and test.

Everything runs as **root** in the Ubuntu VM from Project 0, because creating
cgroups and some mounts needs it. The container does not hold real privilege:
through a user namespace, its root maps to an ordinary user on the host.

---

## What you implement

Eleven functions in `runtime/container.c`. `main.c` calls only `container_run()`;
your own code calls the rest. [SPEC.md](SPEC.md) lists them and says what each
must do.

`container_run()` and `container_init()` have to work before anything runs.
`container_setup()` is the longest of the eleven.

## Layout

```text
.
├── SPEC.md                # the spec, in parts (READ FIRST)
├── README.md              # this file
├── SETUP.md               # how to build, run, and test
├── make-rootfs.sh         # builds the busybox root filesystem the container runs in
├── runner/
│   └── runner.c           # the course function runner (provided; run it with --net)
├── runtime/
│   ├── container.c        # *** YOU IMPLEMENT: the eleven functions ***
│   ├── container.h        # the struct + declarations (provided)
│   ├── main.c             # parses argv, calls container_run() (provided)
│   ├── util.c             # write_file() (provided)
│   ├── net.c              # --net host side: veth/bridge (provided)
│   └── Makefile
└── tests/
    ├── run_tests.sh       # the test suite (runs run_tests.py)
    ├── run_tests.py       # the checks themselves, and their thresholds
    └── observer.c         # a static binary the tests run inside the container
```

---

## Where to look for help

You are not expected to know any of these syscalls already; the SPEC names the
exact one for each step.

**Worth reading before you start:**

| | |
|---|---|
| `man 2 clone`, `man 7 namespaces` | the child stack, and what each `CLONE_NEW*` flag isolates. |
| `man 7 user_namespaces` | the `uid_map` / `setgroups` / `gid_map` rules. |
| `man 2 pivot_root`, `man 2 mount` | switching the root, the read-only remount, and mounting `/proc`. |
| `man 7 netdevice` | bringing an interface up (`SIOCGIFFLAGS`), addresses and routes (`SIOCSIFADDR`, `SIOCADDRT`). |
| `man 2 wait` | reaping children as PID 1. |
| `man 7 capabilities`, `man 2 prctl` | dropping the bounding set + `capset`. |
| `man 2 seccomp` | the seccomp-BPF filter (`SECCOMP_SET_MODE_FILTER`). |
| `man 7 cgroups` | cgroup v2: `subtree_control`, `pids.max`, `memory.max`, `cgroup.procs`. |

---

## How to work

Run these from `<repo>/project_2`:

```bash
make -C runtime             # -> runtime/container, from your container.c
./make-rootfs.sh ./rootfs   # a busybox rootfs to run in
sudo ./tests/run_tests.sh   # the test suite: every check the grader runs
```

Build the parts in SPEC order and re-run `run_tests.sh` after each. Nothing runs
until the namespaces, the spawn, and the pivot are all in place (the command
lives inside the rootfs); after that, each layer you add turns on more checks.
