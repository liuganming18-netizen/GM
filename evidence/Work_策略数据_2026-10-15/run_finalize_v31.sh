#!/bin/sh
# v3.1 数据窗收尾链（is/val 已构建完成；mark 空洞补齐与资金费 mark API 补齐已先行执行）
set -e
cd "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15"
PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python
echo "== 1/4 冻结 manifest =="
$PY 工具_冻结manifest_v31_2026-10-15.py
echo "== 2/4 字段与边界核验 =="
$PY 工具_字段核验_v31_2026-10-15.py
echo "== 3/4 备份/隔离恢复 =="
$PY 工具_备份与隔离恢复_v31_2026-10-15.py
echo "== 4/4 data_ready =="
$PY 工具_生成data_ready_v31_2026-10-15.py
echo "== DONE =="
