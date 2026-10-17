# -*- coding: utf-8 -*-
"""规范批量回测运行脚本（严格 2000 USDT 本金，单标的单目录，生成完整图表与证据链）。"""
import os
import sys
import json
import time
import pandas as pd
import numpy as np

PROJECT_ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if f"{PROJECT_ROOT}/策略" not in sys.path:
    sys.path.insert(0, f"{PROJECT_ROOT}/策略")
if f"{PROJECT_ROOT}/框架/backend" not in sys.path:
    sys.path.insert(0, f"{PROJECT_ROOT}/框架/backend")

from WyckoffMaster.event_trend_v0_1.config import EventTrendConfig
from WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner
import backtest_store as S

TEST_SYMBOLS = [
    "ORDIUSDT",
    "ACEUSDT",
    "MOVRUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "RLCUSDT",
    "SANDUSDT",
    "PYTHUSDT",
    "TRBUSDT",
]

def generate_chart_payload(symbol: str, run_dir: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame, initial_equity: float):
    """统一的 chart 序列化契约（实现见 框架/backend/backtest_chart.py）。

    原实现在每个脚本里各写一份，并写死 signals=[] / funding_events=[] /
    benchmark.return_pct=0.0 / trade_id=null，stop_trace 用 `ts`、candles 用数组元组、
    json.dump 无 indent ⇒ 磁盘上出现**两套 chart 格式**（2026-10-17 查实）。
    现全部收敛到共享实现：K 线为对象、trade_id 取账本真实标识（positions.jsonl）、
    stop_trace 用官方 `t`、信号与资金费取自本 run 的 candidates.jsonl / funding.jsonl、
    基准如实标注为纯价格口径（net_return_pct 留空）。
    """
    _backend = os.path.join(PROJECT_ROOT, "框架", "backend")
    if _backend not in sys.path:
        sys.path.insert(0, _backend)
    import backtest_chart as _BC
    return _BC.generate_chart_payload(symbol, run_dir, df_1h, df_15m, initial_equity)


def main():
    print(f"=== 开始执行 10 标的规范回测（本金严格 2000 USDT）===")
    cfg = EventTrendConfig(initial_equity=2000.0)
    runner = EventTrendRunner()
    
    results = []
    
    for idx, sym in enumerate(TEST_SYMBOLS, 1):
        print(f"\n[{idx}/10] 正在回测 {sym} ...")
        t0 = time.time()
        try:
            df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events = runner.load_symbol_data(sym)
            out = runner.run_variant_abc(
                symbol=sym,
                df_4h=df_4h,
                df_1h=df_1h,
                df_15m=df_15m,
                oi_ts=oi_ts,
                oi_vals=oi_vals,
                funding_events=funding_events,
                cfg=cfg,
                initial_equity=2000.0,
                create_variant_runs=False
            )
            main_run_id = out["main_run_id"]
            run_dir = os.path.join(runner.runs_base, main_run_id)
            
            # 生成前端图表
            generate_chart_payload(sym, run_dir, df_1h, df_15m, 2000.0)
            
            summary_a = out["res_a"]["summary"]
            net_pnl = summary_a.get("net_pnl", 0.0)
            trades_count = summary_a.get("trade_count", 0)
            ret_pct = (net_pnl / 2000.0) * 100.0
            
            results.append({
                "symbol": sym,
                "run_id": main_run_id,
                "status": "PASS",
                "trades": trades_count,
                "net_pnl": net_pnl,
                "return_pct": ret_pct,
                "elapsed_s": round(time.time() - t0, 2),
                "funnel_boxes": out["funnel_a"]["boxes"],
                "candidates": out["funnel_a"]["candidates"]
            })
            print(f"  -> 完成: 交易笔数={trades_count}, 净盈亏={net_pnl:.2f}U ({ret_pct:+.2f}%), 耗时={time.time()-t0:.2f}s")
        except Exception as e:
            print(f"  -> 失败: {e}")
            results.append({
                "symbol": sym,
                "run_id": None,
                "status": "FAIL",
                "error": str(e)
            })

    print("\n" + "="*80)
    print(" 10 标的回测汇总（本金 2000 USDT）")
    print("="*80)
    print(f"{'标的':<10} | {'状态':<6} | {'交易笔数':<6} | {'净盈亏(U)':<10} | {'收益率':<8} | {'4h箱体':<6} | {'候选':<6}")
    print("-" * 80)
    for r in results:
        if r["status"] == "PASS":
            print(f"{r['symbol']:<10} | {r['status']:<6} | {r['trades']:<6} | {r['net_pnl']:<10.2f} | {r['return_pct']:+7.2f}% | {r['funnel_boxes']:<6} | {r['candidates']:<6}")
        else:
            print(f"{r['symbol']:<10} | {r['status']:<6} | 报错: {r.get('error')}")
    print("="*80)

if __name__ == "__main__":
    main()
