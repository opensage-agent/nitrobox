// Package unpack provides UID-preserving layer extraction with whiteout conversion.
package unpack

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

const overflowID = 65534

// ExtractTarInUserns extracts a tar file inside a user namespace with full UID/GID mapping.
// Uses re-exec pattern instead of raw fork (Go's runtime is not fork-safe).
func ExtractTarInUserns(tarPath, dest string, outerUID, outerGID, subStart, subCount uint32) error {
	// Re-exec self with a special subcommand that does the extraction.
	// The child process is started with CLONE_NEWUSER via SysProcAttr.
	self := coreBinary()

	usernsPipeR, usernsPipeW, _ := os.Pipe()
	goPipeR, goPipeW, _ := os.Pipe()

	cmd := exec.Command(self, "_extract-worker")
	cmd.Stdin = nil
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.ExtraFiles = []*os.File{usernsPipeW, goPipeR} // fd 3 = usernsPipeW, fd 4 = goPipeR
	cmd.Env = append(os.Environ(),
		fmt.Sprintf("_NBX_TAR_PATH=%s", tarPath),
		fmt.Sprintf("_NBX_DEST=%s", dest),
		fmt.Sprintf("_NBX_MAX_ID=%d", subCount),
	)
	cmd.SysProcAttr = &unix.SysProcAttr{
		Cloneflags: unix.CLONE_NEWUSER,
	}

	if err := cmd.Start(); err != nil {
		usernsPipeR.Close()
		usernsPipeW.Close()
		goPipeR.Close()
		goPipeW.Close()
		return fmt.Errorf("start extract worker: %w", err)
	}
	usernsPipeW.Close()
	goPipeR.Close()

	// Wait for child to signal userns ready
	buf := make([]byte, 1)
	usernsPipeR.Read(buf)
	usernsPipeR.Close()

	// Set up UID/GID mapping
	mappingErr := setupIDMapping(cmd.Process.Pid, outerUID, outerGID, subStart, subCount)

	// Signal child to proceed
	goPipeW.Write([]byte("G"))
	goPipeW.Close()

	if mappingErr != nil {
		cmd.Process.Kill()
		cmd.Wait()
		return mappingErr
	}

	if err := cmd.Wait(); err != nil {
		return fmt.Errorf("layer extraction in userns failed: %w", err)
	}
	return nil
}

// ExtractWorker is the re-exec entry point for extraction inside a user namespace.
// Called as: nitrobox-core _extract-worker (with env vars and extra fds).
func ExtractWorker() {
	tarPath := os.Getenv("_NBX_TAR_PATH")
	dest := os.Getenv("_NBX_DEST")
	maxIDStr := os.Getenv("_NBX_MAX_ID")
	maxID := uint32(65536)
	fmt.Sscanf(maxIDStr, "%d", &maxID)

	// fd 3 = usernsPipeW, fd 4 = goPipeR (from ExtraFiles)
	usernsPipeW := os.NewFile(3, "usernsPipeW")
	goPipeR := os.NewFile(4, "goPipeR")

	// Signal parent that userns is ready
	usernsPipeW.Write([]byte("R"))
	usernsPipeW.Close()

	// Wait for UID mapping
	buf := make([]byte, 1)
	goPipeR.Read(buf)
	goPipeR.Close()

	if err := doExtract(tarPath, dest, maxID); err != nil {
		fmt.Fprintf(os.Stderr, "nitrobox: layer extraction failed: %v\n", err)
		os.Exit(2)
	}
	os.Exit(0)
}

// RmtreeInUserns removes a directory tree containing files with mapped UIDs.
func RmtreeInUserns(path string, outerUID, outerGID, subStart, subCount uint32) error {
	self := coreBinary()

	usernsPipeR, usernsPipeW, _ := os.Pipe()
	goPipeR, goPipeW, _ := os.Pipe()

	cmd := exec.Command(self, "_rmtree-worker")
	cmd.ExtraFiles = []*os.File{usernsPipeW, goPipeR}
	cmd.Env = append(os.Environ(), fmt.Sprintf("_NBX_RM_PATH=%s", path))
	cmd.SysProcAttr = &unix.SysProcAttr{
		Cloneflags: unix.CLONE_NEWUSER,
	}

	if err := cmd.Start(); err != nil {
		usernsPipeR.Close()
		usernsPipeW.Close()
		goPipeR.Close()
		goPipeW.Close()
		return err
	}
	usernsPipeW.Close()
	goPipeR.Close()

	buf := make([]byte, 1)
	usernsPipeR.Read(buf)
	usernsPipeR.Close()

	_ = setupIDMapping(cmd.Process.Pid, outerUID, outerGID, subStart, subCount)

	goPipeW.Write([]byte("G"))
	goPipeW.Close()

	cmd.Wait()
	return nil
}

