# -*- coding: utf-8 -*-
"""对随机抽取的 10 个数据丰富标的执行 WX_EVENT_TREND_V0_1 最新策略回测。"""
import os
import sys
import json
import time
import pandas as pd

base_dir = "/Users/boos/Desktop/威科夫因子共振趋势"
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from 策略.WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner

def run_10_symbols_test():
    sample_10 = [
        "BLURUSDT", "PENDLEUSDT", "ORDIUSDT", "CELOUSDT", "MAGICUSDT",
        "JTOUSDT", "NMRUSDT", "RVNUSDT", "AVAXUSDT", "SPELLUSDT"
    ]
    start_date = "2024-01-01"
    end_date = "2024-06-30"

    print("================================================================================")
    print(f"开始执行最新策略 WX_EVENT_TREND_V0_1 随机 10 标的回测 ({start_date} ~ {end_date})")
    print("================================================================================")

    runner = EventTrendRunner(base_dir=base_dir)
    results = {}

    for idx, sym in enumerate(sample_10, 1):
        print(f"\n[{idx}/10] 正在回测标的: {sym} ...")
        t0 = time.time()
        try:
            df_4h, df_1h, df_15m, oi_ts, oi_vals, fund = runner.load_symbol_data(sym, start_date, end_date)
            res_abc = runner.run_variant_abc(sym, df_4h, df_1h, df_15m, oi_ts, oi_vals, fund)
            
            funnel_a = res_abc["funnel_a"]
            summary_a = res_abc["res_a"]["summary"]
            elapsed = time.time() - t0

            results[sym] = {
                "symbol": sym,
                "data_bars": {
                    "4h": len(df_4h), "1h": len(df_1h), "15m": len(df_15m), "oi": len(oi_ts)
                },
                "funnel": funnel_a,
                "trading": summary_a,
                "elapsed_sec": round(elapsed, 2)
            }

            print(f"    - 箱体建档: {funnel_a['boxes']} 个 | 扫低: {funnel_a['sweeps']} 次 | 回踩: {funnel_a['retests']} 次 | 突破: {funnel_a['breakouts']} 次")
            print(f"    - 合格候选: {funnel_a['candidates']} 个 | 成交笔数: {summary_a['trade_count']} 笔 | 净盈亏: {summary_a['net_pnl']:.4f} USDT | 耗时: {elapsed:.2f}s")
        except Exception as e:
            print(f"    - 回测异常: {e}")
            results[sym] = {"symbol": sym, "error": str(e)}

    # 保存 10 币回测总结果 JSON
    out_file = os.path.join(base_dir, "evidence/Work_随机10币事件回测结果_2026-10-17.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "strategy": "WX_EVENT_TREND_V0_1",
                "time_range": f"{start_date} ~ {end_date}",
                "timestamp": int(time.time()),
                "sample_symbols": sample_10
            },
            "results": results
        }, f, indent=2, ensure_ascii=False)

    print("\n================================================================================")
    print(f"随机 10 币回测全部完成！汇总文件已保存至: {out_file}")
    print("================================================================================")

if __name__ == "__main__":
    run_10_symbols_test()
