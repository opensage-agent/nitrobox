#!/usr/bin/env python3
"""Benchmark: R2E-Gym e2e — Docker vs nitrobox for SWE agent tasks.

Oracle agent: applies the dataset's gold patch (non-test file changes) and
runs the verifier. This isolates sandbox overhead from LLM inference, matching
the methodology of `examples/results/tb2.md` (terminal-bench oracle bench).

Per-task phases (timing.json):
    environment_setup  RepoEnv init: container/sandbox create + setup_env
    agent_execution    git apply <gold_patch>
    verifier           env.compute_reward() — runs hidden tests
    teardown           env.close()

Usage:

    # Smoke test (4 tasks, c=2, single pass)
    python examples/bench_r2egym_e2e.py \\
        --r2egym-dir /scratch/ruilin/workspace/R2E-Gym \\
        --n-tasks 4 --concurrency 2

    # Full cold+hot run (aligned with tb2.md)
    python examples/bench_r2egym_e2e.py \\
        --r2egym-dir /scratch/ruilin/workspace/R2E-Gym \\
        --n-tasks 88 --concurrency 16 --runs cold,hot

    # Parse existing results only
    python examples/bench_r2egym_e2e.py \\
        --parse-only results/bench_r2egym_<ts>

Fairness guarantees:
- Same docker image tag used by both backends (nitrobox pulls from same tag)
- Same task list (dataset shuffle seed=42)
- Same concurrency, same reward timeout
- Both backends run back-to-back on the same machine
- For "hot" runs: images are pre-pulled for both backends before timing starts
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure rootless nitrobox has XDG_RUNTIME_DIR (SSH sessions on H200 don't set this)
if not os.environ.get("XDG_RUNTIME_DIR"):
    xdg = f"/tmp/xdg-runtime-{os.getuid()}"
    os.makedirs(xdg, mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = xdg


# ---------------------------------------------------------------------------
# Oracle task runner
# ---------------------------------------------------------------------------

_LITELLM_SEED_PATCHED = False


def _patch_litellm_seed(seed: int) -> None:
    """Monkey-patch litellm.completion to inject `seed` (for reproducible
    sampling with vLLM). Idempotent — applied once per process."""
    global _LITELLM_SEED_PATCHED
    if _LITELLM_SEED_PATCHED or seed is None:
        return
    try:
        import litellm
        _orig = litellm.completion
        def _seeded_completion(*args, **kwargs):
            kwargs.setdefault("seed", seed)
            return _orig(*args, **kwargs)
        litellm.completion = _seeded_completion
        _LITELLM_SEED_PATCHED = True
    except ImportError:
        pass


def _run_one_task(
    task: dict,
    backend: str,
    task_result_dir: Path,
    reward_timeout: int,
    agent_kind: str = "oracle",
    llm_name: str = "",
    llm_base_url: str = "",
    max_steps: int = 30,
    llm_seed: int | None = 42,
) -> dict:
    """Run a single R2E-Gym task. agent_kind:
         'oracle' — apply gold patch directly (fast, no LLM)
         'llm'    — run R2E-Gym Agent with function calling (real agent loop)
    Emit timing.json."""
    # Inline imports so workers don't need r2egym importable at module-top
    import logging
    from r2egym.agenthub.environment.env import RepoEnv, EnvArgs
    from r2egym.commit_models.diff_classes import ParsedCommit

    task_result_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"bench.{task.get('docker_image', '?')[:40]}")
    log.setLevel(logging.CRITICAL)  # quiet

    phases: dict[str, Any] = {
        "environment_setup": 0.0,
        "agent_execution": 0.0,
        "verifier": 0.0,
        "teardown": 0.0,
        "llm_inference": 0.0,
        "env_exec_time": 0.0,  # sum of sandbox.run() calls in agent loop
        "n_steps": 1,
        "score": 0.0,
        "error": None,
        "backend": backend,
        "agent_kind": agent_kind,
        "docker_image": task.get("docker_image"),
        "repo_name": task.get("repo_name"),
    }

    env = None
    try:
        # --- env_setup (retry on transient nitrobox buildkit lazy-blob errors) ---
        t0 = time.monotonic()
        _last_err = None
        for _attempt in range(3):
            try:
                env = RepoEnv(EnvArgs(ds=task), logger=log, backend=backend, verbose=False)
                _last_err = None
                break
            except RuntimeError as e:
                _last_err = e
                if "lazy blobs" in str(e) or "missing descriptor handlers" in str(e):
                    time.sleep(1.0 + _attempt)
                    continue
                raise
        if _last_err is not None:
            raise _last_err
        phases["environment_setup"] = time.monotonic() - t0

        # Sanity check: confirm we're actually using the requested backend
        runtime_cls = type(env.runtime).__name__
        expected = "NitroboxRuntime" if backend == "nitrobox" else "DockerRuntime"
        if runtime_cls != expected:
            raise AssertionError(
                f"backend={backend} but runtime={runtime_cls} (expected {expected})"
            )
        phases["runtime_class"] = runtime_cls

        # --- agent ---
        if agent_kind == "llm":
            _patch_litellm_seed(llm_seed)
            from pathlib import Path as _P
            from r2egym.agenthub.agent.agent import Agent, AgentArgs
            r2e_root = _P(os.environ.get("R2E_GYM_DIR", "/scratch/ruilin/workspace/R2E-Gym"))
            # R2E-Gym official open-weight agent is trained for text-parsing mode
            # (README: --use_fn_calling False with R2E-Gym/R2EGym-*-Agent).
            # General instruct models (GPT/Claude/Qwen) use function calling.
            #
            # LiteLLM prefix gotcha: README uses "vllm/..." which actually means
            # "import vllm as a Python lib" (requires vllm installed). For
            # hitting an OpenAI-compatible vLLM server via HTTP, use
            # "hosted_vllm/..." or "openai/..." instead.
            use_fn = "R2EGym" not in llm_name and "R2E-Gym/" not in llm_name
            cfg_name = "edit_fn_calling.yaml" if use_fn else "edit_non_fn_calling.yaml"
            cfg_path = r2e_root / f"src/r2egym/agenthub/config/r2egym/{cfg_name}"
            agent_args = AgentArgs.from_yaml(cfg_path)
            # Rebase relative command_files (yaml has "./src/..." paths) to absolute
            agent_args.command_files = [
                str((r2e_root / str(p).lstrip("./") if not str(p).startswith("/")
                     else _P(p)).resolve())
                for p in agent_args.command_files
            ]
            agent_args.llm_name = llm_name
            if llm_base_url:
                agent_args.llm_base_url = llm_base_url
            agent = Agent(name="EditAgent", args=agent_args, logger=log)
            # R2E-Gym's Agent.__init__ ignores args.llm_base_url and reads
            # $LLM_BASE_URL from env — which is global and racy across threads.
            # Patch the instance attr directly so each worker hits its own URL.
            if llm_base_url:
                agent.llm_base_url = llm_base_url
            t1 = time.monotonic()
            traj = agent.run(
                env, max_steps=max_steps, temperature=0.0,
                max_steps_absolute=max_steps + 10,
                use_fn_calling=use_fn, scaffold="r2egym",
                max_token_limit=32768,
            )
            phases["agent_execution"] = time.monotonic() - t1
            phases["llm_inference"] = float(getattr(traj, "total_llm_time", 0) or 0)
            phases["env_exec_time"] = float(getattr(traj, "total_env_time", 0) or 0)
            phases["n_steps"] = len(traj.trajectory_steps) if traj.trajectory_steps else 0
            phases["exit_reason"] = getattr(traj, "exit_reason", "")
            # Persist trajectory for post-hoc diffing between backends
            try:
                with open(task_result_dir / "trajectory.json", "w") as _tf:
                    _tf.write(traj.model_dump_json())
            except Exception as _e:
                phases["trajectory_save_error"] = f"{type(_e).__name__}: {_e}"
        else:
            t1 = time.monotonic()
            commit = ParsedCommit(**json.loads(task["parsed_commit_content"]))
            gold_patch = commit.get_patch(test_file=False, non_test_file=True)
            patch_out, patch_ec = env.runtime.apply_patch(gold_patch)
            phases["agent_execution"] = time.monotonic() - t1
            phases["patch_exit_code"] = patch_ec

        # --- verifier ---
        t2 = time.monotonic()
        reward = env.runtime._calculate_reward(timeout=reward_timeout)
        phases["verifier"] = time.monotonic() - t2
        phases["score"] = float(reward)

    except Exception as e:
        phases["error"] = f"{type(e).__name__}: {e}"
        phases["traceback"] = traceback.format_exc()
    finally:
        # --- teardown ---
        t3 = time.monotonic()
        if env is not None:
            try:
                env.close()
            except Exception as e:
                phases.setdefault("error", f"close: {type(e).__name__}: {e}")
        phases["teardown"] = time.monotonic() - t3

    # persist
    with open(task_result_dir / "timing.json", "w") as f:
        json.dump(phases, f, indent=2)
    with open(task_result_dir / "result.txt", "w") as f:
        f.write(f"{phases['score']}\n")
    return phases


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    tasks: list[dict]
    backend: str
    result_dir: Path
    concurrency: int
    reward_timeout: int
    agent_kind: str = "oracle"
    llm_name: str = ""
    llm_base_url: str = ""
    llm_base_urls: list[str] = field(default_factory=list)  # round-robin if multiple
    max_steps: int = 30
    llm_seed: int | None = 42


@dataclass
class RunResult:
    backend: str
    wall_time_s: float = 0.0
    tasks: int = 0
    pass_n: int = 0
    fail_n: int = 0
    err_n: int = 0
    per_repo: dict[str, dict[str, int]] = field(default_factory=dict)
    phases: dict[str, list[float]] = field(default_factory=lambda: {
        "environment_setup": [], "agent_execution": [], "verifier": [],
        "teardown": [], "llm_inference": [], "n_steps": [],
    })


def _run_one_wrapper(args):
    """Unpacks args for ThreadPoolExecutor.map() fairness (avoids closure)."""
    (task, backend, task_dir, timeout, agent_kind, llm_name,
     llm_base_url, max_steps, llm_seed) = args
    try:
        return _run_one_task(task, backend, task_dir, timeout,
                             agent_kind=agent_kind, llm_name=llm_name,
                             llm_base_url=llm_base_url, max_steps=max_steps,
                             llm_seed=llm_seed)
    except Exception as e:
        return {
            "error": f"worker exception: {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "score": 0.0, "backend": backend,
            "environment_setup": 0, "agent_execution": 0,
            "verifier": 0, "teardown": 0, "llm_inference": 0, "n_steps": 0,
        }


def run_backend(cfg: RunConfig) -> RunResult:
    cfg.result_dir.mkdir(parents=True, exist_ok=True)
    res = RunResult(backend=cfg.backend)

    urls = cfg.llm_base_urls or [cfg.llm_base_url]
    inputs = [
        (task, cfg.backend, cfg.result_dir / f"task_{i:04d}", cfg.reward_timeout,
         cfg.agent_kind, cfg.llm_name,
         urls[i % len(urls)] if urls[0] else cfg.llm_base_url,
         cfg.max_steps, cfg.llm_seed)
        for i, task in enumerate(cfg.tasks)
    ]

    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        for i, phases in enumerate(ex.map(_run_one_wrapper, inputs)):
            task = cfg.tasks[i]
            repo = task.get("repo_name", "?")
            res.per_repo.setdefault(repo, {"tasks": 0, "pass": 0, "fail": 0, "err": 0})
            res.tasks += 1
            res.per_repo[repo]["tasks"] += 1
            if phases.get("error"):
                res.err_n += 1
                res.per_repo[repo]["err"] += 1
            elif phases.get("score", 0) > 0:
                res.pass_n += 1
                res.per_repo[repo]["pass"] += 1
            else:
                res.fail_n += 1
                res.per_repo[repo]["fail"] += 1
            for p in res.phases:
                v = phases.get(p)
                if isinstance(v, (int, float)):
                    res.phases[p].append(float(v))
            # live-log per task
            err_flag = " ERR" if phases.get("error") else ""
            pass_flag = "PASS" if phases.get("score", 0) > 0 else "FAIL"
            print(
                f"  [{cfg.backend}] task {i+1}/{len(cfg.tasks)} "
                f"repo={repo:20s} setup={phases.get('environment_setup', 0):5.1f}s "
                f"agent={phases.get('agent_execution', 0):5.1f}s "
                f"(llm={phases.get('llm_inference', 0):5.1f}s "
                f"env={phases.get('env_exec_time', 0):5.1f}s "
                f"steps={phases.get('n_steps', 0):>2d}) "
                f"verify={phases.get('verifier', 0):5.1f}s "
                f"tear={phases.get('teardown', 0):4.1f}s {pass_flag}{err_flag}",
                flush=True,
            )
    res.wall_time_s = time.monotonic() - start
    return res


# ---------------------------------------------------------------------------
# Prepull (warmup docker images for fair hot-start comparison)
# ---------------------------------------------------------------------------

def prepull_docker(tasks: list[dict], max_workers: int = 8) -> None:
    """Warm docker daemon image cache."""
    images = sorted({t["docker_image"] for t in tasks})
    print(f"Prepulling {len(images)} docker images (c={max_workers})...")
    def _pull(img):
        t = time.monotonic()
        r = subprocess.run(
            ["docker", "pull", img],
            capture_output=True, text=True, timeout=900,
        )
        return img, r.returncode, time.monotonic() - t
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for img, rc, dt in ex.map(_pull, images):
            mark = "ok" if rc == 0 else "FAIL"
            print(f"  {mark:4s} {dt:5.1f}s  {img}")


def prepull_nitrobox(tasks: list[dict], retries: int = 3) -> None:
    """Warm nitrobox buildkit layer cache (serial — buildkit has concurrency
    bugs with shared layers across images). Retries each image up to N times
    because nitrobox buildkit hits 'lazy blobs' errors when shared layers
    are partially materialized from prior pulls."""
    from nitrobox.image.layers import prepare_rootfs_layers_from_docker
    from pathlib import Path as _P
    images = sorted({t["docker_image"] for t in tasks})
    print(f"Prepulling {len(images)} nitrobox images (serial, retry={retries})...")
    cache_dir = _P(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "nitrobox" / "rootfs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    failed: list[tuple[str, str]] = []
    for img in images:
        t = time.monotonic()
        last_err = None
        for attempt in range(retries):
            try:
                layers = prepare_rootfs_layers_from_docker(img, cache_dir, pull=True)
                dt = time.monotonic() - t
                suffix = f" (attempt {attempt+1})" if attempt > 0 else ""
                print(f"  ok   {dt:5.1f}s  {img}  ({len(layers)} layers){suffix}")
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(1.0)
        if last_err is not None:
            dt = time.monotonic() - t
            msg = str(last_err)[:100]
            print(f"  FAIL {dt:5.1f}s  {img}  {type(last_err).__name__}: {msg}")
            failed.append((img, msg))
    if failed:
        print(f"  [WARN] {len(failed)} images failed to prewarm. "
              f"Their tasks may still succeed (Sandbox retries on demand).")


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _fmt_phase(val: float, total: float) -> str:
    pct = (val / total * 100) if total > 0 else 0
    return f"{val:5.1f}s ({pct:2.0f}%)"


def format_table(results: list[RunResult], concurrency: int, label: str) -> str:
    lines = [f"## {label}", ""]
    lines.append(
        "| C  | Env      | Wall    | EnvSetup    | Agent       | Verify      | Teardown    | Pass | Fail | Err |"
    )
    lines.append(
        "|----|----------|---------|-------------|-------------|-------------|-------------|------|------|-----|"
    )
    for r in results:
        p = r.phases
        s = _mean(p["environment_setup"])
        a = _mean(p["agent_execution"])
        v = _mean(p["verifier"])
        t = _mean(p["teardown"])
        total = s + a + v + t
        lines.append(
            f"| {concurrency:>2} | {r.backend:<8s} | {r.wall_time_s:6.1f}s | "
            f"{_fmt_phase(s, total)} | {_fmt_phase(a, total)} | "
            f"{_fmt_phase(v, total)} | {_fmt_phase(t, total)} | "
            f"{r.pass_n:>4} | {r.fail_n:>4} | {r.err_n:>3} |"
        )
    lines.append("")

    # Per-phase speedup
    if len(results) == 2:
        a, b = results
        lines.append("### Per-phase speedup")
        lines.append("")
        lines.append("| Phase | Docker | nitrobox | Speedup |")
        lines.append("|-------|--------|----------|---------|")
        for phase_key, label_name in [
            ("environment_setup", "env_setup"),
            ("agent_execution", "agent_exec"),
            ("verifier", "verifier"),
            ("teardown", "teardown"),
        ]:
            av = _mean(a.phases[phase_key])
            bv = _mean(b.phases[phase_key])
            sp = (av / bv) if bv > 0 else 0
            lines.append(
                f"| {label_name} | {av:.1f}s | {bv:.1f}s | **{sp:.2f}x** |"
            )
        # Wall speedup
        if b.wall_time_s > 0:
            lines.append("")
            lines.append(
                f"**Wall-clock speedup: {a.wall_time_s / b.wall_time_s:.2f}x** "
                f"({a.wall_time_s/60:.1f} min → {b.wall_time_s/60:.1f} min)"
            )
        lines.append("")

    # Per-repo pass/fail
    all_repos = sorted({r for res in results for r in res.per_repo})
    if all_repos:
        lines.append("### Per-repo breakdown (pass/total)")
        lines.append("")
        header = "| Repo                     |" + "".join(
            f" {r.backend:>10} |" for r in results
        )
        lines.append(header)
        lines.append("|" + "-" * 26 + "|" + "|".join("-" * 12 for _ in results) + "|")
        for repo in all_repos:
            row = f"| {repo:<24s} |"
            for r in results:
                dd = r.per_repo.get(repo, {"tasks": 0, "pass": 0})
                row += f" {dd['pass']}/{dd['tasks']:>4}   |"
            lines.append(row)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_tasks(dataset: str, split: str, n: int, seed: int = 42) -> list[dict]:
    from datasets import load_dataset
    print(f"Loading {dataset} split={split}...")
    ds = load_dataset(dataset, split=split).shuffle(seed=seed)
    tasks = [dict(ds[i]) for i in range(min(n, len(ds)))]
    print(f"Selected {len(tasks)} tasks (of {len(ds)} total)")
    return tasks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--r2egym-dir", default="/scratch/ruilin/workspace/R2E-Gym",
                    help="Path to R2E-Gym repo (added to sys.path if not installed)")
    ap.add_argument("--dataset", default="R2E-Gym/R2E-Gym-Lite")
    ap.add_argument("--split", default="train")
    ap.add_argument("--n-tasks", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--envs", default="docker,nitrobox",
                    help="Comma-separated backends to run")
    ap.add_argument("--runs", default="hot",
                    help="'cold', 'hot', or 'cold,hot'. Cold = no prepull; "
                         "hot = prepull images first. Default: hot.")
    ap.add_argument("--reward-timeout", type=int, default=300)
    ap.add_argument("--agent", default="oracle", choices=["oracle", "llm"],
                    help="oracle = apply gold patch; llm = real R2E-Gym agent loop")
    ap.add_argument("--llm-name", default="hosted_vllm/R2E-Gym/R2EGym-32B-Agent",
                    help="litellm model name. Examples: "
                         "'hosted_vllm/R2E-Gym/R2EGym-32B-Agent' (official R2E agent, non-fn-calling), "
                         "'openai/<name>' (generic instruct model via local vLLM with fn-calling)")
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:8000/v1",
                    help="OpenAI-compatible API base URL for local vLLM")
    ap.add_argument("--llm-base-urls", default="",
                    help="Comma-separated list of vLLM base URLs for round-robin "
                         "distribution across multiple instances. Overrides --llm-base-url.")
    ap.add_argument("--max-steps", type=int, default=30,
                    help="max agent steps per task (llm mode only)")
    ap.add_argument("--llm-seed", type=int, default=42,
                    help="LLM sampling seed (passed via litellm.completion kwargs, "
                         "only meaningful if the backing server respects seed — vLLM does).")
    ap.add_argument("--result-base", default=None,
                    help="Output dir (default: ./results/bench_r2egym_<ts>)")
    ap.add_argument("--parse-only", default=None,
                    help="Parse existing result dir instead of running")
    args = ap.parse_args()

    # Ensure R2E-Gym is importable
    r2e_src = Path(args.r2egym_dir) / "src"
    if r2e_src.is_dir():
        sys.path.insert(0, str(r2e_src))

    envs = [e.strip() for e in args.envs.split(",") if e.strip()]
    runs = [r.strip() for r in args.runs.split(",") if r.strip()]

    ts = time.strftime("%Y%m%d_%H%M%S")
    result_base = Path(args.result_base or f"results/bench_r2egym_{ts}")

    if args.parse_only:
        print(f"[parse-only] loading from {args.parse_only}")
        result_base = Path(args.parse_only)
        # TODO: reconstruct RunResult from timing.json files
        print("parse-only not yet implemented; re-run with full args.")
        return

    print(f"R2E-Gym benchmark")
    print(f"  Dataset:          {args.dataset} / {args.split}")
    print(f"  Tasks:            {args.n_tasks}")
    print(f"  Concurrency:      {args.concurrency}")
    print(f"  Backends:         {envs}")
    print(f"  Agent:            {args.agent}" +
          (f" (llm={args.llm_name} @ {args.llm_base_url}, max_steps={args.max_steps})"
           if args.agent == "llm" else ""))
    print(f"  Runs:             {runs}")
    print(f"  Result dir:       {result_base}")
    print(f"  XDG_RUNTIME_DIR:  {os.environ.get('XDG_RUNTIME_DIR')}")
    print()

    tasks = _load_tasks(args.dataset, args.split, args.n_tasks)

    md_sections: list[str] = [
        f"# R2E-Gym benchmark — Docker vs nitrobox\n",
        f"**Dataset:** `{args.dataset}` split=`{args.split}`  ",
        f"**Tasks:** {args.n_tasks}  ",
        (f"**Agent:** oracle (apply gold patch → run verifier)  "
         if args.agent == "oracle" else
         f"**Agent:** llm ({args.llm_name}, max_steps={args.max_steps})  "),
        f"**Concurrency:** {args.concurrency}  ",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}  ",
        "",
    ]

    for run_label in runs:
        print(f"\n{'=' * 70}\n[{run_label.upper()} RUN]\n{'=' * 70}")

        results: list[RunResult] = []
        for backend in envs:
            rdir = result_base / run_label / backend
            # Backend-specific hot warmup (fair: Docker→daemon, nitrobox→buildkit)
            if run_label == "hot":
                if backend == "docker":
                    prepull_docker(tasks, max_workers=min(8, args.concurrency))
                elif backend == "nitrobox":
                    prepull_nitrobox(tasks)
            print(f"\nRunning backend={backend} → {rdir}")
            cfg = RunConfig(
                tasks=tasks, backend=backend, result_dir=rdir,
                concurrency=args.concurrency, reward_timeout=args.reward_timeout,
                agent_kind=args.agent, llm_name=args.llm_name,
                llm_base_url=args.llm_base_url,
                llm_base_urls=[u.strip() for u in args.llm_base_urls.split(",") if u.strip()],
                max_steps=args.max_steps, llm_seed=args.llm_seed,
            )
            r = run_backend(cfg)
            results.append(r)
            print(
                f"  Wall: {r.wall_time_s:.1f}s  "
                f"pass={r.pass_n} fail={r.fail_n} err={r.err_n}"
            )

        md_sections.append(format_table(
            results, args.concurrency,
            f"{'Cold Start' if run_label == 'cold' else 'Hot Start (images cached)'}"
        ))

    # Write final markdown
    md_path = result_base / "report.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(md_sections))
    print(f"\nReport written to: {md_path}")
    print(f"Result trees: {result_base}/<cold|hot>/<backend>/task_*/timing.json")
    print()
    print("\n".join(md_sections))


if __name__ == "__main__":
    main()
