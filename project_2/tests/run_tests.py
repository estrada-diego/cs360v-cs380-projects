#!/usr/bin/env python3
"""Project 2 grader: run a container runtime, check its isolation host-side.

Staged, like Project 1: a partial runtime passes the layers it implements. We
build a small rootfs, drop the static `observer` (and a few enforcement payloads)
inside it, and run the runtime against it. For each property we read what the
observer reports on stdout; for the cgroup limits we hold the container open (via
fd 3) and inspect /sys/fs/cgroup from the host while it is alive, and we run a
fork bomb and a memory hog to confirm the limits actually bite; for teardown we
check the host after it exits.

Verdicts are host-side: the observer only reports what it sees, it never decides
pass/fail. Must run as root (cgroup delegation + some mounts).

    Run it in place, as root, with no arguments (via ./run_tests.sh); paths are
    resolved relative to tests/.
"""
import json, os, re, shutil, signal, socket, subprocess, sys, tempfile

# Paths are computed from this script's location (tests/), so the autograder can
# run it in place with no arguments: the submission IS the project directory.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RT_DIR = os.path.join(ROOT, "runtime")            # container.c / main.c / net.c / ...
CONTAINER = os.path.join(RT_DIR, "container")     # built from runtime/container.c
OBSERVER = os.path.join(HERE, "observer")         # built from tests/observer.c
IMPL = os.path.join(RT_DIR, "container.c")        # the student's source (sanitizer build)

CG_BASE = "/sys/fs/cgroup"
MEM = 19 * 1024 * 1024
NET_BRIDGE = "cvbr0"
NET_IP, NET_PORT = "10.44.0.2", 9000     # the container's --net address + a server port
NET_FARSIDE = "10.77.0.1"                # a host source addr off the container's subnet
ROWS, P, F = [], 0, 0
ROOTFS = None   # populated by build_rootfs()

# static payloads compiled into the rootfs (path -> C source)
PAYLOADS = {
    "rc42": "int main(void){return 42;}\n",
    # touch far more than the memory cap; volatile so it is not optimized away
    "memhog": ("#include <stdlib.h>\nint main(void){size_t n=100u<<20;"
               "volatile char*p=malloc(n);if(!p)return 2;"
               "for(size_t i=0;i<n;i+=4096)p[i]=(char)i;"
               "size_t s=0;for(size_t i=0;i<n;i+=4096)s+=p[i];return (int)(s&1);}\n"),
    # fork until the pids cap bites (EAGAIN) -> exit 7; if it never bites -> exit 8
    "forkb": ("#include <unistd.h>\n#include <errno.h>\nint main(void){int n=0;"
              "for(;;){pid_t p=fork();if(p<0)return errno==EAGAIN?7:9;"
              "if(p==0){pause();_exit(0);}if(++n>=400)return 8;}}\n"),
    # kills itself with SIGTERM, so a correct runtime returns 128+15 = 143
    "sigdie": ("#include <signal.h>\n#include <unistd.h>\n"
               "int main(void){raise(SIGTERM);pause();return 0;}\n"),
    # a TCP echo server: prints "netready", then replies "pong" to one client
    "netecho": ("#include <stdio.h>\n#include <string.h>\n#include <unistd.h>\n"
                "#include <sys/socket.h>\n#include <netinet/in.h>\nint main(void){"
                "int s=socket(AF_INET,SOCK_STREAM,0);int one=1;"
                "setsockopt(s,SOL_SOCKET,SO_REUSEADDR,&one,4);struct sockaddr_in a;"
                "memset(&a,0,sizeof a);a.sin_family=AF_INET;a.sin_port=htons(%d);"
                "a.sin_addr.s_addr=INADDR_ANY;if(bind(s,(void*)&a,sizeof a))return 1;"
                "listen(s,1);printf(\"netready\\n\");fflush(stdout);int c=accept(s,0,0);"
                "if(c<0)return 2;char b[64]={0};if(read(c,b,63)<0){}"
                "if(write(c,\"pong\\n\",5)<0){}return 0;}\n") % NET_PORT,
}


# --- result harness ---------------------------------------------------------
# Prints human PASS/FAIL (by requirement) to the terminal. When the autograder
# sets CS360V_RESULTS it also appends status<TAB>id<TAB>requirement<TAB>reason<TAB>
# tech per check. No points here; the autograder assigns those.
_RESULTS = os.environ.get("CS360V_RESULTS", "")

