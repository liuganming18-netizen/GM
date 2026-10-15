# ops/schedule · 备份调度（macOS launchd）

复用系统自带 launchd，**不自造调度系统**，不依赖任何聊天窗口存活。

## 频率（北京时间 Asia/Shanghai）

| Label | 时间 | 动作 |
|---|---|---|
| `com.crypto.ops.hotsnapshot` | 00:20 / 08:20 / 16:20 | 热库 + paper 账本在线快照（`hot_snapshot.py`） |
| `com.crypto.ops.parquetmanifest` | 01:30 | 冷层 catalog/state 配对版本 manifest（`version_manifest.py`），前提为归档/水位已完成 |
| `com.crypto.ops.rawverify` | 周日 03:00 | 原始 ZIP/OI 防删旗标巡检 + 计数（`protect_immutable.py --mode verify`） |
| `com.crypto.ops.gitpatrol` | 00:30 | Git 候选/验收 SHA 巡检（**只检查，不 push/main**） |

## 前置门禁（未通过前不得启用）

1. 首批本地在线快照已核验通过（quick_check=ok、SHA 记录、容量护栏满足）。
2. 云端目标账号/目录/授权/容量已由人类确认（缺则不得接线）。
3. Codex 独立审核通过。

## 启用 / 卸载

```sh
# 启用（前置门禁通过后）
OPS_ENABLE_SCHEDULE=1 sh "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/ops/schedule/install_launchd.sh"

# 卸载（可逆）
sh "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/ops/schedule/uninstall_launchd.sh"
```

## 当前状态

**未启用。** 本轮只产出配置模板与启动/卸载说明；`RunAtLoad=false`，未被 `launchctl` 加载。
