/* container.c: a minimal container runtime (Project 2).
 *
 * This follows SPEC.md part by part. Each function does one step of the
 * lifecycle described there; container_run() ties them together in order.
 */
#define _GNU_SOURCE
#include "container.h"

#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <net/if.h>
#include <net/route.h>
#include <arpa/inet.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/audit.h>
#include <sys/prctl.h>

/* ---- Part I: namespaces ----------------------------------------------- */

int container_namespaces(void)
{
    return CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWNET;
}

int container_write_idmaps(struct container *c, pid_t child)
{
    (void)c; /* only the child's pid and our own ids are needed here */
    char path[PATH_MAX], line[64];

    snprintf(path, sizeof path, "/proc/%d/uid_map", (int)child);
    snprintf(line, sizeof line, "0 %d 1", (int)getuid());
    if (write_file(path, line) < 0) return -1;

    snprintf(path, sizeof path, "/proc/%d/setgroups", (int)child);
    if (write_file(path, "deny") < 0) return -1;

    snprintf(path, sizeof path, "/proc/%d/gid_map", (int)child);
    snprintf(line, sizeof line, "0 %d 1", (int)getgid());
    if (write_file(path, line) < 0) return -1;

    return 0;
}

/* ---- Part V: cgroup --------------------------------------------------- */

int container_cgroup_init(struct container *c)
{
    char path[PATH_MAX];

    snprintf(path, sizeof path, "%s/cgroup.subtree_control", c->cgroup_base);
    if (write_file(path, "+pids +memory") < 0) return -1;

    snprintf(c->cg_path, sizeof c->cg_path, "%s/%s", c->cgroup_base, c->name);
    if (mkdir(c->cg_path, 0755) < 0 && errno != EEXIST) {
        fprintf(stderr, "container: mkdir '%s': %s\n", c->cg_path, strerror(errno));
        return -1;
    }

    char val[32];
    snprintf(path, sizeof path, "%s/pids.max", c->cg_path);
    snprintf(val, sizeof val, c->pids_max < 0 ? "max" : "%ld", c->pids_max);
    if (write_file(path, val) < 0) return -1;

    snprintf(path, sizeof path, "%s/memory.max", c->cg_path);
    snprintf(val, sizeof val, c->mem_max < 0 ? "max" : "%ld", c->mem_max);
    if (write_file(path, val) < 0) return -1;

    snprintf(path, sizeof path, "%s/memory.swap.max", c->cg_path);
    if (write_file(path, "0") < 0) return -1;

    return 0;
}

int container_cgroup_enter(struct container *c, pid_t child)
{
    char path[PATH_MAX], pid[16];
    snprintf(path, sizeof path, "%s/cgroup.procs", c->cg_path);
    snprintf(pid, sizeof pid, "%d", (int)child);
    return write_file(path, pid);
}

/* ---- Part I: network (loopback and the --net veth) --------------------- */

static int bring_up(const char *ifname)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return -1;

    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    snprintf(ifr.ifr_name, IFNAMSIZ, "%s", ifname);

    if (ioctl(s, SIOCGIFFLAGS, &ifr) < 0) { close(s); return -1; }
    ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
    if (ioctl(s, SIOCSIFFLAGS, &ifr) < 0) { close(s); return -1; }

    close(s);
    return 0;
}

int container_network(void)
{
    /* Best-effort: a container without loopback should still run. */
    bring_up("lo");
    return 0;
}

int container_net_config(struct container *c)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) { perror("container: socket"); return -1; }

    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    snprintf(ifr.ifr_name, IFNAMSIZ, "%s", c->net_ifname);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;

    /* address */
    inet_pton(AF_INET, c->net_ip, &addr.sin_addr);
    memcpy(&ifr.ifr_addr, &addr, sizeof addr);
    if (ioctl(s, SIOCSIFADDR, &ifr) < 0) { perror("container: SIOCSIFADDR"); close(s); return -1; }

    /* netmask, from the prefix length */
    uint32_t mask = c->net_prefix == 0 ? 0 : htonl(~0u << (32 - c->net_prefix));
    addr.sin_addr.s_addr = mask;
    memcpy(&ifr.ifr_addr, &addr, sizeof addr);
    if (ioctl(s, SIOCSIFNETMASK, &ifr) < 0) { perror("container: SIOCSIFNETMASK"); close(s); return -1; }

    /* up */
    if (ioctl(s, SIOCGIFFLAGS, &ifr) < 0) { perror("container: SIOCGIFFLAGS"); close(s); return -1; }
    ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
    if (ioctl(s, SIOCSIFFLAGS, &ifr) < 0) { perror("container: SIOCSIFFLAGS"); close(s); return -1; }

    /* default route via the gateway */
    struct rtentry rt;
    memset(&rt, 0, sizeof rt);
    struct sockaddr_in *dst = (struct sockaddr_in *)&rt.rt_dst;
    struct sockaddr_in *gw  = (struct sockaddr_in *)&rt.rt_gateway;
    struct sockaddr_in *gm  = (struct sockaddr_in *)&rt.rt_genmask;
    dst->sin_family = AF_INET; dst->sin_addr.s_addr = INADDR_ANY;
    gm->sin_family  = AF_INET; gm->sin_addr.s_addr  = INADDR_ANY;
    gw->sin_family  = AF_INET; inet_pton(AF_INET, c->net_gw, &gw->sin_addr);
    rt.rt_flags = RTF_UP | RTF_GATEWAY;
    if (ioctl(s, SIOCADDRT, &rt) < 0) { perror("container: SIOCADDRT"); close(s); return -1; }

    close(s);
    return 0;
}

