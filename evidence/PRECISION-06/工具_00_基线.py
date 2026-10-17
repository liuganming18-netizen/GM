#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 00：记录开工基线。

记录内容：
  1. 工程根 / 分支 / HEAD / HEAD 提交时间；
  2. 完整 `git status --porcelain`（把 `evidence/PRECISION-06/` 自身条目单独标出，
     其余原样保留，不裁剪）；
  3. 被核验输入的 SHA256：12 个 run 产物、7 个源码文件、PRECISION-05 证据。

用法：python3 工具_00_基线.py [--label 开工|收工]
输出：基线_<label>.json / 基线_<label>.txt
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SELF_PREFIX = "evidence/PRECISION-06/"

RUNS = [
    "run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451",
    "run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703",
    "run_WX_EVENT_TREND_V0_1_TRUTHUSDT_1792174465",
    "run_WX_EVENT_TREND_V0_1_COTIUSDT_1792174145",
    "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973",
    "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568",
]
RUN_FILES = ["trades.jsonl", "orders.jsonl", "fills.jsonl", "inputs.json",
             "summary.json", "manifest.json", "decisions.jsonl", "rejects.jsonl",
             "chart.json"]

CODE = [
    "策略/WyckoffMaster/trading_core/fill.py",
    "策略/WyckoffMaster/trading_core/engine.py",
    "策略/WyckoffMaster/trading_core/position.py",
    "策略/WyckoffMaster/trading_core/policy.py",
    "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py",
    "策略/WyckoffMaster/event_trend_v0_1/runner.py",
    "策略/WyckoffMaster/event_trend_v0_1/instrument_spec.py",
]

P05 = [
    "evidence/PRECISION-05/四笔成交逐笔核验.json",
    "evidence/PRECISION-05/保护机制探针结果.json",
    "evidence/PRECISION-05/表2_四笔成交逐笔核验.csv",
    "evidence/PRECISION-05/PRECISION-05_核验报告_2026-10-17.md",
]


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    label = "开工"
    if "--label" in sys.argv:
        label = sys.argv[sys.argv.index("--label") + 1]

    git = {}
    for key, cmd in [
        ("toplevel", ["git", "rev-parse", "--show-toplevel"]),
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        ("head", ["git", "rev-parse", "HEAD"]),
        ("head_time", ["git", "show", "-s", "--format=%cI", "HEAD"]),
        ("status", ["git", "status", "--porcelain"]),
    ]:
        rc, out, err = run(cmd)
        git[key] = {"exit": rc, "stdout": out, "stderr": err}

    status_lines = [ln for ln in git["status"]["stdout"].splitlines() if ln.strip()]
    self_lines = [ln for ln in status_lines if SELF_PREFIX in ln]
    other_lines = [ln for ln in status_lines if SELF_PREFIX not in ln]

    files = []
    for r in RUNS:
        for f in RUN_FILES:
            rel = f"回测日志/event_trend_v0_1/runs/{r}/{f}"
            p = os.path.join(ROOT, rel)
            files.append({"group": "run", "path": rel, "exists": os.path.exists(p),
                          "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                          "sha256": sha256(p) if os.path.exists(p) else None})
    for rel in CODE:
        p = os.path.join(ROOT, rel)
        files.append({"group": "code", "path": rel, "exists": os.path.exists(p),
                      "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                      "sha256": sha256(p) if os.path.exists(p) else None})
    for rel in P05:
        p = os.path.join(ROOT, rel)
        files.append({"group": "p05", "path": rel, "exists": os.path.exists(p),
                      "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                      "sha256": sha256(p) if os.path.exists(p) else None})

    doc = {
        "label": label,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": ROOT,
        "git": {k: {"exit": v["exit"], "stdout": v["stdout"].strip(),
                    "stderr": v["stderr"].strip()} for k, v in git.items()},
        "status_summary": {
            "total": len(status_lines),
            "self_evidence_p06": len(self_lines),
            "other": len(other_lines),
            "self_entries": self_lines,
            "other_entries": other_lines,
        },
        "files": files,
    }

    jp = os.path.join(HERE, f"基线_{label}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    lines = [
        f"# PRECISION-06 基线（{label}）",
        "",
        f"- 生成时间（UTC）：{doc['generated_utc']}",
        f"- 工程根：`{ROOT}`",
        f"- 分支：`{git['branch']['stdout'].strip()}`",
        f"- HEAD：`{git['head']['stdout'].strip()}`（{git['head_time']['stdout'].strip()}）",
        f"- git status 总条目：**{len(status_lines)}**"
        f"（其中本窗口自有 `{SELF_PREFIX}` 占 {len(self_lines)} 条，其余 {len(other_lines)} 条为既有改动）",
        "",
        f"## 1. git status 全量（不含本窗口自有条目）",
        "",
        "```",
        *other_lines,
        "```",
        "",
        f"## 2. 本窗口自有条目（{SELF_PREFIX}）",
        "",
        "```",
        *(self_lines or ["（无）"]),
        "```",
        "",
        "## 3. 输入 SHA256",
        "",
        "| 组 | 文件 | 字节 | SHA256 |",
        "|---|---|---|---|",
    ]
    for r in files:
        if not r["exists"]:
            lines.append(f'| {r["group"]} | `{r["path"]}` | — | **缺失** |')
        else:
            lines.append(f'| {r["group"]} | `{r["path"]}` | {r["bytes"]} | `{r["sha256"]}` |')
    lines.append("")
    tp = os.path.join(HERE, f"基线_{label}.txt")
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"label={label} status_total={len(status_lines)} self={len(self_lines)} "
          f"other={len(other_lines)} files={len(files)}")
    print(f"→ {os.path.relpath(jp, ROOT)}")
    print(f"→ {os.path.relpath(tp, ROOT)}")


if __name__ == "__main__":
    main()
