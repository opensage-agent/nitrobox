//! Overlay mount helpers.
//!
//! 1. **New mount API** (kernel >= 6.8): rustix `fsopen` + `fsconfig_set_string`
//!    with `lowerdir+` per-layer append. No length limit per layer.
//! 2. **Legacy mount(2)** fallback via nix: single syscall, PAGE_SIZE limit.

use std::io;
use std::sync::OnceLock;

// --- feature detection ---

static NEW_API_SUPPORTED: OnceLock<bool> = OnceLock::new();

pub fn check_new_mount_api() -> bool {
    *NEW_API_SUPPORTED.get_or_init(|| {
        let fd = match rustix::mount::fsopen("overlay", rustix::mount::FsOpenFlags::FSOPEN_CLOEXEC)
        {
            Ok(fd) => fd,
            Err(_) => return false,
        };

        // Try lowerdir+ — if kernel < 6.8, this will EINVAL
        let supported =
            rustix::mount::fsconfig_set_string(&fd, "lowerdir+", "/").is_ok();

        log::debug!("New mount API (lowerdir+): {}", supported);
        supported
    })
}

// --- new mount API via rustix ---

fn mount_overlay_new_api(
    lower_dirs: &[&str],
    upper_dir: &str,
    work_dir: &str,
    target: &str,
) -> io::Result<()> {
    let fd = rustix::mount::fsopen("overlay", rustix::mount::FsOpenFlags::FSOPEN_CLOEXEC)?;

    // Add each lower layer individually (lowerdir+ appends top-to-bottom)
    for layer in lower_dirs {
        rustix::mount::fsconfig_set_string(&fd, "lowerdir+", *layer)?;
    }

    rustix::mount::fsconfig_set_string(&fd, "upperdir", upper_dir)?;
    rustix::mount::fsconfig_set_string(&fd, "workdir", work_dir)?;
    rustix::mount::fsconfig_create(&fd)?;

    let mnt = rustix::mount::fsmount(&fd, rustix::mount::FsMountFlags::FSMOUNT_CLOEXEC, rustix::mount::MountAttrFlags::empty())?;

    rustix::mount::move_mount(
        &mnt,
        "",
        rustix::fs::CWD,
        target,
        rustix::mount::MoveMountFlags::MOVE_MOUNT_F_EMPTY_PATH,
    )?;

    Ok(())
}

// --- legacy mount(2) via nix ---

fn mount_overlay_legacy(
    lowerdir_spec: &str,
    upper_dir: &str,
    work_dir: &str,
    target: &str,
) -> io::Result<()> {
    let options = format!(
        "lowerdir={},upperdir={},workdir={}",
        lowerdir_spec, upper_dir, work_dir
    );

    nix::mount::mount(
        Some("overlay"),
        target,
        Some("overlay"),
        nix::mount::MsFlags::empty(),
        Some(options.as_str()),
    )
    .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

// --- public API ---

/// Mount overlayfs, auto-selecting the best available method.
pub fn mount_overlay(
    lowerdir_spec: &str,
    upper_dir: &str,
    work_dir: &str,
    target: &str,
) -> io::Result<()> {
    let lower_dirs: Vec<&str> = lowerdir_spec.split(':').collect();

    if check_new_mount_api() {
        match mount_overlay_new_api(&lower_dirs, upper_dir, work_dir, target) {
            Ok(()) => return Ok(()),
            Err(e) => {
                log::warn!(
                    "New mount API failed, falling back to legacy mount(2): {}",
                    e
                );
            }
        }
    }

    mount_overlay_legacy(lowerdir_spec, upper_dir, work_dir, target)
}
