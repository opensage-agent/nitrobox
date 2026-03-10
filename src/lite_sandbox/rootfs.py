"""Utilities for preparing base rootfs directories from Docker images."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def prepare_rootfs_from_docker(
    image_name: str,
    output_dir: str | Path,
    pull: bool = True,
) -> Path:
    """Export a Docker image as a rootfs directory.

    Equivalent to::

        docker pull <image_name>
        docker export $(docker create <image_name>) | tar -C <output_dir> -xf -

    Args:
        image_name: Docker image (e.g. ``"ubuntu:22.04"``).
        output_dir: Target directory for the extracted rootfs.
        pull: Pull the image first (set ``False`` if already local).

    Returns:
        Path to *output_dir*.

    Raises:
        RuntimeError: If any Docker/tar command fails.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if pull:
        logger.info("Pulling image: %s", image_name)
        result = subprocess.run(
            ["docker", "pull", image_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker pull failed: {result.stderr.strip()}")

    logger.info("Creating temporary container from %s", image_name)
    create = subprocess.run(
        ["docker", "create", image_name],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise RuntimeError(f"docker create failed: {create.stderr.strip()}")
    container_id = create.stdout.strip()

    try:
        logger.info("Exporting %s -> %s", image_name, output_dir)
        export_proc = subprocess.Popen(
            ["docker", "export", container_id],
            stdout=subprocess.PIPE,
        )
        tar_proc = subprocess.Popen(
            ["tar", "-C", str(output_dir), "-xf", "-"],
            stdin=export_proc.stdout,
        )
        if export_proc.stdout is not None:
            export_proc.stdout.close()
        tar_proc.communicate()

        if tar_proc.returncode != 0:
            raise RuntimeError(
                f"tar extraction failed for {image_name} (exit {tar_proc.returncode})"
            )
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)

    subprocess.run(["docker", "rmi", "-f", image_name], capture_output=True)

    logger.info("Rootfs ready: %s", output_dir)
    return output_dir


def prepare_rootfs_without_docker(
    image_ref: str,
    output_dir: str | Path,
) -> Path:
    """Export an OCI image as a rootfs without requiring a Docker daemon.

    Uses ``skopeo`` + ``umoci`` which can run without root and without
    a running Docker daemon.

    Args:
        image_ref: OCI image reference (e.g. ``"docker://ubuntu:22.04"``).
        output_dir: Target directory for the extracted rootfs.

    Returns:
        Path to the rootfs bundle directory.

    Raises:
        RuntimeError: If skopeo/umoci commands fail.
        FileNotFoundError: If skopeo or umoci is not installed.
    """
    import shutil
    import tempfile

    for tool in ("skopeo", "umoci"):
        if shutil.which(tool) is None:
            raise FileNotFoundError(
                f"{tool} not found. Install it: apt-get install {tool}"
            )

    output_dir = Path(output_dir)

    with tempfile.TemporaryDirectory() as tmp:
        oci_dir = Path(tmp) / "oci_image"

        if not image_ref.startswith("docker://"):
            image_ref = f"docker://{image_ref}"

        logger.info("Copying %s via skopeo", image_ref)
        result = subprocess.run(
            ["skopeo", "copy", image_ref, f"oci:{oci_dir}:latest"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"skopeo copy failed: {result.stderr.strip()}")

        logger.info("Unpacking OCI image via umoci -> %s", output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["umoci", "unpack", "--image", f"{oci_dir}:latest", str(output_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"umoci unpack failed: {result.stderr.strip()}")

    rootfs_path = output_dir / "rootfs"
    if rootfs_path.is_dir():
        logger.info("Rootfs ready: %s", rootfs_path)
        return rootfs_path

    logger.info("Rootfs ready: %s", output_dir)
    return output_dir


def prepare_btrfs_rootfs_from_docker(
    image_name: str,
    subvolume_path: str | Path,
    pull: bool = True,
) -> Path:
    """Export a Docker image into a btrfs subvolume for snapshot-based sandboxes.

    The target path must be on a btrfs-formatted filesystem.
    """
    import shutil as _shutil

    if _shutil.which("btrfs") is None:
        raise FileNotFoundError(
            "btrfs-progs not found. Install: apt-get install btrfs-progs"
        )

    subvolume_path = Path(subvolume_path)

    if subvolume_path.exists():
        check = subprocess.run(
            ["btrfs", "subvolume", "show", str(subvolume_path)],
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            logger.info("Deleting existing btrfs subvolume: %s", subvolume_path)
            subprocess.run(
                ["btrfs", "subvolume", "delete", str(subvolume_path)],
                capture_output=True,
            )
        else:
            _shutil.rmtree(subvolume_path)

    subvolume_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["btrfs", "subvolume", "create", str(subvolume_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"btrfs subvolume create failed: {result.stderr.strip()}. "
            f"Ensure {subvolume_path.parent} is on a btrfs filesystem."
        )
    logger.info("Created btrfs subvolume: %s", subvolume_path)

    if pull:
        logger.info("Pulling image: %s", image_name)
        result = subprocess.run(
            ["docker", "pull", image_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker pull failed: {result.stderr.strip()}")

    logger.info("Creating temporary container from %s", image_name)
    create = subprocess.run(
        ["docker", "create", image_name],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise RuntimeError(f"docker create failed: {create.stderr.strip()}")
    container_id = create.stdout.strip()

    try:
        logger.info(
            "Exporting %s -> %s (btrfs subvolume)", image_name, subvolume_path
        )
        export_proc = subprocess.Popen(
            ["docker", "export", container_id],
            stdout=subprocess.PIPE,
        )
        tar_proc = subprocess.Popen(
            ["tar", "-C", str(subvolume_path), "-xf", "-"],
            stdin=export_proc.stdout,
        )
        if export_proc.stdout is not None:
            export_proc.stdout.close()
        tar_proc.communicate()

        if tar_proc.returncode != 0:
            raise RuntimeError(
                f"tar extraction failed for {image_name} (exit {tar_proc.returncode})"
            )
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)

    subprocess.run(["docker", "rmi", "-f", image_name], capture_output=True)

    logger.info("btrfs rootfs ready: %s", subvolume_path)
    return subvolume_path
