#!/bin/sh
# 启用调度（macOS launchd）· ⚠ 前置门禁未通过前**不要运行**。
# 前置：① 首批本地在线快照已核验通过；② 云端目标/授权/容量已确认；③ Codex 独立审核通过。
# 用法：OPS_ENABLE_SCHEDULE=1 sh install_launchd.sh
set -eu
if [ "${OPS_ENABLE_SCHEDULE:-0}" != "1" ]; then
  echo "未启用：请先满足前置门禁，然后 OPS_ENABLE_SCHEDULE=1 sh install_launchd.sh"
  exit 0
fi
SRC="/Users/boos/WorkBuddy AI/加密货币趋势判断系统/ops/schedule"
DST="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
mkdir -p "$DST" "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/backups/logs"
for L in com.crypto.ops.hotsnapshot com.crypto.ops.parquetmanifest com.crypto.ops.rawverify com.crypto.ops.gitpatrol; do
  cp "$SRC/$L.plist" "$DST/$L.plist"
  launchctl bootout "gui/$UID_/$L" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$DST/$L.plist"
  echo "[ok] 已加载 $L"
done
echo "[done] 调度已启用（北京时间：热快照 00:20/08:20/16:20；parquet 01:30；raw 周日03:00；git 00:30）"
