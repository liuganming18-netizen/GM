#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-07 工具 00：记录开工/收工基线。

与 P06 的差别：
  · 输入覆盖**全池 31 个有成交的 run**（62 条腿重分类要用），不只是 P05 的 6 个；
  · 额外纳入 `config.py`（滑点 3bps 的定义处）与 P06 的关键证据；
  · 本窗口自有目录改为 `evidence/PRECISION-07/`。

用法：python3 工具_00_基线.py --label 开工|收工
输出：基线_<label>.json / 基线_<label>.txt
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SELF_PREFIX = "evidence/PRECISION-07/"
P06 = os.path.join(ROOT, "evidence", "PRECISION-06")
RUNS_DIR = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")

RUN_FILES = ["trades.jsonl", "orders.jsonl", "fills.jsonl", "chart.json",
             "inputs.json", "summary.json"]

CODE = [
    "策略/WyckoffMaster/trading_core/fill.py",
    "策略/WyckoffMaster/trading_core/engine.py",
    "策略/WyckoffMaster/trading_core/position.py",
    "策略/WyckoffMaster/trading_core/policy.py",
    "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py",
    "策略/WyckoffMaster/event_trend_v0_1/runner.py",
    "策略/WyckoffMaster/event_trend_v0_1/instrument_spec.py",
    "策略/WyckoffMaster/event_trend_v0_1/config.py",
]

P06_EVID = [
    "PRECISION-06_越界成交腿与strict保护专项核验_2026-10-17.md",
    "表3_12条成交腿核验明细.csv",
    "表3b_全量成交腿扫描.csv",
    "表5_影子整改覆盖.csv",
    "逐腿核验.json",
    "影子整改.json",
    "影子端到端.json",
    "整改影响面.json",
    "隔离复现/test_p06_现状固定.py",
    "隔离复现/test_p06_整改验收草案.py",
]


def run_cmd(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pool_runs():
    """全池「有成交」的 run：以 P06 的 表3b 为准（31 个），保持与 P06 同口径。"""
    p = os.path.join(P06, "表3b_全量成交腿扫描.csv")
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return sorted({r["run_id"] for r in rows})


def main():
    label = "开工"
    if "--label" in sys.argv:
        label = sys.argv[sys.argv.index("--label") + 1]

    git = {}
    for key, cmd in [("toplevel", ["git", "rev-parse", "--show-toplevel"]),
                     ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
                     ("head", ["git", "rev-parse", "HEAD"]),
                     ("head_time", ["git", "show", "-s", "--format=%cI", "HEAD"]),
                     ("status", ["git", "status", "--porcelain"])]:
        rc, out, err = run_cmd(cmd)
        git[key] = {"exit": rc, "stdout": out.strip(), "stderr": err.strip()}

    status_lines = [ln for ln in git["status"]["stdout"].splitlines() if ln.strip()]
    self_lines = [ln for ln in status_lines if SELF_PREFIX in ln]
    other_lines = [ln for ln in status_lines if SELF_PREFIX not in ln]

    runs = pool_runs()
    files = []

    def add(group, rel):
        p = os.path.join(ROOT, rel)
        files.append({"group": group, "path": rel, "exists": os.path.exists(p),
                      "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                      "sha256": sha256(p) if os.path.exists(p) else None})

    for r in runs:
        for f in RUN_FILES:
            add("run", f"回测日志/event_trend_v0_1/runs/{r}/{f}")
    for rel in CODE:
        add("code", rel)
    for rel in P06_EVID:
        add("p06", f"evidence/PRECISION-06/{rel}")

    doc = {
        "label": label,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": ROOT,
        "git": git,
        "status_summary": {"total": len(status_lines),
                           "self_p07": len(self_lines), "other": len(other_lines),
                           "self_entries": self_lines, "other_entries": other_lines},
        "run_count": len(runs),
        "runs": runs,
        "files": files,
    }
    jp = os.path.join(HERE, f"基线_{label}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    missing = [r["path"] for r in files if not r["exists"]]
    lines = [f"# PRECISION-07 基线（{label}）", "",
             f"- 生成时间（UTC）：{doc['generated_utc']}",
             f"- 工程根：`{ROOT}`",
             f"- 分支 / HEAD：`{git['branch']['stdout']}` / `{git['head']['stdout']}`（{git['head_time']['stdout']}）",
             f"- `git status` 总条目：**{len(status_lines)}**"
             f"（本窗口 `{SELF_PREFIX}` 占 {len(self_lines)} 条，其余 {len(other_lines)} 条为既有/他人改动）",
             f"- 输入：**{len(runs)} 个 run × {len(RUN_FILES)} 文件 + {len(CODE)} 个源码 + "
             f"{len(P06_EVID)} 份 P06 证据 = {len(files)} 项**，缺失 {len(missing)} 项",
             "", f"## 1. git status（不含本窗口自有条目，原样保留）", "", "```", *other_lines, "```",
             "", f"## 2. 本窗口自有条目", "", "```", *(self_lines or ["（无）"]), "```",
             "", "## 3. 输入 SHA256", "", "| 组 | 文件 | 字节 | SHA256 |", "|---|---|---|---|"]
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
          f"other={len(other_lines)} runs={len(runs)} files={len(files)} missing={len(missing)}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
