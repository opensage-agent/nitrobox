package security

import (
	"os"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// Docker default: capability numbers to KEEP.
var dockerDefaultCaps = []uint32{
	0,  // CAP_CHOWN
	1,  // CAP_DAC_OVERRIDE
	3,  // CAP_FOWNER
	4,  // CAP_FSETID
	5,  // CAP_KILL
	6,  // CAP_SETGID
	7,  // CAP_SETUID
	8,  // CAP_SETPCAP
	10, // CAP_NET_BIND_SERVICE
	18, // CAP_SYS_CHROOT
	27, // CAP_MKNOD
	29, // CAP_AUDIT_WRITE
	31, // CAP_SETFCAP
}

func capLastCap() uint32 {
	data, err := os.ReadFile("/proc/sys/kernel/cap_last_cap")
	if err != nil {
		return 41
	}
	v, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 32)
	if err != nil {
		return 41
	}
	return uint32(v)
}

func contains(slice []uint32, val uint32) bool {
	for _, v := range slice {
		if v == val {
			return true
		}
	}
	return false
}

// DropCapabilities drops all capabilities except Docker defaults + extraKeep.
// Caps in extraDrop are removed even if in defaults. Returns count dropped.
func DropCapabilities(extraKeep, extraDrop []uint32) (uint32, error) {
	lastCap := capLastCap()
	var dropped uint32

	for capNum := uint32(0); capNum <= lastCap; capNum++ {
		inDefaults := contains(dockerDefaultCaps, capNum)
		inKeep := contains(extraKeep, capNum)
		inDrop := contains(extraDrop, capNum)

		if inDrop || !(inDefaults || inKeep) {
			err := unix.Prctl(unix.PR_CAPBSET_DROP, uintptr(capNum), 0, 0, 0)
			if err == nil {
				dropped++
			}
		}
	}
	return dropped, nil
}
