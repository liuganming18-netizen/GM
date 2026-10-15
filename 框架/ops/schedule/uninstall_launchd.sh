#!/bin/sh
# 卸载调度（可逆回滚）· 只移除本任务 4 个 label，不动其它服务。
set -eu
DST="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
for L in com.crypto.ops.hotsnapshot com.crypto.ops.parquetmanifest com.crypto.ops.rawverify com.crypto.ops.gitpatrol; do
  launchctl bootout "gui/$UID_/$L" 2>/dev/null || true
  rm -f "$DST/$L.plist"
  echo "[ok] 已卸载 $L"
done
echo "[done] 调度已卸载"
