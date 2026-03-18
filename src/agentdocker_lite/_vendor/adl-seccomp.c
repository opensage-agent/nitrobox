/*
 * Security helper for agentdocker-lite sandboxes.
 * 1. Drop non-essential capabilities
 * 2. Mask sensitive paths (bind /dev/null over them)
 * 3. Make kernel paths read-only
 * 4. Apply seccomp BPF filter from /tmp/.adl_seccomp.bpf
 * 5. exec argv[1..]
 *
 * Runs AFTER pivot_root (so paths are relative to new root) but
 * BEFORE the shell starts — seccomp is inherited across exec.
 *
 * Build: gcc -static -Os -o adl-seccomp adl_seccomp_helper.c && strip adl-seccomp
 */
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define PR_CAPBSET_DROP 24
#define CAP_LAST_CAP 41

static const int keep_caps[] = {
    0, 1, 3, 4, 5, 6, 7, 8, 10, 18, 27, 29, 31, -1
};

static const char *masked_paths[] = {
    "/proc/kcore", "/proc/keys", "/proc/timer_list",
    "/proc/sched_debug", "/sys/firmware", "/proc/scsi", NULL
};

static const char *readonly_paths[] = {
    "/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys",
    "/proc/sysrq-trigger", NULL
};

static int should_keep(int cap) {
    for (int i = 0; keep_caps[i] >= 0; i++)
        if (keep_caps[i] == cap) return 1;
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: adl-seccomp PROGRAM [ARGS...]\n");
        return 1;
    }

    /* 1. Drop capabilities */
    for (int cap = 0; cap <= CAP_LAST_CAP; cap++) {
        if (!should_keep(cap))
            prctl(PR_CAPBSET_DROP, cap, 0, 0, 0);
    }

    /* 2. Mask sensitive paths */
    for (int i = 0; masked_paths[i]; i++) {
        struct stat st;
        if (stat(masked_paths[i], &st) != 0)
            continue;
        if (S_ISDIR(st.st_mode))
            mount("tmpfs", masked_paths[i], "tmpfs", 0, NULL);
        else
            mount("/dev/null", masked_paths[i], NULL, MS_BIND, NULL);
    }

    /* 3. Read-only paths */
    for (int i = 0; readonly_paths[i]; i++) {
        if (mount(readonly_paths[i], readonly_paths[i], NULL, MS_BIND, NULL) == 0)
            mount(NULL, readonly_paths[i], NULL, MS_BIND | MS_REMOUNT | MS_RDONLY, NULL);
    }

    /* 4. Apply seccomp BPF from file */
    int fd = open("/tmp/.adl_seccomp.bpf", O_RDONLY);
    if (fd >= 0) {
        off_t size = lseek(fd, 0, SEEK_END);
        lseek(fd, 0, SEEK_SET);

        if (size > 0 && size % sizeof(struct sock_filter) == 0) {
            struct sock_filter *prog = malloc(size);
            if (prog && read(fd, prog, size) == size) {
                struct sock_fprog fprog = {
                    .len = size / sizeof(struct sock_filter),
                    .filter = prog,
                };
                prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
                prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &fprog);
                free(prog);
            }
        }
        close(fd);
        unlink("/tmp/.adl_seccomp.bpf");
    }

    /* 5. exec target */
    execvp(argv[1], argv + 1);
    perror("exec");
    return 127;
}
