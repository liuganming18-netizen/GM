#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# 加密货币趋势判断系统 · 统一启动入口 (桌面工程独立版)
#
#   后端 API  :8787  → 启动  框架/backend/start.sh
#   实时采集        → 启动  框架/backend/collector.sh
#   前端(唯一) :3000  → 启动  UI界面/web（npm run dev）
#
# 用户访问：http://127.0.0.1:3000/
# ─────────────────────────────────────────────────────────────────────────────
set -u

ROOT="$(cd "$(dirname "$0")" && pwd -P)"
BE_DIR="$ROOT/框架/backend"
FE_DIR="${SYS_FE_DIR:-$ROOT/UI界面/web}"
FE_DIR_REAL="$(cd "$FE_DIR" 2>/dev/null && pwd -P || printf '%s' "$FE_DIR")"
BE_PORT="${SYS_BE_PORT:-8787}"
FE_PORT="${SYS_FE_PORT:-3000}"
STATE="$ROOT/.system"
FE_PID="$STATE/frontend.pid"
FE_LOG="$STATE/frontend.log"

if [ -d "/Users/boos/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin" ]; then
  NODE_BIN_DIR="/Users/boos/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin"
elif command -v node >/dev/null 2>&1; then
  NODE_BIN_DIR="$(dirname "$(command -v node)")"
else
  NODE_BIN_DIR="$(ls -d /Users/boos/.workbuddy-ai/binaries/node/versions/*/bin 2>/dev/null | tail -1 || true)"
fi
NODE="$NODE_BIN_DIR/node"

if [ -f "/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python" ]; then
  PY="/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python"
elif [ -f "/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3" ]; then
  PY="/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3"
else
  PY="$(command -v python3 || echo "python3")"
fi

OPEN_BROWSER=1
PAUSE=0
for a in "$@"; do
  case "$a" in
    --no-open)  OPEN_BROWSER=0 ;;
    --no-pause) PAUSE=0 ;;
    --pause)    PAUSE=1 ;;
  esac
done
[ "${SYS_NO_OPEN:-0}" = "1" ] && OPEN_BROWSER=0
[ -t 1 ] && [ "${SYS_NO_PAUSE:-0}" != "1" ] && PAUSE=1

ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; }
say()  { printf '%s\n' "$*"; }

listener() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }
cwd_of()   { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true; }
http_code() { curl -s -o /dev/null -w '%{http_code}' -m 5 --noproxy '*' "$1" 2>/dev/null || true; }

export PATH="$NODE_BIN_DIR:$PATH"
export DATA_HUB_PROJECT="$ROOT"
export MARKET_DB="$ROOT/数据/市场数据/db/market_v2.sqlite3"
export MARKET_ARCHIVE="$ROOT/数据/市场数据/parquet"
# 纸面账本目录：**显式**绑定（禁止相对推导 + 自动建库）。缺库 ⇒ 后端 fail-closed。
export PAPER_LEDGER_DIR="$ROOT/数据/paper"
export PAPER_LEDGER_ALLOW_INIT="${PAPER_LEDGER_ALLOW_INIT:-0}"
export LIVE_WORKER_INTERVAL_SEC="${LIVE_WORKER_INTERVAL_SEC:-900}"
export PYTHONPATH="$ROOT/框架:$BE_DIR:$ROOT:${PYTHONPATH:-}"

say "════════════════════════════════════════════════"
say " 加密货币趋势判断系统 · 统一启动 (桌面工程版)"
say " 项目目录：$ROOT"
say "════════════════════════════════════════════════"

# ── 1) 后端 :8787 ──
say ""
say "── 1/3 后端 API :$BE_PORT ──"
if [ ! -f "$BE_DIR/start.sh" ]; then
  err "找不到后端启动脚本：$BE_DIR/start.sh"
  [ "$PAUSE" = "1" ] && read -r -p "按回车关闭…" _
  exit 1
