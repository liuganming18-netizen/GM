# -*- coding: utf-8 -*-
"""成交精度修复 · 隔离回放（六标的 + 两对照）。

做什么
──────
对 SPACE / AIGENSYN / TRUTH / COTI（问题标的）与 BNB / ORDI（对照）：
  1. 按该标的**原有 run 的窗口**重新装载数据（同一份 parquet、同一套状态机）；
  2. 「修复前」= 磁盘上已落盘的真实成交记录（`trades.jsonl`），不重算、不改写；
  3. 「修复后」= 用交易所 `exchangeInfo` 的 tickSize / stepSize / minQty / minNotional
     重放同一批候选；
  4. 逐笔对照：原价 → 滑点后 → 取整后、数量、费用、账本恒等式。

不做
────
· 不写 `回测日志/`（只写本证据目录）；
· 不改策略阈值 / 止损 / OI 门槛 / 窗口；
· 不把「收益变化」当通过标准 —— 只对照精度与账本一致性。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROJECT_ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
for p in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "策略"),
          os.path.join(PROJECT_ROOT, "框架/backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd

from WyckoffMaster.event_trend_v0_1.config import EventTrendConfig
from WyckoffMaster.event_trend_v0_1.engine_adapter import run_event_trend_backtest
from WyckoffMaster.event_trend_v0_1.instrument_spec import resolve_instrument_spec
from WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner

RUNS = os.path.join(PROJECT_ROOT, "回测日志/event_trend_v0_1/runs")

TARGETS = [
    ("SPACEUSDT", "run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451"),
    ("AIGENSYNUSDT", "run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703"),
    ("TRUTHUSDT", "run_WX_EVENT_TREND_V0_1_TRUTHUSDT_1792174465"),
    ("COTIUSDT", "run_WX_EVENT_TREND_V0_1_COTIUSDT_1792174145"),
    ("BNBUSDT", "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973"),
    ("ORDIUSDT", "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568"),
]


def _utc(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _read_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_before(run_id: str) -> Dict[str, Any]:
    d = os.path.join(RUNS, run_id)
    trades = _read_jsonl(os.path.join(d, "trades.jsonl"))
    orders = _read_jsonl(os.path.join(d, "orders.jsonl"))
    summary = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
    return {"trades": trades, "orders": orders, "summary": summary,
            "inputs": json.load(open(os.path.join(d, "inputs.json"), encoding="utf-8"))}


def _window_of(inputs: Dict[str, Any]):
    tr = (inputs.get("data_summary") or {}).get("time_range_1h") or {}
    return tr.get("start_ms"), tr.get("end_ms")


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    runner = EventTrendRunner()
    cfg = EventTrendConfig(initial_equity=2000.0)
    report: Dict[str, Any] = {"generated_utc": datetime.now(timezone.utc).isoformat(),
                              "symbols": {}}

    for sym, run_id in TARGETS:
        print(f"\n=== {sym} ===")
        entry: Dict[str, Any] = {"run_id": run_id}
        try:
            before = _load_before(run_id)
            ws, we = _window_of(before["inputs"])
            entry["window"] = {"start_ms": ws, "end_ms": we,
                               "start_utc": _utc(ws), "end_utc": _utc(we)}
            print(f"  窗口 {_utc(ws)} → {_utc(we)}")

            # 同一窗口装载数据（与旧 run 完全同源：同一 parquet、同一裁剪规则）
            df_4h, df_1h, df_15m, oi_ts, oi_vals, funding = runner.load_symbol_data(
                sym, start_date=_utc(ws), end_date=_utc(we))
            entry["bars"] = {"4h": len(df_4h), "1h": len(df_1h), "15m": len(df_15m)}

            sm = runner.run_state_machine_simulation(
                sym, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True)
            entry["candidates"] = len(sm.candidates)
            print(f"  候选 {len(sm.candidates)} 个")

            closes = pd.to_numeric(df_15m["close"], errors="coerce").dropna()
            if len(closes) > 2000:
                closes = closes.iloc[:: max(1, len(closes) // 2000)]
            spec = resolve_instrument_spec(
                sym, window_start_ms=int(df_15m["timestamp"].min()),
                window_end_ms=int(df_15m["timestamp"].max()) + 900_000,
                venue=cfg.venue, market=cfg.market, order_type="MARKET",
                observed_prices=[float(x) for x in closes.tolist() if float(x) > 0])
            entry["spec"] = spec.to_dict()
            print(f"  规格 tick={spec.price_step} step={spec.qty_step} "
                  f"minQty={spec.min_qty} 适用性={spec.applicability} "
                  f"formal={spec.formal_pass_allowed}")

            res = run_event_trend_backtest(
                symbol=sym, df_1h=df_1h, df_15m=df_15m,
                candidates=sm.candidates, funding_events=funding,
                df_4h=df_4h, variant="A", initial_equity=2000.0, config=cfg,
                instrument_spec=spec)
            after_trades = (res.get("engine_result") or {}).get("trades", [])
            recon = (res.get("engine_result") or {}).get("reconcile", {})
            precision = res.get("precision", {})

            # ── 逐笔对照 ──
            rows = []
            for i in range(max(len(before["trades"]), len(after_trades))):
                b = before["trades"][i] if i < len(before["trades"]) else None
                a = after_trades[i] if i < len(after_trades) else None
                rows.append({
                    "index": i,
                    "before": ({"entry_time": _utc(b.get("entry_time")),
                                "exit_time": _utc(b.get("exit_time")),
                                "entry_price": b.get("entry_price"),
                                "exit_price": b.get("exit_price"),
                                "qty": b.get("qty"),
                                "gross_pnl": b.get("gross_pnl"),
                                "fees": b.get("fees"),
                                "funding_fee": b.get("funding_fee"),
                                "net_pnl": b.get("net_pnl"),
                                "slippage_loss": b.get("slippage_loss"),
                                "exit_reason": b.get("exit_reason")} if b else None),
                    "after": ({"entry_time": _utc(a.get("entry_time")),
                               "exit_time": _utc(a.get("exit_time")),
                               "entry_price": a.get("entry_price"),
                               "exit_price": a.get("exit_price"),
                               "qty": a.get("qty"),
                               "gross_pnl": a.get("gross_pnl"),
                               "fees": a.get("fees"),
                               "funding_fee": a.get("funding_fee"),
                               "net_pnl": a.get("net_pnl"),
                               "slippage_loss": a.get("slippage_loss"),
                               "slippage_loss_pure": a.get("slippage_loss_pure"),
                               "rounding_loss": a.get("rounding_loss"),
                               "price_audit": a.get("price_audit"),
                               "exit_reason": a.get("exit_reason")} if a else None),
                })
            entry["trades"] = rows
            entry["reconcile"] = recon
            entry["precision"] = {k: precision.get(k) for k in
                                  ("spec_status", "run_valid", "invalid_reasons",
                                   "rejects_count", "unclosed_positions")}
            entry["summary_after"] = res.get("summary", {})

            for r in rows:
                b, a = r["before"], r["after"]
                if b and a:
                    print(f"    [{r['index']}] 入场 {b['entry_price']} → {a['entry_price']} | "
                          f"出场 {b['exit_price']} → {a['exit_price']} | "
                          f"qty {b['qty']:.4f} → {a['qty']:.4f} | "
                          f"net {b['net_pnl']:+.4f} → {a['net_pnl']:+.4f}")
                elif b:
                    print(f"    [{r['index']}] 修复前有成交、修复后无（{b['entry_price']}→{b['exit_price']}）")
                elif a:
                    print(f"    [{r['index']}] 修复前无成交、修复后有（{a['entry_price']}→{a['exit_price']}）")

            print(f"  账本: cash_identity_ok={recon.get('cash_identity_ok')} "
                  f"equity_identity_ok={recon.get('equity_identity_ok')} "
                  f"slippage_split_ok={recon.get('slippage_split_ok')}")

        except Exception as exc:  # 隔离验证：单标的失败不影响其余
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()[-2000:]
            print(f"  失败: {entry['error']}")
        report["symbols"][sym] = entry

    path = os.path.join(out_dir, "回放结果.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n结果写入 {path}")
    return 0


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        PROJECT_ROOT, "evidence/Work_成交精度修复_2026-10-17/隔离回放")
    sys.exit(main(out))
