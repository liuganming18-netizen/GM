#!/bin/sh
# PRECISION-05 一键复现。任何一步失败即停（set -e）。
# 用法： sh /Users/boos/Desktop/威科夫因子共振趋势/evidence/PRECISION-05/复现命令.sh
set -e

ROOT=/Users/boos/Desktop/威科夫因子共振趋势
PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python

echo "== 0 环境 =="
cd "$ROOT"
pwd
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
"$PY" -V

echo
echo "== 1 独立复核 PRECISION-04（自建解析 + 自建分级实现）=="
cd "$ROOT/evidence/PRECISION-05"
"$PY" 工具_复核PRECISION04.py | tee 复核原始输出.txt

echo
echo "== 2 重跑 PRECISION-04 自身工具链，记录退出码 =="
cd "$ROOT/evidence/PRECISION-04"
: > "$ROOT/evidence/PRECISION-05/P04工具链重跑.txt"
for spec in "工具_公告抓取.py:detail" "工具_证据汇总.py:" "工具_存档快照抓取.py:" \
            "工具_本地证据盘点.py:" "工具_规格时间线.py:" "工具_逐段证据表.py:" \
            "工具_运行侧反证.py:"; do
  f=${spec%%:*}
  a=${spec##*:}
  echo "=== $PY $f $a" >> "$ROOT/evidence/PRECISION-05/P04工具链重跑.txt"
  # shellcheck disable=SC2086
  "$PY" "$f" $a >> "$ROOT/evidence/PRECISION-05/P04工具链重跑.txt" 2>&1
  echo "exit=$? ($f)" | tee -a "$ROOT/evidence/PRECISION-05/P04工具链重跑.txt"
done
echo "（注：工具_公告抓取.py index / index48 未重跑——会重新拉 6735 页；"
echo "  其完整性改为核验已落盘索引的 reported_total 与实际条数是否相等，见报告第 2 节）"

echo
echo "== 3 逐笔核验 6 个 run 的 12 条腿 =="
cd "$ROOT/evidence/PRECISION-05"
"$PY" 工具_逐笔核验.py | tee 逐笔核验原始输出.txt

echo
echo "== 4 运行时代码 vs 当前 vs git HEAD 三方对照 =="
"$PY" 工具_代码版本对照.py

echo
echo "== 5 现有精度保护探针（legacy / strict 两种模式）=="
"$PY" 工具_保护机制探针.py | tee 探针原始输出.txt

echo
echo "== 6 既有测试（隔离临时目录，不写工程 .pytest_cache）=="
cd "$ROOT"
mkdir -p evidence/PRECISION-05/tmp_pytest
"$PY" -m pytest 策略/WyckoffMaster/event_trend_v0_1/tests/ \
      策略/WyckoffMaster/test_work02_core_contract.py \
      -q -p no:cacheprovider --basetemp=evidence/PRECISION-05/tmp_pytest \
      > evidence/PRECISION-05/测试原始输出.txt 2>&1
echo "pytest exit=$?"
tail -3 evidence/PRECISION-05/测试原始输出.txt

echo
echo "== 7 交付表 + SHA256 清单 =="
cd "$ROOT/evidence/PRECISION-05"
"$PY" 工具_生成交付表.py
"$PY" 工具_清单.py

echo
echo "全部完成。"
