"""Tests for the nitrobox CLI."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from nitrobox import Sandbox, SandboxConfig

TEST_IMAGE = os.environ.get("LITE_SANDBOX_TEST_IMAGE", "ubuntu:22.04")


def _skip_if_root():
    if os.geteuid() == 0:
        pytest.skip("CLI tests must run as non-root")


def _requires_gobin():
    """Skip if nitrobox-core Go binary is not available."""
    from nitrobox._gobin import gobin
    bin_path = gobin()
    if not (os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)):
        pytest.skip("requires nitrobox-core Go binary")


def _nbx(
    *args: str,
    env: dict | None = None,
    input: str | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["python", "-m", "nitrobox.cli", *args],
        capture_output=True, text=True, timeout=timeout,
        env=run_env, input=input,
    )


class TestCli:
    def test_ps_empty(self):
        """ps should work with no sandboxes."""
        _skip_if_root()
        result = _nbx("ps")
        assert result.returncode == 0
        assert "No sandboxes" in result.stdout or "NAME" in result.stdout

    def test_cleanup_empty(self):
        """cleanup should work with nothing to clean."""
        _skip_if_root()
        result = _nbx("cleanup")
        assert result.returncode == 0
        assert "No stale" in result.stdout or "Cleaned up" in result.stdout

    def test_kill_nonexistent(self):
        """kill should error on unknown sandbox."""
        _skip_if_root()
        result = _nbx("kill", "nonexistent-sandbox-xyz")
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_no_args_shows_help(self):
        """No subcommand should show help."""
        _skip_if_root()
        result = _nbx()
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

        result = _nbx("--dir", env_dir, "cleanup")
        assert result.returncode == 0
        assert not orphan.exists(), f"orphan dir not cleaned: {list(orphan.rglob('*'))}"

    def test_kill_all(self, tmp_path, shared_cache_dir):
        """kill --all should kill all sandbox shells without killing us."""
        _skip_if_root()
        _requires_gobin()
        env_dir = str(tmp_path / "envs")

        sandboxes = []
        for name in ("kill-all-1", "kill-all-2"):
            box = Sandbox(SandboxConfig(
                image=TEST_IMAGE,
                env_base_dir=env_dir,
                rootfs_cache_dir=shared_cache_dir,
            ), name=name)
            sandboxes.append(box)

        result = _nbx("--dir", env_dir, "ps")
        assert "kill-all-1" in result.stdout
        assert "kill-all-2" in result.stdout

        result = _nbx("--dir", env_dir, "kill", "--all")
        assert result.returncode == 0

        result = _nbx("--dir", env_dir, "ps")
        assert "No sandboxes" in result.stdout

    def test_ps_shows_running_sandbox(self, tmp_path, shared_cache_dir):
        """ps should list a running sandbox."""
        _skip_if_root()
        _requires_gobin()
        env_dir = str(tmp_path / "envs")
        box = Sandbox(SandboxConfig(
            image=TEST_IMAGE,
            env_base_dir=env_dir,
            rootfs_cache_dir=shared_cache_dir,
        ), name="cli-ps-test")
        try:
            result = _nbx("--dir", env_dir, "ps")
            assert result.returncode == 0
            assert "cli-ps-test" in result.stdout
            assert "running" in result.stdout
        finally:
            box.delete()

    def test_kill_and_cleanup(self, tmp_path, shared_cache_dir):
        """kill should terminate the sandbox shell and clean up the dir."""
        _skip_if_root()
        _requires_gobin()
        env_dir = str(tmp_path / "envs")

        # Create sandbox in subprocess
        proc = subprocess.Popen(
            [
                "python", "-c",
                f"from nitrobox import Sandbox, SandboxConfig; "
                f"import time; "
                f"box = Sandbox(SandboxConfig(image='{TEST_IMAGE}', "
                f"env_base_dir='{env_dir}', "
                f"rootfs_cache_dir='{shared_cache_dir}'), "
                f"name='cli-kill-test'); "
                f"time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2)

        try:
            result = _nbx("--dir", env_dir, "ps")
            assert "cli-kill-test" in result.stdout

            # nitrobox kill targets the shell process, not the owner
            result = _nbx("--dir", env_dir, "kill", "cli-kill-test")
            assert result.returncode == 0
            assert "killed" in result.stdout

            # Dir should be cleaned by kill's auto-cleanup
            import pathlib
            env_path = pathlib.Path(env_dir)
            assert not (env_path / "cli-kill-test").exists(), (
                f"sandbox dir not cleaned: {list((env_path / 'cli-kill-test').rglob('*'))}"
            )

            # Owner subprocess should still be alive (only shell was killed)
            assert proc.poll() is None, "owner process should not be killed"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_kill_from_owner_process(self, tmp_path, shared_cache_dir):
        """nitrobox kill from the sandbox owner process should not kill itself."""
        _skip_if_root()
        _requires_gobin()
        env_dir = str(tmp_path / "envs")

        box = Sandbox(SandboxConfig(
            image=TEST_IMAGE,
            env_base_dir=env_dir,
            rootfs_cache_dir=shared_cache_dir,
        ), name="kill-self-test")

        result = _nbx("--dir", env_dir, "ps")
        assert "kill-self-test" in result.stdout

        # nitrobox kill should kill the shell, not us
        result = _nbx("--dir", env_dir, "kill", "kill-self-test")
        assert result.returncode == 0

        # We're still alive — the test process wasn't killed
        # Sandbox is now broken (shell dead) but we can still clean up
        try:
            box.delete()
        except Exception:
            pass  # shell already dead, delete may partially fail
        # cleanup_stale handles the rest
        Sandbox.cleanup_stale(env_dir)


class TestImageAndPruneCli:
    """image ls / image rm / buildkit-prune — cache management commands.

    These point XDG_DATA_HOME at a tmp dir so they don't touch the user's
    real BuildKit cache.
    """

    def test_image_ls_no_cache(self, tmp_path):
        """image ls on a clean XDG dir prints 'No images cached.'"""
        result = _nbx("image", "ls", env={"XDG_DATA_HOME": str(tmp_path)})
        assert result.returncode == 0
        assert "No images cached" in result.stdout

    def test_image_ls_with_fake_registry(self, tmp_path):
        """image ls reads from image-registry.json and formats the listing."""
        import json
        bk = tmp_path / "nitrobox" / "buildkit"
        bk.mkdir(parents=True)
        (bk / "image-registry.json").write_text(json.dumps({
            "alpine:3.20": "sha256:abc123def456" + "0" * 52,
            "python:3.12-slim": "sha256:fedcba987654" + "0" * 52,
        }))

        result = _nbx("image", "ls", env={"XDG_DATA_HOME": str(tmp_path)})
        assert result.returncode == 0
        assert "alpine:3.20" in result.stdout
        assert "python:3.12-slim" in result.stdout
        assert "abc123def456" in result.stdout   # first 12 chars of digest
        assert "2 image(s)" in result.stdout

    def test_image_rm_unknown_image_warns_not_fatal(self, tmp_path):
        """rm of an unregistered image warns but exits 0 — delete is best-effort."""
        _requires_gobin()
        bk = tmp_path / "nitrobox" / "buildkit"
        bk.mkdir(parents=True)
        (bk / "image-registry.json").write_text("{}")

        result = _nbx(
            "image", "rm", "does-not-exist:v1",
            env={"XDG_DATA_HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert "not in registry" in result.stderr
        assert "Deleted: does-not-exist:v1" in result.stdout

    def test_buildkit_prune_abort_preserves_state(self, tmp_path):
        """buildkit-prune with 'n' at the prompt aborts without touching state."""
        bk = tmp_path / "nitrobox" / "buildkit"
        bk.mkdir(parents=True)
        (bk / "image-registry.json").write_text('{"alpine:3.20": "sha256:abc"}')
        (bk / "runc-overlayfs").mkdir()

        result = _nbx(
            "buildkit-prune",
            env={"XDG_DATA_HOME": str(tmp_path)},
            input="n\n",
        )
        assert result.returncode == 0
        assert "Aborted" in result.stdout
        # State must still be there.
        assert (bk / "image-registry.json").exists()
        assert (bk / "runc-overlayfs").exists()

    def test_buildkit_prune_no_state(self, tmp_path):
        """buildkit-prune on a missing cache dir is a no-op."""
        result = _nbx(
            "buildkit-prune", "--yes",
            env={"XDG_DATA_HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "No BuildKit state" in result.stdout

    def test_buildkit_prune_with_yes_wipes(self, tmp_path):
        """buildkit-prune --yes deletes the directory without prompting."""
        bk = tmp_path / "nitrobox" / "buildkit"
        bk.mkdir(parents=True)
        (bk / "image-registry.json").write_text('{}')
        (bk / "marker.txt").write_text("will-be-deleted")

        # With --yes we skip the prompt; daemon-stop is a no-op when no
        # daemon is running for this XDG root.
        result = _nbx(
            "buildkit-prune", "--yes",
            env={"XDG_DATA_HOME": str(tmp_path)},
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "Pruned" in result.stdout
        assert not bk.exists()
