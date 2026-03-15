"""Base classes for agentdocker-lite sandboxes."""

from __future__ import annotations

import abc
import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agentdocker_lite._shell import _PersistentShell

logger = logging.getLogger(__name__)


# ====================================================================== #
#  Configuration                                                          #
# ====================================================================== #


@dataclass
class SandboxConfig:
    """Configuration for a sandbox instance.

    Args:
        image: Path to a rootfs directory, or a Docker image name
            (e.g. ``"ubuntu:22.04"``).  If a Docker image name is given,
            it will be auto-exported to a rootfs directory on first use.
        working_dir: Initial working directory inside the sandbox.
        environment: Extra environment variables for commands.
        volumes: Volume mount specs as ``["host:container:mode", ...]``.
        fs_backend: Filesystem backend: ``"overlayfs"`` (default) or ``"btrfs"``.
        env_base_dir: Base directory for per-sandbox state.
        rootfs_cache_dir: Directory to cache auto-prepared rootfs images.
        cpu_max: cgroup v2 ``cpu.max`` value (e.g. ``"50000 100000"``).
        memory_max: cgroup v2 ``memory.max`` value in bytes.
        pids_max: cgroup v2 ``pids.max`` value.
        tty: Use a pseudo-terminal instead of pipes for command I/O.
            Enables ``write_stdin()`` for interactive programs.  Default
            ``False`` preserves the fast pipe-based path.
        net_isolate: Create a separate network namespace (loopback only).
            Default ``False`` inherits the host network.
        devices: Host device paths to bind-mount into the sandbox
            (e.g. ``["/dev/kvm"]``).
        seccomp: Enable seccomp-bpf filter to block dangerous syscalls
            (ptrace, mount, kexec, bpf, etc.). Default ``True``.
        landlock_read: Paths allowed for read-only access under Landlock.
            If set, all other paths are denied. ``None`` disables Landlock.
        landlock_write: Paths allowed for read-write access under Landlock.
        landlock_tcp_ports: TCP ports allowed for connect under Landlock.
            ``None`` means no network restriction.
    """

    image: str = ""
    working_dir: str = "/"
    environment: dict[str, str] = field(default_factory=dict)
    volumes: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    fs_backend: str = "overlayfs"
    env_base_dir: str = "/tmp/agentdocker_lite"
    rootfs_cache_dir: str = "/tmp/agentdocker_lite_rootfs_cache"
    cpu_max: Optional[str] = None
    memory_max: Optional[str] = None
    pids_max: Optional[str] = None
    tty: bool = False
    net_isolate: bool = False
    seccomp: bool = True
    landlock_read: Optional[list[str]] = None
    landlock_write: Optional[list[str]] = None
    landlock_tcp_ports: Optional[list[int]] = None


# ====================================================================== #
#  Abstract base                                                          #
# ====================================================================== #


