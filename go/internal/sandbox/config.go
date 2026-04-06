// Package sandbox implements the sandbox spawn chain:
// fork → unshare → mount → pivot_root/chroot → security → exec.
package sandbox

// SpawnConfig mirrors the Rust SandboxSpawnConfig.
type SpawnConfig struct {
	Rootfs           string            `json:"rootfs"`
	Shell            string            `json:"shell"`
	WorkingDir       string            `json:"working_dir"`
	Env              map[string]string `json:"env"`
	Rootful          bool              `json:"rootful"`
	LowerdirSpec     *string           `json:"lowerdir_spec"`
	UpperDir         *string           `json:"upper_dir"`
	WorkDir          *string           `json:"work_dir"`
	Userns           bool              `json:"userns"`
	NetIsolate       bool              `json:"net_isolate"`
	NetNs            *string           `json:"net_ns"`
	SharedUserns     *string           `json:"shared_userns"`
	SubuidRange      *[3]uint32        `json:"subuid_range"` // [outer_uid, sub_start, sub_count]
	Seccomp          bool              `json:"seccomp"`
	CapAdd           []uint32          `json:"cap_add"`
	CapDrop          []uint32          `json:"cap_drop"`
	Hostname         *string           `json:"hostname"`
	ReadOnly         bool              `json:"read_only"`
	LandlockReadPaths  []string        `json:"landlock_read_paths"`
	LandlockWritePaths []string        `json:"landlock_write_paths"`
	LandlockPorts    []uint16          `json:"landlock_ports"`
	LandlockStrict   bool              `json:"landlock_strict"`
	Volumes          []string          `json:"volumes"`
	Devices          []string          `json:"devices"`
	ShmSize          *uint64           `json:"shm_size"`
	TmpfsMounts      []string          `json:"tmpfs_mounts"`
	CgroupPath       *string           `json:"cgroup_path"`
	Entrypoint       []string          `json:"entrypoint"`
	Tty              bool              `json:"tty"`
	PortMap          []string          `json:"port_map"`
	PastaBin         *string           `json:"pasta_bin"`
	Ipv6             bool              `json:"ipv6"`
	EnvDir           *string           `json:"env_dir"`
	VmMode           bool              `json:"vm_mode"`
}

// SpawnResult mirrors the Rust PySpawnResult.
type SpawnResult struct {
	Pid          int  `json:"pid"`
	StdinFd      int  `json:"stdin_fd"`
	StdoutFd     int  `json:"stdout_fd"`
	SignalRFd    int  `json:"signal_r_fd"`
	SignalWFdNum int  `json:"signal_w_fd_num"`
	MasterFd     *int `json:"master_fd"`
	Pidfd        *int `json:"pidfd"`
	ErrRFd       int  `json:"err_r_fd"`
}