// coreBinary returns the path to the nitrobox-core binary for re-exec.
// In c-shared mode, os.Executable() returns the Python interpreter, so we
// check NITROBOX_CORE_BIN env var first.
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

// closeInheritedStdio redirects stdout/stderr to /dev/null in forked children.
// When Go runs as a subprocess (Python's Popen), the child inherits the Popen
// stdout/stderr pipes. If we don't close them, Python's subprocess.run() blocks
// forever waiting for the pipe to close.
func closeInheritedStdio() {
	devnull, err := unix.Open("/dev/null", unix.O_WRONLY, 0)
	if err == nil {
		unix.Dup2(devnull, 1)
		unix.Dup2(devnull, 2)
		unix.Close(devnull)
	}
}

type pipe struct{ r, w int }

func makePipe() pipe {
	var fds [2]int
	unix.Pipe2(fds[:], unix.O_CLOEXEC)
	return pipe{r: fds[0], w: fds[1]}
}

func setupIDMapping(childPid int, outerUID, outerGID, subStart, subCount uint32) error {
	pidS := fmt.Sprintf("%d", childPid)
	uidS := fmt.Sprintf("%d", outerUID)
	gidS := fmt.Sprintf("%d", outerGID)
	subS := fmt.Sprintf("%d", subStart)
	cntS := fmt.Sprintf("%d", subCount)

	out, err := exec.Command("newuidmap", pidS, "0", uidS, "1", "1", subS, cntS).CombinedOutput()
	if err != nil {
		return fmt.Errorf("newuidmap failed: %s", string(out))
	}
	out, err = exec.Command("newgidmap", pidS, "0", gidS, "1", "1", subS, cntS).CombinedOutput()
	if err != nil {
		return fmt.Errorf("newgidmap failed: %s", string(out))
	}
	return nil
}

// doExtract is the child-side extraction logic.
// Reads the entire tar into memory first (matching Rust behavior), then parses.
// This is critical for FIFO sources where streaming tar.Reader can deadlock
// on partial reads.
func doExtract(tarPath, dest string, maxID uint32) error {
	f, err := os.Open(tarPath)
	if err != nil {
		return err
	}
	data, err := io.ReadAll(f)
	f.Close()
	if err != nil {
		return fmt.Errorf("read tar failed: %w", err)
	}

	var reader io.Reader
	if len(data) >= 2 && data[0] == 0x1f && data[1] == 0x8b {
		gz, err := gzip.NewReader(bytes.NewReader(data))
		if err != nil {
			return fmt.Errorf("gzip open failed: %w", err)
		}
		defer gz.Close()
		reader = gz
	} else {
		reader = bytes.NewReader(data)
	}

	return unpackTar(tar.NewReader(reader), dest, maxID)
}

