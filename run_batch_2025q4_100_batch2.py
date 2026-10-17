# -*- coding: utf-8 -*-
"""
2025Q4 规范批量回测执行脚本：第二批 100 活跃标的 (2025-09-25 至 2025-12-31)

规格与纪律：
1. 本金严格 2000 USDT；
2. 回测区间严格锁定 2025-09-25 00:00:00 ~ 2025-12-31 23:59:59 (UTC)；
3. 单标的单独立运行目录；
4. 完整生成 chart.json、trades.jsonl、fills.jsonl、ledger.jsonl、orders.jsonl 等全套账本；
5. 自动汇总真实成交逐笔明细表，直观呈现每笔买卖点与盈亏；
6. 自动增量更新前端 UI 全市场成交表 public/all_trades.json。
"""

import os
import sys
import json
import time
import datetime
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
import backtest_chart as _BC

START_DATE = "2025-09-25"
END_DATE = "2025-12-31"

SYMBOLS_FILE = os.path.join(PROJECT_ROOT, "selected_100_symbols_2025q4_batch2.json")
with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
    TARGET_100_SYMBOLS = json.load(f)


def generate_chart(symbol: str, run_dir: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame, initial_equity: float):
    return _BC.generate_chart_payload(symbol, run_dir, df_1h, df_15m, initial_equity)


