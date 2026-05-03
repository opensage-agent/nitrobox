#!/usr/bin/env python3
"""Benchmark: DecodingTrust-Agent ground-truth trajectory tests — Docker vs nitrobox.

Sibling to ``bench_dt.py``. Where ``bench_dt.py`` runs the LLM-driven
``eval/evaluation.py`` pipeline, this one runs DT-Agent's *no-LLM*
trajectory tests at ``tests/crm/benign/<N>/test_task_<N>.py``. Each
test scripts a hard-coded MCP tool call sequence and then invokes the
real judge — so pass/fail is fully deterministic and the only moving
piece across backends is the sandbox layer.

Backend selection is the env var ``SANDBOX_BACKEND={docker,nbx}``,
which the per-task ``conftest.py`` already honours (it dispatches
through ``utils.env_backend.create_backend``).

Usage:
    # Default: tasks 1-3 against both backends
    python examples/bench_dt_nollm.py --dt-dir /path/to/DecodingTrust-Agent

    # Specific task ids
    python examples/bench_dt_nollm.py --dt-dir ... --tasks 1,5,7

    # Only nitrobox
    python examples/bench_dt_nollm.py --dt-dir ... --backends nitrobox

Environment variables:
    DT_AGENT_DIR  Path to DecodingTrust-Agent checkout (alt to --dt-dir)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Per-test status. With ``-s``, pytest interleaves stdout/stderr from
# the test on the SAME line as the test id (e.g.
# ``tests/.../test_task_1.py::Foo::bar [CONFTEST] ... SKIPPED``), so
# ``\S+\s+STATUS`` doesn't match. Anchor on the test path; allow
# anything (including newlines) until the trailing status verb.
_PER_TEST_RE = re.compile(
    r"tests/crm/benign/(?P<task>\d+)/test_task_\d+\.py::[^\s]+"
    r".*?\b(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b",
    re.DOTALL,
)
# pytest summary "SKIPPED [1] tests/crm/benign/N/test_task_N.py:..." —
# emits one such line per skipped test in the short summary section.
_SHORT_SKIP_RE = re.compile(
    r"^(?P<status>SKIPPED|FAILED|ERROR)\s+\[\d+\]\s+"
    r"tests/crm/benign/(?P<task>\d+)/test_task_\d+\.py",
    re.MULTILINE,
)


def _find_dt_dir(hint: str | None) -> str | None:
    candidates = [
        hint,
        os.environ.get("DT_AGENT_DIR"),
        "../DecodingTrust-Agent",
        "../../DecodingTrust-Agent",
    ]
    for c in candidates:
        if c and Path(c).is_dir() and (Path(c) / "tests" / "crm" / "benign").is_dir():
            return str(Path(c).resolve())
    return None


def _resolve_tasks(spec: str, dt_dir: str) -> list[int]:
    """Parse "1,2,3" or "1-5" or "all" into a sorted list of task ids."""
    benign = Path(dt_dir) / "tests" / "crm" / "benign"
    available = sorted(
        int(p.name) for p in benign.iterdir()
        if p.is_dir() and p.name.isdigit() and (p / f"test_task_{p.name}.py").is_file()
    )
    if spec == "all":
        return available
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            lo, hi = (int(x) for x in chunk.split("-", 1))
            out.update(range(lo, hi + 1))
        elif chunk:
            out.add(int(chunk))
    return sorted(t for t in out if t in available)


def _parse_pytest(text: str) -> dict:
    """Pull per-task status + summary counts from pytest stdout."""
    per_task: dict[int, str] = {}
    # Pass 1: short summary block — most reliable because pytest emits
    # one ``STATUS [n] path`` line per non-passing test there.
    for m in _SHORT_SKIP_RE.finditer(text):
        per_task[int(m.group("task"))] = m.group("status")
    # Pass 2: scan progress lines for PASSED (the short summary block
    # only lists non-passing tests). Use re.DOTALL because ``-s``
    # interleaves stdout into the same line.
    for m in _PER_TEST_RE.finditer(text):
        task = int(m.group("task"))
        # Don't downgrade a non-passing status from pass 1.
        if task not in per_task or m.group("status") == "PASSED":
            per_task[task] = m.group("status")

    passed = sum(1 for s in per_task.values() if s == "PASSED")
    failed = sum(1 for s in per_task.values() if s in ("FAILED", "ERROR"))
    skipped = sum(1 for s in per_task.values() if s == "SKIPPED")

    return {
        "per_task": per_task,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": len(per_task),
    }


def run_pytest(
    dt_dir: str,
    tasks: list[int],
    backend: str,
    healthcheck_start_interval: float,
) -> tuple[dict, float, str]:
    """Subprocess-call pytest for the chosen task ids; return (parsed, wall, raw)."""
    test_paths = [
        f"tests/crm/benign/{t}/test_task_{t}.py"
        for t in tasks
    ]
    # ``-s`` keeps the conftest's [CONFTEST] env-startup prints in the
    # captured log — without it pytest swallows them and you can't tell
    # *which* env died on a SKIP.
    cmd = [sys.executable, "-m", "pytest", *test_paths, "-v", "-rs", "-s", "--no-header"]
    env = {
        **os.environ,
        "PYTHONPATH": dt_dir,
        "SANDBOX_BACKEND": "nbx" if backend == "nitrobox" else "docker",
        # Only the nbx backend respects this; docker ignores it.
        "NBX_HEALTHCHECK_START_INTERVAL": str(healthcheck_start_interval),
    }

    print(f"  → {backend}: pytest {len(tasks)} task(s)", flush=True)
    t0 = time.monotonic()
    result = subprocess.run(
        cmd, cwd=dt_dir, env=env,
        capture_output=True, text=True,
        timeout=3600,
    )
    wall = time.monotonic() - t0

    parsed = _parse_pytest(result.stdout + result.stderr)
    parsed["wall_s"] = wall
    parsed["returncode"] = result.returncode
    return parsed, wall, result.stdout + result.stderr


def _print_table(results: dict[str, dict], tasks: list[int]) -> None:
    print(f"\n{'='*72}")
    print(f"  DT-Agent ground-truth trajectory tests — Docker vs nitrobox")
    print(f"{'='*72}\n")

    header = f"{'Backend':>10} {'Wall':>8} {'Total':>6} {'Pass':>5} {'Fail':>5} {'Skip':>5}"
    print(header)
    print("-" * len(header))
    for backend, r in results.items():
        print(
            f"{backend:>10} {r['wall_s']:7.1f}s {r['total']:>6} "
            f"{r['passed']:>5} {r['failed']:>5} {r['skipped']:>5}"
        )

    if "docker" in results and "nitrobox" in results:
        d, n = results["docker"], results["nitrobox"]
        print(f"\n{'-'*72}")
        wall_speedup = d["wall_s"] / n["wall_s"] if n["wall_s"] else float("inf")
        print(f"  Wall speedup (docker/nitrobox): {wall_speedup:.2f}×")

        # Per-task agreement: matters more than aggregate counts because
        # docker vs nbx skips often look identical.
        diff: list[tuple[int, str, str]] = []
        for t in tasks:
            ds = d["per_task"].get(t, "MISSING")
            ns = n["per_task"].get(t, "MISSING")
            if ds != ns:
                diff.append((t, ds, ns))

        if not diff:
            print(f"  Per-task parity:  ✓ all {len(tasks)} tasks agree")
        else:
            print(f"  Per-task parity:  ✗ {len(diff)} disagreement(s):")
            print(f"    {'task':>5} {'docker':>10} {'nitrobox':>10}")
            for t, ds, ns in diff:
                print(f"    {t:>5} {ds:>10} {ns:>10}")
    print()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Bench DT-Agent no-LLM trajectory tests: Docker vs nitrobox",
    )
    p.add_argument("--dt-dir", help="Path to DecodingTrust-Agent checkout (or DT_AGENT_DIR)")
    p.add_argument(
        "--tasks", default="1-3",
        help="Comma list / range / 'all' of benign task ids (default: 1-3)",
    )
    p.add_argument(
        "--backends", default="docker,nitrobox",
        help="Comma-separated backends to run (default: docker,nitrobox)",
    )
    p.add_argument("--healthcheck-start-interval", type=float, default=0.5,
                   help="nitrobox-side healthcheck start_interval override (default 0.5s)")
    p.add_argument("--output", help="Save raw pytest stdout per backend to <prefix>_<backend>.log")
    args = p.parse_args()

    dt_dir = _find_dt_dir(args.dt_dir)
    if not dt_dir:
        print("ERROR: pass --dt-dir or set DT_AGENT_DIR", file=sys.stderr)
        return 1

    tasks = _resolve_tasks(args.tasks, dt_dir)
    if not tasks:
        print(f"ERROR: no matching tasks for spec {args.tasks!r}", file=sys.stderr)
        return 1

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    for b in backends:
        if b not in ("docker", "nitrobox"):
            print(f"ERROR: unknown backend {b!r}", file=sys.stderr)
            return 1

    print(f"DT-Agent: {dt_dir}")
    print(f"Tasks ({len(tasks)}): {tasks}")
    print(f"Backends: {backends}")
    print()

    results: dict[str, dict] = {}
    for backend in backends:
        print(f"=== {backend} ===")
        parsed, wall, raw = run_pytest(
            dt_dir=dt_dir,
            tasks=tasks,
            backend=backend,
            healthcheck_start_interval=args.healthcheck_start_interval,
        )
        results[backend] = parsed
        print(
            f"  done in {wall:.1f}s — {parsed['total']} collected, "
            f"{parsed['passed']} pass / {parsed['failed']} fail / "
            f"{parsed['skipped']} skip (rc={parsed['returncode']})"
        )
        if args.output:
            log_path = f"{args.output}_{backend}.log"
            Path(log_path).write_text(raw)
            print(f"  full log → {log_path}")
        print()

    _print_table(results, tasks)

    # Exit non-zero if backends disagree on any task (parity is the point).
    if "docker" in results and "nitrobox" in results:
        d, n = results["docker"], results["nitrobox"]
        for t in tasks:
            if d["per_task"].get(t) != n["per_task"].get(t):
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
