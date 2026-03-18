# Future Improvements (New Kernel Features)

Based on Linux 6.5–6.19 kernel changes. Prioritized by practical value for AI agent sandbox.

## High Value

### pidfd (6.9+)
Replace raw PID usage with pidfd for more reliable process management. Avoids PID reuse races.
- `PIDFD_GET_INFO` ioctl (6.13): get process info without /proc parsing
- `pidfd_send_signal`: signal without PID race
- Use in `_RestoredProcess` and background process tracking (`_bg_handles`)

### Landlock ABI v8 TSYNC (6.18+)
`LANDLOCK_RESTRICT_SELF_TSYNC` enforces Landlock atomically across all threads, closing a race window where a thread could execute between `restrict_self` calls. Our current single-threaded init_script isn't affected, but multi-threaded sandbox processes would benefit.

### ID-mapped mounts (6.15+)
`open_tree_attr()` can create ID-mapped overlayfs mounts programmatically. Would enable cleaner rootless support without `unshare --map-root-user` workarounds. Could replace the current userns setup script approach.

### CLONE_NEWTIME (5.6+, stable)
Time namespace isolation. Critical for CRIU: after checkpoint/restore, monotonic clock jumps. With time namespace, the restored process sees continuous monotonic time. Add `--net` style `--time` to unshare command.

## Medium Value

### mseal(2) (6.10+)
Memory sealing — irreversibly prevent `mprotect`/`munmap`/`mremap` on sealed regions. Could seal the seccomp filter memory and security helper code to prevent in-process tampering. Call from the security helper after applying seccomp.

### listmount(2) / statmount(2) (6.8+)
Structured mount introspection syscalls, replacing `/proc/self/mountinfo` parsing. Extension in 6.11 supports querying foreign mount namespaces. Could improve `cleanup_stale()` and CRIU mount registration.

### process_madvise (5.10+)
Reclaim memory from idle sandboxes via pidfd without killing them (`MADV_PAGEOUT`, `MADV_COLD`). Useful when running many concurrent sandboxes — swap out idle ones to free RAM for active training.

### close_range(2) (5.9+)
Single syscall to close all FDs above a threshold. Could be used in sandbox setup to ensure no FD leaks. Currently we rely on `FD_CLOEXEC` and `exec`.

## Low Value / Future

### seccomp unotify improvements (6.6+)
`SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV` prevents TOCTOU attacks in supervisor-mode seccomp. Could enable selectively allowing `clone3` with flag inspection in userspace, instead of blocking it entirely.

### listns(2) (6.19)
Enumerate all namespaces system-wide. Could improve `cleanup_stale()` to find orphaned sandbox namespaces.

### cgroup v2 PSI (5.x+)
Pressure Stall Information — monitor CPU/memory/IO pressure per sandbox. Could be exposed as a monitoring API.
