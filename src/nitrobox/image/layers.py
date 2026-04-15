"""Layer management — overlayfs layers via BuildKit.

All image builds and pulls go through the embedded BuildKit server.
Layers are stored in BuildKit's snapshotter and accessed directly
as overlay diff directories.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


# ====================================================================== #
#  Public API                                                              #
# ====================================================================== #


def prepare_rootfs_layers_from_docker(
    image_name: str,
    cache_dir: Path,
    pull: bool = True,
) -> list[Path]:
    """Get image layers as directories for overlayfs stacking.

    Uses BuildKit's embedded server for both build and pull.
    Layer resolution via BuildKit's cache manager API.

    Args:
        image_name: Image reference (e.g. ``"ubuntu:22.04"``).
        cache_dir: Unused (kept for API compatibility).
        pull: If True, pull from registry when image not cached.

    Returns:
        Ordered list of layer directories (bottom to top).
    """
    from nitrobox.image.buildkit import get_buildkit_layers, BuildKitManager

    # 1. Check BuildKit layer cache (from recent builds/pulls)
    bk_layers = get_buildkit_layers(image_name)
    if bk_layers is not None:
        paths = [Path(p) for p in bk_layers]
        logger.info("Layer cache ready for %s: %d layers (buildkit)",
                     image_name, len(paths))
        return paths

    # 2. Pull via BuildKit
    if pull:
        bk = BuildKitManager.get()
        logger.info("Pulling %s via BuildKit", image_name)
        bk.pull(image_name)
        bk_layers = get_buildkit_layers(image_name)
        if bk_layers is not None:
            paths = [Path(p) for p in bk_layers]
            logger.info("Layer cache ready for %s: %d layers (buildkit pull)",
                         image_name, len(paths))
            return paths

    raise RuntimeError(
        f"Failed to pull {image_name!r}. "
        f"Check network connectivity and image name."
    )


# ====================================================================== #
#  Layer locking (for concurrent sandbox safety)                           #
# ====================================================================== #


def acquire_layer_locks(layer_dirs: list[Path]) -> list[int]:
    """Acquire shared (read) locks on layer directories."""
    fds: list[int] = []
    for d in layer_dirs:
        lock_path = d.parent / f".{d.name}.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_SH)
        fds.append(fd)
    return fds
