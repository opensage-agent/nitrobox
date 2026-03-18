"""CRIU-based process checkpoint/restore for agentdocker-lite sandboxes.

Provides full process-state snapshots (memory, registers, file descriptors)
on top of the existing overlayfs filesystem snapshots.  Zero runtime overhead —
CRIU only runs during save/restore operations.

Requirements:
    - CRIU >= 4.0 (``apt install criu`` or ``pacman -S criu``)
    - Root or ``CAP_CHECKPOINT_RESTORE`` + ``CAP_SYS_PTRACE``
    - Kernel 5.9+ (for CAP_CHECKPOINT_RESTORE)

Usage:
    >>> from agentdocker_lite import Sandbox, SandboxConfig
    >>> from agentdocker_lite.checkpoint import CheckpointManager
    >>>
    >>> sb = Sandbox(SandboxConfig(image="ubuntu:22.04", working_dir="/workspace"))
    >>> mgr = CheckpointManager(sb)
    >>>
    >>> sb.run("export FOO=bar && cd /tmp")
    >>> mgr.save("/tmp/ckpt_v1")        # saves filesystem + process state
    >>> sb.run("rm -rf /workspace/*")   # destructive action
    >>> mgr.restore("/tmp/ckpt_v1")     # exact rollback: env vars, cwd, everything
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agentdocker_lite.backends.base import SandboxBase

logger = logging.getLogger(__name__)

# Subdirectory names inside a checkpoint directory.
_FS_DIR = "fs"          # overlayfs upper layer
_CRIU_DIR = "criu"      # CRIU image files
_META_FILE = "meta.json" # pipe inodes, signal fd number, etc.


def _find_criu() -> str:
    """Find the criu binary, raising FileNotFoundError if missing."""
    for candidate in ("criu",):
        path = shutil.which(candidate)
        if path:
            return path
    raise FileNotFoundError(
        "criu not found. Install it:\n"
        "  Arch:   pacman -S criu\n"
        "  Ubuntu: apt install criu\n"
        "  Fedora: dnf install criu"
    )


def _get_pipe_inodes(pid: int) -> dict[int, int]:
    """Read /proc/<pid>/fdinfo to map fd numbers to pipe inodes.

    Returns {fd_number: inode} for all pipe fds.
    """
    fd_dir = Path(f"/proc/{pid}/fd")
    result: dict[int, int] = {}
    try:
        for entry in fd_dir.iterdir():
            try:
                link = os.readlink(str(entry))
                if link.startswith("pipe:["):
                    inode = int(link[6:-1])
                    result[int(entry.name)] = inode
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return result


def _find_shell_pid(sandbox: SandboxBase) -> int:
    """Get the PID of the sandbox's persistent shell process."""
    shell = sandbox._persistent_shell
    if shell._process is None or shell._process.poll() is not None:
        raise RuntimeError("Sandbox shell is not running")
    return shell._process.pid


def _find_init_pid(shell_pid: int) -> int:
    """Find the actual init process (PID 1 in the child namespace).

    The shell_pid is the 'unshare' process. Its first child is the
    actual bash process inside the namespace.
    """
    children_file = Path(f"/proc/{shell_pid}/task/{shell_pid}/children")
    try:
        children = children_file.read_text().strip().split()
        if children:
            return int(children[0])
    except (OSError, ValueError):
        pass
    # Fallback: use the unshare process itself.
    return shell_pid


