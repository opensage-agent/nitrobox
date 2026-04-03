#!/usr/bin/env python3
"""Concurrent VM benchmark: OSWorld Docker vs nitrobox QemuVM.

Measures at different concurrency levels (1, 4, 8, 16):
  1. Reset-to-ready: time from triggering reset to VM being usable
     - Docker: stop + rm + sleep(3) + run + wait_for_vm_ready (includes OS reboot)
     - nitrobox: loadvm (VM instantly usable, no reboot)
  2. Screenshot (HTTP): both through the same Flask /screenshot endpoint

Both use the same OSWorld Ubuntu Desktop qcow2 image and QEMU/KVM.

Usage:
    python bench_concurrent.py --qcow2 /path/to/Ubuntu.qcow2
    python bench_concurrent.py --concurrency 1,4,8,16
"""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

LOG_FILE = "/scratch/ruilin/workspace/bench_concurrent.log"
_log_fh = None

def setup_logging():
    global _log_fh
    _log_fh = open(LOG_FILE, "w")

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


# ---------------------------------------------------------------------------
# QMP helpers
# ---------------------------------------------------------------------------

def _qmp_send(sock_path, command, arguments=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(120)
    s.connect(sock_path)
    s.recv(4096)
    s.sendall(b'{"execute": "qmp_capabilities"}\n')
    s.recv(4096)
    msg = {"execute": command}
    if arguments:
        msg["arguments"] = arguments
    s.sendall(json.dumps(msg).encode() + b"\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
        if b'"return"' in data or b'"error"' in data:
            break
    s.close()
    for line in data.decode(errors="ignore").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "return" in obj or "error" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return {"return": {}}


def _hmp(sock_path, command):
    resp = _qmp_send(sock_path, "human-monitor-command",
                     {"command-line": command})
    if "error" in resp:
        raise RuntimeError(f"HMP failed: {resp['error']}")
    return resp.get("return", "")


# ---------------------------------------------------------------------------
# OSWorld Docker provider
# ---------------------------------------------------------------------------

def _docker_worker(idx, qcow2, memory, cpus):
    """Boot Docker VM, measure reset-to-ready and screenshot."""
    import docker
    import requests

    client = docker.from_env()
    name = f"nitrobox-bench-osworld-{idx}"
    port = 15000 + idx
    env = {"DISK_SIZE": "32G", "RAM_SIZE": memory, "CPU_CORES": str(cpus)}
    devices = ["/dev/kvm"] if os.path.exists("/dev/kvm") else []
    if not devices:
        env["KVM"] = "N"

    def start():
        return client.containers.run(
            "happysixd/osworld-docker", name=name,
            environment=env, cap_add=["NET_ADMIN"], devices=devices,
            volumes={os.path.abspath(qcow2): {"bind": "/System.qcow2", "mode": "ro"}},
            ports={5000: port}, detach=True,
        )

    def wait_ready(timeout=300):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = requests.get(f"http://localhost:{port}/screenshot", timeout=(10, 10))
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise TimeoutError(f"Docker VM {idx} not ready")

    def cleanup():
        try:
            c = client.containers.get(name)
            c.stop()
            c.remove()
        except Exception:
            pass

    # Initial boot
    cleanup()
    container = start()
    wait_ready()

    # --- Reset-to-ready ---
    # This is the full OSWorld reset: destroy container + recreate + wait for OS
    t0 = time.monotonic()
    container.stop()
    container.remove()
    time.sleep(3)  # OSWorld WAIT_TIME
    container = start()
    wait_ready()
    reset_to_ready_ms = (time.monotonic() - t0) * 1000

    # --- Screenshot (HTTP, same endpoint OSWorld uses) ---
    screen_times = []
    for _ in range(5):
        t0 = time.monotonic()
        r = requests.get(f"http://localhost:{port}/screenshot", timeout=(10, 10))
        screen_times.append((time.monotonic() - t0) * 1000)
    screenshot_ms = statistics.median(screen_times)

    # Cleanup
    container.stop()
    container.remove()

    return {"reset_to_ready_ms": reset_to_ready_ms, "screenshot_ms": screenshot_ms}


# ---------------------------------------------------------------------------
# nitrobox QemuVM (raw QEMU with port forwarding)
# ---------------------------------------------------------------------------

def _nbx_worker(idx, qcow2, memory, cpus, rounds=3):
    """Boot QEMU with port forwarding, savevm, measure loadvm and screenshot."""
    import requests

    sock = f"/tmp/nbx_bench_conc_qmp_{idx}.sock"
    port = 16000 + idx
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass

    cmd = [
        "qemu-system-x86_64", "-enable-kvm",
        "-m", memory, "-smp", str(cpus),
        "-drive", f"file={qcow2},format=qcow2,if=virtio,snapshot=on",
        "-qmp", f"unix:{sock},server,nowait",
        "-display", "none", "-serial", "null",
        "-no-shutdown", "-nographic",
        "-device", "virtio-vga",
        # Port forward: guest:5000 → host:port (for fair screenshot comparison)
        "-nic", f"user,hostfwd=tcp::{port}-:5000",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    # Wait QMP
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if os.path.exists(sock):
            try:
                _qmp_send(sock, "query-status")
                break
            except Exception:
                pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise TimeoutError(f"QMP {idx} not ready")

    # Wait for OS to fully boot (need Flask server running for savevm)
    # Poll the forwarded port just like Docker does
    deadline = time.monotonic() + 120
    flask_ready = False
    while time.monotonic() < deadline:
        try:
            import requests
            r = requests.get(f"http://localhost:{port}/screenshot", timeout=(5, 5))
            if r.status_code == 200:
                flask_ready = True
                break
        except Exception:
            pass
        time.sleep(1)

    if not flask_ready:
        proc.kill()
        proc.wait()
        raise TimeoutError(f"nitrobox VM {idx}: Flask server not ready")

    # Save state (one-time cost, VM is fully booted with Flask running)
    _hmp(sock, "savevm bench_base")
    time.sleep(0.5)

    # --- Reset-to-ready (loadvm) ---
    # After loadvm, VM is instantly in the savevm state (Flask already running)
    reset_times = []
    for _ in range(rounds):
        t0 = time.monotonic()
        _hmp(sock, "loadvm bench_base")
        reset_times.append((time.monotonic() - t0) * 1000)
    reset_to_ready_ms = statistics.median(reset_times)

    # --- Screenshot (HTTP, same endpoint, same path as Docker) ---
    screen_times = []
    for _ in range(5):
        t0 = time.monotonic()
        r = requests.get(f"http://localhost:{port}/screenshot", timeout=(10, 10))
        screen_times.append((time.monotonic() - t0) * 1000)
    screenshot_ms = statistics.median(screen_times)

    # Cleanup
    _hmp(sock, "delvm bench_base")
    try:
        _qmp_send(sock, "quit")
    except Exception:
        pass
    proc.wait(timeout=30)

    return {"reset_to_ready_ms": reset_to_ready_ms, "screenshot_ms": screenshot_ms}


# ---------------------------------------------------------------------------
# Concurrent runners
# ---------------------------------------------------------------------------

def bench_concurrent(runner, qcow2, concurrency, memory, cpus, label):
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(runner, i, qcow2, memory, cpus): i
            for i in range(concurrency)
        }
        results = {}
        for f in as_completed(futures):
            idx = futures[f]
            try:
                results[idx] = f.result()
            except Exception as e:
                log(f"    {label} VM {idx} failed: {e}")
                results[idx] = None
    return [v for v in results.values() if v is not None]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--qcow2", default=None)
    parser.add_argument("--concurrency", default="1,4,8,16",
                        help="Comma-separated concurrency levels")
    parser.add_argument("--memory", default="4G")
    parser.add_argument("--cpus", type=int, default=4)
    args = parser.parse_args()

    qcow2 = args.qcow2
    if not qcow2:
        for c in ["docker_vm_data/Ubuntu.qcow2",
                   "../osworld/docker_vm_data/Ubuntu.qcow2",
                   "/scratch/ruilin/workspace/osworld/docker_vm_data/Ubuntu.qcow2"]:
            if os.path.exists(c):
                qcow2 = c
                break
    if not qcow2 or not os.path.exists(qcow2):
        log("ERROR: Ubuntu.qcow2 not found. Use --qcow2")
        return

    levels = [int(x) for x in args.concurrency.split(",")]

    log("Concurrent VM Benchmark: OSWorld Docker vs nitrobox QemuVM")
    log(f"Image: {qcow2} ({os.path.getsize(qcow2)//1024**3}GB)")
    log(f"VM: {args.memory} RAM, {args.cpus} CPUs")
    log(f"Concurrency levels: {levels}")
    log("")
    log("Both sides use the same qcow2 and QEMU/KVM.")
    log("Reset-to-ready = time from triggering reset to VM being usable.")
    log("Screenshot = HTTP GET /screenshot (same Flask endpoint, same path).")
    log("")

    docker_results = {}
    nbx_results = {}

    for n in levels:
        log(f"--- Concurrency = {n} ---")

        log(f"  Docker ({n} VMs)...")
        try:
            docker_data = bench_concurrent(_docker_worker, qcow2, n,
                                           args.memory, args.cpus, "Docker")
            if docker_data:
                docker_results[n] = {
                    "reset": statistics.median([d["reset_to_ready_ms"] for d in docker_data]),
                    "screenshot": statistics.median([d["screenshot_ms"] for d in docker_data]),
                }
                log(f"    Reset-to-ready: {docker_results[n]['reset']/1000:.1f}s")
                log(f"    Screenshot:     {docker_results[n]['screenshot']:.0f}ms")
        except Exception as e:
            log(f"    Error: {e}")

        log(f"  nitrobox loadvm ({n} VMs)...")
        try:
            nbx_data = bench_concurrent(_nbx_worker, qcow2, n,
                                        args.memory, args.cpus, "nitrobox")
            if nbx_data:
                nbx_results[n] = {
                    "reset": statistics.median([d["reset_to_ready_ms"] for d in nbx_data]),
                    "screenshot": statistics.median([d["screenshot_ms"] for d in nbx_data]),
                }
                log(f"    Reset-to-ready: {nbx_results[n]['reset']/1000:.1f}s")
                log(f"    Screenshot:     {nbx_results[n]['screenshot']:.0f}ms")
        except Exception as e:
            log(f"    Error: {e}")

        log("")

    # ======================================================================
    # Summary
    # ======================================================================
    log("=" * 85)
    log("SUMMARY: Reset-to-ready")
    log("=" * 85)
    log("  Docker: stop container + rm + sleep(3) + new container + wait OS boot")
    log("  nitrobox:    QMP loadvm (in-place memory restore, VM instantly usable)")
    log("")

    hdr = f"  {'Conc':>5}  {'Docker':>10}  {'nbx':>10}  {'Speedup':>8}"
    log(hdr)
    log(f"  {'-----':>5}  {'-'*10}  {'-'*10}  {'-'*8}")
    for n in levels:
        d = docker_results.get(n)
        a = nbx_results.get(n)
        if not d or not a:
            continue
        sp = d["reset"] / a["reset"] if a["reset"] > 0 else 0
        log(f"  {n:>5}  {d['reset']/1000:>8.1f}s  {a['reset']/1000:>8.1f}s  {sp:>6.1f}x")

    log("")
    log("=" * 85)
    log("SUMMARY: Screenshot (HTTP GET /screenshot)")
    log("=" * 85)
    log("  Both go through the same Flask server inside the VM via HTTP.")
    log("")

    hdr = f"  {'Conc':>5}  {'Docker':>10}  {'nbx':>10}  {'Speedup':>8}"
    log(hdr)
    log(f"  {'-----':>5}  {'-'*10}  {'-'*10}  {'-'*8}")
    for n in levels:
        d = docker_results.get(n)
        a = nbx_results.get(n)
        if not d or not a:
            continue
        sp = d["screenshot"] / a["screenshot"] if a["screenshot"] > 0 else 0
        log(f"  {n:>5}  {d['screenshot']:>8.0f}ms  {a['screenshot']:>8.0f}ms  {sp:>6.1f}x")



if __name__ == "__main__":
    main()