func unpackTar(tr *tar.Reader, dest string, maxID uint32) error {
	type dirMtime struct {
		path  string
		mtime int64
	}
	var dirHeaders []dirMtime

	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}

		cleaned := filepath.Clean(hdr.Name)
		// Breakout check
		fullPath := filepath.Join(dest, cleaned)
		resolved := filepath.Clean(fullPath)
		if !strings.HasPrefix(resolved, filepath.Clean(dest)) {
			return fmt.Errorf("path breakout: %s is outside %s", hdr.Name, dest)
		}

		// Parent directory creation
		if parent := filepath.Dir(fullPath); parent != dest {
			if _, err := os.Stat(parent); err != nil {
				if err := os.MkdirAll(parent, 0o777); err != nil {
					return err
				}
				unix.Lchown(parent, 0, 0)
			}
		}

		uid := uint32(hdr.Uid)
		gid := uint32(hdr.Gid)
		if uid > maxID {
			uid = overflowID
		}
		if gid > maxID {
			gid = overflowID
		}
		mode := uint32(hdr.Mode & 0o7777)
		mtime := hdr.ModTime.Unix()

		fileName := filepath.Base(cleaned)

		// Skip device nodes
		if hdr.Typeflag == tar.TypeBlock || hdr.Typeflag == tar.TypeChar {
			continue
		}

		// Whiteout handling
		if strings.HasPrefix(fileName, ".wh.") {
			parent := filepath.Dir(fullPath)
			if fileName == ".wh..wh..opq" {
				unix.Setxattr(parent, "user.overlay.opaque", []byte("y"), 0)
			} else {
				originalName := fileName[4:]
				originalPath := filepath.Join(parent, originalName)
				if err := unix.Mknod(originalPath, unix.S_IFCHR, 0); err != nil {
					if err == unix.ENOTDIR {
						continue
					}
					// Fallback: xattr whiteout
					f, _ := os.Create(originalPath)
					if f != nil {
						f.Close()
					}
					unix.Setxattr(originalPath, "user.overlay.whiteout", []byte("y"), 0)
				} else {
					unix.Lchown(originalPath, int(uid), int(gid))
				}
			}
			continue
		}

		// Remove existing
		if info, err := os.Lstat(fullPath); err == nil {
			if info.IsDir() && cleaned == "." {
				continue
			}
			if !(info.IsDir() && hdr.Typeflag == tar.TypeDir) {
				os.Remove(fullPath)
				os.RemoveAll(fullPath)
			}
		}

		// Create entry — skip on permission/access errors (common for /proc, /sys in live containers)
		var createErr error
		switch hdr.Typeflag {
		case tar.TypeDir:
			if info, err := os.Lstat(fullPath); err != nil || !info.IsDir() {
				createErr = os.Mkdir(fullPath, os.FileMode(mode))
			}
		case tar.TypeReg, tar.TypeRegA:
			f, err := os.OpenFile(fullPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(mode))
			if err != nil {
				createErr = err
			} else {
				_, createErr = io.Copy(f, tr)
				f.Close()
			}
		case tar.TypeSymlink:
			linkTarget := hdr.Linkname
			if !filepath.IsAbs(linkTarget) {
				resolvedLink := filepath.Clean(filepath.Join(filepath.Dir(fullPath), linkTarget))
				if !strings.HasPrefix(resolvedLink, filepath.Clean(dest)) {
					createErr = fmt.Errorf("symlink breakout: %s -> %s", fullPath, linkTarget)
				}
			}
			if createErr == nil {
				createErr = os.Symlink(linkTarget, fullPath)
			}
		case tar.TypeLink:
			linkTarget := hdr.Linkname
			targetAbs := filepath.Join(dest, linkTarget)
			if !strings.HasPrefix(filepath.Clean(targetAbs), filepath.Clean(dest)) {
				createErr = fmt.Errorf("hardlink breakout: %s -> %s", fullPath, linkTarget)
			} else {
				createErr = os.Link(targetAbs, fullPath)
			}
		case tar.TypeFifo:
			createErr = unix.Mkfifo(fullPath, mode)
		case tar.TypeXGlobalHeader, tar.TypeXHeader:
			continue
		default:
			continue
		}
		if createErr != nil {
			// Skip permission errors (e.g. /proc files in live container tar)
			if os.IsPermission(createErr) {
				continue
			}
			// Skip "breakout" as fatal, everything else as non-fatal
			errStr := createErr.Error()
			if strings.Contains(errStr, "breakout") {
				return createErr
			}
			continue
		}

		// lchown
		unix.Lchown(fullPath, int(uid), int(gid))

		// chmod (skip symlinks)
		if hdr.Typeflag == tar.TypeLink {
			if info, err := os.Lstat(fullPath); err == nil && info.Mode()&os.ModeSymlink == 0 {
				unix.Chmod(fullPath, mode)
			}
		} else if hdr.Typeflag != tar.TypeSymlink {
			unix.Chmod(fullPath, mode)
		}

		// chtimes
		ts := []unix.Timespec{
			{Sec: mtime, Nsec: 0},
			{Sec: mtime, Nsec: 0},
		}
		if hdr.Typeflag == tar.TypeSymlink {
			unix.UtimesNanoAt(unix.AT_FDCWD, fullPath, ts, unix.AT_SYMLINK_NOFOLLOW)
		} else if hdr.Typeflag == tar.TypeLink {
			if info, err := os.Lstat(fullPath); err == nil && info.Mode()&os.ModeSymlink == 0 {
				unix.UtimesNanoAt(unix.AT_FDCWD, fullPath, ts, 0)
			}
		} else if hdr.Typeflag == tar.TypeDir {
			dirHeaders = append(dirHeaders, dirMtime{path: fullPath, mtime: mtime})
		} else {
			unix.UtimesNanoAt(unix.AT_FDCWD, fullPath, ts, 0)
		}

		// PAX xattrs
		for key, val := range hdr.PAXRecords {
			if xattrKey, ok := strings.CutPrefix(key, "SCHILY.xattr."); ok {
				_ = unix.Lsetxattr(fullPath, xattrKey, []byte(val), 0)
			}
		}
	}

	// Deferred directory mtime
	for _, dh := range dirHeaders {
		ts := []unix.Timespec{
			{Sec: dh.mtime, Nsec: 0},
			{Sec: dh.mtime, Nsec: 0},
		}
		unix.UtimesNanoAt(unix.AT_FDCWD, dh.path, ts, 0)
	}

	return nil
}
