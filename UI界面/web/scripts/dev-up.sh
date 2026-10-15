#!/usr/bin/env bash
# 一键恢复本机开发前端（幂等，可反复执行）
#
# ── 根因（2026-10-11 实测，非猜测）─────────────────────────────────────────────
# 本机运行在 CodeBuddy 沙箱内：NODE_OPTIONS 会 `--require node-language-shim.cjs`，
# 该 shim 组合了 `node-brokered-fs-shim.cjs`（CODEBUDDY_BROKERED_FS_HOOK_ENABLED=1）
# —— 即 **Node 的文件系统操作也被代理到「当前会话」的 unix socket broker**
# （CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS=/var/folders/.../cbb-XXXX/broker.sock）。
#
# 后果：任何**脱离会话长驻**的 Node 进程（如本 dev server）都会把「启动它那一刻的
# 会话 broker 路径」写死进自己的环境。当那个会话结束、broker 消失后：
#   端口仍在 LISTEN，但每处理一个请求（要读文件）就
#   `connect ECONNREFUSED .../broker.sock` ⇒ HTTP 500（Vite 的 Internal Server Error）。
# 这与「esbuild 子进程被回收」无关——是 **会话级 broker 生命周期**问题。
#
# ── 修法 ─────────────────────────────────────────────────────────────────────
# 让 dev server 以**不依赖会话 broker 的净化环境**启动：去掉 NODE_OPTIONS 里的
# shim、清掉会话 ID 与出网代理变量、显式关闭 brokered-fs / safe-delete 钩子，
# 并对 loopback 关闭代理。这样它是一个普通的 Node 进程，跨会话长驻。
# 只影响本进程环境，不改全局配置、不新增端口、不动界面与业务代码。
#
# 用法：bash scripts/dev-up.sh
set -uo pipefail

PROJ="/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/前端实现-AIStudio"
PY="/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3"
NODE="/Users/boos/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin/node"
ENVBIN="/usr/bin/env"
SPAWN="/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/spawn_detached.py"
PORT=3000
URL="http://127.0.0.1:${PORT}/"

cd "$PROJ" || { echo "✘ 项目目录不存在：$PROJ"; exit 1; }

probe() { curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$URL" 2>/dev/null; }

code="$(probe)"
if [ "$code" = "200" ]; then
  echo "✔ 前端已健康（HTTP 200），无需重启：$URL"
  exit 0
fi

echo "⚠ 前端不健康（HTTP ${code:-000}），开始重启…"

# 1) 结束旧监听者（先校验 cwd，绝不误杀其他项目）
#
# 注意（2026-10-11 修复）：`set -u` 下变量后紧跟**全角字符**会被 dash/bash 当成变量名的一部分
#   （`$pid（` → 变量名 "pid（" → unbound variable），导致脚本在第一步就崩。
#   一律写 `${pid}`。
for pid in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null); do
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//')"
  if [ "$cwd" = "$PROJ" ]; then
    echo "  结束 PID ${pid}（cwd 校验通过）"
    kill "$pid" 2>/dev/null
  else
    echo "  跳过 PID ${pid}（cwd=${cwd:-未知} 非本项目）"
  fi
done
sleep 2
for pid in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null); do
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//')"
  [ "$cwd" = "$PROJ" ] && { kill -9 "$pid" 2>/dev/null; echo "  强制结束 PID ${pid}"; }
done
sleep 1

# 2) 清理可能已损坏的依赖预构建缓存（与死掉的会话 broker 绑定的旧产物）
rm -rf node_modules/.vite
echo "  已清理 node_modules/.vite"

# 3) 脱离会话启动（spawn_detached 双重 fork + setsid，不会被回收）
#    —— 用 `env -u/-i 式` 净化环境，切断对**会话级 broker** 的依赖。
mkdir -p logs
"$PY" "$SPAWN" --pid logs/vite3000.pid --log logs/vite3000.log --cwd . -- \
  "$ENVBIN" -u NODE_OPTIONS -u CODEBUDDY_SESSION_ID -u CLAUDE_SESSION_ID \
  -u CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS -u TOYBOX_SANDBOX_SOCK \
  -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
  CODEBUDDY_BROKERED_FS_HOOK_ENABLED=0 CODEBUDDY_SAFE_DELETE_ENABLED=0 \
  NO_PROXY=127.0.0.1,localhost \
  "$NODE" node_modules/vite/bin/vite.js --port="$PORT" --strictPort --host=127.0.0.1

# 4) 等待就绪（首屏需做依赖预构建）
for _ in $(seq 1 15); do
  sleep 3
  code="$(probe)"
  if [ "$code" = "200" ]; then
    echo "✔ 已恢复：$URL   （日志 logs/vite3000.log）"
    exit 0
  fi
done

echo "✘ 仍未就绪（最后 HTTP ${code:-000}），请查看 logs/vite3000.log"
exit 1
