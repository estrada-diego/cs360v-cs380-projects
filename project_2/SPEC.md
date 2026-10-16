# Project 2: Specification

In this assignment, you work on a single file, `runtime/container.c`. This document specifies the contract to implement.

`main.c` parses the command line and calls `container_run()`. The struct and the declarations live in `container.h`; do not change that header.

The command you run lives **inside the root filesystem** (the `--rootfs`
directory), which `make-rootfs.sh` builds from the VM's statically linked
busybox. So three things have to work before anything runs: the namespaces, the
spawn, and the pivot into the rootfs. Once the command runs, you can add
capabilities, the cgroup and teardown one at a time.

## The lifecycle

Below is what `container_run()` calls. You implement the functions to create a functional container environment.

Note: "parent" is the runtime process, "child" is the container's init.

```text
  container_run(c):
    container_cgroup_init(c)                 # Part V: make the cgroup + limits
    pipe(c->sync)                            # used to release the child
    clone(child_entry, stack + CONTAINER_STACK_SIZE,                 # Part I
          container_namespaces() | SIGCHLD, c)
        child_entry -> container_init(c):     # runs as the container's PID 1
            wait on c->sync (parent releases us)
            container_setup(c)                # Parts I/II/III: host, net, fs, caps, seccomp
            fork(); the child execs c->argv;  # Part II: run the command
            reap children; return the command's exit status   # Part IV
    container_write_idmaps(c, child)         # Part I: uid/gid maps
    container_cgroup_enter(c, child)         # Part V: put the child in the cgroup
    if c->net_enabled: container_net_host_setup(c, child)  # Part I (--net, provided)
    release the child through c->sync
    waitpid(child)
    if c->net_enabled: container_net_host_teardown(c)      # Part I (--net, provided)
    container_cleanup(c)                     # Part VI
    return the command's exit status
```

Because the child is created in a fresh **user namespace**, the container's
"root" (uid 0) is your ordinary user outside it, so you never need real root
inside the container.

The functions you implement, and the part that specifies each:

| Function | Role | Part |
|---|---|---|
| `container_namespaces()` | the `CLONE_NEW*` flags to unshare | I |
| `container_write_idmaps()` | map container-root to your user | I |
| `container_network()` | bring up the container's loopback interface | I |
| `container_net_config()` | configure the veth (`--net` mode only) | I |
| `container_setup()` | hostname, network, filesystem, capabilities, seccomp | I / II / III |
| `container_seccomp()` | install the syscall filter | III |
| `container_init()` | the reaping init: launch + reap the command | IV |
| `container_run()` | the whole lifecycle | I..VI |
| `container_cgroup_init()` | create the cgroup, apply limits | V |
| `container_cgroup_enter()` | put the child in the cgroup | V |
| `container_cleanup()` | remove the cgroup | VI |

---

## Part I: Namespaces

A namespace gives a process its own copy of a resource the kernel otherwise shares. You give the
container its own set so it cannot see or touch the host's.

**`container_namespaces()`** returns the bitwise OR of the `CLONE_NEW*` flags for
the namespaces to unshare (see `clone(2)` / `sched.h`):

- `CLONE_NEWUSER`: a **user** namespace. Container uid/gid 0 maps to your real
  (unprivileged) id outside. This is what lets an ordinary user create the other
  namespaces and drop capabilities.
- `CLONE_NEWPID`: a **PID** namespace. Your init becomes **PID 1** and, once it
  has its own `/proc` (Part II), sees only container processes.
- `CLONE_NEWNS`: a **mount** namespace: a private mount table, so the mounts you
  make in Part II do not appear on the host. (`pivot_root` needs this.)
- `CLONE_NEWUTS`: a **UTS** namespace: its own hostname.
- `CLONE_NEWNET`: a **network** namespace: its own network stack, isolated from
  the host's interfaces (it starts with only a down loopback).

**`container_write_idmaps(c, child)`**: inside a new user namespace, the child
has no user or group id until the parent gives it one, and it can do almost
nothing without one. You give it one by writing an *id map*: a line of the form
`<id inside> <id outside> <how many ids>`.

