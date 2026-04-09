# OSWorld — Docker vs nitrobox

**Dataset:** OSWorld (GUI agent benchmark, Ubuntu Desktop VM)
**Agent:** Claude Sonnet 4.6, Computer Use agent
**Tasks:** 10 (1 per domain)
**Max steps:** 30
**Concurrency:** 10
**Date:** 2026-04-09

## E2E Results

| Env | Tasks | Pass | Rate | Wall time |
|-----|-------|------|------|-----------|
| Docker | 10 | 9 | 90.0% | 429s |
| nitrobox | 10 | 9 | 90.0% | 441s |

**Per-task results identical.** Both fail only on `os` (snap install
blocked by sudo password issue in VM). Correctness parity confirmed.

### Per-domain breakdown

| Domain | Docker | nitrobox |
|--------|--------|----------|
| chrome | 1/1 | 1/1 |
| gimp | 1/1 | 1/1 |
| libreoffice_calc | 1/1 | 1/1 |
| libreoffice_impress | 1/1 | 1/1 |
| libreoffice_writer | 1/1 | 1/1 |
| multi_apps | 1/1 | 1/1 |
| os | 0/1 | 0/1 |
| thunderbird | 1/1 | 1/1 |
| vlc | 1/1 | 1/1 |
| vs_code | 1/1 | 1/1 |

## Prior Results (100 tasks, from PR #18)

| Phase | Docker | nitrobox | Speedup |
|-------|--------|----------|---------|
| **environment_setup** | **33.2s** | **7.0s** | **4.7x** |
| agent_execution | 174.2s | 157.6s | 1.1x |
| verifier | 22.5s | 22.5s | 1.0x |
| **total per task** | **230.0s** | **187.1s** | **1.2x** |

### Concurrent VM Reset (from PR #18)

| Concurrency | Docker | nitrobox | Speedup |
|-------------|--------|----------|---------|
| 4 | 16.2s | 2.5s | 6.6x |
| 8 | 18.5s | 2.3s | 8.0x |
| 16 | 22.0s | 2.6s | **8.5x** |

## Reproduce

```bash
# 1. Clone our OSWorld fork (includes nitrobox provider + --api_provider fix)
git clone -b nitrobox-provider https://github.com/rucnyz/OSWorld.git
cd OSWorld && pip install -r requirements.txt

# 2. Verify KVM access
test -w /dev/kvm && echo "KVM OK"

# 3. Run e2e comparison (Claude Sonnet 4.6, 10 tasks)
ANTHROPIC_API_KEY=sk-ant-... python examples/bench_osworld_e2e.py \
    --osworld-dir /path/to/osworld \
    --n-tasks 10 --max-steps 30 \
    --envs docker,nitrobox \
    --concurrency 10

# 4. Concurrent VM reset benchmark
python examples/bench_osworld_concurrent.py \
    --qcow2 /path/to/Ubuntu.qcow2 \
    --concurrency 1,4,8,16
```
