/* container.h: the interface between the provided runtime and your container
 * code (Project 2). Provided; do not change the declarations.
 *
 * SPEC.md is the contract and says what each function below must do. This
 * header is the signatures and the struct they operate on.
 */
#ifndef CONTAINER_H
#define CONTAINER_H

#include <sys/types.h>
#include <limits.h>

/* Size of the stack you hand to clone() for the container's init process. */
#define CONTAINER_STACK_SIZE (1 << 20)

/* --net mode: the host bridge all containers attach to, and its address (which
 * is also the container's default gateway). */
#define CONTAINER_BRIDGE    "cvbr0"
#define CONTAINER_BRIDGE_GW "10.44.0.1"

/* One container to run. main.c fills the config fields from argv; the runtime
 * fields are yours to fill as you set the container up. */
struct container {
    /* ---- config (set by main.c, read-only to you) ---- */
    char *const *argv;      /* NULL-terminated: the command + args to run inside */
    const char *name;       /* container id: the cgroup directory name           */
    const char *hostname;   /* UTS hostname to set inside                         */
    const char *rootfs;     /* directory to use as the container's root filesystem*/
    long        pids_max;   /* cgroup pids.max  (-1 means "max", no limit)        */
    long        mem_max;    /* cgroup memory.max in bytes (-1 means "max")        */
    const char *cgroup_base;/* where container cgroups live, e.g. /sys/fs/cgroup  */

    /* ---- --net mode config (set by main.c; net_enabled is 0 by default) ---- */
    int         net_enabled;/* nonzero if --net was given                        */
    const char *net_ifname; /* the container's interface name, e.g. "ceth0"      */
    const char *net_ip;     /* the container's IPv4 address, e.g. "10.44.0.2"    */
    int         net_prefix; /* the subnet prefix length, e.g. 24                  */
    const char *net_gw;     /* the default gateway (the bridge), CONTAINER_BRIDGE_GW */

    /* ---- runtime state (yours to fill) ---- */
    int  sync[2];           /* parent->child sync pipe; you create it in _run()   */
    char cg_path[PATH_MAX]; /* full path of this container's cgroup; you set it   */
                            /* in container_cgroup_init() so cleanup can find it. */
    char net_host_if[16];   /* host veth name; set by container_net_host_setup()  */
};

/* ---- the functions you implement (container.c) -----------------------------
 *
 * main.c calls only container_run(); everything else is called by your own
 * code. Each returns 0 on success and -1 on failure (message on stderr), except
 * container_init() and container_run(), which return an exit status (0-255). */

/* The CLONE_NEW* bitmask selecting which namespaces to unshare. */
int container_namespaces(void);

/* Write the child's id maps, from the parent. */
int container_write_idmaps(struct container *c, pid_t child);

/* Create this container's cgroup, apply its limits, and store the path in
 * c->cg_path. */
int container_cgroup_init(struct container *c);

/* Move `child` into this container's cgroup. */
int container_cgroup_enter(struct container *c, pid_t child);

/* Runs inside the init before it launches the command: hostname, network, root
 * filesystem, capabilities, seccomp, in that order. */
int container_setup(struct container *c);

/* Bring up the container's loopback interface. Needs CAP_NET_ADMIN, so
 * container_setup() calls it before dropping capabilities. Best-effort. */
int container_network(void);

/* --net only: configure the container's end of the veth the host set up. Also
 * needs CAP_NET_ADMIN, so it runs before the capability drop. */
int container_net_config(struct container *c);

/* --net host side (PROVIDED, net.c; you only call these). host_setup() runs in
 * the parent after clone and before releasing the child: it makes the bridge and
 * veth and moves one end into the child's netns. host_teardown() removes the host
 * veth after the child is reaped. */
int container_net_host_setup(struct container *c, pid_t child);
int container_net_host_teardown(struct container *c);

/* Install the seccomp filter. container_setup() calls this last. */
int container_seccomp(void);

/* The container's init (PID 1), running in the cloned child. Returns the
 * command's exit status, which is what the clone child should _exit() with. */
int container_init(struct container *c);

/* The whole lifecycle; main.c calls only this. Returns the command's exit
 * status. */
int container_run(struct container *c);

/* Remove this container's cgroup, after the child has been reaped. */
int container_cleanup(struct container *c);

/* ---- provided helper (util.c) ---- */

/* Write `value` to the file at `path` (no trailing newline added). Returns 0 on
 * success, -1 on error. Handy for the cgroup and uid_map sysfs/procfs files. */
int write_file(const char *path, const char *value);

#endif /* CONTAINER_H */
