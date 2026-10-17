#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 40：影子整改的**端到端**效果（进程内 monkeypatch，不改文件）。

做法
────
把 `engine_adapter` 模块里的 `SimulatedBarFillAdapter` 名字临时替换成一个带闸门的
子类，跑同一批夹具，看：
  · 入场腿是否被拒（`rejected_orders`）
  · `trades_count` / `run_valid` 怎么变
  · 正常成交是否被误伤

为什么只能这样验
────────────────
R7（成交价必须落在 bar 区间内）需要 bar 的 high/low，而 `quote_entry/quote_exit`
的签名里没有这两个参数 —— 必须改 `engine.py` 的 4 个调用点才能端到端生效。
所以本脚本只能端到端验 R6 / R6'；R7 的端到端验收留在
`隔离复现/test_p06_整改验收草案.py::test_FIX_02`（现在 xfail）。

用法：python3 工具_40_影子端到端.py
输出：影子端到端.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "策略"))

import pandas as pd                                                        # noqa: E402

from WyckoffMaster.event_trend_v0_1 import engine_adapter as EA            # noqa: E402
from WyckoffMaster.event_trend_v0_1 import run_event_trend_backtest        # noqa: E402
from WyckoffMaster.event_trend_v0_1.tests.test_event_trend_closure import (  # noqa: E402
    HOUR_MS, MIN15_MS, _bars_15m, _candidate,
)
from WyckoffMaster.trading_core.fill import (                              # noqa: E402
    FillRejected, SimulatedBarFillAdapter,
)

ORIGINAL = EA.SimulatedBarFillAdapter


