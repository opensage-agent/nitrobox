"""Shared network namespace (Podman-style pod networking) and health-check helpers."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from nitrobox.config import detect_subuid_range

logger = logging.getLogger(__name__)


def _find_pasta_bin() -> str | None:
    """Find the pasta binary (vendored or system)."""
    vendored = Path(__file__).resolve().parent.parent / "_vendor" / "pasta"
    if vendored.exists() and vendored.is_file():
        return str(vendored)
    if shutil.which("pasta"):
        return "pasta"
    return None


class SharedNetwork:
    """Shared userns + netns for compose network isolation.

    Creates a sentinel process that holds a user namespace (with full
    uid mapping) and a network namespace.  Other sandboxes join the
    sentinel's namespaces via ``nsenter``.

    By default, pasta is attached to the shared netns to provide NAT
    and DNS forwarding, giving containers internet access (matching
    Docker Compose's default behaviour).  Pass ``internet=False`` to
    disable this.

    This mirrors Podman's pod infra container: one shared userns+netns
    per pod, individual mount/pid namespaces per container.
    """

    _live_instances: list[SharedNetwork] = []
    _atexit_registered: bool = False

    def __init__(
        self,
        name: str = "default",
        *,
        internet: bool = True,
        port_map: list[str] | None = None,
    ) -> None:
        self.name = name
        self.has_pasta: bool = False
        self.dns_forward_ips: list[str] = []
        self.guest_ip: str | None = None
        # Detect subuid range (reuse rootless sandbox logic)
        self._subuid_range = detect_subuid_range()

        # Create sentinel with userns + netns
        unshare_cmd = ["unshare", "--user", "--net", "--fork"]
        if not self._subuid_range:
            unshare_cmd.insert(2, "--map-root-user")
        unshare_cmd.extend(["--", "sleep", "infinity"])

        self._sentinel = subprocess.Popen(
            unshare_cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        SharedNetwork._live_instances.append(self)

        try:
            # Wait for child to enter new userns
            if self._subuid_range:
                my_userns = os.readlink("/proc/self/ns/user")
                for _ in range(1000):
                    try:
                        child_userns = os.readlink(
                            f"/proc/{self._sentinel.pid}/ns/user"
                        )
                        if child_userns != my_userns:
                            break
                    except (FileNotFoundError, PermissionError):
                        pass
                    time.sleep(0.001)
                else:
                    raise RuntimeError("Timeout waiting for sentinel userns")

                # Set up full uid/gid mapping
                outer_uid, sub_start, sub_count = self._subuid_range
                outer_gid = os.getgid()
                pid = self._sentinel.pid
                subprocess.run(
                    ["newuidmap", str(pid),
                     "0", str(outer_uid), "1",
                     "1", str(sub_start), str(sub_count)],
                    check=True, capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["newgidmap", str(pid),
                     "0", str(outer_gid), "1",
                     "1", str(sub_start), str(sub_count)],
                    check=True, capture_output=True, timeout=10,
                )

            # Attach pasta for NAT + DNS (like Docker Compose default networking)
            if internet:
                self._start_pasta(port_map or [])

        except Exception:
            self.destroy()
            raise

        self._register_atexit()

    @classmethod
    def _register_atexit(cls) -> None:
        if not cls._atexit_registered:
            import atexit
            atexit.register(cls._atexit_cleanup)
            cls._atexit_registered = True

    @classmethod
    def _atexit_cleanup(cls) -> None:
        for sn in list(cls._live_instances):
            try:
                sn.destroy()
            except Exception:
                pass
        cls._live_instances.clear()

    def _start_pasta(self, port_map: list[str]) -> None:
        """Attach pasta to the sentinel's netns for NAT and DNS forwarding.

        Uses pasta's PID mode (``pasta PID``) which attaches to the
        network namespace of the given process — no bind-mount needed.
        """
        pasta_bin = _find_pasta_bin()
        if not pasta_bin:
            logger.warning(
                "pasta not found — shared network will have no internet access. "
                "Install 'passt' package or ensure vendored pasta is available."
            )
            return

        pid = self._sentinel.pid

        cmd: list[str] = [
            pasta_bin, "--config-net",
            "--ipv4-only",
        ]
        # Podman: explicit port mappings, or -t none to disable default
        # TCP forwarding (pasta forwards ALL host TCP ports otherwise).
        if port_map:
            for mapping in port_map:
                cmd.extend(["-t", mapping])
        else:
            cmd.extend(["-t", "none"])
        cmd.extend([
            "-u", "none", "-T", "none", "-U", "none",
            "--dns-forward", "169.254.1.1",
            "--no-map-gw", "--quiet",
            "--map-guest-addr", "169.254.1.2",
            str(pid),
        ])

        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            logger.warning(
                "pasta failed (exit=%d): %s — shared network will have no internet",
                out.returncode, out.stderr.strip(),
            )
            return

        # Parse pasta output to get actual DNS and guest IP
        # (matching Podman's pastaResult.DNSForwardIPs / IPAddresses)
        self.dns_forward_ips = _parse_pasta_dns(out.stderr)
        self.guest_ip = _parse_pasta_guest_ip(out.stderr)
        self.has_pasta = True

        # Verify DNS forwarding is actually working before declaring
        # the network ready.  Pasta forks to background on success, but
        # the DNS forwarder may not be fully initialised yet.
        self._verify_dns(pid)

        logger.debug(
            "pasta ready for shared network %r (pid=%d, dns=%s, guest_ip=%s)",
            self.name, pid, self.dns_forward_ips, self.guest_ip,
        )

    def _verify_dns(self, sentinel_pid: int) -> None:
        """Probe pasta's DNS forwarder inside the shared netns.

        Sends a tiny UDP DNS query to 169.254.1.1:53 via nsenter and
        checks for a response.  Retries up to 3 times with 100ms
        backoff (~1s total worst-case).
        """
        dns_ip = self.dns_forward_ips[0] if self.dns_forward_ips else "169.254.1.1"

        # Minimal DNS query for "localhost"
        query = (
            b"\x12\x34\x01\x00"
            b"\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x09localhost\x00\x00\x01\x00\x01"
        )

        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["nsenter",
                     f"--user=/proc/{sentinel_pid}/ns/user",
                     f"--net=/proc/{sentinel_pid}/ns/net",
                     "python3", "-c",
                     f"import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                     f"s.settimeout(0.5); s.sendto({query!r},('{dns_ip}',53)); "
                     f"d,_=s.recvfrom(512); print(len(d))"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.debug("DNS probe OK on attempt %d", attempt + 1)
                    return
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(0.1)

        logger.warning(
            "DNS probe failed for shared network %r — "
            "pasta DNS forwarder at %s may be unreliable",
            self.name, dns_ip,
        )

    @property
    def userns_path(self) -> str:
        """Path to the sentinel's user namespace."""
        return f"/proc/{self._sentinel.pid}/ns/user"

    @property
    def netns_path(self) -> str:
        """Path to the sentinel's network namespace."""
        return f"/proc/{self._sentinel.pid}/ns/net"

    @property
    def alive(self) -> bool:
        return self._sentinel.poll() is None

    def destroy(self) -> None:
        """Kill the sentinel, releasing the shared namespaces."""
        try:
            SharedNetwork._live_instances.remove(self)
        except ValueError:
            pass
        if self._sentinel.poll() is None:
            import signal as _signal
            try:
                os.killpg(self._sentinel.pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    self._sentinel.kill()
                except Exception:
                    pass
            try:
                self._sentinel.wait(timeout=5)
            except Exception:
                pass

    def __repr__(self) -> str:
        state = "alive" if self.alive else "dead"
        return f"SharedNetwork({self.name!r}, {state})"


def _parse_pasta_dns(output: str) -> list[str]:
    """Extract DNS forward IPs from pasta's stderr output.

    Pasta prints lines like::

        DNS:
            169.254.1.1
    """
    ips: list[str] = []
    in_dns = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("DNS:"):
            in_dns = True
            continue
        if in_dns:
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", stripped):
                ips.append(stripped)
            else:
                in_dns = False
    return ips or ["169.254.1.1"]  # fallback


def _parse_pasta_guest_ip(output: str) -> str | None:
    """Extract DHCP-assigned guest IP from pasta's stderr output.

    Pasta prints lines like::

        DHCP:
            assign: 10.0.2.15
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("assign:"):
            ip = stripped.split(":", 1)[1].strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                return ip
    return None


# ------------------------------------------------------------------ #
#  Duration parsing                                                    #
# ------------------------------------------------------------------ #


def _parse_duration(s: str | int | float) -> float:
    """Parse compose duration string to seconds.

    Supports single units (``"30s"``, ``"2m"``) and compound durations
    (``"1m30s"``, ``"1h2m3s"``) matching the compose-spec/Go
    ``time.ParseDuration`` format.
    """
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()

    _UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}

    # Try compound duration first: "1h2m30s", "1m30s500ms"
    total = 0.0
    found = False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(h|m(?!s)|s|ms)", s):
        total += float(m.group(1)) * _UNITS[m.group(2)]
        found = True
    if found:
        return total

    # Simple number without unit → seconds
    m = re.match(r"^(\d+(?:\.\d+)?)$", s)
    if m:
        return float(m.group(1))

    return 30.0


# ------------------------------------------------------------------ #
#  Health check                                                        #
# ------------------------------------------------------------------ #


def _healthcheck_cmd(test: Any) -> str:
    """Convert healthcheck test to a shell command string."""
    if isinstance(test, str):
        return test
    if isinstance(test, list) and test:
        if test[0] == "CMD":
            return shlex.join(test[1:])
        if test[0] == "CMD-SHELL":
            return " ".join(test[1:])
        # NONE disables
        if test[0] == "NONE":
            return ""
        return shlex.join(test)
    return ""
