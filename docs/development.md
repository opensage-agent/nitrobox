# Development

## Vendored binaries

The pip package bundles static binaries in `src/agentdocker_lite/_vendor/`:

| Binary | Purpose | Size | Source |
|---|---|---|---|
| `pasta` / `pasta.avx2` | NAT'd networking + port mapping | ~1.3MB | [passt](https://passt.top/) |
| `criu` | Process checkpoint/restore | ~2.8MB | [seqeralabs/criu-static](https://github.com/seqeralabs/criu-static/releases) v4.2 |
| `adl-seccomp` | Seccomp BPF + cap drop + mask/readonly paths | ~750KB | Built from `_vendor/adl-seccomp.c` |

### Rebuilding adl-seccomp

```bash
gcc -static -Os -o src/agentdocker_lite/_vendor/adl-seccomp \
    src/agentdocker_lite/_vendor/adl-seccomp.c && \
    strip src/agentdocker_lite/_vendor/adl-seccomp
```

Requires `gcc` and static glibc. Must be statically linked — it runs inside minimal rootfs containers.

What `adl-seccomp` does (in order):
1. Drops non-essential capabilities (keeps Docker-default 13)
2. Masks sensitive paths (`/proc/kcore`, `/proc/keys`, etc.)
3. Remounts kernel paths (`/proc/sys`, `/proc/bus`, etc.) read-only
4. Reads BPF bytecode from `/tmp/.adl_seccomp.bpf` and applies seccomp
5. `exec`s its arguments — seccomp filter inherited across exec

**Note:** Currently only used in rootful mode. Userns mode still uses the Python-based seccomp helper.

### Regenerating protobuf

```bash
protoc --python_out=src/agentdocker_lite/_vendor/ rpc.proto
```

## Running tests

```bash
sudo python -m pytest tests/ -v                    # all tests
sudo python -m pytest tests/test_checkpoint.py -v   # CRIU tests
python -m pytest tests/test_security.py -v -k "UserNamespace"  # rootless
```

## Architecture (rootful mode)

```
Host Python process
  └─ unshare --pid --mount --uts --ipc [--time] --fork bash -c '
       mount /proc, /dev on rootfs (host tools)
       pivot_root into rootfs
       exec setsid adl-seccomp /bin/sh
     '
       └─ adl-seccomp: cap drop → mask → readonly → seccomp → exec /bin/sh
            └─ /bin/sh (persistent shell, reads commands via stdin pipe)
```
