# Userns Mode Fixes for SkyRL/Harbor Integration

Summary of all fixes required to make nitrobox work as a drop-in replacement for Docker in the SkyRL + Harbor RL training pipeline (rootless/userns mode).

## Test Result

```
Generating Trajectories: 100%|██████████| 1/1 [00:49<00:00, 49.30s/it]
# of masked instances: 0 / 1
# of timeout trajectories: 0
# of error trajectories: 0
```

## Fix 1: Full UID Mapping (`newuidmap`/`newgidmap`)

**Problem:** When nitrobox runs in rootless (user namespace) mode with only `--map-root-user`, only UID 0 is mapped. Programs that switch to non-root users (like `apt-get` dropping to `_apt` uid 42) fail with `EINVAL`.

**Fix:** Auto-detect subordinate UID ranges via `/etc/subuid` and apply full mapping with `newuidmap`/`newgidmap`.

### Current Implementation

1. **Detection** (`Sandbox._detect_subuid_range()` in `sandbox.py`):
   - Checks if `newuidmap`/`newgidmap` binaries exist
   - Parses `/etc/subuid` for the current user's subordinate range
   - Returns `(outer_uid, sub_start, sub_count)` or `None` (graceful fallback)
   - Result is cached class-wide

2. **Mapping** (Rust init chain in `init.rs`):
   - Child `unshare(CLONE_NEWUSER)`, blocks on sync pipe
   - Parent calls `newuidmap`/`newgidmap` with full range (if available)
   - Falls back to writing `/proc/{pid}/uid_map` directly with single mapping
   - Parent signals child via pipe → child proceeds with setup

3. **Result**: Full UID mapping enables `apt-get`, `useradd`, `chown`, `setgroups()`.

### Host Setup (One-Time)

```bash
sudo apt-get install -y uidmap
echo "$(whoami):200000:65536" | sudo tee -a /etc/subuid
echo "$(whoami):200000:65536" | sudo tee -a /etc/subgid
```

## Fix 2: DNS Propagation

**Problem:** `apt-get update` fails — DNS resolution doesn't work inside the sandbox.

**Root cause:** Docker-exported rootfs has an empty `/etc/resolv.conf`. Docker normally bind-mounts the host's resolv.conf at runtime; the export doesn't include that.

**Fix:** Rust init chain's `propagate_dns()` copies the host's `/etc/resolv.conf` into the sandbox rootfs if the sandbox copy is empty or missing. Runs in userns mode after overlayfs mount.

## Fix 3: `/tmp` Permissions

**Problem:** `apt-key` fails with `Couldn't create temporary file /tmp/apt.conf.xxx`.

**Root cause:** Docker-exported rootfs has `/tmp` with `775` instead of `1777`. The `_apt` user can't write.

**Fix:** Rust init chain's `fix_tmp_perms()` does `fchmodat(/tmp, 0o1777)` in userns mode after overlayfs mount.

## Fix 4: `/dev` Setup in Userns Mode

**Problem:** Programs fail because `/dev/null`, `/dev/zero`, etc. are missing or broken.

**Root cause:** User namespaces can't create device nodes via `mknod` (requires real root).

**Fix:** Rust init chain's `setup_dev_rootless()`:
1. Mounts tmpfs at `/dev`
2. Bind-mounts individual device nodes from host (`/dev/null`, `/dev/zero`, `/dev/full`, `/dev/random`, `/dev/urandom`, `/dev/tty`)
3. Creates symlinks (`/dev/fd`, `/dev/stdin`, `/dev/stdout`, `/dev/stderr` → `/proc/self/fd/*`)
4. Mounts `devpts` at `/dev/pts` with `newinstance,ptmxmode=0666`
5. Creates `/dev/ptmx` → `pts/ptmx` symlink

This differs from rootful mode, which creates real device nodes via `mknod`.

## Fix 5: `ExecResult.stderr` Must Be String (SkyRL side)

**Problem:** Harbor's terminus-2 agent crashes with `AttributeError: 'NoneType' object has no attribute 'strip'` on `set_history_result.stderr.strip()`.

**Root cause:** nitrobox merges stderr into stdout. The environment provider returned `stderr=None`, but Harbor expects a string.

**Fix:** Return `stderr=""` instead of `stderr=None` in `nitrobox_environment.py`.

## Architecture Note

All initialization (namespace setup, UID mapping, mounts, device creation, security hardening) is handled by the **Rust init chain** (`rust/src/init.rs`). There is no Python-side setup script generation. The Python `Sandbox` class builds a config dict and passes it to `py_spawn_sandbox()`, which calls the Rust child init function.

## All Files Changed

### nitrobox

| File | Changes |
|---|---|
| `rust/src/init.rs` | Full init chain: userns/rootful auto-detect, UID mapping, overlayfs, dev setup, DNS, /tmp perms, security |
| `src/nitrobox/sandbox.py` | `_detect_subuid_range()`, auto-select rootful/userns |
| `src/nitrobox/_shell.py` | `subuid_range` param forwarded to Rust spawn |

### SkyRL

| File | Changes |
|---|---|
| `examples/.../nitrobox_environment.py` | Use `Sandbox()` (auto-detect), `stderr=""` |
| `examples/.../run_harbor_gen_nitrobox.sh` | `NUM_GPUS` from env var with default |

## Debugging

```python
from nitrobox import Sandbox, SandboxConfig
sb = Sandbox(SandboxConfig(image='ubuntu:22.04'), 'debug')

sb.run('cat /proc/self/uid_map')    # Should show full mapping (2 lines)
sb.run('cat /etc/resolv.conf')      # Should have nameserver entries
sb.run('ls -la /dev/null /dev/pts/') # Should exist
sb.run('stat -c %a /tmp')           # Should be 1777
sb.run('apt-get update 2>&1', timeout=60)  # Should work

sb.delete()
```
