# ops/ · 数据保全与进程保活运维接线

安全根运维模块。**只读**正式数据；写只落 `ops/` 与 `backups/`。复用既有成熟件，不重复造轮子。

## 复用清单（不重复造轮子）

| 能力 | 复用的现成件 | 本模块只做 |
|---|---|---|
| 一致性副本 | **SQLite 官方在线 backup API**（`sqlite3.Connection.backup`） | 逐库调用 + 出证据 |
| 长驻启动 | `后端实现/spawn_detached.py`（setsid + 双 fork + PID 文件） | 封装 `ops_common.spawn_detached` |
| 底座校验 | `bootstrap_portable.sh`（热层 quick_check + data_hub SHA + 冷层 catalog 对账） | 不重写，直接复用 |
| 云传输 | 系统 **rclone** | 只读状态核查，不做删除类动作 |
| 调度 | 系统 **launchd** | 出配置模板与启用/卸载说明 |

## 模块

| 文件 | 作用 |
|---|---|
| `ops_common.py` | 公共库：容量（statvfs）、SHA、在线 backup、quick_check、水位、pgrep/lsof、spawn_detached 封装 |
| `hot_snapshot.py` | 热库 + paper_3a~3d 在线快照 → `backups/hot/<batch-id>/`（quick_check + SHA + 大小 + 水位 + manifest + 锁） |
| `keepalive_check.py` | 真实 PID / kill -0 / lsof FD / 目标路径 / 日志增长 / pid 文件一致性 |
| `protect_immutable.py` | 防删清单 + `chmod a-w` + `chflags uchg`（只文件、跳新改动件）+ 前后证据 |
| `cloud_status.py` | rclone 安装/remote/容量状态（只读，不读密钥） |
| `version_manifest.py` | 冷层 `_state.json`/`_catalog.json` 配对版本 manifest（本地，不发布） |
| `schedule/` | launchd 配置模板 + 启用/卸载说明（**当前未启用**） |
| `config/ops_config.json` | 路径 / 保留 12 GiB / 14 天 / 频率 / 进程 / 云目标 |

## 常用命令

```sh
PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
OPS="/Users/boos/WorkBuddy AI/加密货币趋势判断系统/ops"

$PY "$OPS/hot_snapshot.py" --dry-run          # 容量与计划
$PY "$OPS/hot_snapshot.py"                     # 实际快照
$PY "$OPS/keepalive_check.py" --json /tmp/ka.json
$PY "$OPS/protect_immutable.py" --plan
$PY "$OPS/cloud_status.py" --json /tmp/cloud.json
$PY "$OPS/version_manifest.py"
```

## 边界与诚实声明

- `chflags uchg` / `chmod a-w` 只防**误删/误改**；**同一拥有者仍可撤销**，不构成账号级不可绕过的只读隔离。强隔离需**目录 ACL / 独立账号**（见 `01_实施回执.md`「成熟账号/目录 ACL 方案」）。
- 只对**文件**加旗标，**不给目录加 uchg**（否则阻断新增文件）；**不锁**热库 / WAL / SHM / 活动状态清单 / 下载临时件 / 正在重建的分片。
- 跨库快照**不天然同一事务**：每库记录自身 backup 时刻与水位截点，manifest 显式声明。
- 云端动作**禁用** `sync` / `delete` / `purge` / `move`；本轮**无任何上传**。
