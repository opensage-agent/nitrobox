"""User-namespace-based sandbox (rootless mode).

Provides the same namespace + overlayfs + chroot isolation as
RootfulSandbox, but without requiring real root privileges.
Uses ``unshare --user --map-root-user`` to get fake root inside
a user namespace (requires kernel >= 5.11).

cgroup resource limits are applied via systemd delegation
(``systemd-run --user --scope``).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from agentdocker_lite.backends.base import SandboxConfig
from agentdocker_lite.backends.rootful import RootfulSandbox
from agentdocker_lite._shell import _PersistentShell

logger = logging.getLogger(__name__)


class RootlessSandbox(RootfulSandbox):
    """Rootless sandbox using user namespaces.

    Inherits all functionality from RootfulSandbox but forces user
    namespace mode (``_userns = True``).  The Sandbox() factory
    creates this class when not running as root.

    Example::

        from agentdocker_lite import Sandbox, SandboxConfig

        config = SandboxConfig(image="ubuntu:22.04", working_dir="/workspace")
        sb = Sandbox(config, name="worker-0")   # RootlessSandbox if not root
        output, ec = sb.run("echo hello world")
        sb.reset()
        sb.delete()
    """

    def __init__(self, config: SandboxConfig, name: str = "default"):
        # Skip RootfulSandbox.__init__ — it would try rootful mode.
        # We directly initialize in userns mode.
        self._config = config
        self._name = name
        self._userns = True
        self._bg_handles: dict[str, str] = {}
        self._pasta_process = None
        try:
            self._init_userns(config, name)
        except Exception:
            # Clean up overlayfs work dirs (kernel creates d--------- entries)
            env_dir = getattr(self, "_env_dir", None)
            if env_dir and env_dir.exists():
                for child in env_dir.rglob("*"):
                    try:
                        child.chmod(0o700)
                    except OSError:
                        pass
                shutil.rmtree(env_dir, ignore_errors=True)
            raise
        self._register(self)

    # ------------------------------------------------------------------ #
    #  User namespace init (no real root required)                          #
    # ------------------------------------------------------------------ #

    def _init_userns(self, config: SandboxConfig, name: str) -> None:
        """Initialize in user namespace mode -- namespace+overlayfs without root."""
        if not config.image:
            raise ValueError("SandboxConfig.image is required.")

        if config.fs_backend != "overlayfs":
            raise ValueError(
                f"Rootless mode only supports overlayfs, got fs_backend={config.fs_backend!r}. "
                f"btrfs requires root."
            )
        self._fs_backend = "overlayfs"

        self._check_prerequisites_userns()

        # --- paths --------------------------------------------------------
        rootfs_cache_dir = Path(config.rootfs_cache_dir)
        from agentdocker_lite.rootfs import _detect_whiteout_strategy
        whiteout_strategy = _detect_whiteout_strategy()

        if whiteout_strategy == "none":
            logger.debug("Kernel too old for rootless layer cache, using flat rootfs")
            self._base_rootfs = self._resolve_flat_rootfs(
                image=config.image,
                rootfs_cache_dir=rootfs_cache_dir,
            )
            self._layer_dirs: list[Path] | None = None
            self._lowerdir_spec = str(self._base_rootfs)
        else:
            self._base_rootfs, self._layer_dirs = self._resolve_base_rootfs(
                image=config.image,
                rootfs_cache_dir=rootfs_cache_dir,
                fs_backend="overlayfs",
            )
            if self._layer_dirs:
                self._lowerdir_spec = ":".join(
                    str(d) for d in reversed(self._layer_dirs)
                )
            else:
                self._lowerdir_spec = str(self._base_rootfs)

        env_base = Path(config.env_base_dir)
        self._env_dir = env_base / name
        self._rootfs = self._env_dir / "rootfs"   # overlay merged (inside namespace only)
        self._upper_dir = self._env_dir / "upper"
        self._work_dir = self._env_dir / "work"

        for d in (self._upper_dir, self._work_dir, self._rootfs):
            d.mkdir(parents=True, exist_ok=True)

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
            "io_max": config.io_max,
            "cpuset_cpus": config.cpuset_cpus,
            "cpu_shares": config.cpu_shares,
            "memory_swap": config.memory_swap,
        }

        # --- cgroup via systemd delegation --------------------------------
        self._systemd_scope_properties: list[str] = []
        if any(self._cgroup_limits.values()):
            if shutil.which("systemd-run"):
                prop_map = {
                    "cpu_max": "CPUQuota",
                    "memory_max": "MemoryMax",
                    "pids_max": "TasksMax",
                    "io_max": "IOWriteBandwidthMax",
                    "cpu_shares": "CPUWeight",
                    "memory_swap": "MemorySwapMax",
                }
                for key, sd_prop in prop_map.items():
                    value = self._cgroup_limits.get(key)
                    if value:
                        if key == "cpu_max":
                            # Convert "50000 100000" → "50%"
                            parts = str(value).split()
                            if len(parts) == 2:
                                pct = int(int(parts[0]) / int(parts[1]) * 100)
                                self._systemd_scope_properties.append(f"{sd_prop}={pct}%")
                        elif key == "io_max":
                            # io_max is raw cgroup format "MAJ:MIN wbps=N"
                            # systemd uses "IOWriteBandwidthMax=/dev/X N"
                            # Pass as-is; user must use systemd format
                            self._systemd_scope_properties.append(f"{sd_prop}={value}")
                        elif key == "cpu_shares":
                            from agentdocker_lite.backends.base import _convert_cpu_shares
                            weight = _convert_cpu_shares(int(value))
                            self._systemd_scope_properties.append(f"{sd_prop}={weight}")
                        else:
                            self._systemd_scope_properties.append(f"{sd_prop}={value}")
                logger.debug(
                    "cgroup via systemd delegation: %s",
                    self._systemd_scope_properties,
                )
            else:
                logger.warning(
                    "cgroup resource limits requested but systemd-run not found. "
                    "Run as root for direct cgroup access."
                )
        # Device passthrough is handled in the setup script via bind mounts.
        # In user namespaces, bind-mounting from host devtmpfs preserves the
        # original superblock (no SB_I_NODEV), so device nodes work if the
        # user has the required group membership (e.g., kvm group for /dev/kvm).

        # --- Landlock validation ------------------------------------------
        ll_config = self._build_landlock_config(config)

        # Build landlock path lists for Rust init
        ll_read: list[str] = []
        ll_write: list[str] = []
        ll_ports: list[int] = []
        ll_strict = False
        if ll_config:
            ll_strict = True
            essential_writable = {"/dev", "/proc", "/tmp"}
            essential_readable = {"/dev", "/proc", "/sys", "/tmp"}
            writable_set: set[str] = set()
            if config.writable_paths is not None:
                writable_set = set(config.writable_paths) | essential_writable
                ll_write = sorted(writable_set)
            if config.readable_paths is not None:
                all_readable = set(config.readable_paths) | essential_readable
                ll_read = sorted(all_readable - writable_set)
            if config.allowed_ports is not None:
                ll_ports = list(config.allowed_ports)

        # --- cap_add ------------------------------------------------------
        cap_add_nums: list[int] = []
        if config.cap_add:
            from agentdocker_lite.backends.base import cap_names_to_numbers
            cap_add_nums = cap_names_to_numbers(config.cap_add) or []

        # --- DNS ----------------------------------------------------------
        if config.dns:
            self._write_dns(config.dns)

        # --- working dir in upper dir -------------------------------------
        if config.working_dir and config.working_dir != "/":
            wd = self._upper_dir / config.working_dir.lstrip("/")
            wd.mkdir(parents=True, exist_ok=True)

        self._shell = self._detect_shell()
        self._cached_env = self._build_env()

        # --- detect subordinate uid range for full mapping -----------------
        subuid_range = self._detect_subuid_range()

        # --- start persistent shell (Rust init chain) ---------------------
        from agentdocker_lite._shell import SpawnConfig
        shell_net_isolate = config.net_isolate and not config.port_map and not config.net_ns
        spawn_cfg: SpawnConfig = {
            "rootfs": str(self._rootfs),
            "shell": self._shell,
            "working_dir": config.working_dir or "/",
            "env": self._cached_env,
            "rootful": False,
            "userns": True,
            "net_isolate": shell_net_isolate,
            "net_ns": config.net_ns,
            "shared_userns": config.shared_userns,
            "subuid_range": subuid_range,
            "seccomp": config.seccomp,
            "cap_add": cap_add_nums,
            "hostname": config.hostname,
            "read_only": config.read_only,
            "entrypoint": config.entrypoint or [],
            "tty": config.tty,
            "lowerdir_spec": self._lowerdir_spec,
            "upper_dir": str(self._upper_dir),
            "work_dir": str(self._work_dir),
            "volumes": list(config.volumes) if config.volumes else [],
            "devices": config.devices or [],
            "shm_size": int(config.shm_size) if config.shm_size else None,
            "tmpfs_mounts": list(config.tmpfs) if config.tmpfs else [],
            "landlock_read_paths": ll_read,
            "landlock_write_paths": ll_write,
            "landlock_ports": ll_ports,
            "landlock_strict": ll_strict,
            "cgroup_path": None,
            "port_map": list(config.port_map) if config.port_map else [],
            "pasta_bin": self._find_pasta_bin(),
            "ipv6": config.ipv6 if hasattr(config, 'ipv6') else False,
            "env_dir": str(self._env_dir),
        }
        t0 = time.monotonic()
        self._persistent_shell = _PersistentShell(
            spawn_cfg, ulimits=config.ulimits or None,
        )
        shell_ms = (time.monotonic() - t0) * 1000

        self._bg_handles: dict[str, str] = {}
        self._pasta_process = None
        # Rootless pasta is started from inside the setup script (not here)

        if config.oom_score_adj is not None:
            self._apply_oom_score_adj(config.oom_score_adj)

        # Write PID file for stale sandbox cleanup.
        # Store the shell process PID (not the creator's PID) so that
        # adl kill can terminate the sandbox without killing the owner.
        pid_file = self._env_dir / ".pid"
        pid_file.write_text(str(self._persistent_shell.pid))

        self.features: dict[str, object] = {
            "userns": True,
            "layer_cache": self._layer_dirs is not None,
            "whiteout": whiteout_strategy,
            "pidfd": self._persistent_shell._pidfd is not None,
            "seccomp": config.seccomp,
            "landlock": ll_config is not None,
            "netns": config.net_isolate,
            "devices": bool(config.devices),
            "mask_paths": True,
            "cap_drop": True,
        }
        feat_str = ", ".join(
            k if v is True else f"{k}={v}"
            for k, v in self.features.items()
            if v
        )
        logger.info(
            "Sandbox ready (userns): name=%s rootfs=%s features=[%s] [shell=%.1fms]",
            name, self._rootfs, feat_str, shell_ms,
        )

    @staticmethod
    def _find_pasta_bin() -> str | None:
        """Find the pasta binary (vendored or system)."""
        vendored = Path(__file__).parent.parent / "_vendor" / "pasta"
        if vendored.exists() and vendored.is_file():
            return str(vendored)
        if shutil.which("pasta"):
            return "pasta"
        return None

    _prereq_checked = False

    @classmethod
    def _check_prerequisites_userns(cls) -> None:
        """Check user namespace prerequisites (cached after first success)."""
        if cls._prereq_checked:
            return
        if shutil.which("unshare") is None:
            raise FileNotFoundError(
                "unshare not found. Install util-linux: apt-get install util-linux"
            )
        # Test if user namespaces actually work
        result = subprocess.run(
            ["unshare", "--user", "--map-root-user", "true"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "User namespaces are not available. Possible fixes:\n"
                "  sysctl -w kernel.unprivileged_userns_clone=1\n"
                "  sysctl -w kernel.apparmor_restrict_unprivileged_userns=0\n"
                f"Error: {result.stderr.decode().strip()}"
            )
        cls._prereq_checked = True

    _cached_subuid_range: Optional[tuple[int, int, int]] = None
    _subuid_detected = False

    @classmethod
    def _detect_subuid_range(cls) -> Optional[tuple[int, int, int]]:
        """Detect subordinate UID range for full uid mapping in user namespaces.

        Checks for newuidmap/newgidmap and /etc/subuid entry for the current user.
        Returns (outer_uid, sub_start, sub_count) if available, None otherwise.
        When None, the sandbox falls back to --map-root-user (only uid 0 mapped).

        Result is cached — subuid config doesn't change during process lifetime.
        """
        if cls._subuid_detected:
            return cls._cached_subuid_range

        if shutil.which("newuidmap") is None or shutil.which("newgidmap") is None:
            logger.debug(
                "newuidmap/newgidmap not found. Install uidmap package for full "
                "uid mapping (enables apt-get, useradd, etc. inside sandbox). "
                "Falling back to root-only mapping."
            )
            cls._subuid_detected = True
            return None

        import getpass
        try:
            username = getpass.getuser()
        except Exception:
            cls._subuid_detected = True
            return None

        uid = os.getuid()

        # Parse /etc/subuid for the current user
        try:
            with open("/etc/subuid") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) != 3:
                        continue
                    # Match by username or UID
                    if parts[0] == username or parts[0] == str(uid):
                        sub_start = int(parts[1])
                        sub_count = int(parts[2])
                        logger.debug(
                            "Full uid mapping available: %s:%d:%d",
                            username, sub_start, sub_count,
                        )
                        cls._cached_subuid_range = (uid, sub_start, sub_count)
                        cls._subuid_detected = True
                        return cls._cached_subuid_range
        except FileNotFoundError:
            pass

        logger.debug(
            "No /etc/subuid entry for %s. For full uid mapping, run:\n"
            "  echo '%s:%d:65536' | sudo tee -a /etc/subuid\n"
            "  echo '%s:%d:65536' | sudo tee -a /etc/subgid\n"
            "Falling back to root-only mapping.",
            username, username, 200000, username, 200000,
        )
        cls._subuid_detected = True
        return None
