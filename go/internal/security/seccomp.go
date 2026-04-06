// Package security provides seccomp-bpf, capability drop, and Landlock.
package security

import (
	"encoding/binary"
	"unsafe"

	"golang.org/x/sys/unix"
)

// BPF instruction opcodes.
const (
	bpfLD   uint16 = 0x00
	bpfW    uint16 = 0x00
	bpfABS  uint16 = 0x20
	bpfJMP  uint16 = 0x05
	bpfJEQ  uint16 = 0x10
	bpfJSET uint16 = 0x40
	bpfK    uint16 = 0x00
	bpfRET  uint16 = 0x06
)

// Seccomp return values.
const (
	seccompRetAllow       uint32 = 0x7FFF_0000
	seccompRetErrno       uint32 = 0x0005_0000
	seccompRetKillProcess uint32 = 0x8000_0000
	seccompRetEnosys      uint32 = seccompRetErrno | 0x26 // ENOSYS = 38
)

const (
	auditArchX86_64 uint32 = 0xC000_003E
	cloneNSFlags    uint32 = 0x0002_0000 | 0x0400_0000 | 0x0800_0000 | 0x1000_0000 | 0x2000_0000 | 0x4000_0000 | 0x0000_0080
	tiocsti         uint32 = 0x5412
)

// x86_64 blocked syscalls (sorted by number for stable output).
var blockedSyscalls = []uint32{
	155, // pivot_root
	163, // acct
	165, // mount
	166, // umount2
	167, // swapon
	168, // swapoff
	169, // reboot
	170, // sethostname
	171, // setdomainname
	172, // iopl
	173, // ioperm
	175, // init_module
	176, // delete_module
	246, // kexec_load
	248, // add_key
	249, // request_key
	250, // keyctl
	272, // unshare
	298, // perf_event_open
	303, // name_to_handle_at
	304, // open_by_handle_at
	308, // setns
	310, // process_vm_readv
	311, // process_vm_writev
	313, // finit_module
	320, // kexec_file_load
	321, // bpf
	323, // userfaultfd
	425, // io_uring_setup
	426, // io_uring_enter
	427, // io_uring_register
}

const (
	cloneNr  uint32 = 56
	clone3Nr uint32 = 435
	ioctlNr  uint32 = 16
)

// sock_filter is the BPF instruction format (matches kernel struct sock_filter).
type sockFilter struct {
	Code uint16
	Jt   uint8
	Jf   uint8
	K    uint32
}

func bpfStmt(code uint16, k uint32) sockFilter {
	return sockFilter{Code: code, K: k}
}

func bpfJump(code uint16, k uint32, jt, jf uint8) sockFilter {
	return sockFilter{Code: code, Jt: jt, Jf: jf, K: k}
}

// BuildSeccompBPF generates seccomp BPF bytecode identical to the Rust version.
func BuildSeccompBPF() []byte {
	insns := make([]sockFilter, 0, 64)

	// 1. Arch check
	insns = append(insns, bpfStmt(bpfLD|bpfW|bpfABS, 4))
	insns = append(insns, bpfJump(bpfJMP|bpfJEQ|bpfK, auditArchX86_64, 1, 0))
	insns = append(insns, bpfStmt(bpfRET|bpfK, seccompRetKillProcess))

	// 2. clone(2): allow threads, block namespace creation
	insns = append(insns, bpfStmt(bpfLD|bpfW|bpfABS, 0))
	insns = append(insns, bpfJump(bpfJMP|bpfJEQ|bpfK, cloneNr, 0, 4))
	insns = append(insns, bpfStmt(bpfLD|bpfW|bpfABS, 16))
	insns = append(insns, bpfJump(bpfJMP|bpfJSET|bpfK, cloneNSFlags, 0, 1))
	insns = append(insns, bpfStmt(bpfRET|bpfK, seccompRetErrno|1))
	insns = append(insns, bpfStmt(bpfLD|bpfW|bpfABS, 0))

	// 3. clone3 → ENOSYS
	insns = append(insns, bpfJump(bpfJMP|bpfJEQ|bpfK, clone3Nr, 0, 1))
	insns = append(insns, bpfStmt(bpfRET|bpfK, seccompRetEnosys))

	// 4. ioctl(TIOCSTI)
	insns = append(insns, bpfJump(bpfJMP|bpfJEQ|bpfK, ioctlNr, 0, 4))
	insns = append(insns, bpfStmt(bpfLD|bpfW|bpfABS, 16))
	insns = append(insns, bpfJump(bpfJMP|bpfJEQ|bpfK, tiocsti, 0, 1))
	insns = append(insns, bpfStmt(bpfRET|bpfK, seccompRetErrno|1))
	insns = append(insns, bpfStmt(bpfLD|bpfW|bpfABS, 0))

	// 5. Blocklist — syscall numbers are already sorted
	n := len(blockedSyscalls)
	for i, nr := range blockedSyscalls {
		insns = append(insns, bpfJump(bpfJMP|bpfJEQ|bpfK, nr, uint8(n-i), 0))
	}
	insns = append(insns, bpfStmt(bpfRET|bpfK, seccompRetAllow))
	insns = append(insns, bpfStmt(bpfRET|bpfK, seccompRetErrno|1))

	// Serialize to bytes (native endian, matching kernel ABI)
	buf := make([]byte, len(insns)*8) // sizeof(sock_filter) = 8
	for i, insn := range insns {
		off := i * 8
		binary.NativeEndian.PutUint16(buf[off:], insn.Code)
		buf[off+2] = insn.Jt
		buf[off+3] = insn.Jf
		binary.NativeEndian.PutUint32(buf[off+4:], insn.K)
	}
	return buf
}

// ApplySeccompFilter installs the seccomp-bpf filter.
func ApplySeccompFilter() error {
	bpfBytes := BuildSeccompBPF()
	nInsns := len(bpfBytes) / 8

	// sock_fprog struct
	type sockFprog struct {
		Len    uint16
		_      [6]byte // padding to align filter pointer
		Filter *sockFilter
	}
	prog := sockFprog{
		Len:    uint16(nInsns),
		Filter: (*sockFilter)(unsafe.Pointer(&bpfBytes[0])),
	}

	// NO_NEW_PRIVS
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return err
	}

	// Install filter
	_, _, errno := unix.Syscall(unix.SYS_PRCTL,
		uintptr(unix.PR_SET_SECCOMP),
		uintptr(2), // SECCOMP_MODE_FILTER
		uintptr(unsafe.Pointer(&prog)),
	)
	if errno != 0 {
		return errno
	}
	return nil
}