From the parent, write these three files with the provided `write_file()`, in
this order:

1. `/proc/<child>/uid_map` <- `"0 <your-uid> 1"`, where `<your-uid>` is
   `getuid()`. This makes user 0 (root) inside the container the same user as
   you outside;
2. `/proc/<child>/setgroups` <- `"deny"`. This has to come before `gid_map`;
3. `/proc/<child>/gid_map` <- `"0 <your-gid> 1"`, where `<your-gid>` is
   `getgid()`.

**Hostname**: inside `container_setup()` (which runs as the init), set the
hostname to `c->hostname` with `sethostname(2)`.

**Loopback**: the container starts with one network interface, `lo` (loopback,
which `localhost` uses), and it is switched off. `container_network()` switches
it on.

Open a socket with `socket(AF_INET, SOCK_DGRAM, 0)`. Then, with a
`struct ifreq` whose `ifr_name` is `"lo"`:

1. read its flags: `ioctl(SIOCGIFFLAGS)`;
2. add `IFF_UP | IFF_RUNNING` to them;
3. write them back: `ioctl(SIOCSIFFLAGS)`.

If this fails, print a warning and return 0: the container still runs without
loopback.

In `container_setup()`, call `container_network()` before the Part III steps.
Switching `lo` on needs root's powers, and Part III takes them away.

**Connecting the container to the host (`--net`).** Only one test needs this:
"a server in the container is reachable over --net". All the other tests run
the container without `--net`, so you can leave this section until last.

Normally the container's only network is loopback. With `--net`, the host can
reach a server running inside the container. The provided code (`net.c`) gives
the container a network interface, `c->net_ifname`, that is connected to the
host:

```text
  host (10.44.0.1) <-----> ceth0 (10.44.0.2), inside the container
```

**Two functions in `net.c` are already written for you.** You only call them,
from `container_run()`, if `c->net_enabled` is set:

- `container_net_host_setup(c, child)` creates the interface. Call it after
  `container_cgroup_enter()` and before you release the child;
- `container_net_host_teardown(c)` cleans up the connection. Call it after
  `waitpid()`.

**You write `container_net_config()`.** It runs inside the container and sets up
the interface `c->net_ifname` (`ceth0`). This is the interface created by
`container_net_host_setup()` above.

Open a socket with `socket(AF_INET, SOCK_DGRAM, 0)`, the same as in
`container_network()`. Then make these `ioctl()` calls on it. Steps 1 to 3 each
take a `struct ifreq` whose `ifr_name` is `c->net_ifname`:

1. set its address to `c->net_ip`: `SIOCSIFADDR`;
2. set its netmask from `c->net_prefix`: `SIOCSIFNETMASK`;
3. switch it on: read its flags with `SIOCGIFFLAGS`, add `IFF_UP | IFF_RUNNING`,
   and write them back with `SIOCSIFFLAGS`, the same as in `container_network()`;
4. add a default route through the host, `c->net_gw`, so the container can reply
   to any address, not just `10.44.0.x`: fill a `struct rtentry` with gateway
   `c->net_gw` and flags `RTF_UP | RTF_GATEWAY`, then `SIOCADDRT`.

In `container_setup()`, if `c->net_enabled` is set, call
`container_net_config(c)` right after `container_network()`. Like loopback, it
needs root's powers, so it must come before the Part III steps.

---

## Part II: The root filesystem

The container gets its own root: the `--rootfs` directory (`c->rootfs`, a busybox
tree built by `make-rootfs.sh`), mounted **read-only**, with a writable `/tmp`,
its own `/dev`, and its own `/proc`. Build it in `container_setup()`, in this
order; each step depends on the one before it:

1. **Make mount propagation private** so your mounts do not leak back to the
   host: `mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL)`.
2. **Bind the rootfs onto itself** so it becomes a mount you can pivot into
   (`mount(c->rootfs, c->rootfs, NULL, MS_BIND | MS_REC, NULL)`), then **remount
   that bind read-only** (`MS_BIND | MS_REMOUNT | MS_RDONLY`): the container
   cannot modify its own image.
