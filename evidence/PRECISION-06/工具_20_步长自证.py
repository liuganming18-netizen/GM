#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 20：用 K 线自身的小数位反证「步长 = 0.01」。

思路（纯数据内部证伪，不依赖历史规格证据）
────────────────────────────────────────
交易所 K 线的 OHLC 都是**真实成交价**，必然落在 tick 网格上。
若某根 bar 的 open/high/low/close 出现了 0.001 位（如 `12.874`），
则 tick 一定 ≤ 0.001；此时用 0.01 取整**在算术上不可能**是当时的真实 tick。

本工具因此只做一件事：统计每个标的有多少根 bar 的 OHLC 不是 0.01 的整数倍，
并给出「数据允许的最粗步长」= 10^(−最大小数位)。

⚠️ 这是**证伪**，不是规格证明：
   · 它只能证明「tick ≠ 0.01」，不能证明 tick 到底等于多少；
   · 它不能替代历史 exchangeInfo 快照，也不能让任何 run 变成 `verified`。

用法：python3 工具_20_步长自证.py
输出：步长自证.json / 表4_步长自证.csv
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUNS_DIR = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")

SCOPE6 = [
    "run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451",
    "run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703",
    "run_WX_EVENT_TREND_V0_1_TRUTHUSDT_1792174465",
    "run_WX_EVENT_TREND_V0_1_COTIUSDT_1792174145",
    "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973",
    "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568",
]

STEP_TESTED = Decimal("0.01")


def decimals(v: float) -> int:
    """float → 最短往返十进制串 → 小数位数。"""
    s = format(Decimal(str(v)).normalize(), "f")
    if "." not in s:
        return 0
    return len(s.split(".")[1])


def on_grid(v: float, step: Decimal) -> bool:
    d = Decimal(str(v))
    return (d / step) == (d / step).to_integral_value()


def iter_candles(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    for c in doc.get("candles") or []:
        if isinstance(c, dict):
            g = lambda *ks: next(c[k] for k in ks if k in c)
            yield (int(g("time", "timestamp", "t")),
                   [float(g("open", "o")), float(g("high", "h")),
                    float(g("low", "l")), float(g("close", "c"))])
        else:
            yield int(c[0]), [float(c[1]), float(c[2]), float(c[3]), float(c[4])]


def analyse(run_id):
    run_dir = os.path.join(RUNS_DIR, run_id)
    chart = os.path.join(run_dir, "chart.json")
    if not os.path.exists(chart):
        return None
    with open(os.path.join(run_dir, "inputs.json"), encoding="utf-8") as f:
        inputs = json.load(f)
    symbol = inputs.get("symbol")

    bars = 0
    off_grid_bars = 0
    max_dec = 0
    examples = []
    trade_bars = {}
    for ts, ohlc in iter_candles(chart):
        bars += 1
        dec = max(decimals(v) for v in ohlc)
        if dec > max_dec:
            max_dec = dec
        if not all(on_grid(v, STEP_TESTED) for v in ohlc):
            off_grid_bars += 1
            if len(examples) < 3:
                examples.append({"ts": ts, "ohlc": ohlc,
                                 "decimals": [decimals(v) for v in ohlc]})

    # 成交当根 bar 单独看
    trades_p = os.path.join(run_dir, "trades.jsonl")
    if os.path.exists(trades_p):
        with open(trades_p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                t = json.loads(ln)
                for label, key in (("entry", "entry_time"), ("exit", "exit_time")):
                    trade_bars.setdefault(int(t[key]), []).append(label)
    hit = []
    for ts, ohlc in iter_candles(chart):
        if ts in trade_bars:
            hit.append({"ts": ts, "legs": trade_bars[ts], "ohlc": ohlc,
                        "on_grid_001": all(on_grid(v, STEP_TESTED) for v in ohlc),
                        "decimals": [decimals(v) for v in ohlc]})

    return {
        "symbol": symbol,
        "run_id": run_id,
        "bars": bars,
        "off_grid_bars": off_grid_bars,
        "off_grid_ratio": round(off_grid_bars / bars, 6) if bars else None,
        "max_decimals": max_dec,
        "implied_coarsest_tick": str(Decimal(1).scaleb(-max_dec)),
        "step_001_possible": off_grid_bars == 0,
        "examples": examples,
        "trade_bars": hit,
    }


def main():
    rows = []
    for d in sorted(os.listdir(RUNS_DIR)):
        if not d.startswith("run_WX_EVENT_TREND_V0_1_"):
            continue
        r = analyse(d)
        if r:
            rows.append(r)

    scope = [r for r in rows if r["run_id"] in SCOPE6]
    impossible = [r for r in rows if not r["step_001_possible"]]
    doc = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step_tested": str(STEP_TESTED),
        "method": "bar OHLC 是否为 0.01 的整数倍；出现非整数倍 ⇒ tick 必然 < 0.01 ⇒ 用 0.01 取整在算术上不可能。",
        "note": "只证伪，不证明具体 tick；不能替代历史 exchangeInfo 快照。",
        "runs_total": len(rows),
        "runs_where_001_impossible": len(impossible),
        "scope6": scope,
        "all": rows,
    }
    with open(os.path.join(HERE, "步长自证.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    with open(os.path.join(HERE, "表4_步长自证.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["标的", "run_id", "bar数", "OHLC不在0.01网格的bar数", "占比",
                    "最大小数位", "数据允许的最粗tick", "0.01是否可能", "示例"])
        for r in rows:
            w.writerow([r["symbol"], r["run_id"], r["bars"], r["off_grid_bars"],
                        r["off_grid_ratio"], r["max_decimals"],
                        r["implied_coarsest_tick"],
                        "可能" if r["step_001_possible"] else "不可能",
                        (r["examples"][0]["ohlc"] if r["examples"] else "")])

    print("=" * 96)
    print("步长自证：用 K 线 OHLC 小数位反证「step=0.01 不可能」")
    print("=" * 96)
    print(f"扫描 run 数：{len(rows)}；其中 0.01 在算术上**不可能**的 run：{len(impossible)}")
    print()
    print(f"{'标的':<15}{'bar数':>8}{'越格bar':>9}{'占比':>9}{'最大小数位':>11}"
          f"{'最粗tick':>12}{'0.01可能':>10}")
    for r in rows:
        print(f"{r['symbol']:<15}{r['bars']:>8}{r['off_grid_bars']:>9}"
              f"{r['off_grid_ratio']:>9.4f}{r['max_decimals']:>11}"
              f"{r['implied_coarsest_tick']:>12}"
              f"{('是' if r['step_001_possible'] else '否'):>10}")
    print()
    print("—— 6 个 P05 run 的成交当根 bar ——")
    for r in scope:
        for h in r["trade_bars"]:
            print(f"{r['symbol']:<15}{time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(h['ts']/1000))}"
                  f"  OHLC={h['ohlc']}  小数位={h['decimals']}  "
                  f"在0.01网格={h['on_grid_001']}  腿={h['legs']}")
    print()
    print("→ evidence/PRECISION-06/步长自证.json")
    print("→ evidence/PRECISION-06/表4_步长自证.csv")


if __name__ == "__main__":
    main()
