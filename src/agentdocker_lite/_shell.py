"""Persistent shell process for agentdocker-lite sandboxes.

Spawns a shell inside a Linux namespace sandbox via Rust _core.spawn_sandbox(),
then communicates with it via stdin/stdout pipes and a signal fd protocol.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import shlex
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class _PersistentShell:
    """Persistent shell inside a Linux namespace with chroot/pivot_root.

    Instead of ``fork -> exec chroot -> exec bash -> exec cmd`` per command
    (~330 ms each), this keeps a single long-lived bash process.  Commands
    are piped through stdin and output is collected via a separate signaling
    fd to avoid sentinel collision with command output.
    """

    def __init__(
        self,
        rootfs,
        shell: str,
        env: dict[str, str],
        working_dir: str = "/",
        cgroup_path=None,
        tty: bool = False,
        net_isolate: bool = False,
        net_ns: Optional[str] = None,
        seccomp: bool = True,
        hostname: Optional[str] = None,
        read_only: bool = False,
        entrypoint: Optional[list[str]] = None,
        subuid_range: Optional[tuple[int, int, int]] = None,
        shared_userns: Optional[str] = None,
        ulimits: Optional[dict[str, tuple[int, int]]] = None,
        # Rootful mode (pivot_root) vs rootless (chroot)
        rootful: bool = False,
        # Overlay config (rootless)
        lowerdir_spec: Optional[str] = None,
        upper_dir: Optional[str] = None,
        work_dir: Optional[str] = None,
        # Volumes, devices, tmpfs
        volumes: Optional[list[str]] = None,
        devices: Optional[list[str]] = None,
        shm_size: Optional[int] = None,
        tmpfs_mounts: Optional[list[str]] = None,
        # Security
        cap_add: Optional[list[int]] = None,
        landlock_read_paths: Optional[list[str]] = None,
        landlock_write_paths: Optional[list[str]] = None,
        landlock_ports: Optional[list[int]] = None,
        landlock_strict: bool = False,
        # Port mapping
        port_map: Optional[list[str]] = None,
        pasta_bin: Optional[str] = None,
        ipv6: bool = False,
        # Internal
        env_dir: Optional[str] = None,
    ):
        self._rootfs = rootfs
        self._shell = shell
        self._env = env
        self._working_dir = working_dir
        self._cgroup_path = cgroup_path
        self._tty = tty
        self._net_isolate = net_isolate
        self._net_ns = net_ns
        self._seccomp = seccomp
        self._hostname = hostname
        self._read_only = read_only
        self._entrypoint = entrypoint or []
        self._subuid_range = subuid_range
        self._shared_userns = shared_userns
        self._ulimits = ulimits or {}
        self._rootful = rootful
        self._lowerdir_spec = lowerdir_spec
        self._upper_dir = upper_dir
        self._work_dir = work_dir
        self._volumes = volumes or []
        self._devices = devices or []
        self._shm_size = shm_size
        self._tmpfs_mounts = tmpfs_mounts or []
        self._cap_add = cap_add or []
        self._landlock_read_paths = landlock_read_paths or []
        self._landlock_write_paths = landlock_write_paths or []
        self._landlock_ports = landlock_ports or []
        self._landlock_strict = landlock_strict
        self._port_map = port_map or []
        self._pasta_bin = pasta_bin
        self._ipv6 = ipv6
        self._env_dir = env_dir

        # Process state (set by start())
        self.pid: Optional[int] = None
        self._pidfd: Optional[int] = None
        self._stdin_fd: Optional[int] = None
        self._stdout_fd: Optional[int] = None
        self._signal_r: Optional[int] = None
        self._signal_fd: Optional[int] = None  # signal_w fd num inside child
        self._master_fd: Optional[int] = None
        self._lock = threading.Lock()

        self.start()

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Start (or restart) the persistent shell inside a new namespace."""
        if self.pid is not None and self.alive:
            self.kill()

        from agentdocker_lite._core import py_spawn_sandbox

        config = {
            "rootfs": str(self._rootfs),
            "shell": self._shell,
            "working_dir": self._working_dir,
            "env": self._env,
            "rootful": self._rootful,
            "userns": self._subuid_range is not None
            or self._shared_userns is not None
            or (not self._rootful),
            "net_isolate": self._net_isolate,
            "net_ns": self._net_ns,
            "shared_userns": self._shared_userns,
            "map_root_user": self._subuid_range is None
            and self._shared_userns is None
            and not self._rootful,
            "subuid_range": self._subuid_range,
            "seccomp": self._seccomp,
            "cap_add": self._cap_add,
            "hostname": self._hostname,
            "read_only": self._read_only,
            "entrypoint": self._entrypoint,
            "tty": self._tty,
            "lowerdir_spec": self._lowerdir_spec,
            "upper_dir": self._upper_dir,
            "work_dir": self._work_dir,
            "volumes": self._volumes,
            "devices": self._devices,
            "shm_size": self._shm_size,
            "tmpfs_mounts": self._tmpfs_mounts,
            "landlock_read_paths": self._landlock_read_paths,
            "landlock_write_paths": self._landlock_write_paths,
            "landlock_ports": self._landlock_ports,
            "landlock_strict": self._landlock_strict,
            "cgroup_path": str(self._cgroup_path) if self._cgroup_path else None,
            "port_map": self._port_map,
            "pasta_bin": self._pasta_bin,
            "ipv6": self._ipv6,
            "env_dir": self._env_dir,
        }

        result = py_spawn_sandbox(config)

        self.pid = result["pid"]
        self._stdin_fd = result["stdin_fd"]
        self._stdout_fd = result["stdout_fd"]
        self._signal_r = result["signal_r_fd"]
        self._signal_fd = result["signal_w_fd_num"]
        self._master_fd = result.get("master_fd")
        self._pidfd = result.get("pidfd")

        if self._tty and self._master_fd is not None:
            # In TTY mode, stdin and stdout go through master_fd
            self._stdin_fd = self._master_fd
            self._stdout_fd = self._master_fd

        # Build ulimit commands
        _ulimit_map = {
            "nofile": "-n", "nproc": "-u", "memlock": "-l",
            "stack": "-s", "core": "-c", "fsize": "-f",
            "data": "-d", "rss": "-m", "as": "-v",
        }
        ulimit_lines = ""
        for name, (soft, hard) in self._ulimits.items():
            flag = _ulimit_map.get(name)
            if flag:
                ulimit_lines += f"ulimit -H {flag} {hard} 2>/dev/null\n"
                ulimit_lines += f"ulimit -S {flag} {soft} 2>/dev/null\n"

        # Send init script to shell (cd + signal ready)
        init_script = (
            "PS1='' PS2=''\n"
            + ulimit_lines
            + f"cd {shlex.quote(self._working_dir)} 2>/dev/null\n"
            f"echo 0 >&{self._signal_fd}\n"
        )
        self._write_input(init_script.encode())

        # Wait for the init signal
        ec_str = self._read_signal(timeout=30)
        if ec_str is None:
            # Diagnose: is the child dead or just unresponsive?
            detail = ""
            if self.pid is not None:
                try:
                    wpid, status = os.waitpid(self.pid, os.WNOHANG)
                    if wpid != 0:
                        code = os.waitstatus_to_exitcode(status)
                        detail += f" child exited with code {code}."
                        self.pid = None
                except ChildProcessError:
                    detail += " child already reaped."
                    self.pid = None

            # Try to read any output the child produced (errors, etc.)
            if self._stdout_fd is not None:
                try:
                    import select as _sel
                    ep = _sel.epoll()
                    ep.register(self._stdout_fd, _sel.EPOLLIN)
                    events = ep.poll(0.1)
                    if events:
                        data = os.read(self._stdout_fd, 8192)
                        if data:
                            detail += f" output: {data.decode('utf-8', errors='replace').strip()!r}"
                    ep.close()
                except OSError:
                    pass

            raise RuntimeError(
                f"Persistent shell failed to start "
                f"(rootfs={self._rootfs}, shell={self._shell}).{detail}"
            )

        ns_flags = "user,pid,mount" if not self._rootful else "pid,mount"
        if self._net_isolate:
            ns_flags += ",net"
        logger.debug(
            "Persistent shell started: pid=%d rootfs=%s ns=[%s] tty=%s",
            self.pid, self._rootfs, ns_flags, self._tty,
        )

    def kill(self) -> None:
        """Kill the shell and all processes in its PID namespace."""
        if self.pid is not None:
            try:
                import signal
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(self.pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
            self.pid = None

        for fd_name in ("_master_fd", "_pidfd", "_signal_r", "_stdin_fd", "_stdout_fd"):
            fd = getattr(self, fd_name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, None)

    @property
    def alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    # -- command execution ------------------------------------------------- #

    def execute(self, command: str, timeout: Optional[int] = None) -> tuple[str, int]:
        """Execute *command* and return ``(output, exit_code)``."""
        with self._lock:
            if not self.alive:
                logger.warning("Persistent shell died, restarting")
                self.start()

            script = (
                f"cd {shlex.quote(self._working_dir)} 2>/dev/null\n"
                f"bash -c {shlex.quote(command)} </dev/null 2>&1\n"
                f"echo $? >&{self._signal_fd}\n"
            )

            if not self._write_input(script.encode()):
                return "Shell pipe broken", -1

            output, exit_code = self._read_until_signal(timeout=timeout)

            if output is None:
                if exit_code == -2:
                    self.kill()
                    self.start()
                    return (
                        f"Command timed out after {timeout} seconds",
                        124,
                    )
                return "Shell terminated unexpectedly", -1

            return output, exit_code

    def write_stdin(self, data: str | bytes) -> None:
        """Write raw data to the shell's stdin (TTY mode only)."""
        if not self._tty:
            raise RuntimeError("write_stdin() requires tty=True")
        if isinstance(data, str):
            data = data.encode()
        with self._lock:
            self._write_input(data)

    # -- internal I/O ------------------------------------------------------ #

    def _write_input(self, data: bytes) -> bool:
        """Write to the shell's stdin."""
        try:
            fd = self._stdin_fd
            if fd is None:
                return False
            os.write(fd, data)
        except (BrokenPipeError, OSError):
            return False
        return True

    @property
    def _stdout_read_fd(self) -> int:
        """File descriptor to read command output from."""
        return self._stdout_fd

    def _read_signal(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read a single line from the signal fd."""
        if self._signal_r is None:
            return None
        ep = select.epoll()
        ep.register(self._signal_r, select.EPOLLIN)
        try:
            events = ep.poll(timeout if timeout is not None else -1, maxevents=1)
            if not events:
                return None
            data = os.read(self._signal_r, 256)
            if not data:
                return None
            return data.decode("utf-8", errors="replace").strip()
        except OSError:
            return None
        finally:
            ep.close()

    def _read_until_signal(
        self, timeout: Optional[float] = None
    ) -> tuple[Optional[str], int]:
        """Read stdout until the signal fd fires with the exit code."""
        deadline = time.monotonic() + timeout if timeout else None
        stdout_fd = self._stdout_read_fd
        signal_fd = self._signal_r
        buf = b""
        parts: list[str] = []
        exit_code: Optional[int] = None

        ep = select.epoll()
        ep.register(stdout_fd, select.EPOLLIN)
        if signal_fd is not None:
            ep.register(signal_fd, select.EPOLLIN)

        try:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None, -2
                    wait = min(remaining, 2.0)
                else:
                    wait = 5.0

                events = ep.poll(wait)
                ready_fds = {fd for fd, _ in events}

                # No events and process died → shell is gone.
                if not events and not self.alive:
                    if buf:
                        parts.append(buf.decode("utf-8", errors="backslashreplace"))
                    return None, -1

                # Read stdout data if available.
                if stdout_fd in ready_fds:
                    try:
                        chunk = os.read(stdout_fd, 65536)
                    except OSError as e:
                        if e.errno == errno.EIO:
                            break
                        return None, -1
                    if not chunk:
                        return None, -1
                    buf += chunk

                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        line_str = line_bytes.decode("utf-8", errors="backslashreplace")
                        parts.append(line_str + "\n")

                # Check signal fd for exit code.
                if signal_fd is not None and signal_fd in ready_fds:
                    try:
                        sig_data = os.read(signal_fd, 256)
                    except OSError:
                        return None, -1
                    if sig_data:
                        try:
                            exit_code = int(
                                sig_data.decode("utf-8", errors="replace").strip()
                            )
                        except ValueError:
                            exit_code = -1

                # If we got the exit code, drain any remaining stdout.
                if exit_code is not None:
                    while True:
                        if not ep.poll(0.01):
                            break
                        try:
                            chunk = os.read(stdout_fd, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        buf += chunk

                    if buf:
                        parts.append(buf.decode("utf-8", errors="backslashreplace"))

                    return "".join(parts), exit_code
        finally:
            ep.close()

        # Reached via break (e.g. PTY EIO)
        if buf:
            parts.append(buf.decode("utf-8", errors="backslashreplace"))
        return "".join(parts) if parts else None, exit_code if exit_code is not None else -1