/* ---- Part II/III: isolation, run inside the container init ------------ */

static int make_bind_target(const char *path)
{
    int fd = open(path, O_CREAT | O_WRONLY, 0666);
    if (fd < 0) return -1;
    close(fd);
    return 0;
}

static int isolate_filesystem(struct container *c)
{
    char path[PATH_MAX];

    /* 1. private mount propagation, so nothing we mount leaks to the host. */
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) < 0) {
        perror("container: mount private"); return -1;
    }

    /* 2. bind the rootfs onto itself, then make that bind read-only. */
    if (mount(c->rootfs, c->rootfs, NULL, MS_BIND | MS_REC, NULL) < 0) {
        perror("container: bind rootfs"); return -1;
    }
    if (mount(c->rootfs, c->rootfs, NULL, MS_BIND | MS_REMOUNT | MS_RDONLY, NULL) < 0) {
        perror("container: remount rootfs ro"); return -1;
    }

    /* 3. a writable /tmp. */
    snprintf(path, sizeof path, "%s/tmp", c->rootfs);
    if (mount("tmpfs", path, "tmpfs", 0, NULL) < 0) {
        perror("container: mount /tmp"); return -1;
    }

    /* 4. /dev: a tmpfs, then bind the real null/zero nodes in. */
    snprintf(path, sizeof path, "%s/dev", c->rootfs);
    if (mount("tmpfs", path, "tmpfs", 0, NULL) < 0) {
        perror("container: mount /dev"); return -1;
    }
    char target[PATH_MAX];
    snprintf(target, sizeof target, "%s/dev/null", c->rootfs);
    if (make_bind_target(target) < 0 || mount("/dev/null", target, NULL, MS_BIND, NULL) < 0) {
        perror("container: bind /dev/null"); return -1;
    }
    snprintf(target, sizeof target, "%s/dev/zero", c->rootfs);
    if (make_bind_target(target) < 0 || mount("/dev/zero", target, NULL, MS_BIND, NULL) < 0) {
        perror("container: bind /dev/zero"); return -1;
    }

    /* 5. a fresh /proc, while the host's is still visible to this userns. */
    snprintf(path, sizeof path, "%s/proc", c->rootfs);
    if (mount("proc", path, "proc", 0, NULL) < 0) {
        perror("container: mount /proc"); return -1;
    }

    /* 6. switch roots and drop the old one. */
    if (chdir(c->rootfs) < 0) { perror("container: chdir rootfs"); return -1; }
    if (syscall(SYS_pivot_root, ".", ".") < 0) { perror("container: pivot_root"); return -1; }
    if (umount2(".", MNT_DETACH) < 0) { perror("container: umount2 old root"); return -1; }
    if (chdir("/") < 0) { perror("container: chdir /"); return -1; }

    return 0;
}

static int drop_capabilities(void)
{
    for (int cap = 0; cap <= CAP_LAST_CAP; cap++)
        prctl(PR_CAPBSET_DROP, cap, 0, 0, 0);

    struct __user_cap_header_struct hdr = { _LINUX_CAPABILITY_VERSION_3, 0 };
    struct __user_cap_data_struct data[2];
    memset(data, 0, sizeof data);
    if (syscall(SYS_capset, &hdr, data) < 0) { perror("container: capset"); return -1; }

    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        perror("container: PR_SET_NO_NEW_PRIVS"); return -1;
    }
    return 0;
}

int container_setup(struct container *c)
{
    if (sethostname(c->hostname, strlen(c->hostname)) < 0) {
        perror("container: sethostname"); return -1;
    }

    container_network();

    if (c->net_enabled && container_net_config(c) < 0)
        return -1;

    if (isolate_filesystem(c) < 0)
        return -1;

    if (drop_capabilities() < 0)
        return -1;

    if (container_seccomp() < 0)
        return -1;

    return 0;
}

/* ---- Part III: seccomp -------------------------------------------------- */

