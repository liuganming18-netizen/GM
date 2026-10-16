#!/bin/sh
# 启动本机只读后端（薄适配层）。只监听 127.0.0.1，不开放局域网。
# 用法： sh start.sh          （默认端口 8787）
#        PORT=8787 sh start.sh
#
# 幂等 + 身份安全（统一启动入口依赖此行为）：
#   · 端口无人监听            → 启动 adapter
#   · 端口是本目录 adapter 且 /api/health 正常 → **复用**（不重复拉起）
#   · 端口是本目录 adapter 但健康失败          → 先停再启
#   · 端口被**无关进程**占用                  → 明确报错退出（不换端口、不重复拉起）
#
# ⚠ 维护须知：本文件含中文输出。本机 sh/bash 实测（与 locale 无关）会把
#   「$VAR 紧跟中文字符」解析成**更长的变量名**，set -u 下报 unbound variable。
#   → 变量后接中文时**必须写 ${VAR}**。
set -eu

HERE="$(cd "$(dirname "$0")" && pwd -P)"
PROJECT_ROOT="$(cd "$HERE/../.." && pwd -P)"
PROJ="${DATA_HUB_PROJECT:-$PROJECT_ROOT}"
# P3 切流（2026-10-13）关键修复：冷层读取需要 **duckdb + pyarrow**。
# 生产解释器 `versions/3.13.12` **不含** duckdb ⇒ 用它启动时，所有走冷档的端点
# （/api/v1/markets/*/oi、/funding、长历史 candles）会直接 `ModuleNotFoundError`
# ⇒ 表现为「接口 200 但 body 是 读取失败」，且 /api/health 仍正常，极易被漏检。
# 因此默认解释器改为含 duckdb 的 `envs/default`；如需覆盖用 `PY=… sh start.sh`。
PY="${PY:-/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python}"
PORT="${PORT:-8787}"
# pid 文件按端口区分：默认 8787 用 backend.pid，其它端口用 backend-<port>.pid，
# 以免隔离测试/多实例运行时互相覆盖。
if [ "$PORT" = "8787" ]; then PIDFILE="$HERE/backend.pid"; else PIDFILE="$HERE/backend-$PORT.pid"; fi
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

mkdir -p "$HERE/logs"

# 冷层依赖硬闸门（fail-loud）：设了 MARKET_ARCHIVE 却用不含 duckdb 的解释器
# ⇒ 冷档端点必然全失败。与其让它静默降级，不如启动前直接报错退出。
if [ -n "${MARKET_ARCHIVE:-}" ] && ! "$PY" -c "import duckdb, pyarrow" >/dev/null 2>&1; then
  echo "错误：解释器 $PY 缺少 duckdb/pyarrow，无法读取冷档（MARKET_ARCHIVE=$MARKET_ARCHIVE）" >&2
  echo "      请用 PY=<含 duckdb+pyarrow 的解释器> sh start.sh，例如" >&2
  echo "      PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python sh start.sh" >&2
  exit 5
fi

listener() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }
cwd_of()   { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true; }
# 健康探测：只认 200；本机回环不走代理（否则会被 http_proxy 转走导致误判）
health_ok() {
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 --noproxy '*' "$HEALTH_URL" 2>/dev/null || true)"
  [ "$code" = "200" ]
}

# ── 1) 端口已监听：先判身份 ────────────────────────────────────────────────
PID="$(listener)"
if [ -n "$PID" ]; then
  CWD="$(cwd_of "$PID")"
  if [ "$CWD" != "$HERE" ]; then
    echo "错误：端口 $PORT 被无关进程占用：pid=$PID cwd='$CWD'" >&2
    echo "      本脚本不会另换端口，也不会重复拉起服务；请先处理该进程后重试。" >&2
    exit 2
  fi
  if health_ok; then
    echo "$PID" > "$PIDFILE"
    echo "已运行（复用）：pid=$PID port=${PORT}，$HEALTH_URL 正常"
    exit 0
  fi
  echo "端口 $PORT 上的后端 pid=$PID 身份正确但 $HEALTH_URL 未返回 200 → 重启"
  kill "$PID" 2>/dev/null || true
  i=0
  while [ "$i" -lt 20 ]; do
    [ -z "$(listener)" ] && break
    sleep 0.5; i=$((i+1))
  done
  if [ -n "$(listener)" ]; then
    echo "错误：旧后端 pid=$PID 未在 10s 内退出（不强制 kill -9）" >&2
    exit 3
  fi
fi

