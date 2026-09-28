/* util.c: provided helpers for the container runtime. */
#define _GNU_SOURCE
#include "container.h"

#include <fcntl.h>
#include <string.h>
#include <unistd.h>
#include <stdio.h>
#include <errno.h>

int write_file(const char *path, const char *value)
{
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        fprintf(stderr, "container: open '%s': %s\n", path, strerror(errno));
        return -1;
    }
    size_t n = strlen(value);
    ssize_t w = write(fd, value, n);
    int err = errno;
    close(fd);
    if (w < 0 || (size_t)w != n) {
        fprintf(stderr, "container: write '%s': %s\n", path, strerror(err));
        return -1;
    }
    return 0;
}
