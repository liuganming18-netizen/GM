#!/bin/sh
# 原始 ZIP/OI 归档巡检 · 调度入口（周日 03:00 北京时间）
# 只做：防删旗标核验 + 计数报告；不下载、不删除、不改动。
# W1（2026-10-16）：OPS/证据目录由「脚本自身位置」推导，不再写死工程外绝对路径。
# 解释器可用 OPS_PYTHON 覆盖；证据落点可用 OPS_EVIDENCE_DIR 覆盖。
set -eu
OPS="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$OPS/../.." && pwd)"
PY="${OPS_PYTHON:-/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
EVID="${OPS_EVIDENCE_DIR:-$ROOT/backups/manifests}/rawverify_$(date -u +%Y%m%dT%H%M%SZ).json"
mkdir -p "$(dirname "$EVID")"
"$PY" "$OPS/protect_immutable.py" --mode verify --evidence "$EVID" || exit $?
echo "[done] raw verify"