def update_all_trades_json(new_trades: list):
    all_trades_path = os.path.join(PROJECT_ROOT, "UI界面/web/public/all_trades.json")
    existing_items = []
    if os.path.exists(all_trades_path):
        try:
            with open(all_trades_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                existing_items = d.get("items", [])
        except Exception:
            existing_items = []

    # 按 (run_id, order_id/trade_id) 去重合并
    seen_keys = set()
    merged = []
    for t in existing_items:
        key = (t.get("run_id"), t.get("trade_id") or t.get("order_id"))
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(t)

    for t in new_trades:
        key = (t.get("run_id"), t.get("trade_id") or t.get("order_id"))
        if key not in seen_keys:
            seen_keys.add(key)
            # 补齐前端所需的时间和标识字段
            t["entry_time_ms"] = t.get("entry_time") or t.get("entry_time_ms")
            t["exit_time_ms"] = t.get("exit_time") or t.get("exit_time_ms")
            t["trade_id"] = t.get("trade_id") or t.get("order_id")
            merged.append(t)

    merged.sort(key=lambda x: x.get("entry_time_ms") or 0, reverse=True)

    with open(all_trades_path, "w", encoding="utf-8") as f:
        json.dump({
            "total": len(merged),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "items": merged
        }, f, indent=2, ensure_ascii=False)
    print(f"\n[前端同步] 全市场逐笔明细池已更新至 {all_trades_path}，当前共 {len(merged)} 笔真实成交！")


def main():
    print(f"================================================================")
    print(f"=== 开始执行 2025Q4 (2025-09-25 ~ 2025-12-31) 第二批 100 标的批量回测 ===")
    print(f"=== 标的总量: {len(TARGET_100_SYMBOLS)} | 账户本金: 2000 USDT ===")
    print(f"================================================================")

    cfg = EventTrendConfig(initial_equity=2000.0)
    runner = EventTrendRunner()

    results = []
    executed_trades = []
    start_all = time.time()

    for idx, sym in enumerate(TARGET_100_SYMBOLS, 1):
        t0 = time.time()
        print(f"[{idx:03d}/100] 正在回测 {sym:<14} ...", end=" ", flush=True)
        try:
            df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events = runner.load_symbol_data(
                sym, start_date=START_DATE, end_date=END_DATE
            )
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

            # 生成前端单屏图表载荷 (包含日线/15m 与标记)
            generate_chart(sym, run_dir, df_1h, df_15m, 2000.0)

            summary_a = out["res_a"]["summary"]
            net_pnl = summary_a.get("net_pnl", 0.0)
            trades_count = summary_a.get("trade_count", 0)
            ret_pct = (net_pnl / 2000.0) * 100.0
            elapsed = time.time() - t0

            # 读取该 run 产生的真实逐笔
            trades_file = os.path.join(run_dir, "trades.jsonl")
            run_trades = []
            if os.path.exists(trades_file):
                with open(trades_file, "r", encoding="utf-8") as f_tr:
                    for l in f_tr:
                        if l.strip():
                            t_obj = json.loads(l)
                            t_obj["run_id"] = main_run_id
                            t_obj["symbol"] = sym
                            run_trades.append(t_obj)
                            executed_trades.append(t_obj)

            status_str = f"【成交 {trades_count} 笔】净盈亏:{net_pnl:+7.2f} U ({ret_pct:+6.2f}%)" if trades_count > 0 else "0 笔成交 (观望)"
            print(f"-> 完成 ({elapsed:.2f}s) | {status_str}")

            results.append({
                "symbol": sym,
                "run_id": main_run_id,
                "status": "PASS",
                "trades": trades_count,
                "net_pnl": net_pnl,
                "return_pct": ret_pct,
                "elapsed_s": round(elapsed, 2),
                "funnel_boxes": out["funnel_a"]["boxes"],
                "candidates": out["funnel_a"]["candidates"]
            })
        except Exception as e:
            elapsed = time.time() - t0
            print(f"-> 失败 ({elapsed:.2f}s): {e}")
            results.append({
                "symbol": sym,
                "run_id": "",
                "status": f"FAIL: {e}",
                "trades": 0,
                "net_pnl": 0.0,
                "return_pct": 0.0,
                "elapsed_s": round(elapsed, 2),
                "funnel_boxes": 0,
                "candidates": 0
            })

    total_time = time.time() - start_all
    print(f"\n================================================================")
    print(f"=== 2025Q4 第二批 100 标的回测全部完成！总耗时: {total_time:.2f} 秒 ===")
    print(f"================================================================")

    # 统计指标
    tot_trades = sum(r["trades"] for r in results)
    tot_pnl = sum(r["net_pnl"] for r in results)
    trade_runs = [r for r in results if r["trades"] > 0]
    wins = [t for t in executed_trades if t.get("net_pnl", 0.0) > 0]
    losses = [t for t in executed_trades if t.get("net_pnl", 0.0) < 0]
    win_rate = (len(wins) / len(executed_trades) * 100.0) if executed_trades else 0.0

    print(f"1. 覆盖标的: {len(results)} 个")
    print(f"2. 触发成交标的: {len(trade_runs)} 个")
    print(f"3. 累计成交笔数: {tot_trades} 笔")
    print(f"4. 胜率: {len(wins)}/{len(executed_trades)} ({win_rate:.1f}%)")
    print(f"5. 累计净盈亏总和: {tot_pnl:+.2f} USDT")

    # 保存第二批 100 标的汇总结果
    summary_path = os.path.join(PROJECT_ROOT, "evidence/Work_2025Q4_100_batch2_summary.json")
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "window": {"start": START_DATE, "end": END_DATE},
            "batch": 2,
            "total_symbols": len(results),
            "trade_symbols_count": len(trade_runs),
            "total_trades": tot_trades,
            "win_rate": win_rate,
            "total_pnl": tot_pnl,
            "trades": executed_trades,
            "results": results
        }, f, indent=2, ensure_ascii=False)
    print(f"第二批汇总已保存至: {summary_path}")

    # 同步更新前端 all_trades.json
    if executed_trades:
        update_all_trades_json(executed_trades)

    # 打印逐笔明细表
    if executed_trades:
        print("\n【2025Q4 第二批真实成交逐笔明细清单】:")
        print(f"{'序号':<4} | {'标的':<12} | {'进场时间 (UTC)':<16} ~ {'出场时间 (UTC)':<16} | {'方向':<4} | {'开仓价':<10} | {'出场价':<10} | {'净盈亏':<10} | {'退出原因'}")
        print("-" * 105)
        executed_trades.sort(key=lambda x: x.get("entry_time", 0))
        for i, t in enumerate(executed_trades, 1):
            s = t.get("symbol")
            t_in = datetime.datetime.fromtimestamp(t["entry_time"]/1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
            t_out = datetime.datetime.fromtimestamp(t["exit_time"]/1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
            d = "做多" if t.get("direction") == 1 else "做空"
            en_p = float(t.get("entry_price", 0.0))
            ex_p = float(t.get("exit_price", 0.0))
            p = float(t.get("net_pnl", 0.0))
            r = t.get("exit_reason", "")
            print(f"{i:<4} | {s:<12} | {t_in:<16} ~ {t_out:<16} | {d:<4} | {en_p:<10.4f} | {ex_p:<10.4f} | {p:+9.2f} U | {r}")


if __name__ == "__main__":
    main()