class ShadowR6(SimulatedBarFillAdapter):
    def _guard(self, *, raw_price, price_step, kind):
        super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
        raw, step = Decimal(str(raw_price)), Decimal(str(price_step))
        if step > raw:
            raise FillRejected("price_step_coarser_than_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "stage": kind})


class ShadowR6Ratio(SimulatedBarFillAdapter):
    ratio = Decimal("0.01")

    def _guard(self, *, raw_price, price_step, kind):
        super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
        raw, step = Decimal(str(raw_price)), Decimal(str(price_step))
        if step > raw * self.ratio:
            raise FillRejected("price_step_too_coarse_for_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "ratio": str(self.ratio), "stage": kind})


def scenario(open_px, stop_px, *, bar_high, bar_low, tail_low, tail_high,
             tag, symbol):
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate(candidate_id=f"cand_{tag}", event_id=f"ev_{tag}", symbol=symbol,
                      snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                      execution_earliest_ms=t_exec,
                      box_high_h=open_px * 0.9, box_low_l=open_px * 0.8,
                      breakout_close=open_px, initial_stop_price=stop_px,
                      max_entry_price=open_px * 2.0, min_entry_price=open_px * 0.5)
    df_1h = pd.DataFrame([{"timestamp": t_1h_open, "open": open_px, "high": open_px,
                           "low": open_px, "close": open_px, "volume": 1e6}])
    df_15m = _bars_15m(t_exec - MIN15_MS, 21, open_px=open_px, quote_volume=5_000_000.0)
    df_15m.loc[1, ["open", "high", "low", "close"]] = (open_px, bar_high, bar_low, open_px)
    mid = (tail_low + tail_high) / 2.0
    for k in range(2, 21):
        df_15m.loc[k, ["open", "high", "low", "close"]] = (mid, tail_high, tail_low, mid)
    return cand, df_1h, df_15m


CASES = [
    dict(tag="space", symbol="SPACEUSDT", open_px=0.009718,
         stop_px=0.006779650389958329, bar_high=0.009721, bar_low=0.009391,
         tail_low=0.0065, tail_high=0.00655, step="0.01",
         note="SPACE 型：step/raw = 1.03"),
    dict(tag="coti", symbol="COTIUSDT", open_px=0.06228, stop_px=0.0618,
         bar_high=0.06265, bar_low=0.06177, tail_low=0.0610, tail_high=0.0620,
         step="0.01", note="COTI 型：step/raw = 0.16，两条腿都越界"),
    dict(tag="normal", symbol="SOLUSDT", open_px=100.0, stop_px=99.0,
         bar_high=100.2, bar_low=99.9, tail_low=98.5, tail_high=98.7, step="0.01",
         note="正常成交：step/raw = 0.0001"),
    dict(tag="tick", symbol="SPACEUSDT", open_px=0.009718,
         stop_px=0.006779650389958329, bar_high=0.009721, bar_low=0.009391,
         tail_low=0.00670, tail_high=0.00690, step="0.000001",
         note="真实 tick：step/raw = 0.0001"),
]

VARIANTS = [
    ("baseline", "现状 strict=True", None),
    ("R6", "step > raw ⇒ 拒绝", ShadowR6),
    ("R6ratio_0.01", "step > 1% × raw ⇒ 拒绝", ShadowR6Ratio),
]


def run_case(case, cls):
    if cls is None:
        EA.SimulatedBarFillAdapter = ORIGINAL
    else:
        EA.SimulatedBarFillAdapter = cls
    try:
        cand, df_1h, df_15m = scenario(
            case["open_px"], case["stop_px"], bar_high=case["bar_high"],
            bar_low=case["bar_low"], tail_low=case["tail_low"],
            tail_high=case["tail_high"], tag=case["tag"], symbol=case["symbol"])
        res = run_event_trend_backtest(case["symbol"], df_1h, df_15m, [cand], [],
                                       variant="A", price_step=case["step"])
    finally:
        EA.SimulatedBarFillAdapter = ORIGINAL
    eng = res["engine_result"]
    return {
        "trades_count": eng["trades_count"],
        "run_valid": res["precision"].get("run_valid"),
        "invalid_reasons": res["precision"].get("invalid_reasons"),
        "rejects_count": res["precision"].get("rejects_count"),
        "rejected_reasons": [r.get("reason") for r in eng.get("rejected_orders") or []],
        "orders": [(o.get("action"), o.get("status"), o.get("fill_price"))
                   for o in res.get("orders") or []],
        "entry_price": (eng["trades"][0]["entry_price"] if eng["trades_count"] else None),
        "exit_price": (eng["trades"][0]["exit_price"] if eng["trades_count"] else None),
    }


def main():
    out = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "note": "进程内 monkeypatch，跑完立即还原；生产文件未改动。",
           "cases": [], "matrix": {}}
    for case in CASES:
        row = {"case": case["tag"], "symbol": case["symbol"], "step": case["step"],
               "note": case["note"], "results": {}}
        for name, desc, cls in VARIANTS:
            row["results"][name] = run_case(case, cls)
        out["cases"].append(row)
    out["matrix"] = {n: d for n, d, _ in VARIANTS}

    with open(os.path.join(HERE, "影子端到端.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=" * 108)
    print("影子整改 · 端到端（monkeypatch 内存子类；生产文件零改动）")
    print("=" * 108)
    for row in out["cases"]:
        print(f"\n### {row['case']}（{row['symbol']}，step={row['step']}）— {row['note']}")
        print(f"{'方案':<14}{'成交数':>6}{'run_valid':>11}{'精度拒绝':>9}"
              f"{'入场价':>12}{'出场价':>12}  拒绝原因")
        for name, _, _ in VARIANTS:
            r = row["results"][name]
            ep = f"{r['entry_price']:.6g}" if r["entry_price"] is not None else "—"
            xp = f"{r['exit_price']:.6g}" if r["exit_price"] is not None else "—"
            print(f"{name:<14}{r['trades_count']:>6}{str(r['run_valid']):>11}"
                  f"{r['rejects_count']:>9}{ep:>12}{xp:>12}  "
                  f"{','.join(r['rejected_reasons']) or '无'}")
    print("\n→ evidence/PRECISION-06/影子端到端.json")


if __name__ == "__main__":
    main()
