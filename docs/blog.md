# Why nitrobox is faster than Docker: the BuildKit backend

## TL;DR

nitrobox embeds BuildKit as a single binary, eliminating the Docker daemon overhead. On Terminal-Bench 2.0 (16 tasks, c=16), nitrobox achieves:

- **Warm cache**: env_setup 2.0s vs Docker 3.6s
- **Cold start (after --rmi)**: env_setup 2.2s vs Docker 26.8s (12x faster)
- **Teardown**: 1.0s vs Docker 4.0s (4x faster)
- **Wall time**: 290s vs 352s (17% faster)

## Architecture

Docker's image management spans three components:

```
docker compose up
  └── dockerd
        ├── BuildKit (build cache + solver)
        ├── containerd (image store + snapshotter)
        └── runc (container runtime)
```

nitrobox collapses this into one:

```
nitrobox
  └── nitrobox-core (single binary, 48MB)
        ├── embedded BuildKit (solver + cache + content store)
        ├── overlay snapshotter (direct layer access)
        └── Rust sandbox runtime (namespaces + overlayfs)
```

## Why cold start is fast

When Docker does `docker compose down --rmi all`, it:

1. Deletes image metadata from containerd's image store
2. Deletes unpacked snapshots (overlay layer directories)
3. **Keeps** content store blobs (compressed layer tarballs)
4. **Keeps** BuildKit build cache (solver cache + intermediate snapshots)

On the next `docker compose up`, containerd must:

1. Re-verify with the registry (manifest check) — ~1s
2. Skip downloading (blobs still in content store) — 0s
3. **Re-unpack ALL layers** into new snapshots — **~25s** ← bottleneck

When nitrobox does `--rmi`, it:

1. Deletes image entries from the registry file
2. **Keeps** content store blobs (same as Docker)
3. **Keeps** BuildKit solver cache + snapshots (same as Docker)

On the next pull, BuildKit:

1. Re-verifies with the registry (`no-cache` on solve) — ~1s
2. Solver cache hits on layer extraction — snapshots already exist — ~1s
3. **No re-unpack needed** — snapshots are still in the solver cache

The key difference: Docker has **two separate snapshot stores** (BuildKit's and containerd's). `--rmi` deletes containerd's snapshots but not BuildKit's. Since containerd needs its own snapshots to run containers, it must re-create them from blobs (~25s).

nitrobox has **one snapshot store** (BuildKit's). Since the sandbox reads layer paths directly from BuildKit's snapshotter, no re-unpack is ever needed. The solver cache IS the runtime cache.

## Why warm cache is fast

With warm cache (no --rmi), the difference is smaller:

| | nitrobox | Docker |
|---|---|---|
| env_setup | 2.0s | 3.6s |
| teardown | 1.0s | 4.0s |

nitrobox's env_setup is faster because:

- **No Docker daemon round-trip**: nitrobox queries the image registry (JSON file read) and resolves layers via the cache manager — all in-process, no gRPC to dockerd
- **No containerd round-trip**: layer paths come directly from BuildKit's snapshotter, no containerd image store lookup + snapshot prepare

Teardown is faster because:

- **No container deletion** (Docker must stop + remove the container via containerd)
- **No snapshot cleanup** via containerd
- nitrobox just unmounts overlayfs and deletes the upper dir — pure filesystem ops

## What about `docker system prune`?

Neither Docker's `--rmi` nor nitrobox's `--rmi` frees disk space. Both keep content store blobs and build cache. To actually reclaim space:

- Docker: `docker system prune`
- nitrobox: `nitrobox prune` (planned)

This is by design — `--rmi` means "re-verify on next use", not "free disk space".

## Summary

| | Docker | nitrobox | Why |
|---|---|---|---|
| Image store | containerd (bolt DB) | JSON file + BuildKit cache | One store, not two |
| Layer snapshots | containerd snapshotter | BuildKit snapshotter (direct) | No re-unpack on cold |
| Build cache | BuildKit solver | Same BuildKit solver | Identical |
| Container runtime | runc via containerd | Rust sandbox (namespaces) | No daemon overhead |
| Binary | dockerd + containerd + runc (~200MB) | nitrobox-core (48MB) | Single binary |
