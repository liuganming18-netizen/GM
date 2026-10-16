#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WORK-01 行为基线比较（确定性，不读库、不联网）。

用法
────
    python 工具_基线比较.py 基线_改造前.json 基线_改造后.json

判据（分层，互不冒充）
──────────────────────
L1 `fixture_hash`      必须逐位相同 —— 否则输入变了，后面一切比较无意义。
L2 `signal_counts` / `reject_counts`  必须逐位相同 —— 信号层行为未变。
L3 `oi_quality_counts` **允许不同** —— W2-0.1 就是来改它的（整列广播 → 逐行）。
L4 `result_sha256`     **允许不同** —— 新增了 `close_open_positions_at_end*` 两个键。
L5 `behavior_core`     必须逐位相同 —— 只比对**行为**（trades / decisions /
   rejected_orders / equity_curve / reconcile / funding_gaps），
   并把「已知新增键」从两侧归一化掉。这一层才是「架构拆分未改变数值行为」的证据。

退出码：L1/L2/L5 任一不符 ⇒ 1；否则 0。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 归一化时忽略的键：新增字段（不是行为）
IGNORED_KEYS = ("close_open_positions_at_end", "close_open_positions_at_end_source",
                "label")

BEHAVIOR_KEYS = ("trades", "decisions", "rejected_orders", "equity_curve",
                 "reconcile", "funding_gaps", "trades_count", "net_return_pct",
                 "final_equity", "final_cash", "max_drawdown_pct",
                 "open_positions_remaining", "initial_equity", "time_unit",
                 "funding_model", "funding_is_assumption")


def _norm(o):
    """递归去掉 IGNORED_KEYS（字典键），其余原样保留。"""
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in sorted(o.items()) if k not in IGNORED_KEYS}
    if isinstance(o, list):
        return [_norm(x) for x in o]
    return o


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _first_diff(a, b, path="$"):
    """返回第一个差异的描述，相同则 None。"""
    if type(a) is not type(b):
        return f"{path}: 类型不同 {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            return (f"{path}: 键集合不同 仅前={sorted(ka - kb)[:5]} "
                    f"仅后={sorted(kb - ka)[:5]}")
        for k in sorted(ka):
            d = _first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: 长度不同 {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return f"{path}: {a!r} != {b!r}"
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    pa, pb = Path(sys.argv[1]), Path(sys.argv[2])
    A, B = _load(pa), _load(pb)

    ok = True
    print(f"A = {pa.name}  (label={A.get('label')})")
    print(f"B = {pb.name}  (label={B.get('label')})")
    print()

    # L1
    l1 = A.get("fixture_hash") == B.get("fixture_hash")
    print(f"L1 fixture_hash            : {'PASS' if l1 else 'FAIL'}  "
          f"{A.get('fixture_hash', '')[:16]}… vs {B.get('fixture_hash', '')[:16]}…")
    ok &= l1

    # L2
    for k in ("signal_counts", "reject_counts"):
        same = A.get(k) == B.get(k)
        print(f"L2 {k:<23}: {'PASS' if same else 'FAIL'}  {A.get(k)} vs {B.get(k)}")
        ok &= same

    # L3（信息层，允许不同）
    qa, qb = A.get("oi_quality_counts"), B.get("oi_quality_counts")
    print(f"L3 oi_quality_counts       : {'相同' if qa == qb else '不同（预期，W2-0.1）'}")
    print(f"   前: {qa}")
    print(f"   后: {qb}")

    # L4（哈希层，允许不同）
    ha, hb = A.get("result_sha256"), B.get("result_sha256")
    print(f"L4 result_sha256           : {'相同' if ha == hb else '不同（允许：新增键）'}")
    print(f"   前: {str(ha)[:32]}…")
    print(f"   后: {str(hb)[:32]}…")

    # L5（行为层，必须相同）
    print()
    print("L5 behavior_core（trades/decisions/rejected/equity_curve/reconcile/"
          "funding_gaps，已归一化新增键）")
    diffs = []
    for k in BEHAVIOR_KEYS:
        if k not in A and k not in B:
            continue
        d = _first_diff(_norm(A.get(k)), _norm(B.get(k)), f"$.{k}")
        print(f"   {k:<26}: {'PASS' if d is None else 'FAIL'}")
        if d:
            diffs.append(d)
    if diffs:
        print("\n   首个差异：")
        for d in diffs[:5]:
            print("     -", d)
    ok &= not diffs

    print()
    print("总判据:", "PASS —— 行为逐位一致" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
