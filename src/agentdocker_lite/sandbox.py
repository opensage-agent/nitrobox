"""Factory function for creating sandbox instances.

Auto-selects the appropriate sandbox backend based on whether the
current process is running as root (namespace-based isolation) or
as a regular user (Landlock-based sandboxing).
"""

from __future__ import annotations

from agentdocker_lite._base import SandboxBase, SandboxConfig


def Sandbox(config: SandboxConfig, name: str = "default") -> SandboxBase:
    """Factory: auto-selects NamespaceSandbox (root) or LandlockSandbox (rootless).

    Args:
        config: Sandbox configuration.
        name: Unique name for this sandbox instance.

    Returns:
        A sandbox instance (either NamespaceSandbox or LandlockSandbox).
    """
    import os

    if os.geteuid() == 0:
        from agentdocker_lite._namespace import NamespaceSandbox

        return NamespaceSandbox(config, name)
    else:
        from agentdocker_lite._landlock import LandlockSandbox

        return LandlockSandbox(config, name)