fi
if PORT="$BE_PORT" PY="$PY" sh "$BE_DIR/start.sh"; then
  ok "后端就绪 :$BE_PORT"
else
  rc=$?
  err "后端启动失败（退出码 ${rc}）。"
  [ "$PAUSE" = "1" ] && read -r -p "按回车关闭…" _
  exit "$rc"
fi

# ── 2) 实时采集 ──
say ""
say "── 2/3 实时采集进程 ──"
if [ ! -f "$BE_DIR/collector.sh" ]; then
  warn "找不到 $BE_DIR/collector.sh → 跳过实时采集启动"
else
  if sh "$BE_DIR/collector.sh"; then
    ok "实时采集就绪"
  else
    rc=$?
    warn "实时采集未能就绪（退出码 ${rc}）"
  fi
fi

# ── 3) 前端 :3000 ──
say ""
say "── 3/3 前端 :${FE_PORT} ──"
if [ ! -d "$FE_DIR" ]; then
  err "找不到前端目录：$FE_DIR"
  [ "$PAUSE" = "1" ] && read -r -p "按回车关闭…" _
  exit 1
fi

FE_LISTENER="$(listener "$FE_PORT")"
if [ -n "$FE_LISTENER" ]; then
  FE_CWD="$(cwd_of "$FE_LISTENER")"
  if [ "$FE_CWD" != "$FE_DIR_REAL" ]; then
    warn "端口 $FE_PORT 被旧进程或无关进程占用：pid=$FE_LISTENER cwd='$FE_CWD' → 终止并重新启动"
    kill -9 "$FE_LISTENER" 2>/dev/null || true
    sleep 1
  elif [ "$(http_code "http://127.0.0.1:$FE_PORT/")" = "200" ]; then
    ok "复用已运行的前端：pid=${FE_LISTENER}"
  else
    warn "前端在监听但未返回 200 → 重启"
    kill -9 "$FE_LISTENER" 2>/dev/null || true
    sleep 1
  fi
fi

if [ -z "$(listener "$FE_PORT")" ]; then
  say "启动前端 ($FE_DIR : npm run dev)..."
  mkdir -p "$STATE"
  if ! SPAWNED="$("$PY" "$BE_DIR/spawn_detached.py" --pid "$FE_PID" --log "$FE_LOG" \
        --cwd "$FE_DIR" -- "$NODE" "$NODE_BIN_DIR/npm" run dev)"; then
    err "前端启动失败，详见日志：$FE_LOG"
    [ "$PAUSE" = "1" ] && read -r -p "按回车关闭…" _
    exit 4
  fi
  i=0
  while [ "$i" -lt 60 ]; do
    [ "$(http_code "http://127.0.0.1:$FE_PORT/")" = "200" ] && break
    sleep 1; i=$((i+1))
  done
  FE_LISTENER="$(listener "$FE_PORT")"
  if [ -z "$FE_LISTENER" ] || [ "$(http_code "http://127.0.0.1:$FE_PORT/")" != "200" ]; then
    err "前端启动超时（60s），见日志：$FE_LOG"
    [ "$PAUSE" = "1" ] && read -r -p "按回车关闭…" _
    exit 4
  fi
  printf '%s' "$FE_LISTENER" > "$FE_PID"
  ok "前端已启动：pid=${FE_LISTENER}"
fi

# ── 4) 就绪 ──
say ""
URL="http://127.0.0.1:$FE_PORT/"
BE_CODE="$(http_code "http://127.0.0.1:$BE_PORT/api/health")"
FE_CODE="$(http_code "$URL")"
say "─────────────── 就绪 ───────────────"
say "  后端 :$BE_PORT  /api/health → ${BE_CODE:-无响应}"
say "  前端 :$FE_PORT  首页 → ${FE_CODE:-无响应}"
ok  "系统已就绪，请打开：$URL"

if [ "$OPEN_BROWSER" = "1" ] && command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true
fi

[ "$PAUSE" = "1" ] && { say ""; read -r -p "按回车关闭此窗口…" _; }
exit 0