REQUIREMENTS = {
    "compile": "Your container.c compiles with only the provided libraries",
    "exec": "the container runs your command under your init",
    "exit_status": "a command's exit status propagates out of the runtime",
    "signal_exit": "a command killed by a signal reports 128+signal",
    "uts_hostname": "the container has its own hostname (UTS namespace)",
    "pid_small": "the command sees a fresh PID namespace (it is PID 1 or 2)",
    "user_ns": "the container runs in a user namespace (root inside maps to you)",
    "parented": "the command is reparented to your init, not the host",
    "reaping": "your init reaps zombies, leaving none behind",
    "root_replaced": "the container pivots into its own root filesystem",
    "own_proc": "the container mounts its own /proc",
    "root_readonly": "the container's root filesystem is read-only",
    "tmp_writable": "/tmp is writable inside the container",
    "dev_present": "the container has /dev/null and /dev/zero",
    "no_mount_leak": "the container's mounts do not leak to the host",
    "own_netns": "the container has its own network namespace (only lo)",
    "loopback_up": "loopback is up inside the container",
    "net_reachable": "a server in the container is reachable over --net",
    "in_cgroup": "the container runs in its own cgroup",
    "pids_max": "the pids limit is set on the container's cgroup",
    "memory_max": "the memory limit is set on the container's cgroup",
    "member": "the container's process is a member of its cgroup",
    "pids_enforced": "the pids limit actually stops a fork bomb",
    "mem_enforced": "the memory limit actually stops a memory hog",
    "capbnd_empty": "the container drops all capabilities (bounding set empty)",
    "capeff_empty": "the container drops all capabilities (effective set empty)",
    "syscall_filtered": "a denied syscall is blocked by seccomp",
    "stale_cgroup": "a leftover cgroup from a crashed run does not break a new one",
    "concurrent": "two containers run at once without colliding",
    "sanitizer_clean": "the runtime is memory-safe under AddressSanitizer",
    "cgroup_removed": "the container's cgroup is removed on teardown",
    "exit_status_ok": "the exit status is still correct after cleanup",
}


def ck(status, cid, reason="", tech=""):
    global P, F
    ok = status == "pass"
    req = REQUIREMENTS.get(cid, cid.replace("_", " "))
    print(f"  {'PASS' if ok else 'FAIL'}  {req}" + (f"  ({reason})" if reason and not ok else ""))
    P += ok; F += (not ok)
    if _RESULTS:
        with open(_RESULTS, "a") as fh:
            fh.write("\t".join([status, cid, req, reason if not ok else "", tech]) + "\n")


def record(group, name, ok, reason=""):
    # the check bodies call record(group, name, ...); map to the harness by name.
    ck("pass" if ok else "fail", name, reason)


def build_rootfs():
    """A tiny busybox rootfs with the observer and enforcement payloads inside."""
    global ROOTFS
    bb = shutil.which("busybox")
    if not bb:
        print("busybox not found (install busybox-static)", file=sys.stderr); sys.exit(2)
    ROOTFS = tempfile.mkdtemp(prefix="p2rootfs.")
    for d in ("bin", "proc", "dev", "tmp", "etc"):
        os.makedirs(os.path.join(ROOTFS, d), exist_ok=True)
    shutil.copy(bb, os.path.join(ROOTFS, "bin/busybox"))
    for name in ("sh", "ls", "cat", "echo", "id", "ps", "grep", "sleep", "env", "true"):
        os.symlink("busybox", os.path.join(ROOTFS, "bin", name))
    for dev in ("null", "zero"):
        open(os.path.join(ROOTFS, "dev", dev), "w").close()
    shutil.copy(OBSERVER, os.path.join(ROOTFS, "observer"))
    os.chmod(os.path.join(ROOTFS, "observer"), 0o755)
    for name, src in PAYLOADS.items():
        cf = os.path.join(ROOTFS, name + ".c")
        open(cf, "w").write(src)
        subprocess.run(["gcc", "-static", "-O2", "-o", os.path.join(ROOTFS, name), cf], check=True)
        os.remove(cf)


def cleanup_cg(name):
    p = f"{CG_BASE}/{name}"
    if os.path.isdir(p):
        try:
            for pid in open(f"{p}/cgroup.procs").read().split():
                try: os.kill(int(pid), 9)
                except OSError: pass
        except OSError: pass
        try: os.rmdir(p)
        except OSError: pass


