/* net.c: PROVIDED host-side container networking for Project 2 (--net mode).
 *
 * This is the staff-provided half of container networking: it wires the
 * container's network namespace to the host so a serverless platform (Project 4)
 * can reach a server running inside the container. You do NOT implement this; you
 * implement the *in-container* half, container_net_config(), in container.c.
 *
 * The model is a Linux bridge, exactly like Docker's default network:
 *
 *     host: [ cvbr0 10.44.0.1/24 ]---veth---[ ceth0 10.44.0.2/24 ] :container
 *
 * container_net_host_setup() (called from the parent, after clone, before the
 * child is released) creates a persistent bridge, makes a veth pair, attaches one
 * end to the bridge, and moves the other end into the child's network namespace.
 * The child then gives that end an address (your container_net_config()).
 *
 * It shells out to iproute2 (`ip`), which is why this is provided rather than
 * done against the raw netlink interface. Best-effort: a failure here is logged
 * but does not abort the container.
 */
#define _GNU_SOURCE
#include "container.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/wait.h>

/* Run `ip <args...>`; return its exit status (or -1). stderr is muted so that
 * idempotent operations (creating a bridge that already exists) stay quiet. */
static int run_ip(const char *const argv[])
{
    pid_t p = fork();
    if (p < 0) return -1;
    if (p == 0) {
        int null = open("/dev/null", O_WRONLY);
        if (null >= 0) { dup2(null, 2); close(null); }
        execvp("ip", (char *const *)argv);
        _exit(127);
    }
    int st;
    if (waitpid(p, &st, 0) < 0) return -1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

#define IP(...) run_ip((const char *const[]){ "ip", __VA_ARGS__, NULL })

int container_net_host_setup(struct container *c, pid_t child)
{
    char cidr[64], pid[16], tmp[16];
    snprintf(cidr, sizeof cidr, "%s/%d", CONTAINER_BRIDGE_GW, c->net_prefix);
    snprintf(pid, sizeof pid, "%d", (int)child);
    /* deterministic, unique per container: host veth + a temporary peer name */
    snprintf(c->net_host_if, sizeof c->net_host_if, "vh%d", (int)child);
    snprintf(tmp, sizeof tmp, "vc%d", (int)child);

    /* 1. the shared bridge (idempotent: it persists across containers). */
    IP("link", "add", CONTAINER_BRIDGE, "type", "bridge");
    IP("addr", "add", cidr, "dev", CONTAINER_BRIDGE);
    if (IP("link", "set", CONTAINER_BRIDGE, "up") != 0) {
        fprintf(stderr, "container: could not bring up bridge %s\n", CONTAINER_BRIDGE);
        return -1;
    }

    /* 2. a veth pair; one end on the bridge, up. */
    if (IP("link", "add", c->net_host_if, "type", "veth", "peer", "name", tmp) != 0) {
        fprintf(stderr, "container: could not create veth pair\n");
        return -1;
    }
    IP("link", "set", c->net_host_if, "master", CONTAINER_BRIDGE);
    IP("link", "set", c->net_host_if, "up");

    /* 3. move the other end into the child's netns, under its expected name. */
    if (IP("link", "set", tmp, "netns", pid, "name", c->net_ifname) != 0) {
        fprintf(stderr, "container: could not move veth into the container\n");
        IP("link", "del", c->net_host_if);
        return -1;
    }
    return 0;
}

int container_net_host_teardown(struct container *c)
{
    /* The container end vanished with its netns, taking its host peer with it;
     * delete by name anyway in case setup failed partway. The bridge persists. */
    if (c->net_host_if[0])
        IP("link", "del", c->net_host_if);
    return 0;
}
