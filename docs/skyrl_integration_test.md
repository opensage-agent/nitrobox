# SkyRL / Harbor Integration

## Overview

nitrobox replaces Harbor's `DockerEnvironment` in the SkyRL RL training framework, eliminating Docker container overhead during training rollouts. With userns mode (rootless), no root privileges are required — nitrobox runs as a normal user via Linux user namespaces.

## Architecture

SkyRL uses [Harbor](https://github.com/laude-institute/harbor) to manage sandboxed environments during RL training. The execution flow is:

```
SkyRL Training Loop (Ray-based)
  → HarborGenerator.generate(batch)          # async, runs in Ray worker
    → harbor_agent_loop() per trajectory      # up to MAX_CONCURRENCY parallel
      → Trial(TrialConfig)
        → EnvironmentFactory.create_environment_from_config()
          → BaseEnvironment.start()           # ← this is where Docker/nitrobox lives
        → Agent runs commands via env.exec()
        → Verifier runs tests via env.exec()
        → env.stop()
```

Harbor supports custom environment providers via `import_path` in config, which dynamically imports a class implementing `BaseEnvironment`.

## What Was Implemented

### 1. `NitroBoxLiteEnvironment` (Harbor provider)

**File:** `SkyRL_docker_test/examples/train_integrations/harbor/nitrobox_environment.py`

Implements Harbor's `BaseEnvironment` interface:

| Harbor method | nitrobox mapping |
|---|---|
| `start(force_build)` | Build Dockerfile → export rootfs (cached by content hash) → `Sandbox(config)` |
| `exec(cmd, cwd, env)` | `sb.run(cmd)` with cwd/env prepended via shell |
| `upload_file/dir` | `sb.copy_to()` or direct `shutil.copytree` on rootfs |
| `download_file/dir` | `sb.copy_from()` or direct `shutil.copytree` on rootfs |
| `stop(delete)` | `sb.delete()` |

Key features:
- **Rootfs caching:** Dockerfiles are hashed by content. All CodeContests tasks share the same Dockerfile, so the rootfs is built once and reused.
- **Bind mounts:** Trial log directories (`agent/`, `verifier/`, `artifacts/`) are bind-mounted into the sandbox at `/logs/*`, matching Docker's volume mount behavior.
- **WORKDIR extraction:** Automatically parses the Dockerfile to set the correct working directory.
- **Timing instrumentation:** Collects per-operation latencies (start/exec/stop) and prints a summary at process exit.

### 2. Run Scripts

- `run_harbor_gen_nitrobox.sh` — Generation-only test (10 samples, no training)
- `run_codecontest_nitrobox.sh` — Full training run

Both are identical to the Docker baselines except for:
```bash
harbor_trial_config.environment.type=null
harbor_trial_config.environment.import_path=examples.train_integrations.harbor.nitrobox_environment:NitroBoxLiteEnvironment
```

### 3. Dependency Configuration

`nitrobox` was added to SkyRL's `pyproject.toml` under the `harbor` optional dependency group:
```toml
harbor = [
    "harbor; python_version >= '3.12'",
    "nitrobox",
]

[tool.uv.sources]
nitrobox = { path = "/scratch/jingyang/nitrobox" }
```

## Running Without Root (Userns Mode)

nitrobox auto-detects privileges: when running as a non-root user, it uses Linux user namespaces (userns mode). This eliminates the Ray permission problem — **no root or sudo required**.

### How It Works

`Sandbox.__init__()` checks `os.geteuid()`:
- **UID 0** → rootful mode (real root, mount/unshare directly)
- **Non-zero UID** → userns mode (user namespaces, no root needed)

In userns mode, the Rust init chain:
1. `unshare(CLONE_NEWUSER)` to create a new user namespace
2. Parent calls `newuidmap`/`newgidmap` for full UID mapping (if `/etc/subuid` configured)
3. All mounts (overlayfs, proc, dev, volumes) happen inside the namespace
4. Security hardening (seccomp, capabilities, landlock) is applied
5. Mounts auto-cleanup when the shell process exits (no manual umount needed)

### Host Setup (One-Time)

```bash
# Install uidmap (provides newuidmap/newgidmap)
sudo apt-get install -y uidmap

# Configure subordinate UID/GID ranges
echo "$(whoami):200000:65536" | sudo tee -a /etc/subuid
echo "$(whoami):200000:65536" | sudo tee -a /etc/subgid

# Enable unprivileged user namespaces (if not already)
sudo sysctl -w kernel.unprivileged_userns_clone=1
```

### Ray Integration

Since userns mode doesn't need root, it works directly in Ray workers:

```
Ray worker (jingyang)
  → NitroBoxLiteEnvironment.start()
    → Sandbox(config)                # euid != 0 → userns mode
      → Rust init: unshare(CLONE_NEWUSER) + full UID mapping
    → Works without root ✓
```

No need to run the Ray cluster as root.

## Standalone Benchmark

The A/B comparison benchmark in `examples/benchmark.py` matches Harbor's Docker flow (`docker build` → `docker run -d` → `docker exec` × N → `docker rm -f`):

```bash
python examples/benchmark.py
```

## File Inventory

| File | Location | Purpose |
|---|---|---|
| `nitrobox_environment.py` | `SkyRL_docker_test/examples/train_integrations/harbor/` | Harbor BaseEnvironment provider |
| `run_harbor_gen_nitrobox.sh` | same directory | Generation-only test script |
| `run_codecontest_nitrobox.sh` | same directory | Full training test script |
| `NITROBOX_INTEGRATION.md` | same directory | Quick-reference doc |
| `benchmark.py` | `examples/` | A/B benchmark (Harbor-style flow) |
