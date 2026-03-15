"""Namespace-based sandbox (root mode) with overlayfs or btrfs backend.

Provides near-zero-overhead environment isolation using Linux namespaces and
copy-on-write filesystems, designed for high-frequency workloads where
environments need to be created, reset, and destroyed thousands of times.

Supported filesystem backends:
- **overlayfs** (default): lowerdir (base) + upperdir (per-env changes).
  Reset clears upperdir -- O(n) in number of changed files.
- **btrfs**: Subvolume snapshots.  Reset = delete snapshot + re-snapshot
  from base -- O(1) regardless of changes.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from agentdocker_lite._base import SandboxBase, SandboxConfig
from agentdocker_lite._shell import _PersistentShell

logger = logging.getLogger(__name__)


class NamespaceSandbox(SandboxBase):
    """Linux namespace sandbox with pluggable CoW filesystem backend.

    Each instance manages one isolated environment with:
    - ``unshare --pid --mount --fork`` for PID and mount namespace isolation
    - Persistent shell (chroot) for low-latency command execution
    - Copy-on-write filesystem (overlayfs or btrfs) for instant reset
    - Bind mounts for shared volumes
    - cgroup v2 for optional CPU / memory / PID limits

    Example::

        from agentdocker_lite import Sandbox, SandboxConfig

        config = SandboxConfig(image="ubuntu:22.04", working_dir="/workspace")
        sb = Sandbox(config, name="worker-0")
        output, ec = sb.run("echo hello world")
        sb.reset()        # instant filesystem reset
        sb.delete()       # full cleanup
    """

    SUPPORTED_FS_BACKENDS = ("overlayfs", "btrfs")

    def __init__(self, config: SandboxConfig, name: str = "default"):
        self._config = config
        self._name = name
        self._rootless = False
        self._init_rootful(config, name)

    # ------------------------------------------------------------------ #
    #  Rootful init (full namespace / overlayfs / cgroup isolation)        #
    # ------------------------------------------------------------------ #

    def _init_rootful(self, config: SandboxConfig, name: str) -> None:
        """Initialize in rootful mode -- full isolation."""
        if not config.image:
            raise ValueError("SandboxConfig.image is required.")

        self._fs_backend = config.fs_backend

        if self._fs_backend not in self.SUPPORTED_FS_BACKENDS:
            raise ValueError(
                f"Unsupported fs_backend {self._fs_backend!r}. "
                f"Choose from: {self.SUPPORTED_FS_BACKENDS}"
            )

        self._check_prerequisites(self._fs_backend)

        # --- paths --------------------------------------------------------
        rootfs_cache_dir = Path(config.rootfs_cache_dir)
        self._base_rootfs = self._resolve_base_rootfs(
            image=config.image,
            fs_backend=self._fs_backend,
            rootfs_cache_dir=rootfs_cache_dir,
        )

        env_base = Path(config.env_base_dir)
        self._env_dir = env_base / name
        self._rootfs = self._env_dir / "rootfs"

        # overlayfs-only paths
        self._upper_dir: Optional[Path] = None
        self._work_dir: Optional[Path] = None

        # --- state tracking -----------------------------------------------
        self._overlay_mounted = False
        self._btrfs_active = False
        self._bind_mounts: list[Path] = []
        self._cow_tmpdirs: list[str] = []
        self._cgroup_path: Optional[Path] = None
        self._cgroup_limits = {
            "cpu_max": config.cpu_max,
            "memory_max": config.memory_max,
            "pids_max": config.pids_max,
        }

        # --- setup --------------------------------------------------------
        t0 = time.monotonic()
        if self._fs_backend == "btrfs":
            self._setup_btrfs()
        else:
            self._upper_dir = self._env_dir / "upper"
            self._work_dir = self._env_dir / "work"
            self._setup_overlay()
        fs_ms = (time.monotonic() - t0) * 1000

        t1 = time.monotonic()
        self._setup_cgroup()
        cg_ms = (time.monotonic() - t1) * 1000

        t2 = time.monotonic()
        self._apply_config_volumes()
        vol_ms = (time.monotonic() - t2) * 1000

        if config.working_dir and config.working_dir != "/":
            wd = self._rootfs / config.working_dir.lstrip("/")
            wd.mkdir(parents=True, exist_ok=True)

        # Write seccomp helper into rootfs (called from init_script inside chroot)
        if config.seccomp:
            self._write_seccomp_helper()

        self._shell = self._detect_shell()
        self._cached_env = self._build_env()

        t3 = time.monotonic()
        self._persistent_shell = _PersistentShell(
            rootfs=self._rootfs,
            shell=self._shell,
            env=self._cached_env,
            working_dir=config.working_dir or "/",
            cgroup_path=self._cgroup_path,
            tty=config.tty,
            net_isolate=config.net_isolate,
            seccomp=config.seccomp,
            landlock_read=config.landlock_read,
            landlock_write=config.landlock_write,
            landlock_tcp_ports=config.landlock_tcp_ports,
        )
        shell_ms = (time.monotonic() - t3) * 1000

        self._bg_handles: dict[str, str] = {}  # handle -> pid

        logger.info(
            "Sandbox ready: name=%s rootfs=%s fs=%s "
            "[setup: fs=%.1fms cgroup=%.1fms volumes=%.1fms shell=%.1fms]",
            name,
            self._rootfs,
            self._fs_backend,
            fs_ms,
            cg_ms,
            vol_ms,
            shell_ms,
        )

    # ------------------------------------------------------------------ #
    #  Public API -- reset / delete                                        #
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Reset the sandbox filesystem to its initial state.

        This is the RL fast-path: ~27ms for overlayfs, ~28ms for btrfs.
        """
        self._bg_handles.clear()

        t0 = time.monotonic()

        self._persistent_shell.kill()
        self._unmount_binds()

        if self._fs_backend == "btrfs":
            self._reset_btrfs()
        else:
            self._reset_overlayfs()

        self._apply_config_volumes()

        if self._config.working_dir and self._config.working_dir != "/":
            wd = self._rootfs / self._config.working_dir.lstrip("/")
            wd.mkdir(parents=True, exist_ok=True)

        self._persistent_shell.start()

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "Environment reset (%.3fms fs=%s): %s",
            elapsed_ms,
            self._fs_backend,
            self._env_dir,
        )

    def delete(self) -> None:
        """Delete the sandbox and clean up all resources."""
        t0 = time.monotonic()

        self._persistent_shell.kill()

        self._unmount_all()

        if self._fs_backend == "btrfs" and self._btrfs_active:
            subprocess.run(
                ["btrfs", "subvolume", "delete", str(self._rootfs)],
                capture_output=True,
            )
            self._btrfs_active = False

        self._cleanup_cgroup()

        if self._env_dir.exists():
            shutil.rmtree(self._env_dir, ignore_errors=True)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Deleted sandbox (%.1fms fs=%s): %s",
            elapsed_ms,
            self._fs_backend,
            self._env_dir,
        )

    # ------------------------------------------------------------------ #
    #  Auto rootfs preparation                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_base_rootfs(
        image: str,
        fs_backend: str,
        rootfs_cache_dir: Path,
    ) -> Path:
        import fcntl

        candidate = Path(image)
        if candidate.exists() and candidate.is_dir():
            return candidate

        from agentdocker_lite.rootfs import (
            prepare_btrfs_rootfs_from_docker,
            prepare_rootfs_from_docker,
        )

        safe_name = image.replace("/", "_").replace(":", "_").replace(".", "_")
        cached_rootfs = rootfs_cache_dir / safe_name

        if cached_rootfs.exists() and cached_rootfs.is_dir():
            logger.info("Using cached rootfs for %s: %s", image, cached_rootfs)
            if fs_backend == "btrfs":
                NamespaceSandbox._verify_btrfs_subvolume(cached_rootfs)
            return cached_rootfs

        lock_path = rootfs_cache_dir / f".{safe_name}.lock"
        rootfs_cache_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if cached_rootfs.exists() and cached_rootfs.is_dir():
                    logger.info("Rootfs prepared by another worker: %s", cached_rootfs)
                    if fs_backend == "btrfs":
                        NamespaceSandbox._verify_btrfs_subvolume(cached_rootfs)
                    return cached_rootfs

                t0 = time.monotonic()
                logger.info(
                    "Auto-preparing rootfs from Docker image %s -> %s (fs=%s)",
                    image,
                    cached_rootfs,
                    fs_backend,
                )

                if fs_backend == "btrfs":
                    prepare_btrfs_rootfs_from_docker(image, cached_rootfs)
                else:
                    prepare_rootfs_from_docker(image, cached_rootfs)

                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.info(
                    "Auto-prepared rootfs (%.1fms): %s -> %s",
                    elapsed_ms,
                    image,
                    cached_rootfs,
                )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

        return cached_rootfs

    # ------------------------------------------------------------------ #
    #  Prerequisites                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_prerequisites(fs_backend: str = "overlayfs"):
        if os.geteuid() != 0:
            raise PermissionError(
                "Sandbox requires root for mount / cgroup operations. "
                "Run as root or with CAP_SYS_ADMIN."
            )
        if shutil.which("unshare") is None:
            raise FileNotFoundError(
                "unshare not found. Install util-linux: apt-get install util-linux"
            )
        if fs_backend == "overlayfs":
            result = subprocess.run(
                ["grep", "-q", "overlay", "/proc/filesystems"],
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Kernel does not support overlayfs. Load it: modprobe overlay"
                )
        elif fs_backend == "btrfs":
            if shutil.which("btrfs") is None:
                raise FileNotFoundError(
                    "btrfs-progs not found. Install: apt-get install btrfs-progs"
                )

    # ------------------------------------------------------------------ #
    #  Filesystem -- seccomp helper                                        #
    # ------------------------------------------------------------------ #

    def _write_seccomp_helper(self) -> None:
        """Write a self-contained seccomp helper script into the rootfs.

        Called from the init_script inside the chroot (after mounts are done).
        Uses the security module's apply_seccomp_filter via a copy of the source.
        """
        import inspect
        from agentdocker_lite import security

        src = inspect.getsource(security)
        helper = (
            "#!/usr/bin/env python3\n"
            "# Auto-generated seccomp helper — applied inside sandbox chroot\n"
            + src
            + "\napply_seccomp_filter()\n"
        )
        target = self._rootfs / "tmp" / ".adl_seccomp.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(helper)
        target.chmod(0o755)

    # ------------------------------------------------------------------ #
    #  Filesystem -- overlayfs                                             #
    # ------------------------------------------------------------------ #

    def _setup_overlay(self):
        for d in (self._upper_dir, self._work_dir, self._rootfs):
            d.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "mount",
                "-t",
                "overlay",
                "overlay",
                "-o",
                f"lowerdir={self._base_rootfs},"
                f"upperdir={self._upper_dir},"
                f"workdir={self._work_dir}",
                str(self._rootfs),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to mount overlayfs: {result.stderr.strip()}")
        self._overlay_mounted = True
        logger.debug("Mounted overlayfs at %s", self._rootfs)

    # ------------------------------------------------------------------ #
    #  Filesystem -- btrfs                                                 #
    # ------------------------------------------------------------------ #

    def _setup_btrfs(self):
        self._verify_btrfs_subvolume(self._base_rootfs)
        self._env_dir.mkdir(parents=True, exist_ok=True)

        if self._rootfs.exists():
            check = subprocess.run(
                ["btrfs", "subvolume", "show", str(self._rootfs)],
                capture_output=True,
                text=True,
            )
            if check.returncode == 0:
                subprocess.run(
                    ["btrfs", "subvolume", "delete", str(self._rootfs)],
                    capture_output=True,
                )
            else:
                shutil.rmtree(self._rootfs, ignore_errors=True)

        result = subprocess.run(
            [
                "btrfs",
                "subvolume",
                "snapshot",
                str(self._base_rootfs),
                str(self._rootfs),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"btrfs snapshot failed: {result.stderr.strip()}. "
                f"Ensure {self._base_rootfs} is a btrfs subvolume and "
                f"{self._env_dir} is on the same btrfs filesystem."
            )
        self._btrfs_active = True
        logger.debug(
            "Created btrfs snapshot: %s -> %s", self._base_rootfs, self._rootfs
        )

    @staticmethod
    def _verify_btrfs_subvolume(path: Path):
        result = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(
                f"Not a btrfs subvolume: {path}. "
                f"Create one via: btrfs subvolume create {path}"
            )

    # ------------------------------------------------------------------ #
    #  Volume management                                                   #
    # ------------------------------------------------------------------ #

    def _apply_config_volumes(self):
        for spec in self._config.volumes:
            if not isinstance(spec, str) or ":" not in spec:
                continue
            parts = spec.split(":")
            host_path = parts[0]
            container_path = parts[1] if len(parts) > 1 else "/"
            mode = parts[2] if len(parts) > 2 else "rw"
            if mode == "cow":
                self._overlay_mount(host_path, container_path)
            else:
                self._bind_mount(host_path, container_path, read_only=(mode == "ro"))

    def _bind_mount(
        self, host_path: str, container_path: str, read_only: bool = False
    ):
        target = self._rootfs / container_path.lstrip("/")
        target.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["mount", "--bind", host_path, str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to bind mount %s -> %s: %s",
                host_path,
                container_path,
                result.stderr.strip(),
            )
            return

        self._bind_mounts.append(target)

        if read_only:
            subprocess.run(
                ["mount", "-o", "remount,ro,bind", str(target)],
                capture_output=True,
            )
        logger.debug(
            "Bind mounted %s -> %s (%s)",
            host_path,
            container_path,
            "ro" if read_only else "rw",
        )

    def _overlay_mount(self, host_path: str, container_path: str):
        """Mount a host directory as copy-on-write via overlayfs.

        Writes inside the sandbox go to a temporary upperdir; the host
        directory is never modified.  Mode ``"cow"`` in volume specs.
        """
        import tempfile

        target = self._rootfs / container_path.lstrip("/")
        target.mkdir(parents=True, exist_ok=True)

        work_base = tempfile.mkdtemp(prefix="adl_cow_")
        upper = Path(work_base) / "upper"
        work = Path(work_base) / "work"
        upper.mkdir()
        work.mkdir()

        result = subprocess.run(
            [
                "mount", "-t", "overlay", "overlay",
                "-o", f"lowerdir={host_path},upperdir={upper},workdir={work}",
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to overlay mount %s -> %s: %s",
                host_path, container_path, result.stderr.strip(),
            )
            return

        # Track for cleanup (unmount overlay, then remove tmpdir)
        self._bind_mounts.append(target)
        self._cow_tmpdirs.append(work_base)
        logger.debug(
            "Overlay mounted %s -> %s (cow, upper=%s)", host_path, container_path, upper,
        )

    def _unmount_binds(self):
        import shutil as _shutil

        for mount_point in reversed(self._bind_mounts):
            subprocess.run(["umount", "-l", str(mount_point)], capture_output=True)
        self._bind_mounts.clear()
        for tmpdir in self._cow_tmpdirs:
            _shutil.rmtree(tmpdir, ignore_errors=True)
        self._cow_tmpdirs = []

    def _unmount_all(self):
        self._unmount_binds()
        if self._fs_backend == "overlayfs" and self._overlay_mounted:
            subprocess.run(["umount", "-l", str(self._rootfs)], capture_output=True)
            self._overlay_mounted = False

    # ------------------------------------------------------------------ #
    #  cgroup v2 resource limits                                           #
    # ------------------------------------------------------------------ #

    def _setup_cgroup(self):
        if not any(self._cgroup_limits.values()):
            return

        if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
            logger.warning(
                "cgroup v2 not available -- resource limits will not be enforced."
            )
            return

        cgroup_name = self._env_dir.name
        self._cgroup_path = Path(f"/sys/fs/cgroup/agentdocker_lite/{cgroup_name}")
        try:
            self._cgroup_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Failed to create cgroup %s: %s", self._cgroup_path, e)
            self._cgroup_path = None
            return

        parent = self._cgroup_path.parent
        try:
            subtree_ctl = parent / "cgroup.subtree_control"
            if subtree_ctl.exists():
                for key, ctrl in [
                    ("cpu_max", "cpu"),
                    ("memory_max", "memory"),
                    ("pids_max", "pids"),
                ]:
                    if self._cgroup_limits.get(key):
                        try:
                            subtree_ctl.write_text(f"+{ctrl}")
                        except OSError:
                            logger.debug(
                                "Could not enable cgroup controller %s", ctrl
                            )
        except OSError:
            pass

        limit_files = {
            "cpu_max": "cpu.max",
            "memory_max": "memory.max",
            "pids_max": "pids.max",
        }
        for key, filename in limit_files.items():
            value = self._cgroup_limits.get(key)
            if value:
                try:
                    (self._cgroup_path / filename).write_text(str(value))
                    logger.debug("cgroup %s = %s", filename, value)
                except OSError as e:
                    logger.warning("Failed to set cgroup %s: %s", filename, e)

    def _cleanup_cgroup(self):
        if not self._cgroup_path or not self._cgroup_path.exists():
            return
        try:
            procs_file = self._cgroup_path / "cgroup.procs"
            if procs_file.exists():
                for pid in procs_file.read_text().strip().split():
                    try:
                        os.kill(int(pid), 9)
                    except (ProcessLookupError, ValueError):
                        pass
            kill_file = self._cgroup_path / "cgroup.kill"
            if kill_file.exists():
                try:
                    kill_file.write_text("1")
                except OSError:
                    pass
            self._cgroup_path.rmdir()
        except OSError as e:
            logger.debug("cgroup cleanup (non-fatal): %s", e)

    # ------------------------------------------------------------------ #
    #  Reset helpers                                                       #
    # ------------------------------------------------------------------ #

    def _reset_overlayfs(self):
        if self._overlay_mounted:
            subprocess.run(["umount", "-l", str(self._rootfs)], capture_output=True)
            self._overlay_mounted = False

        if self._upper_dir and self._upper_dir.exists():
            shutil.rmtree(self._upper_dir)
        if self._upper_dir:
            self._upper_dir.mkdir(parents=True)

        if self._work_dir and self._work_dir.exists():
            shutil.rmtree(self._work_dir)
        if self._work_dir:
            self._work_dir.mkdir(parents=True)

        self._setup_overlay()

    def _reset_btrfs(self):
        result = subprocess.run(
            ["btrfs", "subvolume", "delete", str(self._rootfs)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "btrfs subvolume delete failed (proceeding): %s",
                result.stderr.strip(),
            )
            if self._rootfs.exists():
                shutil.rmtree(self._rootfs, ignore_errors=True)

        result = subprocess.run(
            [
                "btrfs",
                "subvolume",
                "snapshot",
                str(self._base_rootfs),
                str(self._rootfs),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"btrfs snapshot failed on reset: {result.stderr.strip()}"
            )
        self._btrfs_active = True

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _detect_shell(self) -> str:
        if self._host_path("/bin/bash").exists():
            return "/bin/bash"
        return "/bin/sh"
