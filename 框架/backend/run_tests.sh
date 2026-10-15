#!/bin/sh
# 后端实现 · 常规测试集门禁（离线、零网络；不写正式库）
#
# 用法：
#   sh run_tests.sh                      # 跑全部 tests/test_*.py
#   sh run_tests.sh tests.test_pool_gate # 只跑指定模块（可多个）
#
# 运行环境：优先 POOL_RUNTIME_PY → 推荐受管运行时 → Work 受管 3.13.12。
# 退出码：0 = 全绿；非 0 = 有失败（可直接用于 CI/门禁判据）。
#
# 说明：本脚本只**跑测试**，不下载、不写正式库、不改任何策略常量。
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
RECOMMENDED=/Users/boos/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
FALLBACK=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3

if [ -n "${POOL_RUNTIME_PY:-}" ]; then
  PY="$POOL_RUNTIME_PY"
elif [ -x "$RECOMMENDED" ]; then
  PY="$RECOMMENDED"
else
  PY="$FALLBACK"
fi

if [ ! -x "$PY" ]; then
  echo "⚠ 无可用解释器（POOL_RUNTIME_PY/推荐运行时/受管 3.13.12 均不可用）" >&2
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
cd "$HERE"

if [ "$#" -gt 0 ]; then
  exec "$PY" -m unittest -v "$@"
fi

echo "== 后端实现 · 全部隔离单测（tests/test_*.py）=="
echo "   解释器：$PY"
exec "$PY" -m unittest discover -s tests -p "test_*.py"
