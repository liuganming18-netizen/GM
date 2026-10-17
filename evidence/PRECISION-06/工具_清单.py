#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 60：产物 SHA256 清单（含被核验输入）。

用法：python3 工具_清单.py
输出：产物SHA256清单.txt / 产物SHA256清单.json
"""
from __future__ import annotations

import hashlib
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
P05 = os.path.join(ROOT, "evidence", "PRECISION-05")

OWN = [
    "PRECISION-06_越界成交腿与strict保护专项核验_2026-10-17.md",
    "表3_12条成交腿核验明细.csv",
    "表3b_全量成交腿扫描.csv",
    "表4_步长自证.csv",
    "表5_影子整改覆盖.csv",
    "逐腿核验.json",
    "步长自证.json",
    "影子整改.json",
    "影子端到端.json",
    "整改影响面.json",
    "基线_开工.json",
    "基线_开工.txt",
    "基线_收工.json",
    "基线_收工.txt",
    "逐腿核验原始输出.txt",
    "步长自证原始输出.txt",
    "影子整改原始输出.txt",
    "影子端到端原始输出.txt",
    "隔离复现/test_p06_现状固定.py",
    "隔离复现/test_p06_整改验收草案.py",
    "原始输出/pytest_既有93项.txt",
    "原始输出/pytest_既有93项_加R6影子.txt",
    "原始输出/pytest_P06_现状固定.txt",
    "原始输出/pytest_P06_整改验收.txt",
    "工具_00_基线.py",
    "工具_10_逐腿核验.py",
    "工具_20_步长自证.py",
    "工具_30_影子整改.py",
    "工具_40_影子端到端.py",
    "工具_50_既有测试影响面.py",
    "工具_清单.py",
]

INPUTS = [
    os.path.join(P05, "四笔成交逐笔核验.json"),
    os.path.join(P05, "保护机制探针结果.json"),
    os.path.join(P05, "表2_四笔成交逐笔核验.csv"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/fill.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/engine.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/policy.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/trading_core/position.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/runner.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/tests/test_fill_precision.py"),
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/tests/test_event_trend_closure.py"),
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
        # 统一用「工程根相对路径」，与输入项同一基准，验收方一条命令即可复算
        rows.append({"kind": "本窗口产物", "path": os.path.relpath(p, ROOT),
                     "exists": os.path.exists(p),
                     "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                     "sha256": sha(p) if os.path.exists(p) else None})
    for p in INPUTS:
        rows.append({"kind": "被核验输入", "path": os.path.relpath(p, ROOT),
                     "exists": os.path.exists(p),
                     "bytes": os.path.getsize(p) if os.path.exists(p) else None,
                     "sha256": sha(p) if os.path.exists(p) else None})

    n_own = sum(1 for r in rows if r["kind"] == "本窗口产物")
    n_in = sum(1 for r in rows if r["kind"] == "被核验输入")
    missing = [r["path"] for r in rows if not r["exists"]]

    with open(os.path.join(HERE, "产物SHA256清单.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "root": ROOT,
                   "path_basis": "全部为工程根相对路径",
                   "counts": {"own": n_own, "input": n_in, "total": len(rows),
                              "missing": len(missing)},
                   "files": rows}, f, ensure_ascii=False, indent=1)

    lines = ["# PRECISION-06 产物 SHA256 清单", "",
             f"- 生成时间（UTC）：{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
             f"- 工程根：`{ROOT}`",
             "- 路径基准：**全部为工程根相对路径**（产物与输入同一基准）",
             f"- 计数：本窗口产物 **{n_own}** 项 + 被核验输入 **{n_in}** 项 = **{len(rows)}** 项，"
             f"缺失 **{len(missing)}** 项", ""]
    for kind in ("本窗口产物", "被核验输入"):
        lines += [f"## {kind}", "", "| 文件 | 字节 | SHA256 |", "|---|---|---|"]
        for r in rows:
            if r["kind"] != kind:
                continue
            if not r["exists"]:
                lines.append(f'| `{r["path"]}` | — | **缺失** |')
            else:
                lines.append(f'| `{r["path"]}` | {r["bytes"]} | `{r["sha256"]}` |')
        lines.append("")
    with open(os.path.join(HERE, "产物SHA256清单.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 写入后自校验：从落盘的 json 重新读一遍，按工程根重新算哈希
    back = json.load(open(os.path.join(HERE, "产物SHA256清单.json"), encoding="utf-8"))
    bad = [r["path"] for r in back["files"]
           if not r["exists"] or sha(os.path.join(ROOT, r["path"])) != r["sha256"]]
    footer = ["", "## 自校验（本文件不计入清单，避免自指）", "",
              f"- 项数：{len(back['files'])}（产物 {n_own} + 输入 {n_in}）",
              f"- 缺失：{len(missing)} 项" + (f" → {missing}" if missing else "（无）"),
              f"- 落盘后重算哈希：不符 **{len(bad)}** 项" + (f" → {bad}" if bad else "（全部一致）"),
              "", "复算命令（工程根执行）：", "", "```sh",
              "cd /Users/boos/Desktop/威科夫因子共振趋势",
              "shasum -a 256 -c <(python3 - <<'PY'",
              "import json",
              "d=json.load(open('evidence/PRECISION-06/产物SHA256清单.json'))",
              "print('\\n'.join(f\"{r['sha256']}  {r['path']}\" for r in d['files']))",
              "PY",
              ")",
              "```", ""]
    with open(os.path.join(HERE, "产物SHA256清单.txt"), "a", encoding="utf-8") as f:
        f.write("\n".join(footer))

    print("\n".join(lines))
    print("\n".join(footer))
    print(f"自校验：{len(back['files'])} 项，哈希不符 {len(bad)} 项 {bad if bad else ''}")
    print(f"缺失：{missing if missing else '无'}")
    return 0 if (not bad and not missing) else 1


if __name__ == "__main__":
    raise SystemExit(main())
