#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W1 幽灵依赖清理 · 机械替换（只动指定文件；逐条打印改动）。

把工程外绝对路径改写为**工程内正式路径**（或语义化说明）。不改任何业务逻辑。
用法：python 工具_W1_路径改写.py [--apply]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
APPLY = "--apply" in sys.argv

# 顺序敏感：长串必须先于短串替换
RULES = [
    # ops 配置 / 计划任务（旧外部工作区 → 工程内）
    (f"/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现",
     f"{ROOT}/框架/backend"),
    (f"/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
     "最新唐奇安威科夫接通与收尾_2026-10-11/量价信号接入与模拟历史并行43/纸面模拟43",
     f"{ROOT}/数据/paper"),
    (f"/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究",
     f"{ROOT}/框架"),
    (f"/Users/boos/WorkBuddy AI/加密货币趋势判断系统", ROOT),
    (f"/Users/boos/WorkBuddy AI", ROOT),
]

FILES = [
    "框架/ops/config/ops_config.json",
    "框架/ops/schedule/run_git_patrol.sh",
    "框架/ops/schedule/run_hot_snapshot.sh",
    "框架/ops/schedule/com.crypto.ops.hotsnapshot.plist",
    "框架/ops/schedule/com.crypto.ops.parquetmanifest.plist",
    "框架/ops/schedule/com.crypto.ops.gitpatrol.plist",
    "框架/ops/schedule/com.crypto.ops.rawverify.plist",
    "框架/ops/schedule/README.md",
]

# 说明性文字（注释 / 文案）里出现的旧路径 → 语义化描述，避免残留可被误读为「依赖」的字符串
PROSE_RULES = [
    ("`/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究`", "旧工作区站点根（已删）"),
    ("`/Users/boos/WorkBuddy AI/...`", "旧工作区路径（已删）"),
    ("`/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/`", "外部工作树（数据已迁入）"),
    ("`/Users/boos/.codex/worktrees/c9d7/...`", "外部工作树路径"),
    ("`/Users/boos/WorkBuddy AI`", "旧工作区（已删）"),
    ("随旧工作区 /Users/boos/WorkBuddy AI 一并丢失", "随旧工作区一并丢失"),
    ("随旧工作区 `/Users/boos/WorkBuddy AI` 一并丢失", "随旧工作区一并丢失"),
]

PROSE_FILES = [
    "框架/backend/project_paths.py",
    "框架/backend/adapter.py",
    "框架/backend/migrate.py",
    "框架/backend/strategy_runner.py",
    "框架/backend/smart_money.py",
    "框架/backend/public_market_read.py",
    "框架/backend/live_pool.py",
    "框架/backend/live_worker.py",
    "框架/backend/live_signal_engine.py",
    "框架/backend/paper_engine.py",
    "框架/backend/live_api.py",
    "框架/backend/strategies/wx_daily_375.py",
    "框架/backend/strategies/wx_donchian_wyckoff.py",
]


def main() -> int:
    n = 0
    for rel in FILES:
        p = Path(ROOT) / rel
        if not p.exists():
            print(f"[skip] 不存在 {rel}")
            continue
        s = p.read_text(encoding="utf-8")
        orig = s
        for a, b in RULES:
            if a in s:
                s = s.replace(a, b)
                print(f"[rule] {rel}: {a} -> {b}")
        if s != orig:
            n += 1
            if APPLY:
                p.write_text(s, encoding="utf-8")
            print(f"[{'wrote' if APPLY else 'dry'}] {rel}")

    for rel in PROSE_FILES:
        p = Path(ROOT) / rel
        if not p.exists():
            continue
        s = p.read_text(encoding="utf-8")
        orig = s
        for a, b in PROSE_RULES:
            if a in s:
                s = s.replace(a, b)
        if s != orig:
            n += 1
            if APPLY:
                p.write_text(s, encoding="utf-8")
            print(f"[{'wrote' if APPLY else 'dry'}] {rel} (prose)")

    print(f"\n共 {n} 个文件{'已改写' if APPLY else '待改写（dry-run）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
