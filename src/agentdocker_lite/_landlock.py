"""Landlock-based sandbox (rootless mode) without namespace isolation.

When running without root privileges, this sandbox uses Landlock LSM
for filesystem access control and seccomp-bpf for syscall filtering.
No namespaces, chroot, overlayfs, or cgroup isolation is used.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from agentdocker_lite._base import SandboxBase, SandboxConfig
from agentdocker_lite._shell import _PersistentShell

logger = logging.getLogger(__name__)


class LandlockSandbox(SandboxBase):
    """Rootless sandbox using Landlock LSM for filesystem access control.

    No namespace isolation -- suitable for unprivileged users who need
    basic sandboxing via Landlock + seccomp.

    Example::

        from agentdocker_lite import Sandbox, SandboxConfig

        config = SandboxConfig(working_dir="/tmp/workspace")
        sb = Sandbox(config, name="worker-0")
        output, ec = sb.run("echo hello world")
        sb.delete()       # cleanup
    """

    def __init__(self, config: SandboxConfig, name: str = "default"):
        self._config = config
        self._name = name
        self._rootless = True
        self._init_rootless(config, name)

    # ------------------------------------------------------------------ #
    #  Rootless init (no namespace / chroot / overlayfs / cgroup)          #
    # ------------------------------------------------------------------ #

    def _init_rootless(self, config: SandboxConfig, name: str) -> None:
        """Initialize in rootless mode -- no isolation, Landlock only."""
        self._fs_backend = "none"

        wd = Path(config.working_dir or ".").resolve()
        wd.mkdir(parents=True, exist_ok=True)
        self._rootfs = wd

        self._env_dir = Path(config.env_base_dir) / name
        self._env_dir.mkdir(parents=True, exist_ok=True)

        # Not used in rootless, but keep attributes to avoid AttributeError
        self._base_rootfs: Optional[Path] = None
        self._upper_dir: Optional[Path] = None
        self._work_dir: Optional[Path] = None
        self._overlay_mounted = False
        self._btrfs_active = False
        self._bind_mounts: list[Path] = []
        self._cow_tmpdirs: list[str] = []
        self._cgroup_path: Optional[Path] = None
        self._cgroup_limits: dict[str, Optional[str]] = {}

        # Auto-enable Landlock if not explicitly configured
        ll_read = config.landlock_read
        ll_write = config.landlock_write
        if ll_read is None and ll_write is None:
            ll_read = ["/"]
            ll_write = [str(wd), "/tmp", "/dev"]

        # Find shell directly on the host
        shell = shutil.which("bash") or shutil.which("sh")
        if not shell:
            raise FileNotFoundError("No bash or sh found on PATH")
        self._shell = shell
        self._cached_env = self._build_env()

        self._persistent_shell = _PersistentShell(
            rootfs=self._rootfs,
            shell=self._shell,
            env=self._cached_env,
            working_dir=str(wd),
            cgroup_path=None,
            tty=config.tty,
            net_isolate=False,
            seccomp=config.seccomp,
            landlock_read=ll_read,
            landlock_write=ll_write,
            landlock_tcp_ports=config.landlock_tcp_ports,
            rootless=True,
        )

        self._bg_handles: dict[str, str] = {}

        logger.info(
            "Sandbox ready (rootless): name=%s working_dir=%s",
            name,
            wd,
        )

    # ------------------------------------------------------------------ #
    #  Public API -- reset / delete                                        #
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Reset is a no-op in rootless mode (no overlayfs to clear)."""
        self._bg_handles.clear()
        logger.debug("reset() is a no-op in rootless mode")

    def delete(self) -> None:
        """Delete the sandbox and clean up all resources."""
        import time

        t0 = time.monotonic()

        self._persistent_shell.kill()

        # Rootless: only clean up env_dir, skip unmount/cgroup
        if self._env_dir.exists():
            shutil.rmtree(self._env_dir, ignore_errors=True)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Deleted sandbox (%.1fms rootless): %s",
            elapsed_ms,
            self._env_dir,
        )
