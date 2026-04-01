"""Tests for QemuVM: QEMU/KVM virtual machine management.

Requires: /dev/kvm accessible, qemu-system-x86_64 installed in sandbox image.
These tests install QEMU via apt-get on first run (~2 min).

Run with: python -m pytest tests/test_vm.py -v
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from nitrobox import Sandbox, SandboxConfig
from nitrobox.vm import QemuVM

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
        assert "-qmp unix:/storage/.qmp.sock,server,nowait" in cmd

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
        from nitrobox._core import py_qmp_send
        assert callable(py_qmp_send)

    def test_nonexistent_socket_raises(self):
        """Connecting to a non-existent socket raises OSError."""
        from nitrobox._core import py_qmp_send
        with pytest.raises(OSError):
            py_qmp_send("/tmp/nonexistent_qmp_socket_12345.sock", '{"execute":"query-status"}')

    def test_invalid_socket_path_raises(self):
        """Empty socket path raises OSError."""
        from nitrobox._core import py_qmp_send
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
        qmp_path = "/vm/.nbx_qmp_test.sock"

        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1,
                    qmp_socket=qmp_path)
        vm.start(timeout=30)
        try:
            from nitrobox._core import py_qmp_send
            # /vm is bind-mounted to vm_dir on host
            host_sock = Path(vm_dir) / ".nbx_qmp_test.sock"
            if not host_sock.exists():
                pytest.skip("QMP socket not found on host volume")
            msg = json.dumps({"execute": "query-status"})
            result = py_qmp_send(str(host_sock), msg, 10)
            parsed = json.loads(result)
            assert "return" in parsed
            assert parsed["return"]["status"] == "running"
        finally:
            vm.stop()


# ====================================================================== #
#  Mock QGA server                                                        #
# ====================================================================== #

class _MockQGAServer:
    """Minimal QGA protocol mock for unit testing."""

    def __init__(self, sock_path: str):
        self._path = sock_path
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.exec_output = b"mock output\n"
        self.exec_exitcode = 0
        self.file_content = b"mock file content"
        self._written: list[bytes] = []

    def start(self):
        self._thread.start()

    def stop(self):
        self._srv.close()

    @property
    def written_data(self) -> bytes:
        return b"".join(self._written)

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        f = conn.makefile("rb")
        try:
            for raw in f:
                line = raw.lstrip(b"\xff").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                cmd = req.get("execute", "")
                args = req.get("arguments", {})

                if cmd == "guest-sync-delimited":
                    resp = {"return": args["id"]}
                elif cmd == "guest-ping":
                    resp = {"return": {}}
                elif cmd == "guest-exec":
                    resp = {"return": {"pid": 42}}
                elif cmd == "guest-exec-status":
                    resp = {"return": {
                        "exited": True,
                        "exitcode": self.exec_exitcode,
                        "out-data": base64.b64encode(self.exec_output).decode(),
                    }}
                elif cmd == "guest-file-open":
                    resp = {"return": 1}
                elif cmd == "guest-file-read":
                    resp = {"return": {
                        "count": len(self.file_content),
                        "buf-b64": base64.b64encode(self.file_content).decode(),
                        "eof": True,
                    }}
                elif cmd == "guest-file-write":
                    data = base64.b64decode(args.get("buf-b64", ""))
                    self._written.append(data)
                    resp = {"return": {"count": len(data)}}
                elif cmd == "guest-file-close":
                    resp = {"return": {}}
                else:
                    resp = {"error": {"class": "CommandNotFound", "desc": cmd}}

                conn.sendall(json.dumps(resp).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            f.close()
            conn.close()


@pytest.fixture
def mock_qga(tmp_path):
    """QemuVM wired to a mock QGA server (no KVM needed)."""
    sock_path = str(tmp_path / "qga.sock")
    server = _MockQGAServer(sock_path)
    server.start()

    vm = QemuVM.__new__(QemuVM)
    vm._sb = None
    vm._qga_path = sock_path
    # _resolve_host_socket checks _host_qga_path first
    vm._host_qga_path = sock_path

    yield vm, server
    server.stop()


class TestQGAProtocol:
    """QGA client protocol tests against mock server."""

    def test_guest_ping(self, mock_qga):
        vm, _ = mock_qga
        assert vm.guest_ping(timeout=3) is True

    def test_guest_ping_timeout(self, tmp_path):
        """guest_ping returns False when nothing is listening."""
        vm = QemuVM.__new__(QemuVM)
        vm._sb = None
        sock = str(tmp_path / "dead.sock")
        vm._qga_path = sock
        vm._host_qga_path = sock
        assert vm.guest_ping(timeout=1) is False

    def test_guest_exec(self, mock_qga):
        vm, server = mock_qga
        server.exec_output = b"hello world\n"
        server.exec_exitcode = 0
        output, ec = vm.guest_exec("echo hello world", timeout=5)
        assert ec == 0
        assert "hello world" in output

    def test_guest_exec_nonzero_exit(self, mock_qga):
        vm, server = mock_qga
        server.exec_output = b"error\n"
        server.exec_exitcode = 1
        output, ec = vm.guest_exec("false", timeout=5)
        assert ec == 1
        assert "error" in output

    def test_guest_file_read(self, mock_qga):
        vm, server = mock_qga
        server.file_content = b"secret data"
        data = vm.guest_file_read("/etc/secret")
        assert data == b"secret data"

    def test_guest_file_write(self, mock_qga):
        vm, server = mock_qga
        vm.guest_file_write("/tmp/out.txt", b"written data")
        assert server.written_data == b"written data"

    def test_wait_guest_ready(self, mock_qga):
        vm, _ = mock_qga
        # Should return immediately since mock always responds
        vm.wait_guest_ready(timeout=3)

    def test_build_cmd_includes_qga(self):
        """_build_cmd includes QGA chardev + virtio-serial device."""
        vm = QemuVM.__new__(QemuVM)
        vm._sb = None
        vm._disk = "/vm/disk.qcow2"
        vm._memory = "2G"
        vm._cpus = 2
        vm._display = "none"
        vm._extra_args = []
        vm._qmp_path = "/tmp/.qmp.sock"
        vm._qga_path = "/tmp/.qga.sock"
        vm._cmd_override = None
        cmd = vm._build_cmd()
        assert "-chardev socket,id=nbxqga,path=/tmp/.qga.sock" in cmd
        assert "virtio-serial-pci" in cmd
        assert "org.qemu.guest_agent.0" in cmd

    def test_build_cmd_override_includes_qga(self):
        """cmd_override also gets QGA args appended."""
        vm = QemuVM.__new__(QemuVM)
        vm._sb = None
        vm._qmp_path = "/tmp/.qmp.sock"
        vm._qga_path = "/tmp/.qga.sock"
        vm._cmd_override = "qemu-system-x86_64 -m 4G"
        cmd = vm._build_cmd()
        assert cmd.startswith("qemu-system-x86_64 -m 4G")
        assert "nbxqga" in cmd
        assert "org.qemu.guest_agent.0" in cmd