#if defined(__x86_64__)
#define CONTAINER_AUDIT_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define CONTAINER_AUDIT_ARCH AUDIT_ARCH_AARCH64
#else
#error "unsupported architecture"
#endif

int container_seccomp(void)
{
    static const int denied[] = {
        __NR_ptrace, __NR_mount, __NR_umount2, __NR_pivot_root, __NR_chroot,
        __NR_setns, __NR_unshare, __NR_reboot, __NR_swapon, __NR_swapoff,
        __NR_kexec_load, __NR_init_module, __NR_finit_module, __NR_delete_module,
    };
    size_t ndenied = sizeof denied / sizeof denied[0];

    /* arch check (load + jump + deny, 3 instructions) + syscall-number load
     * (1) + a compare-and-deny pair per denied syscall + a final allow. */
    size_t nfilter = 4 + 2 * ndenied + 1;
    struct sock_filter *f = calloc(nfilter, sizeof *f);
    if (!f) { perror("container: calloc"); return -1; }
    size_t i = 0;

    /* reject any syscall made under a foreign ABI (e.g. the 32-bit one) */
    f[i++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                           offsetof(struct seccomp_data, arch));
    f[i++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, CONTAINER_AUDIT_ARCH, 1, 0);
    f[i++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM);

    /* deny each syscall on the list, allow everything else */
    f[i++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                           offsetof(struct seccomp_data, nr));
    for (size_t k = 0; k < ndenied; k++) {
        f[i++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, denied[k], 0, 1);
        f[i++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM);
    }
    f[i++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);

    struct sock_fprog prog = { .len = (unsigned short)nfilter, .filter = f };

    int ret = 0;
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        perror("container: PR_SET_NO_NEW_PRIVS"); ret = -1;
    } else if (syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog) < 0) {
        perror("container: seccomp"); ret = -1;
    }
    free(f);
    return ret;
}

/* ---- Part IV: the init (launch and reap) -------------------------------- */

int container_init(struct container *c)
{
    /* 1. wait for the parent to release us */
    close(c->sync[1]);
    char dummy;
    if (read(c->sync[0], &dummy, 1) < 0) perror("container: sync read");
    close(c->sync[0]);

    if (container_setup(c) < 0) {
        fprintf(stderr, "container: setup failed\n");
        return 1;
    }

    /* 2. launch the command */
    pid_t cmd = fork();
    if (cmd < 0) { perror("container: fork"); return 1; }
    if (cmd == 0) {
        execvp(c->argv[0], c->argv);
        fprintf(stderr, "container: exec '%s': %s\n", c->argv[0], strerror(errno));
        _exit(127);
    }

    /* 3. reap everyone; stop when the command itself is reaped */
    int status = 0;
    for (;;) {
        int st;
        pid_t p = waitpid(-1, &st, 0);
        if (p < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (p == cmd) {
            status = WIFEXITED(st) ? WEXITSTATUS(st)
                   : WIFSIGNALED(st) ? 128 + WTERMSIG(st)
                   : 1;
            break;
        }
        /* some other (orphaned) child; already reaped by this waitpid call */
    }

    return status;
}

/* ---- the whole lifecycle: main.c calls only this ------------------------ */

static int child_entry(void *arg)
{
    struct container *c = arg;
    int status = container_init(c);
    _exit(status);
}

int container_run(struct container *c)
{
    if (container_cgroup_init(c) < 0) return 1;

    if (pipe(c->sync) < 0) { perror("container: pipe"); return 1; }

    char *stack = malloc(CONTAINER_STACK_SIZE);
    if (!stack) { perror("container: malloc stack"); return 1; }

    pid_t child = clone(child_entry, stack + CONTAINER_STACK_SIZE,
                         container_namespaces() | SIGCHLD, c);
    if (child < 0) {
        perror("container: clone");
        free(stack);
        return 1;
    }

    container_write_idmaps(c, child);
    container_cgroup_enter(c, child);

    if (c->net_enabled)
        container_net_host_setup(c, child);

    /* release the child */
    close(c->sync[0]);
    char byte = 'x';
    if (write(c->sync[1], &byte, 1) < 0) perror("container: sync write");
    close(c->sync[1]);

    int st;
    waitpid(child, &st, 0);
    int status = WIFEXITED(st) ? WEXITSTATUS(st)
               : WIFSIGNALED(st) ? 128 + WTERMSIG(st)
               : 1;

    if (c->net_enabled)
        container_net_host_teardown(c);
    container_cleanup(c);

    free(stack);
    return status;
}

/* ---- Part VI: teardown --------------------------------------------------- */

int container_cleanup(struct container *c)
{
    if (rmdir(c->cg_path) < 0 && errno != ENOENT) {
        perror("container: rmdir cgroup");
        return -1;
    }
    return 0;
}