3. **Mount a writable `/tmp`.** The root is now read-only, so give the container
   an empty, writable filesystem in memory (a *tmpfs*) at `/tmp`:
   `mount("tmpfs", "<rootfs>/tmp", "tmpfs", 0, NULL)`.
4. **Set up `/dev`**. The container cannot create device files itself, so it
   borrows the host's `/dev/null` and `/dev/zero`:
   1. mount a tmpfs on `<rootfs>/dev`;
   2. create empty files `<rootfs>/dev/null` and `<rootfs>/dev/zero`:
      `open(path, O_CREAT | O_WRONLY, 0666)`, then `close()`. (The tmpfs hides
      the ones `make-rootfs.sh` made, and without them the bind below fails
      with `ENOENT`.);
   3. make each empty file show the host's device instead, with a *bind mount*:
      `mount("/dev/null", "<rootfs>/dev/null", NULL, MS_BIND, NULL)`, and the
      same for `zero`.
5. **Mount a fresh `/proc`**: `mount("proc", "<rootfs>/proc", "proc", 0, NULL)`.
   This is what lets the container see only its own processes. Do it **before**
   step 6: the kernel only allows it while the host's `/proc` is still visible,
   and fails with `EPERM` afterwards.
6. **Switch roots**. Until now, the container still sees the host's whole
   filesystem, and the rootfs is just one directory in it. This step makes the
   rootfs the container's `/` and takes the host's files out of its view:
   1. `chdir(c->rootfs)`: move into the rootfs;
   2. `syscall(SYS_pivot_root, ".", ".")`: make the current directory (the
      rootfs) the new `/`. The host's old `/` is still attached, on top of the
      new one. (There is no glibc `pivot_root()` function, so you call it
      through `syscall`.);
   3. `umount2(".", MNT_DETACH)`: remove the host's old `/`, so the container
      can no longer reach any host files;
   4. `chdir("/")`: move to the new `/`.

---

## Part III: Restricting the process (capabilities and seccomp)

Even as the container's "root", the command should not be able to do dangerous
things. Two mechanisms prevent that, both at the **end** of `container_setup()`,
so that the steps above still have the privileges and syscalls they need.

### Capabilities

A capability is one piece of root's power. Drop them all, in this order:

1. **Empty the list of capabilities the command is allowed to have.** Linux
   keeps a list of the capabilities a process, and any program it starts, may
   have. For every `cap` from 0 to `CAP_LAST_CAP`, call
   `prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)` to remove `cap` from that list. Do
   this before step 2: it needs root's powers, and step 2 takes them away.
2. **Drop the capabilities you hold now** with `capset`. glibc has no `capset()`
   function, so call `syscall(SYS_capset, &hdr, data)`, where:
   - `hdr` is a `struct __user_cap_header_struct` with version
     `_LINUX_CAPABILITY_VERSION_3`;
   - `data` is an array of **two** `struct __user_cap_data_struct`, all zero.

   Both are in `<linux/capability.h>`.
3. **Block setuid programs:** `prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)`, so running
   one cannot give capabilities back.

### Seccomp

Dropping capabilities stops root from doing privileged things. A **seccomp**
filter goes further: it blocks chosen system calls completely, for any user. The
kernel runs the filter every time the process makes a system call, and the
filter decides whether to allow it.

In `container_seccomp()`, install a filter that makes these system calls fail
with `EPERM` and allows everything else: `ptrace`, `mount`, `umount2`,
`pivot_root`, `chroot`, `setns`, `unshare`, `reboot`, `swapon`, `swapoff`,
`kexec_load`, `init_module`, `finit_module`, `delete_module`.

The filter is a small program: an array of `struct sock_filter` instructions,
which you write with the `BPF_STMT` and `BPF_JUMP` macros (`<linux/filter.h>`,
`<linux/seccomp.h>`). `man 2 seccomp` has an example. Your filter should:

