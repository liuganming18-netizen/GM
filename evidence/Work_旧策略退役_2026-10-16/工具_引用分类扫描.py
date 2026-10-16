# -*- coding: utf-8 -*-
"""WORK-04 · 旧策略引用分类扫描（只读）。

对工程内旧策略 / 旧执行桥 / 币池依赖的**符号级引用**做分类计数，
供 `引用分类扫描_2026-10-16.md` 复核。**不写任何文件、不改任何代码**。

运行（从工程根）：
  /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python \
    evidence/Work_旧策略退役_2026-10-16/工具_引用分类扫描.py
"""
from __future__ import annotations

import os
import re

ROOTS = ["框架", "策略", "UI界面/web/src", "tests", "scripts", "配置", "docs"]
EXCL = ("node_modules", "__pycache__", ".git", "快照", "数据",
        "_legacy_archive_v31", "/logs/")

PATS = {
    "exec_bridge": r"\bexec_bridge\b",
    "run_portfolio_execution": r"\brun_portfolio_execution\b",
    "strategy_a": r"\bstrategy_a\b",
    "strategy_b": r"\bstrategy_b\b",
    "wx_donchian_wyckoff": r"\bwx_donchian_wyckoff\b",
    "wx_config": r"\bwx_config\b",
    "wx_daily_375": r"\bwx_daily_375\b",
    "_pool_scope": r"\b_pool_scope\b",
    "ARM_IDS": r"\bARM_IDS\b",
    "oi_resonance_events_for": r"\boi_resonance_events_for\b",
}


def main() -> int:
    hits: dict[str, list[tuple[str, int]]] = {k: [] for k in PATS}
    for r in ROOTS:
        if not os.path.isdir(r):
            continue
        for dp, _dn, fn in os.walk(r):
            if any(x in dp for x in EXCL):
                continue
            for f in fn:
                if not f.endswith((".py", ".ts", ".tsx", ".json", ".sh", ".command", ".md")):
                    continue
                p = os.path.join(dp, f)
                try:
                    t = open(p, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                for k, pat in PATS.items():
                    n = len(re.findall(pat, t))
                    if n:
                        hits[k].append((p, n))
    for k, v in hits.items():
        print(f"\n### {k}  ({len(v)} files, {sum(n for _, n in v)} hits)")
        for p, n in sorted(v, key=lambda x: -x[1]):
            print(f"   {n:3d}  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
