package security

import (
	"fmt"
	"os"
	"unsafe"

	"golang.org/x/sys/unix"
)

// Landlock ABI constants.
const (
	landlockCreateRulesetVersion uint32 = 1 << 0

	landlockAccessFsExecute    uint64 = 1 << 0
	landlockAccessFsWriteFile  uint64 = 1 << 1
	landlockAccessFsReadFile   uint64 = 1 << 2
	landlockAccessFsReadDir    uint64 = 1 << 3
	landlockAccessFsRemoveDir  uint64 = 1 << 4
	landlockAccessFsRemoveFile uint64 = 1 << 5
	landlockAccessFsMakeChar   uint64 = 1 << 6
	landlockAccessFsMakeDir    uint64 = 1 << 7
	landlockAccessFsMakeReg    uint64 = 1 << 8
	landlockAccessFsMakeSock   uint64 = 1 << 9
	landlockAccessFsMakeFifo   uint64 = 1 << 10
	landlockAccessFsMakeBlock  uint64 = 1 << 11
	landlockAccessFsMakeSym    uint64 = 1 << 12
	landlockAccessFsRefer      uint64 = 1 << 13
	landlockAccessFsTruncate   uint64 = 1 << 14
	landlockAccessFsIoctlDev   uint64 = 1 << 15

	landlockAccessNetBindTCP    uint64 = 1 << 0
	landlockAccessNetConnectTCP uint64 = 1 << 1

	landlockScopeAbstractUnixSocket uint64 = 1 << 0
	landlockScopeSignal             uint64 = 1 << 1

	landlockRulePathBeneath uint32 = 1
	landlockRuleNetPort     uint32 = 2

	landlockRestrictSelfLogNewExecOn uint32 = 1 << 1
	landlockRestrictSelfTsync        uint32 = 1 << 3

	fsRead uint64 = landlockAccessFsExecute | landlockAccessFsReadFile | landlockAccessFsReadDir

	fsWriteV1 uint64 = landlockAccessFsWriteFile |
		landlockAccessFsRemoveDir |
		landlockAccessFsRemoveFile |
		landlockAccessFsMakeChar |
		landlockAccessFsMakeDir |
		landlockAccessFsMakeReg |
		landlockAccessFsMakeSock |
		landlockAccessFsMakeFifo |
		landlockAccessFsMakeBlock |
		landlockAccessFsMakeSym

	sysLandlockCreateRuleset = 444
	sysLandlockAddRule       = 445
	sysLandlockRestrictSelf  = 446
)

type landlockRulesetAttr struct {
	HandledAccessFs  uint64
	HandledAccessNet uint64
	Scoped           uint64
}

type landlockPathBeneathAttr struct {
	AllowedAccess uint64
	ParentFd      int32
}

type landlockNetPortAttr struct {
	AllowedAccess uint64
	Port          uint64
}

// LandlockABIVersion returns the Landlock ABI version (0 if unsupported).
func LandlockABIVersion() uint32 {
	r1, _, errno := unix.Syscall(sysLandlockCreateRuleset, 0, 0, uintptr(landlockCreateRulesetVersion))
	if errno != 0 {
		return 0
	}
	unix.Close(int(r1))
	return uint32(r1)
}

func fsWriteMask(abi uint32) uint64 {
	mask := fsWriteV1
	if abi >= 2 {
		mask |= landlockAccessFsRefer
	}
	if abi >= 3 {
		mask |= landlockAccessFsTruncate
	}
	if abi >= 5 {
		mask |= landlockAccessFsIoctlDev
	}
	return mask
}

func addPathRule(rulesetFd int, path string, access uint64) error {
	if _, err := os.Stat(path); err != nil {
		return nil // skip non-existent paths
	}
	flags := unix.O_PATH | unix.O_CLOEXEC
	info, err := os.Stat(path)
	if err == nil && info.IsDir() {
		flags |= unix.O_DIRECTORY
	}
	fd, err := unix.Open(path, flags, 0)
	if err != nil {
		return err
	}
	defer unix.Close(fd)

	rule := landlockPathBeneathAttr{
		AllowedAccess: access,
		ParentFd:      int32(fd),
	}
	_, _, errno := unix.Syscall6(sysLandlockAddRule,
		uintptr(rulesetFd),
		uintptr(landlockRulePathBeneath),
		uintptr(unsafe.Pointer(&rule)),
		0, 0, 0,
	)
	if errno != 0 {
		// Non-fatal for path rules
		return nil
	}
	return nil
}

