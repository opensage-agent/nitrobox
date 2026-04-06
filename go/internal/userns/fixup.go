// Package userns provides user namespace helpers for sandbox cleanup.
package userns

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"

	"golang.org/x/sys/unix"
)

// coreBinary returns the path to the nitrobox-core binary for re-exec.
func coreBinary() string {
	if p := os.Getenv("NITROBOX_CORE_BIN"); p != "" {
		return p
	}
	self, err := os.Executable()
	if err != nil {
		return "nitrobox-core"
	}
	return self
}

// FixupDirForDelete enters a user namespace and recursively chmod+chown
// a directory so the host user can rmtree it. Uses re-exec pattern.
func FixupDirForDelete(usernsPid int, dirPath string) (uint32, error) {
	self := coreBinary()

	cmd := exec.Command(self, "_fixup-worker")
	cmd.Env = append(os.Environ(),
		fmt.Sprintf("_NBX_USERNS_PID=%d", usernsPid),
		fmt.Sprintf("_NBX_DIR_PATH=%s", dirPath),
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return 0, fmt.Errorf("userns fixup failed: %w", err)
	}
	return 0, nil
}

// FixupWorker is the re-exec entry point for fixup inside a user namespace.
func FixupWorker() {
	usernsPid := 0
	fmt.Sscanf(os.Getenv("_NBX_USERNS_PID"), "%d", &usernsPid)
	dirPath := os.Getenv("_NBX_DIR_PATH")

	nsPath := fmt.Sprintf("/proc/%d/ns/user", usernsPid)
	nsFd, err := unix.Open(nsPath, unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open userns fd: %v\n", err)
		os.Exit(1)
	}

	if err := unix.Setns(nsFd, unix.CLONE_NEWUSER); err != nil {
		unix.Close(nsFd)
		fmt.Fprintf(os.Stderr, "setns: %v\n", err)
		os.Exit(1)
	}
	unix.Close(nsFd)

	walkFixup(dirPath)
	os.Exit(0)
}

func walkFixup(dir string) {
	fixupEntry(dir)

	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		path := filepath.Join(dir, entry.Name())
		if entry.IsDir() {
			walkFixup(path)
		} else {
			fixupEntry(path)
		}
	}
}

func fixupEntry(path string) {
	_ = unix.Lchown(path, 0, 0)

	var st unix.Stat_t
	if unix.Lstat(path, &st) == nil {
		mode := uint32(0o666)
		if st.Mode&syscall.S_IFDIR != 0 {
			mode = 0o777
		}
		_ = unix.Chmod(path, mode)
	}
}