1. load `seccomp_data.arch` (which CPU architecture the system call is for). If
   it is not this machine's, return `SECCOMP_RET_KILL_PROCESS`. Compare against
   `AUDIT_ARCH_X86_64` on x86-64 or `AUDIT_ARCH_AARCH64` on ARM
   (`<linux/audit.h>`);
2. load `seccomp_data.nr` (the system call number). For each system call in the
   list above, compare it against that call's number (`__NR_ptrace`,
   `__NR_mount`, ...), and return `SECCOMP_RET_ERRNO | EPERM` if it matches;
3. otherwise, return `SECCOMP_RET_ALLOW`.

To install it, put the array and its length in a `struct sock_fprog prog`, then
call `syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog)`. This only works
after `PR_SET_NO_NEW_PRIVS`, which the capabilities steps already set. The
command inherits the filter when your init starts it. `container_setup()` calls
this **last**.

---

## Part IV: The init (launch and reap)

The cloned child is **PID 1** in the container's PID namespace: it is the
container's **init**. It launches the command and reaps orphans. In `container_init()`:

1. **Wait for the parent.** Before you do anything, the parent has to write your
   id maps (Part I) and put you in the cgroup (Part V). It tells you it is done
   by writing one byte to the `c->sync` pipe. So:
   1. close `c->sync[1]`, the write end (only the parent writes);
   2. read one byte from `c->sync[0]`. This waits until the parent writes it;
   3. close `c->sync[0]`.

   Then call `container_setup(c)`.
2. **Start the command.** Call `fork()`. In the new process, call
   `execvp(c->argv[0], c->argv)` to run the command. The command becomes
   **PID 2**, and your init stays PID 1.

   Do not close any file descriptors you did not open yourself. The tests give
   the command an open file descriptor, and use it to keep the container
   running while they check it.
3. **Wait for the command to exit.** When a process exits, it stays in the
   process table as a *zombie* until its parent collects it with `waitpid()`.
   If a process's parent exits first, the kernel makes PID 1 (your init) its
   parent, so your init has to collect those processes too.

   Call `waitpid(-1, &st, 0)` in a loop. Each call collects one process that
   has exited. When the pid it returns is the command's (the pid `fork()`
   returned in step 2), stop and return the command's exit status:
   - `WEXITSTATUS(st)` if it exited normally;
   - `128 + WTERMSIG(st)` if a signal killed it.

   The container exits with the value you return.

---

## Part V: The cgroup (resource limits)

A cgroup (control group) is a group of processes that the kernel puts limits
on. Here, it limits how many processes the container can run and how much
memory it can use. Each cgroup is a directory under `/sys/fs/cgroup`, and you set
its limits by writing to files in that directory.

**`container_cgroup_init(c)`** (parent, before the child runs):

1. **Turn on the limits you need.** A new cgroup can only use the process and
   memory limits if its parent directory allows them. Write `"+pids +memory"` to
   `<cgroup_base>/cgroup.subtree_control`.
2. **Create the container's cgroup:** `mkdir` `<cgroup_base>/<name>`, and save
   that path in `c->cg_path` (cleanup needs it). If `mkdir` fails with `EEXIST`,
   carry on: a run that crashed can leave the directory behind, and the next run
   must still work.
3. **Set the limits** by writing to files in `c->cg_path`:
   - `pids.max` <- `c->pids_max`;
   - `memory.max` <- `c->mem_max`;
   - `memory.swap.max` <- `"0"`. Without this, a process that goes over the
     memory limit is moved to swap instead of being killed.

   If a limit is `-1`, write the string `"max"` (no limit) instead.

**`container_cgroup_enter(c, child)`** (parent, after `clone`): write the child's
pid to `<cg_path>/cgroup.procs`. Every process the child starts later is put in
the same cgroup, so the limits cover the whole container.

---

## Part VI: Teardown

**`container_cleanup(c)`** (parent, after `waitpid()` returns): remove the
cgroup directory with `rmdir(c->cg_path)`. If it fails with `ENOENT`, the
directory is already gone, so ignore it.

That is the only cleanup you need. The mounts from Part II disappear on their
own when the container exits.

---
