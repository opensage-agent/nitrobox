"""Kernel-level security hardening — delegates to Rust core."""

from __future__ import annotations

from agentdocker_lite._core import (
    py_apply_landlock,
    py_apply_seccomp_filter,
    py_build_seccomp_bpf,
    py_drop_capabilities,
    py_landlock_abi_version,
)


def drop_capabilities(extra_keep: list[int] | None = None) -> bool:
    """Drop all capabilities except Docker defaults from the bounding set."""
    dropped = py_drop_capabilities(extra_keep)
    return dropped > 0


def build_seccomp_bpf() -> bytes | None:
    """Build seccomp BPF bytecode and return as raw bytes."""
    return py_build_seccomp_bpf()


def apply_seccomp_filter() -> bool:
    """Apply a seccomp-bpf filter that blocks dangerous syscalls."""
    try:
        py_apply_seccomp_filter()
        return True
    except OSError:
        return False


def _landlock_abi_version() -> int:
    """Query the kernel's Landlock ABI version. Returns 0 if unavailable."""
    return py_landlock_abi_version()


def apply_landlock(
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    allowed_tcp_ports: list[int] | None = None,
    strict: bool = False,
) -> bool:
    """Apply Landlock filesystem + network restrictions."""
    return py_apply_landlock(read_paths, write_paths, allowed_tcp_ports, strict)
