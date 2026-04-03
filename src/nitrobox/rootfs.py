"""Utilities for preparing base rootfs directories from Docker images."""

from __future__ import annotations

import fcntl
import io
import json
import logging
import os
import subprocess
import tarfile
from pathlib import Path
from typing import TypedDict

from nitrobox.docker_api import get_client

logger = logging.getLogger(__name__)


# ====================================================================== #
#  Image config type + parsing helpers                                     #
# ====================================================================== #


class ImageConfig(TypedDict, total=False):
    """OCI/Docker image configuration.

    Used as the canonical format throughout nitrobox for image metadata.
    Produced by :func:`get_image_config`, persisted in the manifest
    cache, and consumed by :func:`_apply_image_defaults`.
    """
    cmd: list[str] | None
    entrypoint: list[str] | None
    env: dict[str, str]
    working_dir: str | None
    exposed_ports: list[int]
    diff_ids: list[str]


def _parse_docker_env(env_list: list[str] | None) -> dict[str, str]:
    """Convert Docker ``Env`` list (``["K=V", ...]``) to dict."""
    result: dict[str, str] = {}
    for entry in env_list or []:
        key, _, value = entry.partition("=")
        result[key] = value
    return result


def _parse_docker_ports(exposed_ports: dict | None) -> list[int]:
    """Convert Docker ``ExposedPorts`` (``{"8080/tcp": {}, ...}``) to ``[8080, ...]``."""
    result: list[int] = []
    for port_proto in exposed_ports or {}:
        try:
            result.append(int(port_proto.split("/")[0]))
        except (ValueError, IndexError):
            pass
    return result


def _docker_inspect_to_config(info: dict) -> ImageConfig:
    """Convert a Docker API ``/images/{id}/json`` response to :class:`ImageConfig`."""
    config = info.get("Config") or {}
    return ImageConfig(
        cmd=config.get("Cmd"),
        entrypoint=config.get("Entrypoint"),
        env=_parse_docker_env(config.get("Env")),
        working_dir=config.get("WorkingDir") or None,
        exposed_ports=_parse_docker_ports(config.get("ExposedPorts")),
        diff_ids=info.get("RootFS", {}).get("Layers", []),
    )


# ====================================================================== #
#  Docker layer-level caching                                              #
# ====================================================================== #


def _safe_cache_key(diff_id: str) -> str:
    """Convert a diff_id like 'sha256:abc...' to a short filesystem-safe key.

    Uses first 16 hex chars of the hash for brevity.  The new mount API
    (``fsconfig``) has a ~256-byte limit per lowerdir parameter, so full
    64-char SHA256 hashes cause mount failures with many layers.
    16 hex chars = 64 bits of collision resistance — sufficient for a
    local per-user cache.
    """
    # "sha256:abcdef..." → "abcdef..."[:16]
    _, _, hexpart = diff_id.partition(":")
    return hexpart[:16] if hexpart else diff_id.replace(":", "_")[:16]


def _detect_whiteout_strategy() -> str:
    """Detect the best whiteout conversion strategy for this environment.

    Returns:
        ``"root"``  — real root: mknod(0,0) + trusted.overlay.* (any kernel)
        ``"xattr"`` — kernel >= 6.7: user.overlay.whiteout xattr, no mknod
        ``"userns"`` — kernel >= 5.11: mknod(0,0) inside unshare --user
        ``"none"``  — unsupported: layer caching unavailable
    """
    if os.geteuid() == 0:
        return "root"

    major, minor = _kernel_version()

    if (major, minor) >= (6, 7):
        return "xattr"
    if (major, minor) >= (5, 11):
        return "userns"
    return "none"


def _kernel_version() -> tuple[int, int]:
    """Return (major, minor) kernel version."""
    release = os.uname().release  # e.g. "6.19.8-1-cachyos"
    parts = release.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


