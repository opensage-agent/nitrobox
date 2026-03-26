"""pidfd wrappers — delegates to Rust core."""

from __future__ import annotations

from typing import Optional

from agentdocker_lite._core import (
    py_pidfd_is_alive,
    py_pidfd_open,
    py_pidfd_send_signal,
)


def pidfd_open(pid: int) -> Optional[int]:
    """Create a pidfd for *pid*. Returns fd or None on failure."""
    return py_pidfd_open(pid)


def pidfd_send_signal(pidfd: int, sig: int) -> bool:
    """Send *sig* to the process identified by *pidfd*. Returns True on success."""
    return py_pidfd_send_signal(pidfd, sig)


def pidfd_is_alive(pidfd: int) -> bool:
    """Check if the process behind *pidfd* is still alive."""
    return py_pidfd_is_alive(pidfd)
