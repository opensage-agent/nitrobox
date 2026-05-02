# Nitrobox Benchmarks

Track how nitrobox compares to Docker across real-world datasets, plus
stand-alone micro-benchmarks.

## Harbor dataset compatibility

Each dataset is run against Docker as baseline. A dataset is
**supported** when every task that passes on Docker also passes on
nitrobox.

| Dataset                                           | Version | Tasks | Status     | Notes                                |
|---------------------------------------------------|---------|-------|------------|--------------------------------------|
| [terminal-bench](results/tb2.md)                  | 2.0     | 89    | **match**  | 4 both-fail (task bugs)              |
| [swebench-verified](results/swebench_verified.md) | —       | 500   | **match**  | 5 upstream-broken tasks fail on both |
| swebenchpro                                       | —       | —     | Not tested |                                      |
| swesmith                                          | —       | —     | Not tested |                                      |
| swtbench                                          | —       | —     | Not tested |                                      |

## Prerequisites

```bash
# 1. Install nitrobox + system helpers
uv sync --all-extras --dev
nitrobox setup

# 2. Install uidmap (rootless multi-UID mapping)
sudo apt-get install -y uidmap

# 3. Clone + install harbor
git clone https://github.com/rucnyz/harbor.git
cd harbor && uv sync --all-extras --dev

# 4. (optional) docker login to avoid Docker Hub rate limits
docker login
```

## Harbor E2E (`bench_harbor_e2e.py`)

Compares Docker vs nitrobox as harbor's execution environment across
any dataset harbor supports (named `-d <dataset>@<version>` or local
`-p <path>`).

```bash
# Named dataset (auto-downloads to ~/.cache/harbor/tasks/)
python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset terminal-bench@2.0 \
    --agent oracle \
    --n-tasks 40 --concurrency 4 \
    --envs docker,nitrobox

# Full concurrency sweep, results saved for plotting
python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset terminal-bench@2.0 \
    --agent oracle \
    --n-tasks 100 --concurrency 1,4,8,16,32 \
    --envs docker,nitrobox \
    --output results.json

# With a real LLM agent
ANTHROPIC_API_KEY=sk-ant-... python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset terminal-bench@2.0 \
    --agent claude-code --model anthropic/claude-sonnet-4-6 \
    --n-tasks 100 --concurrency 1,4,8,16,32
```

## Validating a new dataset

Two passes per dataset — first verifies correctness and collects cold
numbers, second measures the warm-cache steady state.

### 1. Correctness + performance (oracle, cold → warm)

```bash
# Cold: first run populates caches, --no-delete keeps them
python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset <dataset>@<version> \
    --agent oracle --concurrency 4 \
    --envs docker,nitrobox --no-delete

# Warm: second run reuses caches, default --delete cleans up after
python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset <dataset>@<version> \
    --agent oracle --concurrency 4 \
    --envs docker,nitrobox
```

### 2. Real agent (LLM overhead)

The `llm_inference` column shows API time; `overhead` is everything
else (sandbox + verifier + teardown).

```bash
ANTHROPIC_API_KEY=sk-ant-... python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset <dataset>@<version> \
    --agent terminus-2 \
    --model anthropic/claude-sonnet-4-6 \
    --n-tasks 3 --concurrency 1 \
    --envs docker,nitrobox
```

## DT-Agent eval e2e (`bench_dt.py`)

Subprocess-calls [DecodingTrust-Agent](https://github.com/AI-secure/DecodingTrust-Agent)'s
own `eval/evaluation.py` once per backend on the same task list. Same
shape as `bench_harbor_e2e.py` — the only thing that swaps between
runs is the `SANDBOX_BACKEND={docker,nbx}` env var, which DT-Agent's
`TaskExecutor` already honours. Per-instance timings come from the
`[TIMING:{backend}:...]` lines that `utils/env_backend.py` emits.

```bash
# Default: small task list against both backends
python examples/bench_dt.py --dt-dir /path/to/DecodingTrust-Agent

# Specific task list, parallel
python examples/bench_dt.py \
    --dt-dir /path/to/DecodingTrust-Agent \
    --task-list scripts/e2e_task_lists/test_docker_envs.jsonl \
    --max-parallel 4

# Compare against docker-engine's default healthcheck start_interval
# (5s) for nitrobox to quantify the override gain
python examples/bench_dt.py \
    --dt-dir /path/to/DecodingTrust-Agent \
    --healthcheck-start-interval 5.0
```

Sample warm-cache run, 1 task, c=1 (`legal/bankruptcy_law/2`):

|   Backend |  Wall | create | health | shutdown | Pass-parity |
|-----------|------:|-------:|-------:|---------:|------------:|
|    docker | 17.4s |  0.39s |  6.37s |   10.33s |       0/1 ✓ |
|  nitrobox |  1.6s |  0.10s |  1.00s |    0.15s |       0/1 ✓ |
| **speedup** | **10.97×** | **3.90×** | **6.37×** | **68.87×** | |

(Both backends fail the task identically — same LLM-API config issue —
which is exactly the parity check we want; the backends themselves
introduce zero infra errors.)

The shutdown gap (~70×) is structural: nitrobox flips an overlayfs
upper-layer rename (~30 ms) where Docker does `stop → remove →
network teardown → volume gc`. The health-wait gap comes from
nitrobox's `healthcheck_overrides={"start_interval": 0.5}` (the
`bench_dt.py` default) routing through `ComposeProject` without
touching the upstream compose file — docker engine's own default
keeps it on a 5 s cadence that bench_dt can't change.

## Micro Benchmark

```bash

python examples/micro_benchmark.py --help            # Full per-op comparison (all backends)
```

## Clean state

```bash
# Kill any leftover sandboxes + remove orphan state dirs
nitrobox cleanup

# Harbor caches
rm -rf ~/.cache/harbor/tasks/
rm -rf /path/to/harbor/jobs/bench_*

# Docker images pulled for terminal-bench (alexgshaw/*)
docker images --format "{{.Repository}}:{{.Tag}}" | grep alexgshaw | xargs -r docker rmi -f
```