def _convert_whiteouts_in_layer(layer_dir: Path, strategy: str = "") -> None:
    """Convert OCI whiteout files to overlayfs-native whiteouts.

    OCI uses ``.wh.<name>`` sentinel files for deletions.
    The conversion strategy depends on the environment:

    - ``"root"``: mknod(0,0) + trusted.overlay.opaque (standard)
    - ``"xattr"``: user.overlay.whiteout xattr (kernel >= 6.7, no root)
    - ``"userns"``: mknod(0,0) inside unshare --user (kernel >= 5.11)
    """
    if not strategy:
        strategy = _detect_whiteout_strategy()

    if strategy == "userns":
        _convert_whiteouts_in_userns(layer_dir)
        return

    # Use Rust implementation: direct setxattr/mknod syscalls,
    # ~100x faster than spawning setfattr per file.
    from nitrobox._core import py_convert_whiteouts
    py_convert_whiteouts(str(layer_dir), strategy == "xattr")


def _convert_whiteouts_in_userns(layer_dir: Path) -> None:
    """Convert whiteouts by running mknod inside a user namespace.

    Kernel >= 5.11: fake CAP_MKNOD in userns allows creating (0,0) device.
    Uses user.overlay.opaque for opaque dirs (userns can't set trusted.*).
    """
    # Build a small script that does the conversion inside a userns
    script = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "layer_dir = Path(sys.argv[1])\n"
        "for dirpath, _dns, fnames in os.walk(layer_dir):\n"
        "    dp = Path(dirpath)\n"
        "    for fname in fnames:\n"
        "        if not fname.startswith('.wh.'): continue\n"
        "        wh = dp / fname\n"
        "        if fname == '.wh..wh..opq':\n"
        "            wh.unlink()\n"
        "            subprocess.run(['setfattr','-n','user.overlay.opaque','-v','y',str(dp)],capture_output=True)\n"
        "        else:\n"
        "            target = dp / fname[4:]\n"
        "            wh.unlink()\n"
        "            os.mknod(str(target), 0o600|0o020000, os.makedev(0,0))\n"
    )
    result = subprocess.run(
        ["unshare", "--user", "--map-root-user",
         "python3", "-c", script, str(layer_dir)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        logger.warning("userns whiteout conversion failed: %s", result.stderr.strip())




# ====================================================================== #
#  Image metadata (CLI detection, diff-IDs, config)                        #
# ====================================================================== #


def _get_image_diff_ids(image_name: str) -> list[str]:
    """Get layer diff_ids: ImageStore → registry → Docker API.

    Also caches the full image config (WORKDIR, CMD, etc.) in the
    ImageStore so that subsequent ``get_image_config()`` calls never
    need a second registry round-trip.

    Raises ``RuntimeError`` with a descriptive message (including root
    causes from each failed source) if all sources fail.
    """
    # 1. Rust in-memory store (~0ms)
    cached = _image_store_get(image_name)
    if cached and cached.get("diff_ids"):
        return cached["diff_ids"]

    errors: list[tuple[str, Exception]] = []

    # 2. Registry API — single call for both diff_ids and config (~100ms)
    from nitrobox._registry import get_image_metadata_from_registry
    try:
        metadata = get_image_metadata_from_registry(image_name)
        if metadata.get("diff_ids"):
            _image_store_populate(image_name, metadata)
            return metadata["diff_ids"]
    except Exception as exc:
        errors.append(("registry", exc))

    # 3. Docker API — for locally-built images not on any registry
    try:
        info = get_client().image_inspect(image_name)
        config = _docker_inspect_to_config(info)
        diff_ids = config.get("diff_ids")
        if diff_ids:
            _image_store_populate(image_name, config)
            return diff_ids
    except Exception as exc:
        errors.append(("docker", exc))

    detail = "; ".join(f"{src}: {exc}" for src, exc in errors)
    raise RuntimeError(
        f"Cannot get image metadata for {image_name!r} [{detail}]"
    )


def get_image_config(image_name: str) -> dict | None:
    """Extract CMD, ENTRYPOINT, ENV, WORKDIR from a Docker/OCI image.

    Resolution order:
      1. Rust in-memory ImageStore (~0ms)
      2. Disk manifest cache — persisted config from prior rootfs prep
      3. Registry API — single call for diff_ids + config (~100ms)
      4. Docker API — for locally-built images not on any registry

    Returns a dict with keys: ``cmd``, ``entrypoint``, ``env``,
    ``working_dir``, ``exposed_ports``.  Returns ``None`` only when
    no source has the image at all (e.g. typo in image name).
    """
    # 1. Rust in-memory store (~0ms)
    cached = _image_store_get(image_name)
    if cached is not None:
        return cached

    # 2. Disk manifest cache — populated by prepare_rootfs_layers_from_docker
    disk_cfg = _read_config_from_manifest_cache(image_name)
    if disk_cfg is not None:
        _image_store_populate(image_name, disk_cfg)
        return disk_cfg

    # 3. Registry API — single call for diff_ids + config (~100ms)
    from nitrobox._registry import get_image_metadata_from_registry
    try:
        metadata = get_image_metadata_from_registry(image_name)
        _image_store_populate(image_name, metadata)
        return metadata
    except Exception:
        pass

    # 4. Docker API — for locally-built images not on any registry
    try:
        info = get_client().image_inspect(image_name)
        result = _docker_inspect_to_config(info)
        _image_store_populate(image_name, result)
        return result
    except Exception:
        pass

    return None


# -- Disk config cache ------------------------------------------------ #

def _read_config_from_manifest_cache(image_name: str) -> dict | None:
    """Read image config from the on-disk manifest cache.

    The manifest cache lives alongside the rootfs layer cache and is
    populated by ``prepare_rootfs_layers_from_docker``.  Since the
    manifest is written during rootfs extraction (which succeeds even
    when the registry is rate-limited on subsequent calls), this
    provides a reliable local source for WORKDIR/CMD/ENV.
    """
    cache_dir = _default_rootfs_cache_dir()
    if cache_dir is None:
        return None
    manifests_dir = cache_dir / "manifests"
    if not manifests_dir.exists():
        return None

    safe_name = image_name.replace("/", "_").replace(":", "_").replace(".", "_")
    for path in [manifests_dir / f"{safe_name}.json"]:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            cfg = data.get("config")
            if cfg and cfg.get("working_dir") is not None:
                return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _default_rootfs_cache_dir() -> Path | None:
    """Return the default rootfs cache directory."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(cache_home) / "nitrobox" / "rootfs"
    return d if d.exists() else None


# -- ImageStore helpers ------------------------------------------------ #

def _image_store_get(image_name: str) -> ImageConfig | None:
    """Look up image config in Rust in-memory store."""
    try:
        from nitrobox._core import py_image_store_get
        raw = py_image_store_get(image_name)
        if raw is None:
            return None
        data = json.loads(raw)
        return ImageConfig(
            cmd=data.get("cmd"),
            entrypoint=data.get("entrypoint"),
            env=data.get("env", {}),
            working_dir=data.get("working_dir"),
            exposed_ports=data.get("exposed_ports", []),
            diff_ids=data.get("diff_ids", []),
        )
    except Exception as exc:
        logger.debug("ImageStore lookup failed for %s: %s", image_name, exc)
        return None


def _image_store_populate(image_name: str, config: ImageConfig) -> None:
    """Populate Rust ImageStore from an :class:`ImageConfig`."""
    try:
        from nitrobox._core import py_image_store_put
        payload = json.dumps({
            "image_id": "",
            "diff_ids": config.get("diff_ids", []),
            "cmd": config.get("cmd"),
            "entrypoint": config.get("entrypoint"),
            "env": config.get("env", {}),
            "working_dir": config.get("working_dir"),
            "exposed_ports": config.get("exposed_ports", []),
        })
        py_image_store_put(image_name, payload)
    except Exception as exc:
        logger.debug("Failed to populate image store for %s: %s", image_name, exc)



# ====================================================================== #
#  Public API — rootfs preparation                                         #
# ====================================================================== #


def prepare_rootfs_layers_from_docker(
    image_name: str,
    cache_dir: Path,
    pull: bool = True,
) -> list[Path]:
    """Extract Docker image as individual cached layers for overlayfs stacking.

    Uses ``docker save`` to get image layers, caches each by its content
    hash (diff_id).  Images sharing base layers skip re-extraction.

    Args:
        image_name: Docker image (e.g. ``"ubuntu:22.04"``).
        cache_dir: Root cache directory (e.g. ``~/.cache/nitrobox/rootfs``).
        pull: Pull the image first.

    Returns:
        Ordered list of layer directories (bottom to top) for overlayfs
        ``lowerdir`` stacking.

    Raises:
        RuntimeError: If layer extraction fails.
    """
    layers_dir = cache_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)

    # Fast path: check manifest from a previous run to skip docker pull
    # entirely when all layers are already cached.
    diff_ids = _get_manifest_diff_ids(cache_dir, image_name)
    if diff_ids:
        layer_dirs = list(dict.fromkeys(
            layers_dir / _safe_cache_key(did) for did in diff_ids
        ))
        if all(d.exists() for d in layer_dirs):
            logger.info("All %d layers cached for %s", len(layer_dirs), image_name)
            return layer_dirs

    # Get diff_ids: ImageStore → registry → Docker API
    # Raises RuntimeError with descriptive message if all sources fail.
    diff_ids = _get_image_diff_ids(image_name)

    # Check if all layers are already cached
    layer_dirs = list(dict.fromkeys(
        layers_dir / _safe_cache_key(did) for did in diff_ids
    ))
    # Get cached config from ImageStore (populated by _get_image_diff_ids)
    img_config = _image_store_get(image_name)

    if all(d.exists() for d in layer_dirs):
        logger.info("All %d layers cached for %s", len(layer_dirs), image_name)
        _write_manifest(cache_dir, image_name, diff_ids, image_config=img_config)
        return layer_dirs

    # Need to extract missing layers
    needed = {did for did, d in zip(diff_ids, layer_dirs) if not d.exists()}
    logger.info("Extracting layers for %s (%d layers, %d cached)",
                image_name, len(diff_ids), len(diff_ids) - len(needed))

    # Primary: download from registry directly (no Docker needed)
    try:
        _extract_layers_from_registry(image_name, needed, layers_dir)
    except Exception as e:
        logger.debug("Registry extraction failed for %s: %s", image_name, e)
        # Fallback: Docker API save (for locally-built images)
        try:
            if pull:
                _pull_or_check_local(image_name)
            resp = get_client().image_save(image_name)
            with tarfile.open(fileobj=resp, mode="r|") as outer_tar:
                _extract_layers_from_save_tar(outer_tar, diff_ids, layers_dir)
        except Exception:
            raise RuntimeError(
                f"Cannot extract layers for {image_name!r} from "
                f"registry or Docker."
            ) from e

    # Verify ALL layers were extracted before writing the manifest.
    still_missing = [d for d in layer_dirs if not d.exists()]
    if still_missing:
        names = [d.name for d in still_missing[:3]]
        raise RuntimeError(
            f"Layer extraction incomplete for {image_name!r}: "
            f"{len(still_missing)} layer(s) missing ({', '.join(names)})"
        )

    _write_manifest(cache_dir, image_name, diff_ids, image_config=img_config)
    # Deduplicate layers preserving order (overlayfs ELOOP on duplicate lowerdir).
    seen: set[Path] = set()
    unique_dirs: list[Path] = []
    for d in layer_dirs:
        if d not in seen:
            seen.add(d)
            unique_dirs.append(d)
    if len(unique_dirs) < len(layer_dirs):
        logger.debug("Deduplicated %d → %d layers", len(layer_dirs), len(unique_dirs))
    logger.info("Layer cache ready for %s: %d layers", image_name, len(unique_dirs))
    return unique_dirs



# ====================================================================== #
#  Internal — layer extraction & cache management                          #
# ====================================================================== #


def _extract_layers_from_registry(
    image_name: str,
    needed_diff_ids: set[str],
    layers_dir: Path,
) -> None:
    """Download and extract layers directly from registry (no Docker/Podman)."""
    import gzip
    from nitrobox._registry import pull_image_layers

    blobs = pull_image_layers(image_name, needed_diff_ids)
    for diff_id, compressed_blob in blobs.items():
        layer_dir = layers_dir / _safe_cache_key(diff_id)
        if layer_dir.exists():
            continue
        # Registry layers are gzip-compressed tarballs
        try:
            raw = gzip.decompress(compressed_blob)
        except gzip.BadGzipFile:
            raw = compressed_blob  # already uncompressed
        _extract_single_layer_locked(raw, layer_dir, layers_dir)


def _extract_layers_from_save_tar(
    outer_tar: tarfile.TarFile,
    diff_ids: list[str],
    layers_dir: Path,
) -> None:
    """Parse docker save tar and extract layer tarballs into cache dirs.

    Handles both legacy Docker format (hash/layer.tar) and modern
    Docker/OCI hybrid format (blobs/sha256/<hash>).
    """
    # Read all members into memory.  Docker save tarballs are typically
    # small (just metadata + compressed layers), so this is fine.
    manifest_data = None
    blobs: dict[str, bytes] = {}

    for member in outer_tar:
        f = outer_tar.extractfile(member)
        if f is None:
            continue
        data = f.read()
        f.close()

        if member.name == "manifest.json":
            manifest_data = json.loads(data)
        else:
            blobs[member.name] = data

    if not manifest_data:
        raise RuntimeError("Cannot parse docker save output: no manifest.json found")

    # manifest.json = [{"Layers": ["blobs/sha256/<hash>", ...], ...}]
    layer_paths = manifest_data[0].get("Layers", [])
    if len(layer_paths) != len(diff_ids):
        raise ValueError(
            f"Layer count mismatch: manifest has {len(layer_paths)}, "
            f"diff_ids has {len(diff_ids)}"
        )

    for layer_path, diff_id in zip(layer_paths, diff_ids):
        cache_key = _safe_cache_key(diff_id)
        layer_dir = layers_dir / cache_key
        if layer_dir.exists():
            continue  # Already cached

        raw = blobs.get(layer_path)
        if raw is None:
            raise RuntimeError(f"Layer blob not found in archive: {layer_path}")

        _extract_single_layer_locked(raw, layer_dir, layers_dir)


def _extract_single_layer_locked(
    raw: bytes,
    layer_dir: Path,
    layers_dir: Path,
) -> None:
    """Extract a single layer tarball with file locking for concurrent safety."""
    import shutil

    lock_path = layers_dir / f".{layer_dir.name}.lock"
    tmp_dir = layer_dir.with_suffix(".extracting")
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if layer_dir.exists():
                return  # Another process extracted while we waited

            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(parents=True)

            # Extract tar (use "tar" filter not "data" — rootfs layers
            # contain absolute symlinks which "data" rejects)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as lt:
                lt.extractall(tmp_dir, filter="tar")

            # Convert OCI whiteouts to overlayfs whiteouts
            _convert_whiteouts_in_layer(tmp_dir)

            # Atomic rename
            tmp_dir.rename(layer_dir)
            logger.debug("Extracted layer: %s", layer_dir.name)
        except Exception:
            # Clean up partial extraction
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    # Clean up lock file (best effort)
    try:
        lock_path.unlink()
    except OSError:
        pass


def _get_image_digest(image_name: str) -> str | None:
    """Get the content digest of a Docker image for cache keying."""
    try:
        info = get_client().image_inspect(image_name)
        digest = info.get("Id", "")
        return digest.replace(":", "_")[:80] if digest else None
    except Exception:
        return None


def _get_manifest_diff_ids(
    cache_dir: Path,
    image_name: str,
) -> list[str] | None:
    """Read cached manifest to get diff_ids without docker inspect.

    Checks both digest-based and name-based manifest keys so that
    images with different tags but identical content (e.g. different
    compose project names) share the same cached layer set.

    If the manifest also contains a ``config`` section (WORKDIR, CMD,
    etc.), it is loaded into the in-memory ImageStore so that
    ``get_image_config()`` can find it without a registry call.
    """
    manifests_dir = cache_dir / "manifests"

    def _try_load(path: Path) -> list[str] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        # Populate ImageStore from persisted config (if present)
        cfg = data.get("config")
        if cfg:
            merged = dict(cfg)
            merged["diff_ids"] = data.get("diff_ids", [])
            _image_store_populate(image_name, merged)
        return data.get("diff_ids")

    # Try digest-based key first (content-addressable)
    digest = _get_image_digest(image_name)
    if digest:
        result = _try_load(manifests_dir / f"{digest}.json")
        if result is not None:
            return result

    # Fall back to name-based key (backward compat)
    safe_name = image_name.replace("/", "_").replace(":", "_").replace(".", "_")
    return _try_load(manifests_dir / f"{safe_name}.json")


def _write_manifest(
    cache_dir: Path,
    image_name: str,
    diff_ids: list[str],
    image_config: dict | None = None,
) -> None:
    """Write manifest mapping image to its layer diff_ids and config.

    Writes under both the digest-based key and the name-based key
    so that future lookups by either tag or digest hit the cache.

    The optional *image_config* dict (cmd, entrypoint, env,
    working_dir, exposed_ports) is persisted alongside diff_ids so
    that ``get_image_config()`` can read it from disk without a
    registry round-trip.  This mirrors Podman's approach of storing
    the OCI config blob locally after pull.
    """
    manifests_dir = cache_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    data: dict = {
        "image": image_name,
        "diff_ids": diff_ids,
        "layers": [_safe_cache_key(did) for did in diff_ids],
    }
    if image_config:
        data["config"] = {
            "cmd": image_config.get("cmd"),
            "entrypoint": image_config.get("entrypoint"),
            "env": image_config.get("env", {}),
            "working_dir": image_config.get("working_dir"),
            "exposed_ports": image_config.get("exposed_ports", []),
        }
    payload = json.dumps(data, indent=2)

    # Write name-based manifest
    safe_name = image_name.replace("/", "_").replace(":", "_").replace(".", "_")
    (manifests_dir / f"{safe_name}.json").write_text(payload)

    # Write digest-based manifest (content-addressable)
    digest = _get_image_digest(image_name)
    if digest and digest != safe_name:
        (manifests_dir / f"{digest}.json").write_text(payload)


def _pull_or_check_local(image_name: str, **_kwargs: object) -> None:
    """Ensure image is available locally, pulling only if needed."""
    client = get_client()
    if client.image_exists(image_name):
        logger.debug("Image exists locally: %s", image_name)
        return

    logger.info("Pulling image: %s", image_name)
    client.image_pull(image_name)


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

    client = get_client()
    if pull:
        _pull_or_check_local(image_name)

    logger.info("Creating temporary container from %s", image_name)
    container_id = client.container_create(image_name)

    try:
        logger.info("Exporting %s -> %s", image_name, output_dir)
        resp = client.container_export(container_id)
        with tarfile.open(fileobj=resp, mode="r|") as tar:
            tar.extractall(output_dir, filter="tar")
    finally:
        client.container_remove(container_id, force=True)

    try:
        client.image_remove(image_name, force=True)
    except Exception:
        pass

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

    client = get_client()
    if pull:
        _pull_or_check_local(image_name)

    logger.info("Creating temporary container from %s", image_name)
    container_id = client.container_create(image_name)

    try:
        logger.info(
            "Exporting %s -> %s (btrfs subvolume)", image_name, subvolume_path
        )
        resp = client.container_export(container_id)
        with tarfile.open(fileobj=resp, mode="r|") as tar:
            tar.extractall(subvolume_path, filter="tar")
    finally:
        client.container_remove(container_id, force=True)

    try:
        client.image_remove(image_name, force=True)
    except Exception:
        pass

    logger.info("btrfs rootfs ready: %s", subvolume_path)
    return subvolume_path
