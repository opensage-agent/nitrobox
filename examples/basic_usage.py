#!/usr/bin/env python3
"""Basic usage example for lite-sandbox.

Must be run as root (requires mount/cgroup operations).
Requires Docker to auto-prepare rootfs from image names.
"""

from lite_sandbox import Sandbox, SandboxConfig


def main():
    # Create a sandbox from a Docker image (auto-exports to rootfs on first use).
    # Or pass a path to an existing rootfs directory.
    config = SandboxConfig(
        image="ubuntu:22.04",
        working_dir="/workspace",
        cpu_max="50000 100000",  # 50% of one core
        memory_max="536870912",  # 512 MB
        pids_max="256",
    )

    sb = Sandbox(config, name="demo")

    # Run commands (~42ms per command via persistent shell).
    output, ec = sb.run("echo hello from sandbox")
    print(f"[exit={ec}] {output.strip()}")

    output, ec = sb.run("cat /etc/os-release | head -2")
    print(f"[exit={ec}] {output.strip()}")

    # Write and read files directly (bypasses shell, even faster).
    sb.write_file("/workspace/test.txt", "hello world\n")
    content = sb.read_file("/workspace/test.txt")
    print(f"File content: {content.strip()}")

    # Reset filesystem to initial state (~27ms).
    sb.reset()

    # File is gone after reset.
    output, ec = sb.run("cat /workspace/test.txt 2>&1")
    print(f"After reset [exit={ec}]: {output.strip()}")

    # Clean up.
    sb.delete()
    print("Done.")


if __name__ == "__main__":
    main()
