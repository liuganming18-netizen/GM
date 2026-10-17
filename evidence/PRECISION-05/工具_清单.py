#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-05 工具：生成产物 SHA256 清单（含被核验的输入文件）。

用法：python3 工具_清单.py
输出：产物SHA256清单.txt / 产物SHA256清单.json
"""
from __future__ import annotations

import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
P04 = os.path.join(ROOT, "evidence", "PRECISION-04")

# 本窗口产物（不含 pytest 临时目录）
OWN = [
    "PRECISION-05_核验报告_2026-10-17.md",
    "表1_六标的证据对照.csv",
    "表2_四笔成交逐笔核验.csv",
    "四笔成交逐笔核验.json",
    "PRECISION04复核结果.json",
    "保护机制探针结果.json",
    "测试原始输出.txt",
    "探针原始输出.txt",
    "P04工具链重跑.txt",
    "逐笔核验原始输出.txt",
    "复核原始输出.txt",
    "一键复现日志.txt",
    "复现命令.sh",
    "工具_逐笔核验.py",
    "工具_保护机制探针.py",
    "工具_复核PRECISION04.py",
    "工具_生成交付表.py",
    "工具_代码版本对照.py",
    "工具_清单.py",
    "运行时代码/fill.py.HEAD",
    "运行时代码/position.py.HEAD",
    "运行时代码/运行时代码指纹对照.json",
]

# 被核验的输入（PRECISION-04 证据与当前源码）
INPUTS = [
    os.path.join(P04, "PRECISION-04_历史合约规格证据盘点_2026-10-17.md"),
    os.path.join(P04, "六标的逐时间段证据.json"),
    os.path.join(P04, "公告规格变更明细.json"),
    os.path.join(P04, "规格时间线.json"),
    os.path.join(P04, "存档快照索引.json"),
    os.path.join(P04, "运行侧反证.json"),
    os.path.join(P04, "本地证据盘点.json"),
    os.path.join(P04, "当前响应_exchangeInfo.json"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/fill.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/engine.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/position.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/runner.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/instrument_spec.py"),
]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main():
    rows = []
    for rel in OWN:
        p = os.path.join(HERE, rel)
        rows.append({"kind": "本窗口产物", "path": rel, "exists": os.path.exists(p),
                     "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                     "sha256": sha(p) if os.path.exists(p) else None})
    for p in INPUTS:
        rows.append({"kind": "被核验输入", "path": os.path.relpath(p, ROOT),
                     "exists": os.path.exists(p),
                     "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                     "sha256": sha(p) if os.path.exists(p) else None})

    with open(os.path.join(HERE, "产物SHA256清单.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": __import__("time").strftime(
            "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()), "files": rows},
            f, ensure_ascii=False, indent=1)

    lines = ["# PRECISION-05 产物 SHA256 清单", ""]
    for kind in ("本窗口产物", "被核验输入"):
        lines.append(f"## {kind}")
        lines.append("")
        lines.append("| 文件 | 字节 | SHA256 |")
        lines.append("|---|---|---|")
        for r in rows:
            if r["kind"] != kind:
                continue
            if not r["exists"]:
                lines.append(f'| `{r["path"]}` | — | **缺失** |')
                continue
            lines.append(f'| `{r["path"]}` | {r["bytes"]} | `{r["sha256"]}` |')
        lines.append("")
    with open(os.path.join(HERE, "产物SHA256清单.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
