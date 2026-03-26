//! adl-core: Rust core for agentdocker-lite.
//!
//! Provides direct syscall interfaces for Linux namespace sandboxing,
//! replacing Python ctypes/subprocess string concatenation.

pub mod cgroup;
pub mod init;
pub mod mount;
pub mod pidfd;
pub mod security;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::collections::HashMap;

// ======================================================================
// PyO3 bindings
// ======================================================================

/// Check if kernel supports new mount API with lowerdir+ (>= 6.8).
#[pyfunction]
fn py_check_new_mount_api() -> bool {
    mount::check_new_mount_api()
}

/// Mount overlayfs, auto-selecting new mount API or legacy mount(2).
#[pyfunction]
#[pyo3(signature = (lowerdir_spec, upper_dir, work_dir, target))]
fn py_mount_overlay(
    lowerdir_spec: &str,
    upper_dir: &str,
    work_dir: &str,
    target: &str,
) -> PyResult<()> {
    mount::mount_overlay(lowerdir_spec, upper_dir, work_dir, target)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Build seccomp BPF bytecode as raw bytes.
#[pyfunction]
fn py_build_seccomp_bpf(py: Python<'_>) -> PyResult<Py<PyBytes>> {
    let bpf = security::build_seccomp_bpf();
    Ok(PyBytes::new(py, &bpf).into())
}

/// Apply seccomp-bpf filter. Raises OSError on failure.
#[pyfunction]
fn py_apply_seccomp_filter() -> PyResult<()> {
    security::apply_seccomp_filter()
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Drop capabilities except Docker defaults + extra_keep.
#[pyfunction]
#[pyo3(signature = (extra_keep = None))]
fn py_drop_capabilities(extra_keep: Option<Vec<u32>>) -> PyResult<u32> {
    security::drop_capabilities(&extra_keep.unwrap_or_default())
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Apply Landlock filesystem + network restrictions.
#[pyfunction]
#[pyo3(signature = (read_paths = None, write_paths = None, allowed_tcp_ports = None, strict = false))]
fn py_apply_landlock(
    read_paths: Option<Vec<String>>,
    write_paths: Option<Vec<String>>,
    allowed_tcp_ports: Option<Vec<u16>>,
    strict: bool,
) -> PyResult<bool> {
    security::apply_landlock(
        &read_paths.unwrap_or_default(),
        &write_paths.unwrap_or_default(),
        &allowed_tcp_ports.unwrap_or_default(),
        strict,
    )
    .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Query kernel Landlock ABI version (0 if unavailable).
#[pyfunction]
fn py_landlock_abi_version() -> u32 {
    security::landlock_abi_version()
}

/// Create a pidfd for the given PID. Returns fd or None.
#[pyfunction]
fn py_pidfd_open(pid: i32) -> Option<i32> {
    pidfd::pidfd_open(pid).ok()
}

/// Send signal to process via pidfd. Returns True on success.
#[pyfunction]
fn py_pidfd_send_signal(pidfd: i32, sig: i32) -> bool {
    pidfd::pidfd_send_signal(pidfd, sig).is_ok()
}

/// Check if process behind pidfd is alive.
#[pyfunction]
fn py_pidfd_is_alive(pidfd: i32) -> bool {
    pidfd::pidfd_is_alive(pidfd)
}

/// Hint kernel to reclaim (swap out) sandbox process memory via MADV_COLD.
#[pyfunction]
fn py_process_madvise_cold(pidfd: i32) -> PyResult<bool> {
    match pidfd::process_madvise_cold(pidfd) {
        Ok(()) => Ok(true),
        Err(e) => {
            log::debug!("process_madvise failed: {}", e);
            Ok(false)
        }
    }
}

// --- cgroup bindings ---

/// Check if cgroup v2 is available.
#[pyfunction]
fn py_cgroup_v2_available() -> bool {
    cgroup::cgroup_v2_available()
}

/// Create a cgroup for the sandbox. Returns the cgroup path.
#[pyfunction]
fn py_create_cgroup(name: &str) -> PyResult<String> {
    cgroup::create_cgroup(name)
        .map(|p| p.to_string_lossy().into_owned())
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Apply resource limits to a cgroup.
#[pyfunction]
fn py_apply_cgroup_limits(cgroup_path: &str, limits: HashMap<String, String>) -> PyResult<()> {
    let path = std::path::Path::new(cgroup_path);
    cgroup::enable_controllers(path, &limits)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
    cgroup::apply_limits(path, &limits)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Move a process into a cgroup.
#[pyfunction]
fn py_cgroup_add_process(cgroup_path: &str, pid: u32) -> PyResult<()> {
    cgroup::add_process(std::path::Path::new(cgroup_path), pid)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Kill all processes in a cgroup and remove it.
#[pyfunction]
fn py_cleanup_cgroup(cgroup_path: &str) -> PyResult<()> {
    cgroup::cleanup_cgroup(std::path::Path::new(cgroup_path))
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
}

/// Convert Docker CPU shares to cgroup v2 weight.
#[pyfunction]
fn py_convert_cpu_shares(shares: u64) -> u64 {
    cgroup::convert_cpu_shares(shares)
}

// --- spawn_sandbox binding ---

/// Helper: extract optional string from PyDict.
fn get_opt_str(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

fn get_str(d: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<String> {
    get_opt_str(d, key).map(|v| v.unwrap_or_else(|| default.to_string()))
}

fn get_bool(d: &Bound<'_, PyDict>, key: &str, default: bool) -> PyResult<bool> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(v.extract()?),
        _ => Ok(default),
    }
}

fn get_vec_str(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<String>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(v.extract()?),
        _ => Ok(Vec::new()),
    }
}

fn get_vec_u32(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<u32>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(v.extract()?),
        _ => Ok(Vec::new()),
    }
}

fn get_vec_u16(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<u16>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(v.extract()?),
        _ => Ok(Vec::new()),
    }
}

fn get_opt_u64(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u64>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

fn get_opt_subuid(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<(u32, u32, u32)>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => {
            let tuple: (u32, u32, u32) = v.extract()?;
            Ok(Some(tuple))
        }
        _ => Ok(None),
    }
}

fn get_env(d: &Bound<'_, PyDict>, key: &str) -> PyResult<HashMap<String, String>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(v.extract()?),
        _ => Ok(HashMap::new()),
    }
}

/// Spawn a sandbox process. Takes a config dict, returns a result dict.
///
/// Config keys: rootfs, shell, working_dir, env, rootful, lowerdir_spec,
/// upper_dir, work_dir, userns, net_isolate, net_ns, shared_userns,
/// map_root_user, subuid_range, time_ns, seccomp, cap_add, hostname,
/// read_only, landlock_read_paths, landlock_write_paths, landlock_ports,
/// landlock_strict, volumes, devices, shm_size, tmpfs_mounts,
/// cgroup_path, entrypoint, tty, env_dir.
///
/// Returns dict: pid, stdin_fd, stdout_fd, signal_r_fd, signal_w_fd_num,
/// master_fd (optional), pidfd (optional).
#[pyfunction]
fn py_spawn_sandbox<'py>(py: Python<'py>, config: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
    let cfg = init::SandboxSpawnConfig {
        rootfs: get_str(config, "rootfs", "/")?,
        shell: get_str(config, "shell", "/bin/sh")?,
        working_dir: get_str(config, "working_dir", "/")?,
        env: get_env(config, "env")?,
        rootful: get_bool(config, "rootful", false)?,
        lowerdir_spec: get_opt_str(config, "lowerdir_spec")?,
        upper_dir: get_opt_str(config, "upper_dir")?,
        work_dir: get_opt_str(config, "work_dir")?,
        userns: get_bool(config, "userns", false)?,
        net_isolate: get_bool(config, "net_isolate", false)?,
        net_ns: get_opt_str(config, "net_ns")?,
        shared_userns: get_opt_str(config, "shared_userns")?,
        map_root_user: get_bool(config, "map_root_user", false)?,
        subuid_range: get_opt_subuid(config, "subuid_range")?,
        time_ns: get_bool(config, "time_ns", false)?,
        seccomp: get_bool(config, "seccomp", true)?,
        cap_add: get_vec_u32(config, "cap_add")?,
        hostname: get_opt_str(config, "hostname")?,
        read_only: get_bool(config, "read_only", false)?,
        landlock_read_paths: get_vec_str(config, "landlock_read_paths")?,
        landlock_write_paths: get_vec_str(config, "landlock_write_paths")?,
        landlock_ports: get_vec_u16(config, "landlock_ports")?,
        landlock_strict: get_bool(config, "landlock_strict", false)?,
        volumes: get_vec_str(config, "volumes")?,
        devices: get_vec_str(config, "devices")?,
        shm_size: get_opt_u64(config, "shm_size")?,
        tmpfs_mounts: get_vec_str(config, "tmpfs_mounts")?,
        cgroup_path: get_opt_str(config, "cgroup_path")?,
        entrypoint: get_vec_str(config, "entrypoint")?,
        tty: get_bool(config, "tty", false)?,
        port_map: get_vec_str(config, "port_map")?,
        pasta_bin: get_opt_str(config, "pasta_bin")?,
        ipv6: get_bool(config, "ipv6", false)?,
        env_dir: get_opt_str(config, "env_dir")?,
    };

    let result = init::spawn_sandbox(&cfg)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;

    let dict = PyDict::new(py);
    dict.set_item("pid", result.pid)?;
    dict.set_item("stdin_fd", result.stdin_fd)?;
    dict.set_item("stdout_fd", result.stdout_fd)?;
    dict.set_item("signal_r_fd", result.signal_r_fd)?;
    dict.set_item("signal_w_fd_num", result.signal_w_fd_num)?;
    dict.set_item("master_fd", result.master_fd)?;
    dict.set_item("pidfd", result.pidfd)?;
    Ok(dict)
}

// ======================================================================
// Module definition
// ======================================================================

/// agentdocker-lite Rust core: direct syscall interface for namespace sandboxing.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // mount
    m.add_function(wrap_pyfunction!(py_check_new_mount_api, m)?)?;
    m.add_function(wrap_pyfunction!(py_mount_overlay, m)?)?;

    // security
    m.add_function(wrap_pyfunction!(py_build_seccomp_bpf, m)?)?;
    m.add_function(wrap_pyfunction!(py_apply_seccomp_filter, m)?)?;
    m.add_function(wrap_pyfunction!(py_drop_capabilities, m)?)?;
    m.add_function(wrap_pyfunction!(py_apply_landlock, m)?)?;
    m.add_function(wrap_pyfunction!(py_landlock_abi_version, m)?)?;

    // pidfd
    m.add_function(wrap_pyfunction!(py_pidfd_open, m)?)?;
    m.add_function(wrap_pyfunction!(py_pidfd_send_signal, m)?)?;
    m.add_function(wrap_pyfunction!(py_pidfd_is_alive, m)?)?;
    m.add_function(wrap_pyfunction!(py_process_madvise_cold, m)?)?;

    // cgroup
    m.add_function(wrap_pyfunction!(py_cgroup_v2_available, m)?)?;
    m.add_function(wrap_pyfunction!(py_create_cgroup, m)?)?;
    m.add_function(wrap_pyfunction!(py_apply_cgroup_limits, m)?)?;
    m.add_function(wrap_pyfunction!(py_cgroup_add_process, m)?)?;
    m.add_function(wrap_pyfunction!(py_cleanup_cgroup, m)?)?;
    m.add_function(wrap_pyfunction!(py_convert_cpu_shares, m)?)?;

    // spawn
    m.add_function(wrap_pyfunction!(py_spawn_sandbox, m)?)?;

    Ok(())
}
