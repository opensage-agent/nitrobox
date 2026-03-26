"""Overlay mount helpers — delegates to Rust core."""

from __future__ import annotations

from agentdocker_lite._core import py_check_new_mount_api, py_mount_overlay


def _check_new_mount_api() -> bool:
    """Test if fsopen + lowerdir+ is available (kernel >= 6.8)."""
    return py_check_new_mount_api()


def mount_overlay(
    lowerdir_spec: str,
    upper_dir: str,
    work_dir: str,
    target: str,
) -> None:
    """Mount overlayfs, auto-selecting the best available method."""
    py_mount_overlay(lowerdir_spec, upper_dir, work_dir, target)
