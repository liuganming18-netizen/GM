#!/bin/sh
# 热层在线快照 · 调度入口（北京时间 00:20 / 08:20 / 16:20）
# 只做：SQLite 在线 backup → quick_check → SHA → manifest。
# 云上传默认关闭（OPS_CLOUD_ENABLED=0）；未过前置门禁不得开启。
set -eu
OPS="/Users/boos/Desktop/威科夫因子共振趋势/ops"
PY="/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3"
export PYTHONDONTWRITEBYTECODE=1
BATCH="$(date -u +%Y%m%dT%H%M%SZ)"
"$PY" "$OPS/hot_snapshot.py" --batch-id "$BATCH" || exit $?
if [ "${OPS_CLOUD_ENABLED:-0}" = "1" ]; then
  echo "[云] OPS_CLOUD_ENABLED=1 —— 上传接线须由人类批准且目标/授权/容量已确认；当前未实现自动上传。"
fi
echo "[done] hotsnapshot $BATCH"