class SandboxBase(abc.ABC):
    """Abstract base class for sandbox implementations.

    Concrete shared methods (run, read_file, write_file, etc.) delegate
    to ``self._persistent_shell``.  Subclasses must implement
    ``reset()`` and ``delete()`` as well as their own ``__init__``.
    """

    # -- attributes set by subclass __init__ ------------------------------- #
    _config: SandboxConfig
    _name: str
    _rootfs: Path
    _env_dir: Path
    _shell: str
    _cached_env: dict[str, str]
    _persistent_shell: _PersistentShell
    _bg_handles: dict[str, str]
    _rootless: bool

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def run(
        self, command: str | list[str], timeout: Optional[int] = None
    ) -> tuple[str, int]:
        """Run a command inside the sandbox.

        Args:
            command: Shell command string or list of arguments.
            timeout: Timeout in seconds (None = no timeout).

        Returns:
            ``(stdout_output, exit_code)`` tuple.
        """
        t0 = time.monotonic()
        if isinstance(command, list):
            cmd_str = shlex.join(command)
        else:
            cmd_str = command

        output, exit_code = self._persistent_shell.execute(cmd_str, timeout=timeout)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug("cmd (%.1fms exit=%d): %.200s", elapsed_ms, exit_code, cmd_str)
        return output, exit_code

    def write_stdin(self, data: str | bytes) -> None:
        """Write raw data to the sandbox shell's stdin (PTY mode only).

        Use this to send input to interactive programs.  Requires
        ``SandboxConfig(tty=True)``.

        Example::

            sb.run("cat")         # blocks waiting for stdin
            sb.write_stdin("hello\\n")
        """
        self._persistent_shell.write_stdin(data)

    # -- background processes ---------------------------------------------- #

    def run_background(self, command: str | list[str]) -> str:
        """Start a command in the background inside the sandbox.

        Returns a handle string to use with :meth:`check_background` and
        :meth:`stop_background`.  The command runs asynchronously; the
        persistent shell remains available for ``run()`` calls.

        Example::

            handle = sb.run_background("python -m http.server 8080")
            time.sleep(1)
            output, running = sb.check_background(handle)
        """
        if isinstance(command, list):
            command = shlex.join(command)
        handle = uuid.uuid4().hex[:8]
        out_file = f"/tmp/.bg_{handle}.out"
        pid_file = f"/tmp/.bg_{handle}.pid"
        self.run(
            f"nohup bash -c {shlex.quote(command)} > {out_file} 2>&1 & echo $! > {pid_file}"
        )
        pid_str, _ = self.run(f"cat {pid_file} 2>/dev/null")
        self._bg_handles[handle] = pid_str.strip()
        return handle

    def check_background(self, handle: str) -> tuple[str, bool]:
        """Check a background process started with :meth:`run_background`.

        Returns ``(output_so_far, is_running)`` tuple.
        """
        out_file = f"/tmp/.bg_{handle}.out"
        pid = self._bg_handles.get(handle, "")
        output, _ = self.run(f"cat {out_file} 2>/dev/null")
        if pid:
            _, ec = self.run(f"kill -0 {pid} 2>/dev/null")
            running = ec == 0
        else:
            running = False
        return output, running

    def stop_background(self, handle: str) -> str:
        """Stop a background process and return its final output."""
        out_file = f"/tmp/.bg_{handle}.out"
        pid_file = f"/tmp/.bg_{handle}.pid"
        pid = self._bg_handles.pop(handle, "")
        if pid:
            self.run(f"kill {pid} 2>/dev/null; kill -9 {pid} 2>/dev/null")
        output, _ = self.run(f"cat {out_file} 2>/dev/null")
        self.run(f"rm -f {out_file} {pid_file}")
        return output

    # -- interactive processes --------------------------------------------- #

    def popen(
        self,
        command: str | list[str],
        **kwargs,
    ) -> subprocess.Popen:
        """Start an interactive process inside the sandbox with stdio pipes.

        Unlike :meth:`run` (which aggregates output) and :meth:`run_background`
        (which redirects to a file), this returns a :class:`subprocess.Popen`
        object with direct ``stdin``/``stdout``/``stderr`` pipes for
        bidirectional communication.

        Useful for long-running interactive processes like LSP servers, REPLs,
        or any protocol that requires streaming stdin/stdout (e.g. JSON-RPC).

        The process runs inside the sandbox's namespace (PID + mount isolation)
        and chroot, sharing the same filesystem view as :meth:`run`.

        Args:
            command: Command string or argument list to execute.
            **kwargs: Additional keyword arguments passed to
                :class:`subprocess.Popen` (e.g. ``stderr=subprocess.PIPE``).
                ``stdin``, ``stdout``, and ``env`` are set automatically
                unless explicitly overridden.

        Returns:
            :class:`subprocess.Popen` with ``stdin`` and ``stdout`` pipes.

        Example::

            # Start an LSP server inside the sandbox
            proc = sb.popen(["pyright-langserver", "--stdio"])
            proc.stdin.write(b'...')  # send JSON-RPC request
            proc.stdin.flush()
            response = proc.stdout.readline()  # read response
            proc.terminate()
        """
        if isinstance(command, list):
            cmd_args = command
        else:
            cmd_args = ["bash", "-c", command]

        if self._rootless:
            full_cmd = cmd_args
            popen_cwd = str(self._rootfs)
        else:
            shell_pid = self._persistent_shell._process.pid
            full_cmd = [
                "nsenter",
                f"--target={shell_pid}",
                "--pid",
                "--mount",
                "--",
                "chroot", str(self._rootfs),
            ] + cmd_args
            popen_cwd = None

        defaults = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self._cached_env,
        }
        if popen_cwd:
            defaults["cwd"] = popen_cwd
        defaults.update(kwargs)

        proc = subprocess.Popen(full_cmd, **defaults)
        logger.debug(
            "popen pid=%d in sandbox (rootless=%s): %s",
            proc.pid, self._rootless, cmd_args,
        )
        return proc

    # -- file operations --------------------------------------------------- #

    def copy_to(self, local_path: str, container_path: str) -> None:
        """Copy a file from host into the sandbox."""
        host_dst = self._host_path(container_path)
        host_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, str(host_dst))

    def copy_from(self, container_path: str, local_path: str) -> None:
        """Copy a file from the sandbox to host."""
        host_src = self._host_path(container_path)
        if not host_src.exists():
            raise FileNotFoundError(
                f"File {container_path} does not exist in the sandbox."
            )
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        shutil.copy2(str(host_src), local_path)

    def read_file(self, container_path: str) -> str:
        """Read file content from the sandbox."""
        host_path = self._host_path(container_path)
        if not host_path.exists():
            raise FileNotFoundError(
                f"File {container_path} does not exist in the sandbox."
            )
        return host_path.read_text(encoding="latin-1")

    def write_file(self, container_path: str, content: str | bytes) -> None:
        """Write content to a file inside the sandbox."""
        host_path = self._host_path(container_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            host_path.write_bytes(content)
        else:
            host_path.write_text(content)

    @property
    def rootfs(self) -> Path:
        """Path to the sandbox's rootfs on the host."""
        return self._rootfs

    # -- abstract methods -------------------------------------------------- #

    @abc.abstractmethod
    def reset(self) -> None:
        """Reset the sandbox filesystem to its initial state."""
        ...

    @abc.abstractmethod
    def delete(self) -> None:
        """Delete the sandbox and clean up all resources."""
        ...

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _host_path(self, container_path: str) -> Path:
        return self._rootfs / container_path.lstrip("/")

    def _build_env(self) -> dict[str, str]:
        env = {
            "HOME": "/root",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
        }
        if self._config.tty:
            env["TERM"] = "dumb"
            env["NO_COLOR"] = "1"
        env.update(self._config.environment)
        return env

    def __del__(self):
        try:
            if hasattr(self, "_persistent_shell"):
                self._persistent_shell.kill()
            if not getattr(self, "_rootless", False):
                self._unmount_all()
        except Exception:
            pass

    def _unmount_all(self):
        """Default no-op; overridden in NamespaceSandbox."""
        pass

    def __repr__(self) -> str:
        return f"Sandbox(name={self._name!r}, fs={self._fs_backend}, rootfs={self._rootfs})"
