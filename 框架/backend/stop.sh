#!/bin/sh
# 停止本机只读后端。
# 身份校验三条件同时满足才停止：① 监听本端口 ② 工作目录是本目录 ③ 命令行含本目录 adapter.py
# 不杀任何未知进程。
set -eu

HERE="$(cd "$(dirname "$0")" && pwd -P)"
PORT="${PORT:-8787}"
# pid 文件按端口区分（与 start.sh 一致）
if [ "$PORT" = "8787" ]; then PIDFILE="$HERE/backend.pid"; else PIDFILE="$HERE/backend-$PORT.pid"; fi

owner_of_port() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

# 本机工具环境的 python 有包装进程：$! 可能是包装进程，真实监听进程另有 PID。
# 因此以**端口监听者**为准，PID 文件仅作提示。
PID="$(owner_of_port)"

if [ -z "$PID" ]; then
  echo "端口 $PORT 无人监听，无需停止"
  # 只清理**确已失效**的 pid 文件；若其中的 PID 仍存活（属于别的端口/实例），保留
  if [ -f "$PIDFILE" ] && ! kill -0 "$(cat "$PIDFILE" 2>/dev/null || echo 0)" 2>/dev/null; then
    rm -f "$PIDFILE"
  fi
  exit 0
fi

CWD="$(lsof -a -p "$PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true)"
CMD="$(ps -o command= -p "$PID" 2>/dev/null || true)"

if [ "$CWD" != "$HERE" ]; then
  echo "拒绝停止：pid=$PID 的工作目录为 '$CWD'，不是本目录（不杀未知进程）"
  exit 1
fi
case "$CMD" in
  *adapter.py*) : ;;
  "") : ;;   # 沙箱内 ps 不可用时，已用工作目录校验身份
  *) echo "拒绝停止：pid=$PID 命令行不含 adapter.py（不杀未知进程）"; exit 1 ;;
esac

kill "$PID"
i=0
while [ "$i" -lt 20 ]; do
  [ -z "$(owner_of_port)" ] && break
  sleep 0.5; i=$((i+1))
done

if [ -n "$(owner_of_port)" ]; then
  echo "未退出：pid=${PID} 发送 TERM 后仍监听端口 ${PORT}（不强制 kill -9）" >&2
  exit 1
fi
rm -f "$PIDFILE"
echo "已停止 pid=${PID}（端口 ${PORT} 已释放）"
