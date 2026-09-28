/* observer.c: the container introspection payload (static; part of the test suite).
 *
 * The autograder runs this AS the container command. It prints, on stdout, what
 * it observes about its environment as "key=value" lines; the grader diffs those
 * against what a correctly isolated container must look like. Then, if fd 3 is
 * open, it blocks reading it: that gives the grader a window to inspect the host
 * side (the cgroup's limits and membership) while the container is still alive,
 * and closing fd 3 lets the container exit so teardown can be checked.
 *
 * It runs as the command the container's init forks, so in a correct runtime it
 * is PID 2 with init as PID 1 (ppid == 1). Before reporting, it also creates an
 * orphan and counts zombies, so the grader can tell whether PID 1 reaps.
 *
 * Built statically and placed inside the container's rootfs by the grader:
 *     gcc -static -O1 -o observer observer.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <dirent.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/ptrace.h>
#include <sys/utsname.h>

/* read the value of "Key:" from a "Key:\tvalue" file (e.g. /proc/self/status) */
static void field(const char *path, const char *key, char *out, size_t n)
{
    out[0] = '\0';
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[512];
    size_t kl = strlen(key);
    while (fgets(line, sizeof line, f)) {
        if (strncmp(line, key, kl) == 0) {
            char *p = line + kl;
            while (*p == ' ' || *p == '\t') p++;
            snprintf(out, n, "%s", p);
            out[strcspn(out, "\n")] = '\0';
            break;
        }
    }
    fclose(f);
}

/* try to create (and remove) a file at `path`; return 1 if writable. */
static int can_write(const char *path)
{
    int fd = open(path, O_CREAT | O_WRONLY, 0644);
    if (fd < 0) return 0;
    close(fd);
    unlink(path);
    return 1;
}

/* count processes in state Z anywhere in /proc (zombies awaiting a reaper). */
static int count_zombies(void)
{
    DIR *d = opendir("/proc");
    if (!d) return -1;
    int z = 0; struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] < '1' || e->d_name[0] > '9') continue;
        char p[300]; snprintf(p, sizeof p, "/proc/%s/stat", e->d_name);
        FILE *f = fopen(p, "r"); if (!f) continue;
        char buf[512];
        if (fgets(buf, sizeof buf, f)) {
            char *rp = strrchr(buf, ')');            /* state is 2 chars after ')' */
            if (rp && rp[1] == ' ' && rp[2] == 'Z') z++;
        }
        fclose(f);
    }
    closedir(d);
    return z;
}

