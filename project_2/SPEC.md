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

**`container_write_idmaps(c, child)`**: a user namespace starts with an *empty*
uid/gid map, and the child can do almost nothing until you fill it. From the
parent, write (with the provided `write_file()`):

- `/proc/<child>/uid_map` <- `"0 <your-uid> 1"` (container id 0 -> your id, width 1);
- `/proc/<child>/setgroups` <- `"deny"` (**required** before you may write gid_map);
- `/proc/<child>/gid_map` <- `"0 <your-gid> 1"`.

**Hostname**: inside `container_setup()` (which runs as the init), set the
hostname to `c->hostname` with `sethostname(2)`.

**Loopback**: the fresh network namespace starts with only a `lo` interface, and
it is **down**. In `container_network()` bring it up so in-container localhost
works: open an `AF_INET` `SOCK_DGRAM` socket, fill a `struct ifreq` with
`ifr_name` `"lo"`, `ioctl(SIOCGIFFLAGS)` to read the flags, OR in
`IFF_UP | IFF_RUNNING`, and `ioctl(SIOCSIFFLAGS)` to set them. This needs
`CAP_NET_ADMIN` over the namespace, so `container_setup()` calls it **before**
dropping capabilities. It is best-effort (a container without loopback still
runs).

**Connecting the container to the host (`--net`).** By default the container is
network-isolated (only loopback). With `--net`, the runtime attaches it to the
host through a **veth pair on a bridge**, the same arrangement as Docker's
default network, so a process on the host can reach a server inside the container:

```text
  host: [ cvbr0 10.44.0.1/24 ]---veth---[ ceth0 10.44.0.2/24 ] :container
```

The **host side is provided** (`net.c`): `container_net_host_setup()` makes the
bridge, creates the veth pair, and moves one end into the container's netns; you
call it from `container_run()` (after the cgroup step, before releasing the
child), and `container_net_host_teardown()` after the child is reaped. You
implement the **container side**, `container_net_config()`: give the interface
`c->net_ifname` the address `c->net_ip`/`c->net_prefix` (`ioctl` `SIOCSIFADDR`,
`SIOCSIFNETMASK`), bring it up (`SIOCSIFFLAGS`), and add a default route via
`c->net_gw` (a `struct rtentry` with `RTF_UP | RTF_GATEWAY`, `ioctl SIOCADDRT`).
It needs `CAP_NET_ADMIN`, so `container_setup()` calls it (when `c->net_enabled`)
before the capability drop.

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
3. **Mount a writable `/tmp`**: a fresh tmpfs on `<rootfs>/tmp`.
4. **Set up `/dev`**: a tmpfs on `<rootfs>/dev`, then **bind** the host's
   `/dev/null` and `/dev/zero` onto empty files there. (You cannot `mknod(2)` a
   device in a user namespace, so you bind the real nodes in instead.) Mounting
   the tmpfs hides whatever was in `<rootfs>/dev` before, including the empty
   files `make-rootfs.sh` put there, so create the bind targets yourself after
   mounting it: `open(path, O_CREAT | O_WRONLY, 0666)`, then close the
   descriptor. Without them the bind fails with `ENOENT`.
5. **Mount a fresh `/proc`**: `mount("proc", "<rootfs>/proc", "proc", 0, NULL)`.
   This is what lets the container see only its own processes. Mount it **before** you switch roots, while the host's `/proc`
   is still visible in your mount namespace. Mounting a new `/proc` inside a user
   namespace is only permitted when a `/proc` is already visible there, so doing
   this after detaching the old root fails with `EPERM`.
6. **Switch roots**: `pivot_root(2)` into the rootfs and detach the old one, then
   `chdir("/")`. (The idiom `pivot_root(".", ".")` after `chdir(rootfs)` avoids
   needing a separate directory for the old root; `umount2(".", MNT_DETACH)`
   drops it.) There is no glibc wrapper for `pivot_root`, so call it through
   `syscall(SYS_pivot_root, ".", ".")`.

---

## Part III: Restricting the process (capabilities and seccomp)

Even as the container's "root", the command should not be able to do dangerous
things. Two mechanisms prevent that, both at the **end** of `container_setup()`,
so that the steps above still have the privileges and syscalls they need.

### Capabilities

Drop every capability:

- empty the **bounding set** so no future `exec` can regain a capability:
  `prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)` for every `cap` in `0 .. CAP_LAST_CAP`;
