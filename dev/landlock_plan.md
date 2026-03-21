# Landlock 路径/端口限制实现计划

**状态：已实现** ✓ （2026-03-21）

## API

```python
SandboxConfig(
    writable_paths=["/workspace", "/tmp"],   # 只有这些路径可写
    readable_paths=["/usr", "/lib"],          # 只有这些路径可读（None=不限制读）
    allowed_ports=[80, 443],                 # 只允许连接这些 TCP 端口（None=不限制）
)
```

参数在 `SandboxConfig` 中声明（`base.py`），通过 `adl-seccomp` 二进制执行 Landlock 限制。
Rootful 和 rootless 模式均支持。Reset 后 Landlock 自动重新应用。

## 实现方案

### 方案：在 adl-seccomp.c 中实现（推荐）

跟 seccomp BPF 一样的模式：Python 侧写配置文件，C 二进制读取并用 raw syscall 应用。

#### 1. Python 侧（rootful.py / rootless.py）

写 `/tmp/.adl_landlock` 到 rootfs upper dir：

```
# 格式：一行一条规则
R /usr
R /lib
R /etc
W /workspace
W /tmp
P 80
P 443
```

跟 `.adl_seccomp.bpf` 和 `.adl_readonly` marker 一样的方式。

#### 2. adl-seccomp.c

在现有流程中（cap drop 和 seccomp 之间）加 Landlock：

```c
/* 新增 syscall numbers */
#define NR_landlock_create_ruleset 444
#define NR_landlock_add_rule      445
#define NR_landlock_restrict_self 446

/* Landlock constants */
#define LANDLOCK_CREATE_RULESET_VERSION  (1 << 0)
#define LANDLOCK_ACCESS_FS_EXECUTE       (1 << 0)
#define LANDLOCK_ACCESS_FS_WRITE_FILE    (1 << 1)
#define LANDLOCK_ACCESS_FS_READ_FILE     (1 << 2)
#define LANDLOCK_ACCESS_FS_READ_DIR      (1 << 3)
/* ... (see security.py for full list) */
#define LANDLOCK_RULE_PATH_BENEATH  1
#define LANDLOCK_RULE_NET_PORT      2

/* 流程 */
1. open("/tmp/.adl_landlock")
2. 逐行解析 R/W/P 前缀和路径/端口
3. landlock_create_ruleset() 创建规则集
4. 对每个路径：open(O_PATH) → landlock_add_rule(RULE_PATH_BENEATH)
5. 对每个端口：landlock_add_rule(RULE_NET_PORT)
6. prctl(PR_SET_NO_NEW_PRIVS, 1)（seccomp 已经做了这步）
7. landlock_restrict_self()
8. close(fd), unlink("/tmp/.adl_landlock")
```

需要在 C 中实现的：
- 简单的行解析（逐字符读取，识别 R/W/P 前缀）
- O_PATH open（`sc2(NR_open, path, O_PATH | O_CLOEXEC)`，需要加 `#define NR_openat 257`）
- 三个 Landlock syscall 的 struct 定义

#### 3. 注意事项

- **ABI 版本检测**：先调 `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`
  获取 ABI 版本。不同版本支持不同的 access flags（security.py 中有完整映射）。
  ABI < 1 说明 kernel 不支持，静默跳过。
- **graceful fallback**：如果 `/tmp/.adl_landlock` 不存在，跳过（跟 seccomp BPF 一样）。
  如果 Landlock syscall 失败（老 kernel），静默跳过不阻塞。
- **顺序**：在 seccomp 之前应用（seccomp 会阻止 landlock syscall 本身）。
  当前 adl-seccomp.c 的顺序：
  1. Mount /proc + /dev
  2. Cap drop
  3. Mask paths
  4. Read-only paths
  5. Read-only rootfs
  6. **← Landlock 在这里**
  7. Seccomp BPF
  8. exec shell
- **网络端口限制**：需要 Landlock ABI v4+（kernel 6.7+）。ABI < 4 时端口规则静默跳过。

#### 4. 测试

- `test_writable_paths`：设 `writable_paths=["/workspace"]`，验证写 `/workspace/x` 成功，写 `/tmp/x` 失败
- `test_readable_paths`：设 `readable_paths=["/workspace"]`，验证读 `/usr/bin/ls` 失败
- `test_allowed_ports`：设 `allowed_ports=[80]`，验证连 80 成功，连 8080 失败
- `test_landlock_not_available`：mock kernel < 5.13，验证 graceful skip

#### 5. 工作量

- adl-seccomp.c：~80 行（解析 + syscall）
- Python 侧 wire-up：~20 行（写配置文件）
- 测试：~40 行
- 重新编译 adl-seccomp binary

## 参考

- `security.py` 中的 `apply_landlock()` — 完整的 Python/ctypes 实现，可作为 C 实现的参考
- kernel 文档：`ref/Linux/Documentation/userspace-api/landlock.rst`
- ABI 版本 → access flags 映射：`security.py` 中的 `_fs_write_mask(abi)`
