#!/bin/sh
# 确保**统一实时采集进程**在运行（幂等、身份安全）。
#
#   本脚本管什么：只管理「统一实时采集进程」（realtime_collector.py）本身。
#   本脚本**不管**什么：
#     · 不启动、不停止**历史批量下载**（那是用户既有进程 run11，另有 PID/日志）；
#     · 不新建第二个历史 run；不改策略、不下单、不读凭据。
#
# 幂等与身份判据（两个条件都满足才算"健康"）：
#   ① logs/realtime.pid 里的进程**存活**（kill -0）；
#   ② collector_state 心跳在 120s 内（**心跳**，不是 run 记录）。
#   健康 → 复用（不重复拉起）；不健康 → 启动。
#   端口/进程被无关程序占用的情况在本脚本不适用（采集进程不监听端口），
#   但若 pid 文件指向的进程**不是**本项目采集进程（cwd 不符），本脚本明确报错、不杀它。
#
# ⚠ 维护须知：本文件含中文输出。本机 sh 实测（与 locale 无关）会把「$VAR 紧跟中文」
#   解析成更长的变量名，set -u 下报 unbound variable → 变量后接中文**必须写 ${VAR}**。
set -eu

HERE="$(cd "$(dirname "$0")" && pwd -P)"
PY="${PY:-/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3}"
PROJ="${DATA_HUB_PROJECT:-/Users/boos/WorkBuddy AI/加密货币趋势判断系统}"
PIDFILE="$HERE/logs/realtime.pid"
LOG="$HERE/logs/realtime.log"
mkdir -p "$HERE/logs"

export PYTHONDONTWRITEBYTECODE=1
export DATA_HUB_PROJECT="$PROJ"
# P3 切流（2026-10-13）：采集器默认写入**新热库** market_v2.sqlite3（旧库已退役）
export MARKET_DB="${MARKET_DB:-$PROJ/data/market_v2.sqlite3}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
export no_proxy="$NO_PROXY"

cwd_of() { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true; }

PID=""
[ -f "$PIDFILE" ] && PID="$(cat "$PIDFILE" 2>/dev/null || true)"

# ── 1) 已有 pid：先判身份，再判健康 ─────────────────────────────────────────
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  CWD="$(cwd_of "$PID")"
  if [ "$CWD" != "$HERE" ]; then
    echo "错误：$PIDFILE 记录的 pid=$PID cwd='$CWD'，不是本目录的采集进程。" >&2
    echo "      本脚本不会杀它，也不会重复拉起；请人工确认后处理。" >&2
    exit 2
  fi
  if "$PY" "$HERE/realtime_collector.py" --health >/dev/null 2>&1; then
    echo "已运行（复用）：采集进程 pid=${PID}，心跳新鲜，未重复拉起"
    exit 0
  fi
  echo "采集进程 pid=$PID 存活但心跳不新鲜 → 重启"
  kill "$PID" 2>/dev/null || true
  i=0
  while [ "$i" -lt 20 ]; do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5; i=$((i+1))
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "错误：旧采集进程 pid=$PID 未在 10s 内退出（不强制 kill -9）" >&2
    exit 3
  fi
fi

# ── 2) 启动采集进程（脱离会话，用既有 spawn_detached）────────────────────────
PID="$("$PY" "$HERE/spawn_detached.py" --pid "$PIDFILE" --log "$LOG" \
        --cwd "$HERE" -- "$PY" "$HERE/realtime_collector.py")"

# ── 3) 等待健康（进程存活 + 心跳新鲜）────────────────────────────────────────
i=0
while [ "$i" -lt 90 ]; do
  "$PY" "$HERE/realtime_collector.py" --health >/dev/null 2>&1 && break
  sleep 1; i=$((i+1))
done
if ! "$PY" "$HERE/realtime_collector.py" --health >/dev/null 2>&1; then
  echo "错误：采集进程已拉起 pid=$PID 但 90s 内未通过健康判据，见 $LOG" >&2
  exit 4
fi

echo "已启动采集进程 pid=${PID}（WS 快照/费率 + 实时 K 线，含未收盘临时层与闭合入库）"
echo "  数据库: $MARKET_DB"
echo "  日志:   $LOG"
echo "  验证:   $PY $HERE/realtime_collector.py --health"
echo "          $PY $HERE/realtime_collector.py --kline-report"
