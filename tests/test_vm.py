"""Tests for QemuVM: QEMU/KVM virtual machine management.

Requires: /dev/kvm accessible, qemu-system-x86_64 installed in sandbox image.
These tests install QEMU via apt-get on first run (~2 min).

Run with: python -m pytest tests/test_vm.py -v
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from agentdocker_lite import Sandbox, SandboxConfig
from agentdocker_lite.vm import QemuVM

TEST_IMAGE = os.environ.get("LITE_SANDBOX_TEST_IMAGE", "ubuntu:22.04")


def _skip_if_no_kvm():
    if not os.path.exists("/dev/kvm"):
        pytest.skip("/dev/kvm not available")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("no read/write access to /dev/kvm")


def _skip_if_root():
    if os.geteuid() == 0:
        pytest.skip("userns test must run as non-root")


def _requires_docker():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("requires Docker")


@pytest.fixture(scope="module")
def vm_sandbox(tmp_path_factory, shared_cache_dir):
    """Sandbox with /dev/kvm and QEMU installed (module-scoped for speed)."""
    _skip_if_root()
    _skip_if_no_kvm()
    _requires_docker()

    tmp = tmp_path_factory.mktemp("vm")
    vm_dir = tmp / "vms"
    vm_dir.mkdir()

    config = SandboxConfig(
        image=TEST_IMAGE,
        devices=["/dev/kvm"],
        volumes=[f"{vm_dir}:/vm:rw"],
        env_base_dir=str(tmp / "envs"),
        rootfs_cache_dir=shared_cache_dir,
    )
    sb = Sandbox(config, name="vm-test")

    # Install QEMU if not available
    out, ec = sb.run("which qemu-system-x86_64 2>/dev/null || echo notfound")
    if "notfound" in out:
        _, ec = sb.run(
            "apt-get update -qq 2>/dev/null && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "--no-install-recommends qemu-system-x86 qemu-utils 2>/dev/null "
            "| tail -1",
            timeout=300,
        )
        if ec != 0:
            sb.delete()
            pytest.skip("failed to install qemu-system-x86")

    out, ec = sb.run("qemu-system-x86_64 --version 2>&1 | head -1")
    if ec != 0:
        sb.delete()
        pytest.skip("qemu-system-x86_64 not available in sandbox")

    # Create test disk
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(vm_dir / "test.qcow2"), "64M"],
        capture_output=True,
    )

    yield sb, str(vm_dir)
    sb.delete()


class TestQemuVM:
    """QEMU/KVM VM management tests."""

    def test_check_available(self):
        """QemuVM.check_available() returns True when /dev/kvm exists."""
        _skip_if_no_kvm()
        assert QemuVM.check_available() is True

    def test_start_stop(self, vm_sandbox):
        """VM starts and stops cleanly."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        assert vm.running
        vm.stop()
        assert not vm.running

    def test_query_status(self, vm_sandbox):
        """QMP query-status returns running state."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            resp = vm.qmp("query-status")
            assert resp["return"]["status"] == "running"
        finally:
            vm.stop()

    def test_savevm_loadvm(self, vm_sandbox):
        """savevm/loadvm round-trip works."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            vm.savevm("test_snap")
            info = vm.info_snapshots()
            assert "test_snap" in info

            vm.loadvm("test_snap")
            # VM should still be running after loadvm
            resp = vm.qmp("query-status")
            assert resp["return"]["status"] == "running"
        finally:
            vm.stop()

    def test_delvm(self, vm_sandbox):
        """delvm removes a snapshot."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            vm.savevm("to_delete")
            assert "to_delete" in vm.info_snapshots()
            vm.delvm("to_delete")
            assert "to_delete" not in vm.info_snapshots()
        finally:
            vm.stop()

    def test_multiple_snapshots(self, vm_sandbox):
        """Multiple savevm/loadvm cycles work."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            vm.savevm("snap_a")
            vm.savevm("snap_b")
            info = vm.info_snapshots()
            assert "snap_a" in info
            assert "snap_b" in info

            vm.loadvm("snap_a")
            vm.loadvm("snap_b")
            vm.loadvm("snap_a")
            assert vm.running
        finally:
            vm.stop()

    def test_hmp_command(self, vm_sandbox):
        """HMP commands work via QMP human-monitor-command."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            info = vm.hmp("info version")
            assert info.strip(), "info version should return non-empty"
        finally:
            vm.stop()

    def test_build_cmd(self, vm_sandbox):
        """_build_cmd generates correct QEMU command line."""
        sb, _ = vm_sandbox
        vm = QemuVM(sb, disk="/vm/disk.qcow2", memory="4G", cpus=4,
                    extra_args=["-vnc", ":0"])
        cmd = vm._build_cmd()
        assert "-enable-kvm" in cmd
        assert "-m 4G" in cmd
        assert "-smp 4" in cmd
        assert "/vm/disk.qcow2" in cmd
        assert "-vnc :0" in cmd

    def test_build_cmd_override(self, vm_sandbox):
        """cmd_override replaces the default QEMU command."""
        sb, _ = vm_sandbox
        override = "qemu-system-x86_64 -enable-kvm -m 8G -drive file=/my/disk.qcow2"
        vm = QemuVM(sb, cmd_override=override)
        cmd = vm._build_cmd()
        # cmd_override used verbatim with -qmp appended
        assert cmd.startswith(override)
        assert "-qmp unix:" in cmd
        # Default args should NOT be present
        assert "-smp" not in cmd
        assert "-display" not in cmd

    def test_build_cmd_override_preserves_qmp_socket(self, vm_sandbox):
        """cmd_override + custom qmp_socket works."""
        sb, _ = vm_sandbox
        override = "qemu-system-x86_64 -m 4G"
        vm = QemuVM(sb, cmd_override=override, qmp_socket="/storage/.qmp.sock")
        cmd = vm._build_cmd()
        assert cmd.endswith("-qmp unix:/storage/.qmp.sock,server,nowait")

    def test_repr(self, vm_sandbox):
        """repr shows useful info."""
        sb, _ = vm_sandbox
        vm = QemuVM(sb, disk="/vm/disk.qcow2", memory="2G", cpus=2)
        r = repr(vm)
        assert "disk=" in r
        assert "stopped" in r


class TestRustQMP:
    """Tests for the Rust QMP client binding."""

    def test_binding_importable(self):
        """py_qmp_send is importable from _core."""
        from agentdocker_lite._core import py_qmp_send
        assert callable(py_qmp_send)

    def test_nonexistent_socket_raises(self):
        """Connecting to a non-existent socket raises OSError."""
        from agentdocker_lite._core import py_qmp_send
        with pytest.raises(OSError):
            py_qmp_send("/tmp/nonexistent_qmp_socket_12345.sock", '{"execute":"query-status"}')

    def test_invalid_socket_path_raises(self):
        """Empty socket path raises OSError."""
        from agentdocker_lite._core import py_qmp_send
        with pytest.raises(OSError):
            py_qmp_send("", '{"execute":"query-status"}')

    def test_qmp_via_rust_binding_on_volume(self, vm_sandbox, tmp_path):
        """Rust QMP binding works when QMP socket is on a volume mount."""
        sb, vm_dir = vm_sandbox

        # Place QMP socket on a host-accessible volume path.
        # Sockets on overlayfs are not connectable from the host side.
        qmp_dir = tmp_path / "qmp"
        qmp_dir.mkdir()
        # The volume was already set up when vm_sandbox was created,
        # but /vm is already a volume mount, so use that path.
        qmp_path = "/vm/.adl_qmp_test.sock"

        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1,
                    qmp_socket=qmp_path)
        vm.start(timeout=30)
        try:
            import json
            from agentdocker_lite._core import py_qmp_send
            # /vm is bind-mounted to vm_dir on host
            host_sock = Path(vm_dir) / ".adl_qmp_test.sock"
            if not host_sock.exists():
                pytest.skip("QMP socket not found on host volume")
            msg = json.dumps({"execute": "query-status"})
            result = py_qmp_send(str(host_sock), msg, 10)
            parsed = json.loads(result)
            assert "return" in parsed
            assert parsed["return"]["status"] == "running"
        finally:
            vm.stop()
