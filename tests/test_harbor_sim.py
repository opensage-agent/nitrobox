"""Reproduce harbor's exact nitrobox workflow to find the crash.

Run:
  CONTAINERS_STORAGE_ROOT=/tmp/nbx_store/graph \
  XDG_RUNTIME_DIR=/tmp/run-$(id -u) \
  PATH=src/nitrobox/_vendor:$PATH \
  DOCKER_CONFIG=~/.docker \
  python tests/test_harbor_sim.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path


async def simulate_harbor_trial(task_id: str, image: str, trial_dir: Path):
    """Exact harbor NitroboxEnvironment flow."""
    loop = asyncio.get_running_loop()
    print(f"[{task_id}] Starting...", flush=True)

    # ---- Step 1: ComposeProject.up() in executor (harbor line 183) ----
    def _start():
        from nitrobox import ComposeProject

        # Create a minimal compose file (same as harbor's swebench template)
        compose_dir = trial_dir / "compose"
        compose_dir.mkdir(parents=True, exist_ok=True)

        env_dir = trial_dir / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)

        # Dockerfile: FROM swebench image + install uv (same as swebench adapter)
        (env_dir / "Dockerfile").write_text(
            f"FROM {image}\n"
            "WORKDIR /testbed\n"
            "RUN mkdir -p /logs\n"
        )

        # docker-compose.yaml
        compose_content = {
            "services": {
                "main": {
                    "build": {"context": str(env_dir), "dockerfile": "Dockerfile"},
                    "image": f"{task_id}-main",
                    "working_dir": "/testbed",
                }
            }
        }
        compose_file = compose_dir / "docker-compose.yaml"
        compose_file.write_text(json.dumps(compose_content))

        t0 = time.time()
        proj = ComposeProject([compose_file], project_name=task_id)
        proj.up()
        print(f"[{task_id}] up() done in {time.time()-t0:.1f}s", flush=True)
        return proj

    proj = await loop.run_in_executor(None, _start)

    # ---- Step 2: exec() in executor (harbor line 247) ----
    def _exec(cmd):
        sb = proj.services.get("main")
        if sb is None:
            raise RuntimeError("No 'main' service")
        return sb.run(cmd)

    out, ec = await loop.run_in_executor(
        None, _exec,
        "source /opt/miniconda3/bin/activate testbed 2>/dev/null; python -c 'import astropy; print(astropy.__version__)' 2>&1"
    )
    print(f"[{task_id}] exec: {out.strip()!r} ec={ec}", flush=True)

    # ---- Step 3: run verifier test ----
    out2, ec2 = await loop.run_in_executor(
        None, _exec,
        "source /opt/miniconda3/bin/activate testbed 2>/dev/null; cd /testbed && python -m pytest astropy/units/tests/test_units.py -x -q --tb=no 2>&1 | tail -3"
    )
    print(f"[{task_id}] pytest: {out2.strip()!r} ec={ec2}", flush=True)

    # ---- Step 4: stop() in executor (harbor line 199-207) ----
    def _stop():
        proj.down()
        print(f"[{task_id}] down() done", flush=True)

    await loop.run_in_executor(None, _stop)
    print(f"[{task_id}] DONE", flush=True)


async def main():
    image = "swebench/sweb.eval.x86_64.astropy_1776_astropy-7606:latest"
    n_tasks = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    tmp = Path(tempfile.mkdtemp(prefix="harbor_sim_"))
    print(f"Harbor sim: {n_tasks} task(s), image={image}", flush=True)

    tasks = []
    for i in range(n_tasks):
        trial_dir = tmp / f"trial-{i}"
        tasks.append(simulate_harbor_trial(f"trial-{i}", image, trial_dir))

    await asyncio.gather(*tasks)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