// ApplyLandlock applies filesystem + network restrictions.
func ApplyLandlock(readPaths, writePaths []string, allowedTCPPorts []uint16, strict bool) (bool, error) {
	abi := LandlockABIVersion()
	if abi == 0 {
		msg := "Landlock not available (kernel < 5.13)"
		if strict {
			return false, fmt.Errorf("%s", msg)
		}
		return false, nil
	}

	fsWrite := fsWriteMask(abi)
	writeOnlyMask := fsWrite &^ fsRead

	var handledFS uint64
	if len(readPaths) > 0 {
		handledFS |= fsRead
	}
	if len(writePaths) > 0 {
		if len(readPaths) == 0 {
			handledFS |= writeOnlyMask
		} else {
			handledFS |= fsWrite
		}
	}

	var handledNet uint64
	if len(allowedTCPPorts) > 0 && abi >= 4 {
		handledNet = landlockAccessNetBindTCP | landlockAccessNetConnectTCP
	}

	var scoped uint64
	if abi >= 6 {
		scoped = landlockScopeAbstractUnixSocket | landlockScopeSignal
	}

	attr := landlockRulesetAttr{
		HandledAccessFs:  handledFS,
		HandledAccessNet: handledNet,
		Scoped:           scoped,
	}
	r1, _, errno := unix.Syscall(sysLandlockCreateRuleset,
		uintptr(unsafe.Pointer(&attr)),
		uintptr(unsafe.Sizeof(attr)),
		0,
	)
	if errno != 0 {
		msg := fmt.Sprintf("landlock_create_ruleset failed: %v", errno)
		if strict {
			return false, fmt.Errorf("%s", msg)
		}
		return false, nil
	}
	rulesetFd := int(r1)
	defer unix.Close(rulesetFd)

	// Add path rules
	for _, path := range readPaths {
		if err := addPathRule(rulesetFd, path, fsRead&handledFS); err != nil {
			return false, err
		}
	}
	for _, path := range writePaths {
		if err := addPathRule(rulesetFd, path, (fsWrite|fsRead)&handledFS); err != nil {
			return false, err
		}
	}

	// Add TCP port rules
	for _, port := range allowedTCPPorts {
		if abi < 4 {
			break
		}
		rule := landlockNetPortAttr{
			AllowedAccess: landlockAccessNetConnectTCP,
			Port:          uint64(port),
		}
		unix.Syscall6(sysLandlockAddRule,
			uintptr(rulesetFd),
			uintptr(landlockRuleNetPort),
			uintptr(unsafe.Pointer(&rule)),
			0, 0, 0,
		)
	}

	// NO_NEW_PRIVS
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return false, err
	}

	// Restrict self
	var restrictFlags uint32
	if abi >= 7 {
		restrictFlags |= landlockRestrictSelfLogNewExecOn
	}
	if abi >= 8 {
		restrictFlags |= landlockRestrictSelfTsync
	}

	_, _, errno = unix.Syscall(sysLandlockRestrictSelf, uintptr(rulesetFd), uintptr(restrictFlags), 0)
	if errno != 0 && (restrictFlags&landlockRestrictSelfTsync) != 0 {
		restrictFlags &^= landlockRestrictSelfTsync
		_, _, errno = unix.Syscall(sysLandlockRestrictSelf, uintptr(rulesetFd), uintptr(restrictFlags), 0)
	}
	if errno != 0 {
		msg := fmt.Sprintf("landlock_restrict_self failed: %v", errno)
		if strict {
			return false, fmt.Errorf("%s", msg)
		}
		return false, nil
	}

	return true, nil
}
