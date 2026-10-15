#!/bin/sh
# 全池研究输入构建启动器（供 spawn_detached 调用，避免超长参数串）
set -e
cd "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15"
PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python
exec "$PY" 工具_构建研究输入_2026-10-15.py \
  --datasets ohlcv,funding,oi_metrics,mark \
  --workers 6 \
  --state "/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster/_state/build_state.json"
