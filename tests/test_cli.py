"""Tests for the adl CLI."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from agentdocker_lite import Sandbox, SandboxConfig

TEST_IMAGE = os.environ.get("LITE_SANDBOX_TEST_IMAGE", "ubuntu:22.04")


def _skip_if_root():
    if os.geteuid() == 0:
        pytest.skip("CLI tests must run as non-root")


def _requires_docker():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("requires Docker")


def _adl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python", "-m", "agentdocker_lite.cli", *args],
        capture_output=True, text=True, timeout=10,
    )


class TestCli:
    def test_ps_empty(self):
        """ps should work with no sandboxes."""
        _skip_if_root()
        result = _adl("ps")
        assert result.returncode == 0
        assert "No sandboxes" in result.stdout or "NAME" in result.stdout

    def test_cleanup_empty(self):
        """cleanup should work with nothing to clean."""
        _skip_if_root()
        result = _adl("cleanup")
        assert result.returncode == 0
        assert "No stale" in result.stdout or "Cleaned up" in result.stdout

    def test_kill_nonexistent(self):
        """kill should error on unknown sandbox."""
        _skip_if_root()
        result = _adl("kill", "nonexistent-sandbox-xyz")
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_no_args_shows_help(self):
        """No subcommand should show help."""
        _skip_if_root()
        result = _adl()
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "ps" in result.stdout

    def test_cleanup_orphaned_dir(self, tmp_path):
        """cleanup should remove dirs with work/ but no .pid file."""
        _skip_if_root()
        env_dir = str(tmp_path / "envs")
        # Simulate orphaned sandbox dir (partial atexit cleanup)
        orphan = tmp_path / "envs" / "orphan-sandbox"
        (orphan / "work" / "work").mkdir(parents=True)
        (orphan / "work" / "work").chmod(0o000)
        (orphan / "upper").mkdir()

        result = _adl("--dir", env_dir, "cleanup")
        assert result.returncode == 0
        assert not orphan.exists(), f"orphan dir not cleaned: {list(orphan.rglob('*'))}"

    def test_kill_all(self, tmp_path, shared_cache_dir):
        """kill --all should kill all sandboxes."""
        _skip_if_root()
        _requires_docker()
        env_dir = str(tmp_path / "envs")

        # Create sandboxes in subprocesses (so adl kill --all
        # doesn't SIGTERM the test process itself)
        procs = []
        for name in ("kill-all-1", "kill-all-2"):
            p = subprocess.Popen(
                ["python", "-c",
                 f"from agentdocker_lite import Sandbox, SandboxConfig; import time; "
                 f"Sandbox(SandboxConfig(image='{TEST_IMAGE}', "
                 f"env_base_dir='{env_dir}', "
                 f"rootfs_cache_dir='{shared_cache_dir}'), "
                 f"name='{name}'); time.sleep(60)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            procs.append(p)
        time.sleep(2)

        try:
            result = _adl("--dir", env_dir, "ps")
            assert "kill-all-1" in result.stdout
            assert "kill-all-2" in result.stdout

            result = _adl("--dir", env_dir, "kill", "--all")
            assert result.returncode == 0

            result = _adl("--dir", env_dir, "ps")
            assert "No sandboxes" in result.stdout
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def test_ps_shows_running_sandbox(self, tmp_path, shared_cache_dir):
        """ps should list a running sandbox."""
        _skip_if_root()
        _requires_docker()
        env_dir = str(tmp_path / "envs")
        sb = Sandbox(SandboxConfig(
            image=TEST_IMAGE,
            env_base_dir=env_dir,
            rootfs_cache_dir=shared_cache_dir,
        ), name="cli-ps-test")
        try:
            result = _adl("--dir", env_dir, "ps")
            assert result.returncode == 0
            assert "cli-ps-test" in result.stdout
            assert "running" in result.stdout
        finally:
            sb.delete()

    def test_kill_and_cleanup(self, tmp_path, shared_cache_dir):
        """kill + cleanup should fully remove a sandbox."""
        _skip_if_root()
        _requires_docker()
        env_dir = str(tmp_path / "envs")

        # Start sandbox in a subprocess so we can kill the owner
        proc = subprocess.Popen(
            [
                "python", "-c",
                f"from agentdocker_lite import Sandbox, SandboxConfig; "
                f"import time; "
                f"sb = Sandbox(SandboxConfig(image='{TEST_IMAGE}', "
                f"env_base_dir='{env_dir}', "
                f"rootfs_cache_dir='{shared_cache_dir}'), "
                f"name='cli-kill-test'); "
                f"time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2)  # wait for sandbox to start

        try:
            # Verify it shows up
            result = _adl("--dir", env_dir, "ps")
            assert "cli-kill-test" in result.stdout

            # Kill it — adl kill now also runs cleanup_stale internally
            result = _adl("--dir", env_dir, "kill", "cli-kill-test")
            assert result.returncode == 0
            assert "killed" in result.stdout

            # Directory should be fully cleaned by kill's auto-cleanup
            import pathlib
            env_path = pathlib.Path(env_dir)
            assert not (env_path / "cli-kill-test").exists(), (
                f"sandbox dir not cleaned: {list((env_path / 'cli-kill-test').rglob('*'))}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