def run_container(name, cmd="/observer", pids_max=37, mem_max=MEM, window=False,
                  timeout=30, pre_clean=True):
    """Run the container with `cmd` inside the rootfs. Returns
    (returncode, {obs}, cgroup_snapshot_or_None). If window, hold the container
    open via fd 3 and snapshot the host cgroup while it is alive."""
    if pre_clean:
        cleanup_cg(name)
    argv = [CONTAINER, "--name", name, "--hostname", "box", "--rootfs", ROOTFS,
            "--pids-max", str(pids_max), "--mem-max", str(mem_max),
            "--cgroup-base", CG_BASE, "--", cmd]
    snap = None
    if window:
        cr, cw = os.pipe()  # observer's fd 3 = cr; we hold cw
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             pass_fds=(cr,), preexec_fn=lambda: os.dup2(cr, 3))
        os.close(cr)
        out = b""
        while b"observer-done" not in out:
            chunk = p.stdout.read(1)
            if not chunk: break
            out += chunk
        cg = f"{CG_BASE}/{name}"
        def rd(f):
            try: return open(f"{cg}/{f}").read().strip()
            except OSError: return None
        snap = {"pids_max": rd("pids.max"), "memory_max": rd("memory.max"),
                "procs": (rd("cgroup.procs") or "").split(), "exists": os.path.isdir(cg)}
        os.close(cw)                 # release the observer
        out += p.stdout.read()
        p.wait()
        rc = p.returncode
    else:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            # a hung container (e.g. a workload that is PID 1 and so ignores
            # signals). Kill the whole group and report a timeout code.
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError: pass
            out, _ = p.communicate()
            rc = 124
    obs = {}
    for line in out.decode(errors="replace").splitlines():
        if "=" in line and not line.startswith(("container:", "  ")):
            k, _, v = line.partition("=")
            obs[k.strip()] = v.strip()
    return rc, obs, snap