# ── 2) 启动 adapter ────────────────────────────────────────────────────────
# 避免在只读的数据层包内写 __pycache__
export PYTHONDONTWRITEBYTECODE=1
export DATA_HUB_PROJECT="$PROJ"
export PYTHONPATH="$PROJECT_ROOT/框架:$HERE:$PROJECT_ROOT:${PYTHONPATH:-}"
# 桌面工程默认数据路径与冷档 Parquet 根
if [ -f "$PROJECT_ROOT/数据/市场数据/db/market_v2.sqlite3" ]; then
  DEFAULT_DB="$PROJECT_ROOT/数据/市场数据/db/market_v2.sqlite3"
elif [ -f "$PROJ/data/market_v2.sqlite3" ]; then
  DEFAULT_DB="$PROJ/data/market_v2.sqlite3"
else
  DEFAULT_DB="$HERE/data/market_v2.sqlite3"
fi
export MARKET_DB="${MARKET_DB:-$DEFAULT_DB}"
export MARKET_ARCHIVE="${MARKET_ARCHIVE:-$PROJECT_ROOT/数据/市场数据/parquet}"
# ── 纸面账本目录（**唯一权威，显式注入**）──────────────────────────────────────
# 历史缺陷：账本路径由 `HERE.parent` 相对推导 + `create=True` 自动建库，源码迁移后
# 解析到**另一个空目录**，凭空建出 4 份空账本并当成原账户读（等同新开四个 5000U 假账户）。
# 现纪律：目录**只来自本变量**，缺省 = 工程根 `数据/paper`（迁移清单 M002–M005 目标）。
# 缺库 ⇒ 后端 fail-closed（纸面/复盘端点返回 unavailable），**绝不自动建库冒充**。
export PAPER_LEDGER_DIR="${PAPER_LEDGER_DIR:-$PROJECT_ROOT/数据/paper}"
# 仅隔离测试/受控建档才置 1；正式运行**必须**为 0。
export PAPER_LEDGER_ALLOW_INIT="${PAPER_LEDGER_ALLOW_INIT:-0}"
# worker 实际调度间隔（`live_worker.py --interval`）：健康 TTL **绑定**它，
# 不因「进程在」或「接口 200」误报健康。
export LIVE_WORKER_INTERVAL_SEC="${LIVE_WORKER_INTERVAL_SEC:-900}"
# 冷档「已对账归档」固定水位（毫秒）。**不再写死**：读侧以归档
# `archive/parquet/_state.json` 的 `watermark_ms` 为单一事实源（由归档器校验
# 通过后推进）；此处只在状态文件缺失时兜底，避免「读侧旧水位 / 归档器新水位」
# 两套语义并存（用户 P3-3 明令禁止）。
export MARKET_WATERMARK_MS="${MARKET_WATERMARK_MS:-}"
export MARKET_RETENTION_DAYS="${MARKET_RETENTION_DAYS:-30}"
export PORT
# 本机回环不走代理：否则 http_proxy 会把 127.0.0.1 请求转给代理并连接被拒
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
export no_proxy="$NO_PROXY"

# 用**双重 fork + setsid** 脱离会话启动：本机 `nohup cmd &` 会被工具会话回收，
# 进程在 1~2 分钟内静默消失（表现为 Connection refused，且日志无报错）。
# spawn_detached.py 会把真正的孙进程 PID 写进 pid 文件并回显。
PID="$("$PY" "$HERE/spawn_detached.py" --pid "$PIDFILE" --log "$HERE/logs/backend.log" \
        --cwd "$HERE" -- "$PY" "$HERE/adapter.py")"

# ── 3) 等待就绪 ────────────────────────────────────────────────────────────
i=0
while [ "$i" -lt 40 ]; do
  health_ok && break
  sleep 0.5; i=$((i+1))
done
if ! health_ok; then
  echo "错误：后端已拉起 pid=$PID 但 $HEALTH_URL 未就绪（10s），见 $HERE/logs/backend.log" >&2
  exit 4
fi

echo "已启动 pid=$PID port=${PORT} (监听 127.0.0.1 与 ::1)"
echo "  数据库: $MARKET_DB"
echo "  账本:   $PAPER_LEDGER_DIR （缺库 ⇒ 纸面/复盘端点 unavailable，不自动建档）"
echo "  日志:   $HERE/logs/backend.log"
echo "  验证:   curl -s --noproxy '*' $HEALTH_URL"
echo "          curl -s --noproxy '*' http://127.0.0.1:${PORT}/api/v1/pool/status"
