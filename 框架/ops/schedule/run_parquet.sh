#!/bin/sh
# 冷层 catalog/state 配对版本 manifest · 调度入口（北京时间 01:30）
# 前提：归档/水位已完成。本脚本只做本地配对快照 + manifest，不做云动作。
set -eu
OPS="/Users/boos/WorkBuddy AI/加密货币趋势判断系统/ops"
PY="/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3"
export PYTHONDONTWRITEBYTECODE=1
BATCH="$(date -u +%Y%m%dT%H%M%SZ)"
"$PY" "$OPS/version_manifest.py" --batch-id "$BATCH" || exit $?
echo "[done] parquet manifest $BATCH"
