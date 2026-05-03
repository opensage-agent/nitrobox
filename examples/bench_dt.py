#!/usr/bin/env python3
"""Benchmark: DecodingTrust-Agent eval/evaluation.py — Docker vs nitrobox.

Mirrors ``bench_harbor_e2e.py``: subprocess-calls DT-Agent's own
``eval/evaluation.py`` once per backend on the same task list, parses
the per-instance ``[TIMING:{backend}:...]`` lines that
``utils/env_backend.py`` emits, and prints a side-by-side phase
comparison + correctness check.

Backend selection is the env var ``SANDBOX_BACKEND={docker,nbx}``,
which DT-Agent's ``TaskExecutor._get_backend_type()`` already honours.
We don't bypass anything — same eval entrypoint, same task runner,
same MCP plumbing — only the sandbox backend swaps.

The aggregated table mirrors the bench_harbor_e2e one:
    | Env | Trials | Pass | Fail | Err | Wall | Setup | Reset | Teardown |

Setup:
    git clone https://github.com/AI-secure/DecodingTrust-Agent.git
    cd DecodingTrust-Agent && pip install -r requirements.txt
    set -a && source .env && set +a    # OPENAI_API_KEY etc.

Usage:
    # Default: 1-task smoke against both backends
    python examples/bench_dt.py --dt-dir /path/to/DecodingTrust-Agent

    # Specific task list, both backends
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent \\
        --task-list scripts/e2e_task_lists/test_docker_envs.jsonl \\
        --max-parallel 2

    # Only run nitrobox (re-use a prior docker run for comparison)
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent \\
        --backends nitrobox

    # Override healthcheck start_interval (default 0.5s; 5.0s = docker
    # engine default — useful to quantify the gain)
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent \\
        --healthcheck-start-interval 5.0

Environment variables:
    DT_AGENT_DIR  Path to DecodingTrust-Agent checkout (alt to --dt-dir)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# ── stdout parsing ────────────────────────────────────────────────────

# `[TIMING:nbx:pool_legal_1_2268831] create=6.12s health_wait=6.34s`
# `[TIMING:docker:pool_finance_1_42] reset=10.86s`
# `[TIMING:nbx-vm:pool_macos_1_77] create=12.3s mcp_wait=2.1s`
_TIMING_RE = re.compile(
    r"\[TIMING:(?P<backend>nbx-vm|nbx|docker):(?P<inst>[^\]]+)\]\s+(?P<rest>.+)$"
)
_FIELD_RE = re.compile(r"(\w+)=([\d.]+)s")
_SUMMARY_TOTAL_RE = re.compile(r"Total tasks\s*:\s*(\d+)")
_SUMMARY_SUCC_RE = re.compile(r"Succeeded\s*:\s*(\d+)")
_SUMMARY_FAIL_RE = re.compile(r"Failed\s*:\s*(\d+)")
_FAILED_INST_RE = re.compile(r"\[EXECUTOR\] Failed to start instance \w+:")


def _parse_eval_output(text: str) -> dict:
    """Extract per-phase timings and pass/fail counts from eval stdout."""
    phases: dict[str, list[float]] = defaultdict(list)
    instances_seen: set[str] = set()

    for line in text.splitlines():
        m = _TIMING_RE.search(line)
        if m:
            inst = m.group("inst")
            instances_seen.add(inst)
            for field, val in _FIELD_RE.findall(m.group("rest")):
                phases[field].append(float(val))

    total = succ = fail = 0
    for line in text.splitlines():
        if (m := _SUMMARY_TOTAL_RE.search(line)):
            total = int(m.group(1))
        if (m := _SUMMARY_SUCC_RE.search(line)):
            succ = int(m.group(1))
        if (m := _SUMMARY_FAIL_RE.search(line)):
            fail = int(m.group(1))

    failed_to_start = sum(1 for line in text.splitlines() if _FAILED_INST_RE.search(line))

    return {
        "instances_started": len(instances_seen),
        "phases": dict(phases),
        "total_tasks": total,
        "succeeded": succ,
        "failed": fail,
        "infra_errors": failed_to_start,
    }


# ── runner ────────────────────────────────────────────────────────────


def _find_dt_dir(hint: str | None) -> str | None:
    candidates = [
        hint,
        os.environ.get("DT_AGENT_DIR"),
        "../DecodingTrust-Agent",
        "../../DecodingTrust-Agent",
    ]
    for c in candidates:
        if c and Path(c).is_dir() and (Path(c) / "eval" / "evaluation.py").is_file():
            return str(Path(c).resolve())
    return None


def run_eval(
    dt_dir: str,
    task_list: str,
    backend: str,
    max_parallel: int,
    agent_type: str,
    model: str,
    healthcheck_start_interval: float,
    extra_args: list[str],
) -> tuple[dict, float, str]:
    """Subprocess-call DT-Agent's eval pipeline; return (parsed, wall, raw)."""
    cmd = [
        sys.executable, "eval/evaluation.py",
        "--task-list", task_list,
        "--max-parallel", str(max_parallel),
        "--agent-type", agent_type,
        "--model", model,
        "--skip-judge",
        *extra_args,
    ]
    env = {
        **os.environ,
        "PYTHONPATH": dt_dir,
        # Backend selector — DT-Agent's TaskExecutor reads this. We
        # spell the nitrobox backend "nbx" because that's what
        # _get_backend_type() expects in DT-Agent.
        "SANDBOX_BACKEND": "nbx" if backend == "nitrobox" else "docker",
        # Override applies only on the nbx side. NbxBackend reads
        # this and threads it into ComposeProject(healthcheck_overrides=...);
        # docker side ignores it (handled by docker engine's own ticker).
        "NBX_HEALTHCHECK_START_INTERVAL": str(healthcheck_start_interval),
    }

    print(f"  → {backend}: {' '.join(cmd)}", flush=True)
    t0 = time.monotonic()
    result = subprocess.run(
        cmd, cwd=dt_dir, env=env,
        capture_output=True, text=True,
        timeout=3600,
    )
    wall = time.monotonic() - t0

    if result.returncode != 0:
        # Don't bail — failed eval still has timings worth reporting.
        print(f"  [WARN] eval exited rc={result.returncode}", flush=True)

    parsed = _parse_eval_output(result.stdout + result.stderr)
    parsed["wall_s"] = wall
    parsed["returncode"] = result.returncode
    return parsed, wall, result.stdout + result.stderr


