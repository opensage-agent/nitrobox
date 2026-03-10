#!/usr/bin/env python3
"""Concurrent sandbox example: create N sandboxes, run commands, reset, destroy."""

import time
from concurrent.futures import ThreadPoolExecutor

from lite_sandbox import Sandbox, SandboxConfig


def worker(worker_id: int) -> dict:
    config = SandboxConfig(
        image="ubuntu:22.04",
        working_dir="/workspace",
        pids_max="128",
    )

    t0 = time.monotonic()
    sb = Sandbox(config, name=f"worker-{worker_id}")
    create_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    output, ec = sb.run("echo hello && uname -r")
    run_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    sb.reset()
    reset_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    sb.delete()
    delete_ms = (time.monotonic() - t0) * 1000

    return {
        "worker": worker_id,
        "create_ms": create_ms,
        "run_ms": run_ms,
        "reset_ms": reset_ms,
        "delete_ms": delete_ms,
    }


def main():
    n_workers = 32
    print(f"Launching {n_workers} sandboxes concurrently...")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        results = list(pool.map(worker, range(n_workers)))
    wall_ms = (time.monotonic() - t0) * 1000

    print(f"\n{'worker':>8} {'create':>10} {'run':>10} {'reset':>10} {'delete':>10}")
    for r in results:
        print(
            f"{r['worker']:>8} {r['create_ms']:>9.1f}ms {r['run_ms']:>9.1f}ms "
            f"{r['reset_ms']:>9.1f}ms {r['delete_ms']:>9.1f}ms"
        )

    avg_create = sum(r["create_ms"] for r in results) / len(results)
    avg_reset = sum(r["reset_ms"] for r in results) / len(results)
    print(f"\nAvg create: {avg_create:.1f}ms  Avg reset: {avg_reset:.1f}ms")
    print(f"Wall clock: {wall_ms:.0f}ms for {n_workers} workers")


if __name__ == "__main__":
    main()
