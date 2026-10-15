#!/bin/sh
# WyckoffMaster 回测窗口 · 脱离会话启动器（禁用 nohup）
#
# 复用已验证的「脱离会话 + 净化环境」模式（来源：回测资源改造_2026-10-14/_launch_ra.sh）：
#   · 必须用 spawn_detached.py（os.setsid 双 fork）—— `nohup cmd &` 与工具后台模式
#     都会在工具调用结束后 1~2 分钟内被回收。
#   · 必须净化 CODEBUDDY_* 沙箱环境变量，否则孙进程被沙箱钩子拦截。
#
# 用法： sh launch_isolated.sh <pidfile> <logfile> <脚本> [脚本参数...]
#   sh launch_isolated.sh logs/ladder.pid logs/ladder.log scripts/run_ladder.py --phase kline
set -e
cd "$(dirname "$0")/.."          # -> 回测日志/WyckoffMaster
PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
SPAWN="/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/spawn_detached.py"
PYBIN="${PYBIN:-/Users/boos/.workbuddy-ai/binaries/python/envs/freqtrade/bin/python}"
PIDF="$1"; LOGF="$2"; shift 2
mkdir -p "$(dirname "$PIDF")" "$(dirname "$LOGF")"
exec /usr/bin/env \
  -u NODE_OPTIONS \
  -u CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS \
  -u CODEBUDDY_SANDBOX_BROKER_SESSION_ID \
  -u CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID \
  -u CODEBUDDY_SANDBOX_BROKER_TRACE_ID \
  -u CODEBUDDY_SERVICE_PROXY_URL \
  -u CODEBUDDY_MCP_CONFIG \
  -u CODEBUDDY_BROKERED_SHELL_ENV \
  PYTHONDONTWRITEBYTECODE=1 \
  CODEBUDDY_BROKERED_FS_HOOK_ENABLED=0 \
  CODEBUDDY_SAFE_DELETE_ENABLED=0 \
  CODEBUDDY_SAFE_DELETE_SANDBOX=0 \
  NO_PROXY=127.0.0.1,localhost \
  no_proxy=127.0.0.1,localhost \
  "$PY" "$SPAWN" --pid "$PIDF" --log "$LOGF" --cwd . -- \
  "$PYBIN" "$@"