# ── reporting ─────────────────────────────────────────────────────────


def _avg(xs: list[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def _print_table(results: dict[str, dict]) -> None:
    print(f"\n{'='*92}")
    print(f"  DT-Agent eval/evaluation.py — Docker vs nitrobox")
    print(f"{'='*92}\n")

    header = (
        f"{'Backend':>10} {'Wall':>8} {'Trials':>7} {'Pass':>5} {'Fail':>5} {'Err':>5}"
        f"  {'create':>8} {'health':>8} {'reset':>8} {'shutdown':>9}"
    )
    print(header)
    print("-" * len(header))

    for backend, r in results.items():
        ph = r["phases"]
        print(
            f"{backend:>10} {r['wall_s']:7.1f}s {r['total_tasks']:>7} "
            f"{r['succeeded']:>5} {r['failed']:>5} {r['infra_errors']:>5}  "
            f"{_avg(ph.get('create', [])):7.2f}s {_avg(ph.get('health_wait', [])):7.2f}s "
            f"{_avg(ph.get('reset', [])):7.2f}s {_avg(ph.get('shutdown', [])):8.2f}s"
        )

    if "docker" in results and "nitrobox" in results:
        d, n = results["docker"], results["nitrobox"]
        print(f"\n{'-'*92}")
        wall_speedup = d["wall_s"] / n["wall_s"] if n["wall_s"] else float("inf")
        print(f"  Wall speedup (docker/nitrobox): {wall_speedup:.2f}×")

        # Pass-count parity check (both should match within flake noise)
        if d["succeeded"] == n["succeeded"]:
            print(f"  Pass-count parity: ✓ both {d['succeeded']}/{d['total_tasks']}")
        else:
            print(
                f"  Pass-count parity: ✗ docker={d['succeeded']}/{d['total_tasks']} "
                f"nitrobox={n['succeeded']}/{n['total_tasks']}"
            )

        # Per-phase breakdown
        print(f"\n  {'Phase':>10} {'Docker':>10} {'NitroBox':>10} {'Speedup':>10}")
        for phase in ("create", "health_wait", "reset", "shutdown"):
            dv = _avg(d["phases"].get(phase, []))
            nv = _avg(n["phases"].get(phase, []))
            sp = (dv / nv) if nv else float("inf")
            sp_str = f"{sp:.2f}×" if nv else "—"
            print(f"  {phase:>10} {dv:9.2f}s {nv:9.2f}s {sp_str:>10}")
    print()


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(
        description="Bench DT-Agent eval/evaluation.py: Docker vs nitrobox",
    )
    p.add_argument("--dt-dir", help="Path to DecodingTrust-Agent checkout (or DT_AGENT_DIR)")
    p.add_argument(
        "--task-list", default="scripts/e2e_task_lists/test_docker_envs.jsonl",
        help="Path to a DT-Agent task list (relative to --dt-dir or absolute)",
    )
    p.add_argument(
        "--backends", default="docker,nitrobox",
        help="Comma-separated backends to run (default: docker,nitrobox)",
    )
    p.add_argument("--max-parallel", type=int, default=1)
    p.add_argument("--agent-type", default="openaisdk")
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--healthcheck-start-interval", type=float, default=0.5,
                   help="nitrobox-side healthcheck start_interval override (default 0.5s)")
    p.add_argument("--output", help="Save raw eval stdout per backend to <prefix>_<backend>.log")
    p.add_argument("extra_args", nargs="*",
                   help="Extra args passed verbatim to eval/evaluation.py "
                        "(e.g. -- --skip-mcp --debug)")
    args = p.parse_args()

    dt_dir = _find_dt_dir(args.dt_dir)
    if not dt_dir:
        print("ERROR: pass --dt-dir or set DT_AGENT_DIR", file=sys.stderr)
        return 1

    task_list = args.task_list
    if not os.path.isabs(task_list):
        task_list = str(Path(dt_dir) / task_list)
    if not Path(task_list).exists():
        print(f"ERROR: task list not found: {task_list}", file=sys.stderr)
        return 1

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    for b in backends:
        if b not in ("docker", "nitrobox"):
            print(f"ERROR: unknown backend {b!r}", file=sys.stderr)
            return 1

    print(f"DT-Agent: {dt_dir}")
    print(f"Task list: {task_list}")
    print(f"Backends: {backends}")
    print(f"Agent: {args.agent_type} model={args.model} max-parallel={args.max_parallel}")
    print()

    results: dict[str, dict] = {}
    for backend in backends:
        print(f"=== {backend} ===")
        parsed, wall, raw = run_eval(
            dt_dir=dt_dir,
            task_list=task_list,
            backend=backend,
            max_parallel=args.max_parallel,
            agent_type=args.agent_type,
            model=args.model,
            healthcheck_start_interval=args.healthcheck_start_interval,
            extra_args=args.extra_args,
        )
        results[backend] = parsed
        print(
            f"  done in {wall:.1f}s — {parsed['total_tasks']} tasks, "
            f"{parsed['succeeded']} pass / {parsed['failed']} fail / "
            f"{parsed['infra_errors']} infra-err"
        )
        if args.output:
            log_path = f"{args.output}_{backend}.log"
            Path(log_path).write_text(raw)
            print(f"  full log → {log_path}")
        print()

    _print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
