#!/bin/sh
# 冷层 catalog/state 配对版本 manifest · 调度入口（北京时间 01:30）
# 前提：归档/水位已完成。本脚本只做本地配对快照 + manifest，不做云动作。
# W1（2026-10-16）：OPS 由「脚本自身位置」推导（本文件在 <工程根>/框架/ops/schedule/），
# 不再写死工程外绝对路径。解释器可用 OPS_PYTHON 覆盖。
set -eu
OPS="$(cd "$(dirname "$0")/.." && pwd)"
PY="${OPS_PYTHON:-/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
BATCH="$(date -u +%Y%m%dT%H%M%SZ)"
"$PY" "$OPS/version_manifest.py" --batch-id "$BATCH" || exit $?
echo "[done] parquet manifest $BATCH"
