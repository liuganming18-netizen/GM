# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 第二批 10 个随机标的真实数据回测与事件穿透。

标的列表 (seed=456 抽取自 157 个 2024H1 完整覆盖数据池):
['RLCUSDT', 'SANDUSDT', 'PYTHUSDT', 'MOVRUSDT', 'ACEUSDT', 'TRBUSDT', 'BCHUSDT', 'JOEUSDT', 'STEEMUSDT', 'BANDUSDT']

参数约束:
- 本金: 2000.0 USDT
- 单笔风险预算: 0.5% (10 USDT)
- 费率: Taker 5 bps (0.0005) + 滑点 3 bps (0.0003)
- 参与率限制: 前一根 15m 成交额 0.1%
- 止损: 初始止损 Lr - 0.1A，盈利达到 2R 启用 10 根 4h 低点 Wilder ATR 跟踪止损
- 退出: 10 个自然日上限平仓
"""
import json
import os
import sys
import time
from pathlib import Path

# 加入路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "策略"))
sys.path.insert(0, str(PROJECT_ROOT / "框架" / "backend"))

from WyckoffMaster.event_trend_v0_1 import EventTrendRunner, EventTrendConfig
import backtest_store as S

SYMBOLS = [
    'RLCUSDT', 'SANDUSDT', 'PYTHUSDT', 'MOVRUSDT', 'ACEUSDT',
    'TRBUSDT', 'BCHUSDT', 'JOEUSDT', 'STEEMUSDT', 'BANDUSDT'
]

INITIAL_EQUITY = 2000.0

def main():
    cfg = EventTrendConfig(initial_equity=INITIAL_EQUITY)
    runner = EventTrendRunner()
    
    results = []
    print(f"=== 开始 WX_EVENT_TREND_V0_1 第二批 10 标的测试 (本金: {INITIAL_EQUITY} USDT) ===")
    
    for idx, symbol in enumerate(SYMBOLS, 1):
        print(f"\n[{idx}/10] 正在回测 {symbol} ...")
        t0 = time.time()
        try:
            # 1. 取数
            df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events = runner.load_symbol_data(
                symbol=symbol, start_date="2024-01-01", end_date="2024-06-30"
            )
            data_info = {
                "bar_4h": len(df_4h),
                "bar_1h": len(df_1h),
                "bar_15m": len(df_15m),
                "oi_records": len(oi_ts),
                "funding_records": len(funding_events)
            }
            
            # 2. 运行 A/B/C 三方案回测
            res = runner.run_variant_abc(
                symbol=symbol,
                df_4h=df_4h,
                df_1h=df_1h,
                df_15m=df_15m,
                oi_ts=oi_ts,
                oi_vals=oi_vals,
                funding_events=funding_events,
                cfg=cfg,
                initial_equity=INITIAL_EQUITY
            )
            
            main_run_id = res["main_run_id"]
            funnel_a = res["funnel_a"]
            funnel_b = res["funnel_b"]
            res_a = res["res_a"]
            sm_res = res_a["summary"]
            
            # 3. 提取执行明细
            trades = res_a.get("trades") or res_a.get("engine_result", {}).get("trades", [])
            val_results = res_a.get("validation_results", [])
            canceled_reasons = [v.execution_canceled_reason for v in val_results if not v.executed]
            
            # 4. 生成标准工作台 chart.json（使得 Web UI 也能查看）
            target_dir = S.run_dir(main_run_id)
            candles = [
                {"t": int(r.open_time), "o": float(r.open), "h": float(r.high),
                 "l": float(r.low), "c": float(r.close), "v": float(r.volume)}
                for r in df_15m.itertuples(index=False)
            ]
            chart_trades = []
            for tid_idx, t in enumerate(trades):
                chart_trades.append({
                    "trade_id": t.get("trade_id") or f"trd_{tid_idx+1}",
                    "symbol": symbol,
                    "direction": "long",
                    "signal_type": 1,
                    "entry": {"t": int(t.get("entry_time") or t.get("entry_time_ms") or 0),
                              "price": float(t.get("entry_price") or 0.0),
                              "qty": float(t.get("qty") or 0.0)},
                    "exit": {"t": int(t.get("exit_time") or t.get("exit_time_ms") or 0),
                             "price": float(t.get("exit_price") or 0.0),
                             "reason": str(t.get("exit_reason") or "")},
                    "net_pnl": float(t.get("net_pnl") or 0.0),
                    "initial_stop": float(t.get("initial_stop") or 0.0),
                    "stop_trace": [{"t": int(x.get("ts", 0)), "stop": float(x.get("stop", 0)), "reason": str(x.get("reason", ""))} for x in t.get("stop_trace", [])],
                })
            
            signals = []
            cand_file = target_dir / "candidates.jsonl"
            if cand_file.exists():
                for line in cand_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip(): continue
                    c = json.loads(line)
                    signals.append({
                        "t": int(c.get("entry_bar_time_ms") or 0),
                        "kind": "breakout_long",
                        "direction": "long",
                        "stop_price": float(c.get("initial_stop_price") or 0.0),
                        "atr": float(c.get("atr_1h") or 0.0),
                        "oi_score": float(c.get("oi_growth_pct") or 0.0),
                        "oi_available": bool(c.get("oi_available", False)),
                        "oi_quality": "real_5m",
                    })
            
            chart_equity = []
            eq_file = target_dir / "equity_curve.jsonl"
            if eq_file.exists():
                for line in eq_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip(): continue
                    e = json.loads(line)
                    chart_equity.append({
                        "t": int(e.get("timestamp") or e.get("t") or 0),
                        "equity": float(e.get("total_equity") or e.get("equity") or INITIAL_EQUITY),
                        "cash": float(e.get("cash_equity") or e.get("cash") or INITIAL_EQUITY),
                        "unrealized": float(e.get("unrealized_pnl") or 0.0),
                    })

            open_0 = float(df_15m['open'].iloc[0]) if len(df_15m) else 1.0
            bench_series = [
                {"t": int(r.open_time), "equity": float(INITIAL_EQUITY * (float(r.close) / open_0))}
                for r in df_15m.iloc[::4].itertuples(index=False)
            ]

            chart = {
                "symbol": symbol,
                "exec_timeframe": "15m",
                "window": {
                    "start_ms": int(df_1h["timestamp"].iloc[0]),
                    "end_ms": int(df_1h["timestamp"].iloc[-1]) + 3600 * 1000,
                },
                "candles": candles,
                "signals": signals,
                "trades": chart_trades,
                "equity_curve": chart_equity,
                "funding_events": [
                    {"t": int(f.get("time_ms") or 0), "rate": float(f.get("rate") or 0.0),
                     "mark": float(f.get("mark_price") or 0.0), "amount": 0.0}
                    for f in funding_events[:200]
                ],
                "benchmark": {
                    "symbol": symbol,
                    "return_pct": ((float(df_15m['close'].iloc[-1]) - float(df_15m['open'].iloc[0])) / float(df_15m['open'].iloc[0]) * 100.0) if len(df_15m) else 0.0,
                    "equity_series": bench_series,
                }
            }
            S.write_chart(main_run_id, chart)

            item = {
                "symbol": symbol,
                "run_id": main_run_id,
                "data_info": data_info,
                "funnel_a": funnel_a,
                "funnel_b": funnel_b,
                "candidates_count": funnel_a["candidates"],
                "executed_trades": len(trades),
                "canceled_reasons": canceled_reasons,
                "initial_equity": INITIAL_EQUITY,
                "final_equity": sm_res.get("final_equity", INITIAL_EQUITY),
                "net_pnl": sm_res.get("net_pnl", 0.0),
                "net_return_pct": ((sm_res.get("final_equity", INITIAL_EQUITY) - INITIAL_EQUITY) / INITIAL_EQUITY * 100.0),
                "trades": [
                    {
                        "entry_time": t.get("entry_time"),
                        "entry_price": t.get("entry_price"),
                        "qty": t.get("qty"),
                        "exit_time": t.get("exit_time"),
                        "exit_price": t.get("exit_price"),
                        "exit_reason": t.get("exit_reason"),
                        "net_pnl": t.get("net_pnl"),
                        "fees": t.get("fees"),
                        "funding_fee": t.get("funding_fee"),
                    }
                    for t in trades
                ],
                "elapsed_sec": round(time.time() - t0, 2)
            }
            results.append(item)
            
            pnl_str = f"{item['net_pnl']:+.2f} USDT ({item['net_return_pct']:+.2f}%)" if item['executed_trades'] > 0 else "0 交易"
            print(f"  -> 完成! 候选: {funnel_a['candidates']}, 成交: {len(trades)}, 盈亏: {pnl_str}, 耗时: {item['elapsed_sec']}s")
            if canceled_reasons:
                print(f"     [风控拦截原因]: {canceled_reasons}")
            if trades:
                for tr in trades:
                    print(f"     [成交闭环]: 入场 {tr.get('entry_price')} -> 出场 {tr.get('exit_price')} ({tr.get('exit_reason')}), 净利 {tr.get('net_pnl'):+.4f}u")
                    
        except Exception as exc:
            import traceback
            print(f"  -> 错误: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            results.append({
                "symbol": symbol,
                "error": str(exc),
                "elapsed_sec": round(time.time() - t0, 2)
            })

    # 保存报告
    out_file = PROJECT_ROOT / "evidence" / "Work_随机10币第二批事件回测结果_2026-10-17.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n=== 全部完成! 结果已保存至 {out_file} ===")

if __name__ == "__main__":
    main()