def run_net():
    """Run the container in --net mode with the echo server inside, then reach it
    from the host. The client binds a source address on a DIFFERENT subnet than
    the container (a second address on the bridge), so the container's reply must
    go via its default route -- this exercises the whole of container_net_config
    (address + up + route), not just on-link connectivity. Returns True if it
    replied."""
    name = "p2net"
    cleanup_cg(name)
    argv = [CONTAINER, "--name", name, "--hostname", "box", "--rootfs", ROOTFS,
            "--net", "--pids-max", "37", "--mem-max", str(MEM),
            "--cgroup-base", CG_BASE, "--", "/netecho"]
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True)
    reachable = False
    try:
        out = b""
        while b"netready" not in out:
            ch = p.stdout.read(1)
            if not ch: break
            out += ch
        if b"netready" in out:
            # a source address off the container's subnet (reply needs the route)
            subprocess.run(["ip", "addr", "add", NET_FARSIDE + "/24", "dev", NET_BRIDGE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                cs = socket.socket(); cs.settimeout(5)
                cs.bind((NET_FARSIDE, 0)); cs.connect((NET_IP, NET_PORT))
                cs.sendall(b"ping"); reachable = b"pong" in cs.recv(16); cs.close()
            except OSError:
                pass
    finally:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError: pass
            p.wait()
    cleanup_cg(name)
    subprocess.run(["ip", "link", "del", NET_BRIDGE],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return reachable


def mount_points():
    pts = set()
    try:
        for line in open("/proc/self/mountinfo"):
            f = line.split()
            if len(f) > 4: pts.add(f[4])
    except OSError:
        pass
    return pts


def run_mount_leak():
    """A container's mounts live in its private mount namespace and must not leak
    to the host. Run one and confirm the host mount table is unchanged; if it is
    not (propagation was not made private), clean the leaked mounts and fail."""
    before = mount_points()
    run_container("p2mnt")
    leaked = mount_points() - before
    for pt in leaked:
        subprocess.run(["umount", "-l", pt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup_cg("p2mnt")
    return not leaked


def run_stale_cgroup():
    """A leftover cgroup of the same name (from a prior or crashed run) must not
    stop the container: creating the cgroup has to tolerate it already existing."""
    name = "p2stale"
    cleanup_cg(name)
    try: os.mkdir(f"{CG_BASE}/{name}")     # a stale cgroup with the same name
    except OSError: pass
    rc, o, _ = run_container(name, pre_clean=False)
    ok = o.get("pid") is not None and not os.path.isdir(f"{CG_BASE}/{name}")
    cleanup_cg(name)
    return ok


def run_concurrent():
    """Two containers at once (distinct names) must both succeed: no global state
    (a fixed cgroup/interface/temp name) may be clobbered between them."""
    names = ("p2cc1", "p2cc2")
    procs = []
    for n in names:
        cleanup_cg(n)
        procs.append(subprocess.Popen(
            [CONTAINER, "--name", n, "--rootfs", ROOTFS, "--cgroup-base", CG_BASE,
             "--", "/rc42"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
    rcs = []
    for p in procs:
        try:
            p.wait(timeout=20); rcs.append(p.returncode)
        except subprocess.TimeoutExpired:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError: pass
            p.wait(); rcs.append(124)
    for n in names: cleanup_cg(n)
    return all(rc == 42 for rc in rcs)


def run_sanitizer():
    """Build the submission's runtime with ASan+UBSan and run one container: a
    memory or undefined-behavior bug that the functional tests miss shows up here.
    Skipped (returns None) if the source was not provided via --impl."""
    if not IMPL:
        return None
    san = "/tmp/p2_container_san"
    build = subprocess.run(
        ["gcc", "-fsanitize=address,undefined", "-g", "-O1", "-I", RT_DIR, "-o", san,
         f"{RT_DIR}/main.c", f"{RT_DIR}/util.c", f"{RT_DIR}/net.c", IMPL],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if build.returncode != 0:
        return False
    cleanup_cg("p2san")
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:exitcode=99",
               UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=0")
    p = subprocess.Popen([san, "--name", "p2san", "--rootfs", ROOTFS,
                          "--cgroup-base", CG_BASE, "--", "/observer"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                         start_new_session=True)
    try:
        out, _ = p.communicate(timeout=60); rc = p.returncode
    except subprocess.TimeoutExpired:
        try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except OSError: pass
        p.communicate(); rc = 124
    cleanup_cg("p2san")
    text = out.decode(errors="replace")
    return rc != 99 and not re.search(r"AddressSanitizer|runtime error|LeakSanitizer", text)


def main():
    ok = build()
    ck("pass" if ok else "fail", "compile",
       "" if ok else "the runtime did not build with the provided toolchain")
    if not ok:
        finish()
        return

    build_rootfs()

    # one representative run captures every observer property
    rc, o, _ = run_container("p2grade")

    # ---- run: the command executes and its exit status propagates ----
    record("run", "exec", o.get("pid") is not None and "capbnd" in o,
           "observer did not run / produced no output (did the runtime spawn and pivot into the rootfs?)")
    rrc, _, _ = run_container("p2rc", cmd="/rc42")
    record("run", "exit_status", rrc == 42, f"exit status was {rrc}, want 42")
    src2, _, _ = run_container("p2sig", cmd="/sigdie", timeout=10)
    record("run", "signal_exit", src2 == 143,
           f"a command killed by SIGTERM returned {src2}, want 143 (128+15). "
           "(A command that is PID 1 itself would ignore the signal -- run it under your init.)")

    # ---- namespace: UTS / PID / USER ----
    record("namespace", "uts_hostname", o.get("host") == "box",
           f"hostname inside = {o.get('host')!r}, want 'box'")
    # the command runs under our init in a fresh PID namespace: a tiny pid
    record("namespace", "pid_small", o.get("pid", "999").isdigit() and int(o.get("pid", "999")) <= 2,
           f"getpid() inside = {o.get('pid')}, want a small pid in a fresh namespace")
    um = o.get("uid_map", "")
    record("namespace", "user_ns", bool(re.match(r"^0 \d+ 1$", um)),
           f"uid_map = {um!r}, want a 1-wide 'container-root' mapping")

    # ---- init: the command runs under a reaping PID 1 ----
    record("init", "parented", o.get("ppid") == "1",
           f"command's ppid = {o.get('ppid')}, want 1 (an init process should be PID 1)")
    record("init", "reaping", o.get("zombies") == "0",
           f"zombies after an orphan test = {o.get('zombies')}, want 0 (PID 1 must reap)")

    # ---- filesystem isolation ----
    record("filesystem", "root_replaced",
           o.get("have_busybox") == "present" and o.get("host_usr") == "absent"
           and o.get("host_etc_hostname") == "absent",
           "the container root is not the provided rootfs (host paths still visible)")
    record("filesystem", "own_proc",
           o.get("procs_visible", "999").isdigit() and int(o.get("procs_visible", "999")) <= 3,
           "/proc is not the container's own")
    record("filesystem", "root_readonly", o.get("root_writable") == "no",
           "the container's root filesystem is writable (want a read-only rootfs)")
    record("filesystem", "tmp_writable", o.get("tmp_writable") == "yes",
           "/tmp is not writable (want a writable tmpfs)")
    record("filesystem", "dev_present", o.get("dev_null") == "chardev" and o.get("dev_zero") == "chardev",
           "/dev/null and /dev/zero are not real device nodes (bind them from the host)")
    record("filesystem", "no_mount_leak", run_mount_leak(),
           "the container's mounts leaked to the host mount table (make propagation private)")

    # ---- network namespace: only loopback, and it is up ----
    record("network", "own_netns", o.get("net_ifaces") == "lo",
           f"interfaces inside = {o.get('net_ifaces')!r}, want just 'lo' (own network namespace)")
    record("network", "loopback_up", o.get("lo_up") == "yes",
           "the loopback interface is not up")
    # --net mode: the host reaches a server inside the container over the bridge
    record("network", "net_reachable", run_net(),
           "could not reach a server in the container over --net (configure the veth: "
           "ip, up, default route)")

    # ---- cgroup: path (from inside) + limits & membership (from host window) ----
    record("cgroup", "in_cgroup", o.get("cgroup", "").endswith("/p2grade"),
           f"container cgroup = {o.get('cgroup')!r}, want .../p2grade")
    wrc, wo, snap = run_container("p2win", window=True)
    record("cgroup", "pids_max", snap and snap["pids_max"] == "37",
           f"pids.max = {snap and snap['pids_max']}, want 37")
    record("cgroup", "memory_max", snap and snap["memory_max"] == str(MEM),
           f"memory.max = {snap and snap['memory_max']}, want {MEM}")
    record("cgroup", "member", bool(snap and snap["procs"]),
           "the container process is not in its cgroup during the run")

    # ---- limits actually enforced ----
    frc, _, _ = run_container("p2fork", cmd="/forkb", pids_max=15)
    record("limits", "pids_enforced", frc == 7,
           f"fork bomb under pids.max=15 returned {frc}, want 7 (EAGAIN at the cap)")
    mrc, _, _ = run_container("p2mem", cmd="/memhog", mem_max=MEM)
    record("limits", "mem_enforced", mrc == 137,
           f"memory hog under memory.max returned {mrc}, want 137 (OOM-killed)")

    # ---- capabilities ----
    record("capabilities", "capbnd_empty", o.get("capbnd") == "0000000000000000",
           f"CapBnd = {o.get('capbnd')}, want all-zero (bounding set emptied)")
    record("capabilities", "capeff_empty", o.get("capeff") == "0000000000000000",
           f"CapEff = {o.get('capeff')}, want all-zero (no effective caps)")

    # ---- seccomp: a denylisted, unprivileged syscall is blocked ----
    record("seccomp", "syscall_filtered", o.get("seccomp_ptrace") == "blocked",
           f"ptrace(PTRACE_TRACEME) was {o.get('seccomp_ptrace')!r}, want 'blocked' "
           "(install a seccomp filter denying it)")

    # ---- robustness (adversarial) ----
    record("robustness", "stale_cgroup", run_stale_cgroup(),
           "a leftover cgroup of the same name stopped the container (tolerate EEXIST)")
    record("robustness", "concurrent", run_concurrent(),
           "two containers run at once did not both succeed (some global state is shared)")
    san = run_sanitizer()
    if san is not None:
        record("robustness", "sanitizer_clean", san,
               "the runtime tripped AddressSanitizer/UBSan (a memory or undefined-behavior bug)")

    # ---- teardown ----
    # the FULL lifecycle: the cgroup existed while the container ran (from the
    # window snapshot) and is gone afterwards. This fails both for a runtime that
    # never made a cgroup and for one that leaves it behind.
    record("teardown", "cgroup_removed",
           bool(snap and snap["exists"]) and not os.path.isdir(f"{CG_BASE}/p2win"),
           "the cgroup was not created-during-run and removed-after")
    record("teardown", "exit_status_ok", rrc == 42, "exit status not propagated after cleanup")

    finish()


def finish():
    for n in ("p2grade", "p2rc", "p2sig", "p2win", "p2fork", "p2mem", "p2net",
              "p2mnt", "p2stale", "p2cc1", "p2cc2", "p2san"):
        cleanup_cg(n)
    subprocess.run(["ip", "link", "del", NET_BRIDGE],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if ROOTFS and os.path.isdir(ROOTFS):
        shutil.rmtree(ROOTFS, ignore_errors=True)
    print("=" * 35)
    print(f"{P} passed, {F} failed  (of {P + F})")


def build():
    """Build the provided observer and the student's runtime (the compile check)."""
    subprocess.run(["gcc", "-static", "-O1", "-o", OBSERVER, os.path.join(HERE, "observer.c")],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["make", "-C", RT_DIR, "clean"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(["make", "-C", RT_DIR], capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(CONTAINER)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("run_tests.py must run as root (cgroup delegation + mounts); use sudo",
              file=sys.stderr)
        sys.exit(2)
    main()
    sys.exit(0 if F == 0 else 1)
