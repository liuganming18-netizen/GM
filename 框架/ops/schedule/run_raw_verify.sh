#!/bin/sh
# 原始 ZIP/OI 归档巡检 · 调度入口（周日 03:00 北京时间）
# 只做：防删旗标核验 + 计数报告；不下载、不删除、不改动。
set -eu
OPS="/Users/boos/WorkBuddy AI/加密货币趋势判断系统/ops"
PY="/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3"
export PYTHONDONTWRITEBYTECODE=1
EVID="/Users/boos/WorkBuddy AI/加密货币趋势判断系统/backups/manifests/rawverify_$(date -u +%Y%m%dT%H%M%SZ).json"
"$PY" "$OPS/protect_immutable.py" --mode verify --evidence "$EVID" || exit $?
echo "[done] raw verify"
