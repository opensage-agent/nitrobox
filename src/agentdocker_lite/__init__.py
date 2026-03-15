"""agentdocker-lite: Lightweight Linux namespace sandbox for high-frequency workloads."""

from agentdocker_lite._base import SandboxBase, SandboxConfig
from agentdocker_lite.sandbox import Sandbox

__all__ = ["Sandbox", "SandboxConfig", "SandboxBase"]
__version__ = "0.0.1"
