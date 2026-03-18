"""pidfd wrappers — race-free process management via file descriptors.

Uses ``os.pidfd_open`` if available (Python 3.9+ with kernel headers),
otherwise falls back to ``syscall(SYS_pidfd_open)`` via ctypes.
Returns ``None`` on unsupported kernels (< 5.3).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
from typing import Optional

# Syscall numbers for pidfd_open / pidfd_send_signal.
_SYS_PIDFD_OPEN = {"x86_64": 434, "aarch64": 434}.get(platform.machine())
_SYS_PIDFD_SEND_SIGNAL = {"x86_64": 424, "aarch64": 424}.get(platform.machine())

_libc_name = ctypes.util.find_library("c")
_libc = ctypes.CDLL(_libc_name, use_errno=True) if _libc_name else None


def pidfd_open(pid: int) -> Optional[int]:
    """Create a pidfd for *pid*. Returns fd or None on failure."""
    # Try Python stdlib first (3.9+ compiled with kernel support).
    if hasattr(os, "pidfd_open"):
        try:
            return os.pidfd_open(pid)
        except OSError:
            return None

    # Fallback: raw syscall.
    if _libc is None or _SYS_PIDFD_OPEN is None:
        return None
    fd = _libc.syscall(_SYS_PIDFD_OPEN, pid, 0)
    if fd < 0:
        return None
    return fd


def pidfd_send_signal(pidfd: int, sig: int) -> bool:
    """Send *sig* to the process identified by *pidfd*. Returns True on success."""
    if hasattr(os, "pidfd_send_signal"):
        try:
            os.pidfd_send_signal(pidfd, sig)
            return True
        except OSError:
            return False

    if _libc is None or _SYS_PIDFD_SEND_SIGNAL is None:
        return False
    ret = _libc.syscall(_SYS_PIDFD_SEND_SIGNAL, pidfd, sig, 0, 0)
    return ret == 0


def pidfd_is_alive(pidfd: int) -> bool:
    """Check if the process behind *pidfd* is still alive."""
    return pidfd_send_signal(pidfd, 0)
