# lite-sandbox

Lightweight Linux namespace sandbox with persistent shell and instant filesystem reset.

**20x faster lifecycle** than Docker. Designed for high-frequency workloads like RL training where environments are created, reset, and destroyed thousands of times.

## Key features

- **Persistent shell**: ~42ms per command (vs ~330ms with fork/exec/chroot per command)
- **Instant reset**: overlayfs ~27ms, btrfs ~28ms -- clear filesystem without recreating the sandbox
- **Fast lifecycle**: ~4ms create, ~6ms delete (overlayfs)
- **Signal-pipe protocol**: uses a separate fd for command completion signaling -- no sentinel collision with command output
- **CoW filesystem backends**: overlayfs (default) or btrfs snapshots
- **cgroup v2**: optional CPU, memory, PID limits
- **Auto rootfs**: pass a Docker image name, rootfs is auto-prepared and cached

## Requirements

- Linux with kernel supporting overlayfs (or btrfs)
- Root or `CAP_SYS_ADMIN` (for mount/cgroup)
- `util-linux` (`unshare`)
- Docker (only for auto-preparing rootfs from image names)
- Python >= 3.10

## Install

```bash
cd lite-sandbox
pip install -e .
```

## Quick start

```python
from lite_sandbox import Sandbox, SandboxConfig

config = SandboxConfig(
    image="ubuntu:22.04",       # Docker image or path to rootfs dir
    working_dir="/workspace",
)

sb = Sandbox(config, name="worker-0")

# Run commands (~42ms each)
output, ec = sb.run("echo hello world")

# Direct file I/O (bypasses shell)
sb.write_file("/workspace/test.txt", "content")
content = sb.read_file("/workspace/test.txt")

# Reset filesystem to initial state (~27ms)
sb.reset()

# Cleanup
sb.delete()
```

## Configuration

```python
SandboxConfig(
    image="ubuntu:22.04",           # Docker image or rootfs path
    working_dir="/workspace",       # Initial cwd inside sandbox
    environment={"FOO": "bar"},     # Extra env vars
    volumes=["/host/path:/container/path:ro"],  # Bind mounts
    fs_backend="overlayfs",         # "overlayfs" or "btrfs"
    env_base_dir="/tmp/lite_sandbox",
    rootfs_cache_dir="/tmp/lite_sandbox_rootfs_cache",
    cpu_max="50000 100000",         # cgroup cpu.max
    memory_max="536870912",         # cgroup memory.max (bytes)
    pids_max="256",                 # cgroup pids.max
)
```

## API

| Method | Description |
|--------|-------------|
| `sb.run(cmd, timeout=None)` | Run command, returns `(output, exit_code)` |
| `sb.reset()` | Reset filesystem to initial state |
| `sb.delete()` | Full cleanup (unmount, remove cgroup, delete files) |
| `sb.copy_to(local, container)` | Copy file into sandbox |
| `sb.copy_from(container, local)` | Copy file out of sandbox |
| `sb.read_file(path)` | Read file content |
| `sb.write_file(path, content)` | Write file content |
| `sb.rootfs` | Host path to sandbox rootfs |

## Examples

```bash
# Basic usage
sudo python examples/basic_usage.py

# 32-worker concurrent benchmark
sudo python examples/concurrent_sandboxes.py
```

## Architecture

```
Host kernel (shared)
  |
  +-- Sandbox "worker-0"
  |     +-- PID namespace (unshare --pid)
  |     +-- Mount namespace (unshare --mount)
  |     +-- chroot into overlayfs rootfs
  |     |     +-- lowerdir: shared base image (read-only)
  |     |     +-- upperdir: per-sandbox changes (cleared on reset)
  |     +-- Persistent bash process (stdin/stdout pipes + signal fd)
  |     +-- cgroup v2 limits
  |
  +-- Sandbox "worker-1"
  |     +-- (same structure, independent namespaces)
  ...
```