- clear the permitted / effective / inheritable sets with `capset(2)`. There is
  no glibc wrapper, so call it through `syscall(SYS_capset, &hdr, data)` with a
  `struct __user_cap_header_struct` whose version is
  `_LINUX_CAPABILITY_VERSION_3`, and a **two**-element
  `struct __user_cap_data_struct` array, since version 3 covers two 32-bit
  words (`<linux/capability.h>`);
- set `PR_SET_NO_NEW_PRIVS` so a setuid bit cannot hand privileges back.

### Seccomp

Capabilities gate *privileged* operations. A **seccomp** filter restricts which
**system calls** the process may make, privileged or not; it is the mechanism
behind Docker's default profile. In `container_seccomp()`, install a
seccomp-BPF filter that denies a denylist of dangerous syscalls with `EPERM` and
allows everything else. Using `<linux/filter.h>` and `<linux/seccomp.h>`, build a
`struct sock_filter[]` that:

1. loads `seccomp_data.arch` and rejects a foreign syscall ABI (compare against
   `AUDIT_ARCH_X86_64` or `AUDIT_ARCH_AARCH64` for your build architecture, so
   the same filter works on either architecture);
2. loads `seccomp_data.nr` and, for each denied `__NR_*` (for example `ptrace`,
   `mount`, `umount2`, `pivot_root`, `chroot`, `setns`, `unshare`, `reboot`,
   `swapon`/`swapoff`, `kexec_load`, and the `*_module` calls), returns
   `SECCOMP_RET_ERRNO | EPERM`;
3. otherwise returns `SECCOMP_RET_ALLOW`.

Then `prctl(PR_SET_NO_NEW_PRIVS, 1, ...)` and
`syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog)`. The filter is
inherited across the `fork`/`exec`, so it also covers the command.
`container_setup()` calls this **last**.

---

## Part IV: The init (launch and reap)

The cloned child is **PID 1** in the container's PID namespace: it is the
container's **init**. It launches the command and reaps orphans. In `container_init()`:

1. **Wait to be released.** The parent must write your id-maps and put you in the
   cgroup before you run. Close `c->sync[1]`, read one byte from `c->sync[0]`
   (it blocks until the parent writes it), then close `c->sync[0]`. Then call
   `container_setup(c)`.
2. **Launch the command as a child.** `fork()`; in the child, `execvp(c->argv[0],
   c->argv)`. The command is therefore **PID 2**, and you remain PID 1.
   Leave the descriptors the command inherits alone: do not set `FD_CLOEXEC` on
   anything but your own, and do not close what you did not open. The test suite
   passes the command a descriptor and holds that descriptor open to keep the
   container alive while it inspects the container from the host.
3. **Reap.** Loop `waitpid(-1, &st, 0)`: reap every child that dies, including
   orphans the kernel re-parents to PID 1. Stop when the **command itself** is
   reaped, and return its exit status (the low 8 bits of `WEXITSTATUS`, or
   `128 + signal` if it was killed). That value is what the container exits with.

Without a reaping PID 1, orphaned processes accumulate as zombies for as long as
the container runs.

---

## Part V: The cgroup (resource limits)

A cgroup limits what the container may consume. On cgroup v2 every cgroup is a
directory under `/sys/fs/cgroup`.

**`container_cgroup_init(c)`** (parent, before the child runs):

- a controller only works in a cgroup if its parent **delegated** it, so first
  enable the controllers you need in the base cgroup:
  write `"+pids +memory"` to `<cgroup_base>/cgroup.subtree_control`;
- `mkdir` `<cgroup_base>/<name>` and store that path in `c->cg_path` (cleanup
  needs it). Treat `EEXIST` as success: a run that crashed or was killed leaves
  its cgroup behind, and the next run must still start;
- write `c->pids_max` to `<cg_path>/pids.max` and `c->mem_max` to
  `<cg_path>/memory.max` (a limit of `-1` means the literal string `"max"`), and
  write `"0"` to `<cg_path>/memory.swap.max` so that hitting the memory cap
  **OOM-kills** the offending process instead of swapping it out.

**`container_cgroup_enter(c, child)`** (parent, after `clone`): write the child's
pid to `<cg_path>/cgroup.procs`. Moving a process into the cgroup moves it and
all its future children, so the whole container is accounted and limited.

---

## Part VI: Teardown

**`container_cleanup(c)`** (parent, after the child is reaped): the container's
mounts lived in its mount namespace, which the kernel destroyed along with the
process, so the only host-side state left is the cgroup directory. It is empty by
now: `rmdir(c->cg_path)`, and ignore `ENOENT` if it has already gone.

---

