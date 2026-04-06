// Package nsenter uses a CGO constructor to perform namespace entry
// before Go's multi-threaded runtime starts.
//
// setns(CLONE_NEWUSER) requires a single-threaded process. Go's runtime
// is always multi-threaded. By running setns in a C constructor
// (__attribute__((constructor))), we execute before Go starts its
// goroutine scheduler. This is the same approach runc uses.
//
// Triggered by _NITROBOX_NSENTER env var. If not set, the constructor
// returns immediately and Go starts normally.
package nsenter

/*
#cgo CFLAGS: -Wall
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sched.h>
#include <linux/sched.h>
#include <errno.h>
#include <sys/types.h>

// nsenter_init runs before Go's runtime.
// If _NITROBOX_NSENTER is set, performs setns + chroot + exec and never returns.
// Otherwise returns immediately, Go starts normally.
__attribute__((constructor)) void nsenter_init(void) {
    char *nsenter = getenv("_NITROBOX_NSENTER");
    if (nsenter == NULL || nsenter[0] == '\0')
        return;

    // Parse: _NITROBOX_NSENTER=<target_pid>
    int target_pid = atoi(nsenter);
    if (target_pid <= 0) {
        fprintf(stderr, "nsenter: invalid pid: %s\n", nsenter);
        _exit(1);
    }

    char *rootfs = getenv("_NITROBOX_NSENTER_ROOTFS");
    char *workdir = getenv("_NITROBOX_NSENTER_WORKDIR");
    char *mode = getenv("_NITROBOX_NSENTER_MODE");
    // Default: enter userns. Set MODE=rootful to skip userns entry.
    int userns = !(mode != NULL && strcmp(mode, "rootful") == 0);

    char path[256];
    int fd;

    if (userns) {
        // Enter user namespace FIRST (must be single-threaded)
        snprintf(path, sizeof(path), "/proc/%d/ns/user", target_pid);
        fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) {
            fprintf(stderr, "nsenter: open %s: %s\n", path, strerror(errno));
            _exit(1);
        }
        if (setns(fd, CLONE_NEWUSER) < 0) {
            fprintf(stderr, "nsenter: setns user: %s\n", strerror(errno));
            _exit(1);
        }
        close(fd);
    }

    // Enter mount namespace
    snprintf(path, sizeof(path), "/proc/%d/ns/mnt", target_pid);
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        fprintf(stderr, "nsenter: open %s: %s\n", path, strerror(errno));
        _exit(1);
    }
    if (setns(fd, CLONE_NEWNS) < 0) {
        fprintf(stderr, "nsenter: setns mnt: %s\n", strerror(errno));
        _exit(1);
    }
    close(fd);

    // Chroot
    if (userns) {
        if (chroot(rootfs) < 0) {
            fprintf(stderr, "nsenter: chroot %s: %s\n", rootfs, strerror(errno));
            _exit(1);
        }
    } else {
        // Rootful: chroot to target's root
        snprintf(path, sizeof(path), "/proc/%d/root", target_pid);
        int root_fd = open(path, O_RDONLY | O_CLOEXEC);
        if (root_fd >= 0) {
            fchdir(root_fd);
            close(root_fd);
            chroot(".");
        }
    }

    // Chdir
    if (workdir && workdir[0] != '\0') {
        chdir(workdir);
    } else {
        chdir("/");
    }

    // Find command: _NITROBOX_NSENTER_CMD=arg0\narg1\narg2
    char *cmd = getenv("_NITROBOX_NSENTER_CMD");
    if (cmd == NULL || cmd[0] == '\0') {
        fprintf(stderr, "nsenter: no command specified\n");
        _exit(1);
    }

    // Make a mutable copy (we'll replace \n with \0)
    int cmdlen = (int)strlen(cmd);
    char *cmdbuf = strdup(cmd);

    // Count args (number of \n separators + 1)
    int argc = 1;
    for (int i = 0; i < cmdlen; i++) {
        if (cmdbuf[i] == '\n') {
            cmdbuf[i] = '\0';
            argc++;
        }
    }

    char **argv = malloc(sizeof(char*) * (argc + 1));
    argv[0] = cmdbuf;
    int ai = 1;
    for (int i = 0; i < cmdlen && ai < argc; i++) {
        if (cmdbuf[i] == '\0') {
            argv[ai++] = &cmdbuf[i+1];
        }
    }
    argv[argc] = NULL;

    // Clear nsenter env vars so child doesn't re-trigger
    unsetenv("_NITROBOX_NSENTER");
    unsetenv("_NITROBOX_NSENTER_ROOTFS");
    unsetenv("_NITROBOX_NSENTER_WORKDIR");
    unsetenv("_NITROBOX_NSENTER_CMD");
    unsetenv("_NITROBOX_NSENTER_MODE");

    execvp(argv[0], argv);
    fprintf(stderr, "nsenter: exec %s: %s\n", argv[0], strerror(errno));
    _exit(127);
}
*/
import "C"
