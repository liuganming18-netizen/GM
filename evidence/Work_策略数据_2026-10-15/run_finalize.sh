#!/bin/sh
# 收尾链：等构建进程退出 → 补结算mark → 冻结manifest → 字段核验 → 备份与隔离恢复
set -x
cd "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15"
PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python
PID=$(cat logs/build.pid)
echo "waiting build pid=$PID"
while kill -0 "$PID" 2>/dev/null; do sleep 10; done
echo "build exited"
$PY 工具_补结算mark_2026-10-15.py
$PY 工具_冻结manifest_2026-10-15.py
$PY 工具_字段核验_2026-10-15.py
echo "CHAIN_DONE"
