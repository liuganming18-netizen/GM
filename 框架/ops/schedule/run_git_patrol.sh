#!/bin/sh
# Git 巡检 · 调度入口（北京时间 00:30）
# 只检查候选/验收 SHA 与工作树状态；**绝不** push / 改 main / 改可见性。
set -eu
CAND="/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/Git代码基线保护_2026-10-14/candidate"
if [ ! -d "$CAND/.git" ]; then
  echo "[git] 候选目录无 .git：$CAND（跳过）"; exit 0
fi
echo "=== git patrol $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
git -C "$CAND" --no-pager log -1 --format='HEAD %H %s' || true
git -C "$CAND" --no-pager status --short | head -40 || true
echo "[note] 仅巡检；未审核不 push/main。"