int main(void)
{
    struct utsname u;
    uname(&u);
    char buf[512];

    /* Orphan test: fork a helper that forks a grandchild and exits, orphaning
     * the grandchild to PID 1. If PID 1 reaps (a proper init), no zombie lingers;
     * if the command itself is PID 1 and does not reap, the grandchild sticks. */
    pid_t helper = fork();
    if (helper == 0) {
        if (fork() == 0) _exit(0);   /* grandchild: orphaned, then exits */
        _exit(0);                     /* helper: exits immediately        */
    }
    if (helper > 0) waitpid(helper, NULL, 0);   /* reap our own direct child */
    usleep(300000);                             /* let the orphan die + be reaped */

    /* PID + UTS namespaces (we are the command: PID 2 under init in a correct run) */
    printf("pid=%d\n", getpid());
    printf("ppid=%d\n", getppid());
    printf("host=%s\n", u.nodename);
    printf("zombies=%d\n", count_zombies());

    /* USER namespace: uid/gid inside, and the maps (which distinguish a fresh
     * userns even when the runtime itself is root: a mapped container shows a
     * single 1-wide mapping, an unmapped one shows "0 0 4294967295"). */
    printf("uid=%d gid=%d\n", getuid(), getgid());
    FILE *m = fopen("/proc/self/uid_map", "r");
    if (m) { if (fgets(buf, sizeof buf, m)) { buf[strcspn(buf, "\n")] = 0;
             /* squeeze runs of spaces so the grader can compare simply */
             char o[512]; int j = 0, sp = 0;
             for (int i = 0; buf[i]; i++) { if (buf[i]==' '||buf[i]=='\t'){ if(!sp){o[j++]=' ';sp=1;} } else {o[j++]=buf[i];sp=0;} }
             o[j]=0; printf("uid_map=%s\n", o[0]==' '?o+1:o); } fclose(m); }
    else printf("uid_map=\n");

    /* PID namespace, seen a second way: how many processes are visible */
    int nproc = 0;
    DIR *d = opendir("/proc");
    if (d) { struct dirent *e; while ((e = readdir(d))) if (e->d_name[0] >= '1' && e->d_name[0] <= '9') nproc++; closedir(d); }
    printf("procs_visible=%d\n", nproc);

    /* filesystem: we should be in the provided rootfs (busybox present), the
     * host root should be gone (no /usr, no host /etc/hostname), the root should
     * be read-only, /tmp writable, and the device nodes present. */
    printf("have_busybox=%s\n", access("/bin/busybox", F_OK) == 0 ? "present" : "absent");
    printf("host_usr=%s\n", access("/usr", F_OK) == 0 ? "present" : "absent");
    printf("host_etc_hostname=%s\n", access("/etc/hostname", F_OK) == 0 ? "present" : "absent");
    printf("root_writable=%s\n", can_write("/.observer_probe") ? "yes" : "no");
    printf("tmp_writable=%s\n", can_write("/tmp/.observer_probe") ? "yes" : "no");
    /* a real device is a character special file; the rootfs ships empty
     * regular-file placeholders, so only a bound-in node reads as "chardev". */
    struct stat ds;
    printf("dev_null=%s\n", stat("/dev/null", &ds) == 0 ? (S_ISCHR(ds.st_mode) ? "chardev" : "regular") : "absent");
    printf("dev_zero=%s\n", stat("/dev/zero", &ds) == 0 ? (S_ISCHR(ds.st_mode) ? "chardev" : "regular") : "absent");

    /* network namespace: a fresh netns has only `lo`. List the distinct
     * interface names (comma-joined) and whether lo is UP. getifaddrs reflects
     * the current netns (unlike /sys/class/net, which needs a fresh sysfs). */
    char ifaces[256] = ""; int lo_up = 0;
    struct ifaddrs *ial, *ip;
    if (getifaddrs(&ial) == 0) {
        for (ip = ial; ip; ip = ip->ifa_next) {
            char tok[64]; snprintf(tok, sizeof tok, ",%s,", ip->ifa_name);
            char hay[300]; snprintf(hay, sizeof hay, ",%s,", ifaces);
            if (!strstr(hay, tok)) {
                if (ifaces[0]) strncat(ifaces, ",", sizeof ifaces - strlen(ifaces) - 1);
                strncat(ifaces, ip->ifa_name, sizeof ifaces - strlen(ifaces) - 1);
            }
            if (!strcmp(ip->ifa_name, "lo")) lo_up = !!(ip->ifa_flags & IFF_UP);
        }
        freeifaddrs(ial);
    }
    printf("net_ifaces=%s\n", ifaces);
    printf("lo_up=%s\n", lo_up ? "yes" : "no");

    /* which cgroup we ended up in (path only; the container can't see limits) */
    field("/proc/self/cgroup", "0::", buf, sizeof buf);
    printf("cgroup=%s\n", buf);

    /* capabilities: bounding + effective sets */
    field("/proc/self/status", "CapBnd:", buf, sizeof buf); printf("capbnd=%s\n", buf);
    field("/proc/self/status", "CapEff:", buf, sizeof buf); printf("capeff=%s\n", buf);

    /* seccomp: ptrace(PTRACE_TRACEME) needs no privilege, so it succeeds unless
     * a syscall filter blocks it. It is on the denylist, so a filtered container
     * gets EPERM; without a filter it returns 0 (and we detach again). This
     * isolates the seccomp layer from the capability drop, which would not affect
     * an unprivileged ptrace. */
    long tr = ptrace(PTRACE_TRACEME, 0, 0, 0);
    printf("seccomp_ptrace=%s\n", tr == 0 ? "allowed" : (errno == EPERM ? "blocked" : "other"));
    if (tr == 0) ptrace(PTRACE_DETACH, 0, 0, 0);

    printf("observer-done\n");
    fflush(stdout);

    /* Hold the container open while the grader inspects the host side, if it
     * wired a control pipe to fd 3. Otherwise exit immediately. */
    if (fcntl(3, F_GETFD) != -1) {
        char c;
        while (read(3, &c, 1) > 0) { }
    }
    return 0;
}
