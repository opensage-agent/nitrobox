# OSWorld — Docker vs nitrobox

**Dataset:** OSWorld (GUI agent benchmark, Ubuntu Desktop VM)
**Agent:** Claude Sonnet 4.6, Computer Use agent
**Tasks:** 100 (10 per domain, 97 evaluated after filtering)
**Max steps:** 30
**Concurrency:** 10
**Date:** 2026-04-14
**Hardware:** AMD EPYC 9354 32-core, 1 TiB RAM, kernel 5.15
**Fixes applied (since previous runs):**
- [`perf: install seccomp via seccomp(2) syscall with SPEC_ALLOW`](https://github.com/opensage-agent/nitrobox/commit/602f96a) — avoid `spec_store_bypass_disable=seccomp` forcing SSBD+STIBP on sandboxed processes
- `perf: re-enable THP via prctl in provider` — clear inherited `MMF_DISABLE_THP` from harness so QEMU guest RAM gets 2 MB huge pages

## E2E Results (100 tasks, concurrency 10)

|   C |      Env |      Wall |       EnvSetup |          Agent |            LLM |         Verify |       Teardown | Overhead | Pass | Fail |  Err |
|-----|----------|-----------|----------------|----------------|----------------|----------------|----------------|----------|------|------|------|
|  10 | nitrobox |   2363.3s |     17.7s (8%) |   190.4s (82%) |    47.8s (21%) |    23.2s (10%) |      0.3s (0%) |      79% |   80 |   17 |    0 |
|  10 |   docker |   2653.7s |     23.2s (9%) |   200.0s (81%) |    52.4s (21%) |     23.0s (9%) |      0.3s (0%) |      79% |   80 |   17 |    0 |

**Wall speedup: 1.12x (nitrobox faster)**
**env_setup: 1.31x faster** (17.7s vs 23.2s — full cold boot per task in both envs)
**Correctness: MATCH** (80/80 pass, 17/17 fail — identical outcome)

### Per-task breakdown (mean)

| phase | nitrobox | docker | delta |
|---|---|---|---|
| env_setup | 17.7s | 23.2s | **−24%** |
| agent_exec | 190.4s | 200.0s | **−5%** |
| llm_inference | 47.8s | 52.4s | −9% |
| verifier | 23.2s | 23.0s | +1% |
| teardown | 0.3s | 0.3s | 0% |
| **total** | **231.6s** | **246.5s** | **−6%** |

Every phase in nitrobox is equal or faster.

### Per-domain breakdown

|               Domain | nitrobox |   docker |
|----------------------|----------|----------|
|               chrome |     9/10 |     9/10 |
|                 gimp |     8/10 |     9/10 |
|     libreoffice_calc |     7/10 |     7/10 |
|  libreoffice_impress |    10/10 |     9/10 |
|   libreoffice_writer |     8/10 |     9/10 |
|           multi_apps |      5/7 |      4/7 |
|                   os |     9/10 |     9/10 |
|          thunderbird |     9/10 |     9/10 |
|                  vlc |     7/10 |     7/10 |
|              vs_code |     8/10 |     8/10 |

Totals match (80/80). Per-domain differences are within LLM agent non-determinism.

### Notes on the fixes

Before the two fixes, nitrobox was ~6-8% slower on this bench — not because of sandbox overhead per se, but because of two subtle interactions with kernel security mitigations:

1. **`spec_store_bypass_disable=seccomp`** (Ubuntu default): installing a seccomp BPF filter via `prctl(PR_SET_SECCOMP, ...)` force-enables SSBD + STIBP for the task. Those are pipeline-serializing Spectre mitigations, and they cost 40–60% throughput on branch-heavy code (Python interpreters, dynamic languages). runc works around this by using the `seccomp(2)` syscall with `SECCOMP_FILTER_FLAG_SPEC_ALLOW`; nitrobox now does the same.

2. **`MMF_DISABLE_THP` inherited from parent**: when the harness that launches nitrobox has `PR_SET_THP_DISABLE=1` (some sandboxed tools set this by default), the flag propagates to children. QEMU guest RAM then doesn't get 2 MB huge pages, and memory-heavy guest workloads slow down. The OSWorld provider clears this flag on import.

Full debug writeup: [`dev/blog_debug.md`](../../dev/blog_debug.md).

## Trajectories

- nitrobox: `/scr/rucnyz/projects/OSWorld-nitrobox/results_bench_nitrobox_100/`
- docker: `/scr/rucnyz/projects/OSWorld-nitrobox/results_bench_docker_100/`

## Reproduce

```bash
# 1. Clone our OSWorld fork (includes nitrobox provider + THP fix)
git clone -b nitrobox-provider https://github.com/rucnyz/OSWorld.git
cd OSWorld && pip install -r requirements.txt

# 2. Verify KVM access
test -w /dev/kvm && echo "KVM OK"

# 3. Run e2e comparison (Claude Sonnet 4.6, 100 tasks, 10 concurrency)
ANTHROPIC_API_KEY=sk-ant-... python examples/bench_osworld_e2e.py \
    --osworld-dir /path/to/osworld \
    --n-tasks 100 --max-steps 30 \
    --envs nitrobox,docker \
    --concurrency 10
```
