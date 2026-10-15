#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# 加密货币趋势判断系统 · 统一停止入口 (桌面工程独立版)
#
#   · 后端 :8787  → 调用 框架/backend/stop.sh
#   · 前端 :3000  → 停止 UI界面/web 的前端进程
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

PAUSE=0
for a in "$@"; do
  case "$a" in
    --pause)   PAUSE=1 ;;
    --no-pause) PAUSE=0 ;;
  esac
done
[ -t 1 ] && [ "${SYS_NO_PAUSE:-0}" != "1" ] && PAUSE=1

ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; }
say()  { printf '%s\n' "$*"; }

listener() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }
cwd_of()   { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true; }

say "════════════════════════════════════════════════"
say " 加密货币趋势判断系统 · 统一停止 (桌面工程版)"
say "════════════════════════════════════════════════"

# ── 1) 后端 :8787 ──
say ""
say "── 1/2 后端 API :$BE_PORT ──"
if [ -f "$BE_DIR/stop.sh" ]; then
  if PORT="$BE_PORT" sh "$BE_DIR/stop.sh"; then
    ok "后端已停止"
  else
    warn "后端未停止"
  fi
else
  warn "找不到 $BE_DIR/stop.sh"
fi

# ── 2) 前端 :3000 ──
say ""
say "── 2/2 前端 :$FE_PORT ──"
FE_LISTENER="$(listener "$FE_PORT")"
if [ -z "$FE_LISTENER" ]; then
  ok "前端未运行（端口 $FE_PORT 空闲）"
  rm -f "$FE_PID"
else
  kill -9 "$FE_LISTENER" 2>/dev/null || true
  rm -f "$FE_PID"
  ok "前端已停止（端口 $FE_PORT 已释放）"
fi

say ""
ok "系统已全部停止"
[ "$PAUSE" = "1" ] && { say ""; read -r -p "按回车关闭此窗口…" _; }
exit 0