class CheckpointManager:
    """Manages CRIU checkpoint/restore for a sandbox instance.

    This is a standalone class (not a mixin) to keep the sandbox API clean.
    Wraps the sandbox's existing filesystem snapshot with CRIU process state.

    Args:
        sandbox: A running Sandbox instance (must be rootful mode).
        criu_binary: Path to criu binary. Auto-detected if None.
    """

    def __init__(
        self,
        sandbox: SandboxBase,
        criu_binary: Optional[str] = None,
    ):
        self._sandbox = sandbox
        self._criu = criu_binary or _find_criu()

        if os.geteuid() != 0:
            raise PermissionError(
                "CheckpointManager requires root "
                "(CRIU needs CAP_CHECKPOINT_RESTORE + CAP_SYS_PTRACE)"
            )

    def save(
        self,
        path: str,
        *,
        leave_running: bool = True,
        track_mem: bool = True,
    ) -> None:
        """Checkpoint the sandbox: filesystem + full process state.

        After save(), the sandbox continues running (default) and can be
        used normally.  Call restore() later to roll back to this point.

        Args:
            path: Directory to save checkpoint to (must not exist).
            leave_running: Keep sandbox alive after checkpoint.
                If False, the process tree is killed after dump.
            track_mem: Enable memory change tracking for faster
                incremental checkpoints via PAGEMAP_SCAN.
        """
        ckpt_dir = Path(path)
        if ckpt_dir.exists():
            raise FileExistsError(f"Checkpoint directory already exists: {path}")
        ckpt_dir.mkdir(parents=True)

        fs_dir = ckpt_dir / _FS_DIR
        criu_dir = ckpt_dir / _CRIU_DIR
        criu_dir.mkdir()

        # 1. Save filesystem state (overlayfs upper layer).
        self._sandbox.snapshot(str(fs_dir))
        logger.debug("Checkpoint: filesystem saved to %s", fs_dir)

        # 2. Identify target process and its external pipe fds.
        shell_pid = _find_shell_pid(self._sandbox)
        init_pid = _find_init_pid(shell_pid)
        pipe_inodes = _get_pipe_inodes(shell_pid)

        # The shell's signal_r is in the parent (our process), not the child.
        # We need to record signal_fd number so we can reconnect on restore.
        shell = self._sandbox._persistent_shell
        signal_fd = shell._signal_fd  # fd number used inside the shell

        # 3. Save metadata for restore.
        meta = {
            "shell_pid": shell_pid,
            "init_pid": init_pid,
            "signal_fd": signal_fd,
            "pipe_inodes": {str(k): v for k, v in pipe_inodes.items()},
            "tty": shell._tty,
            "working_dir": shell._working_dir,
        }
        (ckpt_dir / _META_FILE).write_text(json.dumps(meta, indent=2))

        # 4. Build CRIU dump command.
        cmd = [
            self._criu, "dump",
            "-t", str(shell_pid),
            "-D", str(criu_dir),
            "-o", "dump.log",
            "-v4",
            "--shell-job",       # handle external terminal/pipes
            "--tcp-established",  # save TCP connections (if any)
        ]

        if leave_running:
            cmd.append("--leave-running")
        if track_mem:
            cmd.append("--track-mem")

        # Mark external pipes so CRIU doesn't try to save the other end.
        for _, inode in pipe_inodes.items():
            cmd.extend(["--external", f"pipe:[{inode}]"])

        # 5. Run CRIU dump.
        logger.info(
            "CRIU dump: pid=%d dir=%s leave_running=%s",
            shell_pid, criu_dir, leave_running,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
        )

        if result.returncode != 0:
            # Read CRIU's log for diagnostics.
            log_file = criu_dir / "dump.log"
            criu_log = ""
            if log_file.exists():
                criu_log = log_file.read_text()[-2000:]  # last 2KB
            stderr = result.stderr.decode(errors="replace")
            raise RuntimeError(
                f"CRIU dump failed (exit {result.returncode}):\n"
                f"stderr: {stderr}\n"
                f"log (tail): {criu_log}"
            )

        logger.info("Checkpoint saved: %s", path)

    def restore(self, path: str) -> None:
        """Restore sandbox to a previously saved checkpoint.

        Kills the current process tree, restores filesystem state,
        then uses CRIU to restore the exact process state (memory,
        registers, env vars, open files, cwd).

        After restore, the sandbox is fully operational — run() works
        with the exact state from the checkpoint.

        Args:
            path: Directory containing a previous save().
        """
        ckpt_dir = Path(path)
        if not ckpt_dir.exists():
            raise FileNotFoundError(f"Checkpoint not found: {path}")

        fs_dir = ckpt_dir / _FS_DIR
        criu_dir = ckpt_dir / _CRIU_DIR
        meta_file = ckpt_dir / _META_FILE

        if not criu_dir.exists() or not meta_file.exists():
            raise FileNotFoundError(
                f"Invalid checkpoint (missing CRIU images or metadata): {path}"
            )

        meta = json.loads(meta_file.read_text())
        signal_fd_num = meta["signal_fd"]

        shell = self._sandbox._persistent_shell

        # 1. Kill current shell process tree.
        shell.kill()
        logger.debug("Checkpoint restore: killed current shell")

        # 2. Restore filesystem state.
        upper = getattr(self._sandbox, "_upper_dir", None)
        if upper:
            if upper.exists():
                shutil.rmtree(upper)
            shutil.copytree(str(fs_dir), str(upper))

            # Clear overlayfs work dir (kernel metadata).
            work = getattr(self._sandbox, "_work_dir", None)
            if work and work.exists():
                shutil.rmtree(work)
                work.mkdir(parents=True)

            logger.debug("Checkpoint restore: filesystem restored from %s", fs_dir)

        # 3. Create new pipes and run CRIU restore.
        use_tty = bool(meta.get("tty"))
        pipe_inodes = meta.get("pipe_inodes", {})

        if use_tty:
            self._restore_tty(shell, criu_dir, signal_fd_num, pipe_inodes)
        else:
            self._restore_pipe(shell, criu_dir, signal_fd_num, pipe_inodes)

        # 4. Clear background handles (they're lost on restore).
        if hasattr(self._sandbox, "_bg_handles"):
            self._sandbox._bg_handles.clear()

        logger.info("Checkpoint restored: %s (pid=%s)", path, shell._process.pid)

    def _run_criu_restore(
        self,
        criu_dir: Path,
        cmd: list[str],
        pass_fds: list[int],
        cleanup_fds: list[int],
    ) -> None:
        """Run CRIU restore and raise on failure."""
        for fd in pass_fds:
            os.set_inheritable(fd, True)

        logger.info("CRIU restore: dir=%s", criu_dir)
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
            close_fds=False,
        )

        if result.returncode != 0:
            log_file = criu_dir / "restore.log"
            criu_log = ""
            if log_file.exists():
                criu_log = log_file.read_text()[-2000:]
            stderr = result.stderr.decode(errors="replace")
            for fd in cleanup_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise RuntimeError(
                f"CRIU restore failed (exit {result.returncode}):\n"
                f"stderr: {stderr}\n"
                f"log (tail): {criu_log}"
            )

    def _restore_pipe(
        self,
        shell: object,
        criu_dir: Path,
        signal_fd_num: int,
        pipe_inodes: dict[str, int],
    ) -> None:
        """Restore in pipe mode (non-TTY)."""
        from agentdocker_lite._shell import _PersistentShell
        assert isinstance(shell, _PersistentShell)

        signal_r, signal_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()

        cmd = [
            self._criu, "restore",
            "-D", str(criu_dir),
            "-o", "restore.log",
            "-v4",
            "--shell-job",
            "--tcp-established",
            "--restore-detached",
        ]

        # Reconnect signal pipe.
        cmd.extend(["--inherit-fd", f"fd[{signal_w}]:pipe:[{signal_fd_num}]"])

        # Reconnect stdin/stdout pipes.
        for old_fd_str, inode in pipe_inodes.items():
            old_fd = int(old_fd_str)
            if old_fd == 0:
                cmd.extend(["--inherit-fd", f"fd[{stdin_r}]:pipe:[{inode}]"])
            elif old_fd in (1, 2):
                cmd.extend(["--inherit-fd", f"fd[{stdout_w}]:pipe:[{inode}]"])

        all_fds = [signal_r, signal_w, stdin_r, stdin_w, stdout_r, stdout_w]
        self._run_criu_restore(
            criu_dir, cmd,
            pass_fds=[signal_w, stdin_r, stdout_w],
            cleanup_fds=all_fds,
        )

        # Close fds passed to the restored process.
        os.close(signal_w)
        os.close(stdin_r)
        os.close(stdout_w)

        # Reconnect shell object.
        shell._signal_r = signal_r
        shell._signal_fd = signal_fd_num
        shell._master_fd = None

        restored_pid = self._find_restored_pid(criu_dir)
        shell._process = _RestoredProcess(  # type: ignore[assignment]
            pid=restored_pid,
            stdin_fd=stdin_w,
            stdout_fd=stdout_r,
        )

    def _restore_tty(
        self,
        shell: object,
        criu_dir: Path,
        signal_fd_num: int,
        pipe_inodes: dict[str, int],
    ) -> None:
        """Restore in PTY mode."""
        import pty as pty_mod
        import termios
        from agentdocker_lite._shell import _PersistentShell
        assert isinstance(shell, _PersistentShell)

        signal_r, signal_w = os.pipe()
        master_fd, slave_fd = pty_mod.openpty()
        attrs = termios.tcgetattr(master_fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(master_fd, termios.TCSANOW, attrs)

        cmd = [
            self._criu, "restore",
            "-D", str(criu_dir),
            "-o", "restore.log",
            "-v4",
            "--shell-job",
            "--tcp-established",
            "--restore-detached",
        ]

        cmd.extend(["--inherit-fd", f"fd[{signal_w}]:pipe:[{signal_fd_num}]"])
        for _, inode in pipe_inodes.items():
            cmd.extend(["--inherit-fd", f"fd[{slave_fd}]:pipe:[{inode}]"])

        all_fds = [signal_r, signal_w, master_fd, slave_fd]
        self._run_criu_restore(
            criu_dir, cmd,
            pass_fds=[signal_w, slave_fd],
            cleanup_fds=all_fds,
        )

        os.close(signal_w)
        os.close(slave_fd)

        shell._signal_r = signal_r
        shell._signal_fd = signal_fd_num
        shell._master_fd = master_fd

        restored_pid = self._find_restored_pid(criu_dir)
        shell._process = _RestoredProcess(  # type: ignore[assignment]
            pid=restored_pid,
            stdin_fd=-1,  # not used in TTY mode
            stdout_fd=-1,
        )

    def _find_restored_pid(self, criu_dir: Path) -> int:
        """Find the PID of the restored root process."""
        # CRIU writes pidfile or we can parse pstree.img.
        # Simplest: scan /proc for our restored process.
        # The crit tool can extract it, but let's use the JSON approach.
        try:
            result = subprocess.run(
                ["crit", "decode", "-i", str(criu_dir / "pstree.img")],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                pstree = json.loads(result.stdout)
                # pstree entries have "pid" field.
                for entry in pstree.get("entries", []):
                    return entry["pid"]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            pass

        raise RuntimeError("Could not determine restored process PID")

    @staticmethod
    def check_available() -> bool:
        """Check if CRIU is installed and the kernel supports it."""
        try:
            criu = _find_criu()
            result = subprocess.run(
                [criu, "check"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False


class _RestoredProcess:
    """Minimal Popen-like wrapper for a CRIU-restored process.

    After CRIU restore, the process is running but we don't have a
    Popen object for it.  This provides the minimal interface that
    _PersistentShell needs: .pid, .poll(), .stdin, .stdout, .kill().
    """

    def __init__(self, pid: int, stdin_fd: int, stdout_fd: int):
        self.pid = pid
        self.stdin = os.fdopen(stdin_fd, "wb", buffering=0)
        self.stdout = os.fdopen(stdout_fd, "rb", buffering=0)

    def poll(self) -> Optional[int]:
        """Check if process is still running. Returns None if alive."""
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == 0:
                return None
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        except ChildProcessError:
            # Not our child — check via /proc.
            if Path(f"/proc/{self.pid}").exists():
                return None
            return -1

    def kill(self) -> None:
        """Kill the process."""
        import signal
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait(self, timeout: Optional[float] = None) -> int:
        """Wait for process to exit."""
        import time
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            ret = self.poll()
            if ret is not None:
                return ret
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(
                    cmd="criu-restored", timeout=timeout or 0,
                )
            time.sleep(0.01)
