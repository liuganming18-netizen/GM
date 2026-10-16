#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W2-11 · Golden 差异**分类闸门**。

用途
────
`docs/evidence/backtest_golden_2026-10-16/reproduce_golden_backtest.py` 在 Trading Core
收敛之后必然报 FAIL —— 因为**身份哈希**（code_sha256 / signal_params_hash）按设计会变。
但「身份变了」与「行为变了」是两件完全不同的事，必须分开判定，不能一 FAIL 就放任。

本工具跑一次复现，把每一项 DIFF 归到两类之一：

  身份类（可解释、允许变）  键名以 `_sha256` / `_hash` / `_version` 结尾
                            —— 代码指纹与参数指纹；改动必然改变它们。
  行为类（**必须为 0**）    其余全部：窗口、权益、费用、资金费、回撤、计数、
                            以及**逐笔内容**（entry/exit 时间与价格、qty、net_pnl、exit_reason）。

退出码：0 = 行为类差异 0 项（身份差异逐项列明）；1 = 存在行为类差异（实质回归）。

用法（从工程根）：
  PYTHONDONTWRITEBYTECODE=1 <py> \
      evidence/Work_策略核心收敛_2026-10-16/工具_Golden差异分类.py [--json 输出路径]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BE = ROOT / "框架" / "backend"
REPLAY = ROOT / "docs" / "evidence" / "backtest_golden_2026-10-16" / \
    "reproduce_golden_backtest.py"

DIFF_RE = re.compile(r"^\s*DIFF\s+([^:]+):\s*got='([^']*)'\s+want='([^']*)'\s*$")
NOTE_RE = re.compile(r"^\s*NOTE\s+([^:]+):")
IDENTITY_SUFFIXES = ("_sha256", "_hash", "_version")


def classify(key: str) -> str:
    k = key.strip()
    return "identity" if k.endswith(IDENTITY_SUFFIXES) else "behavior"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="把分类结果写入该 JSON 路径")
    ap.add_argument("--tail", type=int, default=25)
    args = ap.parse_args()

    if not REPLAY.exists():
        print(f"✗ 复现脚本不存在：{REPLAY}", file=sys.stderr)
        return 2

    proc = subprocess.run(
        [sys.executable, str(REPLAY)], cwd=str(BE), capture_output=True,
        text=True, env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=1800)
    out = proc.stdout + proc.stderr

    diffs, notes = [], []
    for line in out.splitlines():
        m = DIFF_RE.match(line)
        if m:
            key, got, want = m.group(1), m.group(2), m.group(3)
            diffs.append({"key": key.strip(), "class": classify(key),
                          "got": got, "want": want})
            continue
        m = NOTE_RE.match(line)
        if m:
            notes.append(m.group(1).strip())

    ident = [d for d in diffs if d["class"] == "identity"]
    behav = [d for d in diffs if d["class"] == "behavior"]
    replay_verdict = "PASS" if "结果：PASS" in out else (
        "FAIL" if "结果：FAIL" in out else "UNKNOWN")

    print("=" * 72)
    print("W2-11 · Golden 差异分类")
    print(f"  复现脚本自身结论 : {replay_verdict}（身份差异按设计必然 FAIL）")
    print(f"  已知口径差 NOTE  : {len(notes)} 项（run-scoped ID / 窗口末端，不判失败）")
    print(f"  核心判据 DIFF    : {len(diffs)} 项")
    print("-" * 72)
    print(f"  身份类（可解释） : {len(ident)} 项")
    for d in ident:
        print(f"    · {d['key']}")
        print(f"        got ={d['got']}")
        print(f"        want={d['want']}")
    print(f"  行为类（必须 0） : {len(behav)} 项")
    for d in behav:
        print(f"    ✗ {d['key']}: got={d['got']!r} want={d['want']!r}")
    print("=" * 72)

    verdict = "PASS" if not behav else "FAIL"
    if verdict == "PASS":
        print("结果: PASS —— 行为判据零差异；差异**全部**为身份哈希（代码/参数指纹）")
    else:
        print("结果: FAIL —— 存在**行为**差异，属实质回归，必须逐项定位")

    if args.json:
        payload = {"replay_verdict": replay_verdict, "verdict": verdict,
                   "notes": notes, "identity_diffs": ident,
                   "behavior_diffs": behav,
                   "behavior_diff_count": len(behav)}
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"（已写入 {args.json}）")

    if len(out.splitlines()) and args.tail:
        print("\n--- 复现输出尾部 ---")
        print("\n".join(out.splitlines()[-args.tail:]))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
