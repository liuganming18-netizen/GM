# -*- coding: utf-8 -*-
"""全自动化执行 BTC/ETH/SOL 真实数据回放、四大逐字段对账、独立合成边界反例验证，
并动态生成 evidence/Work_三币事件回测与对账结果_2026-10-16.json。
"""
from __future__ import annotations

import os
import sys

base_dir = "/Users/boos/Desktop/威科夫因子共振趋势"
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

import glob
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from 策略.WyckoffMaster.event_trend_v0_1.config import EventTrendConfig
from 策略.WyckoffMaster.event_trend_v0_1.contracts import CandidateSnapshot, ValidationResult
from 策略.WyckoffMaster.event_trend_v0_1.engine_adapter import run_event_trend_backtest
from 策略.WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner


def main():
    base_dir = "/Users/boos/Desktop/威科夫因子共振趋势"
    runner = EventTrendRunner(base_dir=base_dir)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    
    start_date = "2024-01-01"
    end_date = "2024-06-30"

    print("==================================================")
    print("开始执行 WORK-07 三币真实数据事件回测与四大逐字段对账...")
    print("==================================================")

    reconciliation_results = {}
    real_market_playback = {}

    for sym in symbols:
        print(f"\n>>> 正在加载并执行标的: {sym} ({start_date} ~ {end_date})")
        df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events = runner.load_symbol_data(
            sym, start_date=start_date, end_date=end_date
        )

        # 1. 执行四大逐字段对账
        print(f"[{sym}] 执行四大确定性与抗扰对账 (重复全量运行 / 逐条vs分块批量 / 注入99根重复Bar / 断点持久化与恢复)...")
        recon = runner.execute_reconciliation_suite(sym, df_4h, df_1h, oi_ts, oi_vals)
        reconciliation_results[sym] = {
            "repeatability_pass": recon["repeatability_pass"],
            "chunked_batch_pass": recon["chunked_batch_pass"],
            "duplicate_injection_pass": recon["duplicate_injection_pass"],
            "crash_recovery_pass": recon["crash_recovery_pass"]
        }
        print(f"[{sym}] 对账结果: 重复运行={recon['repeatability_pass']}, 分块批量={recon['chunked_batch_pass']}, 重复注入={recon['duplicate_injection_pass']}, 断点恢复={recon['crash_recovery_pass']}")

        # 2. 运行 A/B/C 三版本并落盘
        print(f"[{sym}] 运行 Variant A (完整含OI)、Variant B (关OI消融)、Variant C (成本压力测试)...")
        res_abc = runner.run_variant_abc(
            sym, df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events
        )

        real_market_playback[sym] = {
            "data_summary": {
                "bar_4h_count": len(df_4h),
                "bar_1h_count": len(df_1h),
                "bar_15m_count": len(df_15m),
                "oi_records_count": len(oi_ts),
                "funding_records_count": len(funding_events),
                "time_range": f"{start_date} ~ {end_date}"
            },
            "run_ids": res_abc["run_ids"],
            "funnel_variant_a": res_abc["funnel_a"],
            "funnel_variant_b": res_abc["funnel_b"],
            "summary_variant_a": res_abc["res_a"]["summary"],
            "summary_variant_b": res_abc["res_b"]["summary"],
            "summary_variant_c": res_abc["res_c"]["summary"]
        }

    # 3. 独立合成边界反例与撮合链路对账（严格区隔，不与真实数据混报）
    print("\n>>> 正在提取独立合成边界反例检验数据...")
    t0 = 1788652800000
    t_1h_open = t0 + 24 * 3600 * 1000
    t_1h_close = t_1h_open + 3600 * 1000
    t_exec = t_1h_close + 15 * 60 * 1000

    cand_valid = CandidateSnapshot.create(
        candidate_id="cand_synth_valid_001",
        event_id="ev_synth_001",
        strategy_version="v0.1.0",
        symbol="SOLUSDT",
        venue="binance",
        snapshot_time_ms=t_1h_close,
        available_at_ms=t_1h_close,
        box_high_h=110.0, box_low_l=100.0, box_atr_a=2.0, box_er=0.15,
        sweep_low_s=99.2, retest_low_lr=100.3, breakout_close=111.0,
        breakout_volume=2500.0, v20_median_volume=1000.0, volume_ratio=2.5,
        oi_breakout=103500.0, oi_6h_ago=100000.0, oi_growth_6h=0.035,
        initial_stop_price=100.1, initial_risk_r=10.9,
        max_entry_price=112.0, min_entry_price=110.0,
        execution_earliest_ms=t_exec
    )

    t_future_1h_open = t_1h_open + 30 * 24 * 3600 * 1000
    t_future_1h_close = t_future_1h_open + 3600 * 1000
    t_future_exec = t_future_1h_close + 15 * 60 * 1000
    cand_future = CandidateSnapshot.create(
        candidate_id="cand_synth_future_002",
        event_id="ev_synth_002",
        strategy_version="v0.1.0",
        symbol="SOLUSDT",
        venue="binance",
        snapshot_time_ms=t_future_1h_close,
        available_at_ms=t_future_1h_close,
        box_high_h=110.0, box_low_l=100.0, box_atr_a=2.0, box_er=0.15,
        sweep_low_s=99.2, retest_low_lr=100.3, breakout_close=111.0,
        breakout_volume=2500.0, v20_median_volume=1000.0, volume_ratio=2.5,
        oi_breakout=103500.0, oi_6h_ago=100000.0, oi_growth_6h=0.035,
        initial_stop_price=100.1, initial_risk_r=10.9,
        max_entry_price=112.0, min_entry_price=110.0,
        execution_earliest_ms=t_future_exec
    )

    df_1h_synth = pd.DataFrame([{
        "timestamp": t_1h_open, "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])
    # 第 0 根 = t_exec 前 15m 的已完成 bar（参与率上限需要）；第 1 根起为原反例序列
    records_15m_synth = [{
        "timestamp": t_exec - 15 * 60 * 1000,
        "open": 111.0, "high": 111.3, "low": 110.9, "close": 111.1,
        "volume": 50000.0, "quote_volume": 5_500_000.0
    }, {
        "timestamp": t_exec, "open": 111.2, "high": 111.8, "low": 111.0, "close": 111.5,
        "volume": 50000.0, "quote_volume": 5_575_000.0
    }]
    for k in range(1, 20):
        records_15m_synth.append({
            "timestamp": t_exec + k * 15 * 60 * 1000,
            "open": 111.5 + k * 0.5, "high": 111.8 + k * 0.5, "low": 111.3 + k * 0.5, "close": 111.6 + k * 0.5,
            "volume": 50000.0, "quote_volume": 5_580_000.0
        })
    df_15m_synth = pd.DataFrame(records_15m_synth)
    fund_synth = [{
        "time_ms": t_exec + 4 * 3600 * 1000, "rate": 0.0001, "mark_price": 115.0, "rate_type": "settled"
    }]

    # 运行多候选绑定验证（含真实 order_id 身份贯穿）
    res_binding = run_event_trend_backtest(
        symbol="SOLUSDT", df_1h=df_1h_synth, df_15m=df_15m_synth,
        candidates=[cand_valid, cand_future], funding_events=fund_synth, variant="A"
    )
    val_cand1 = res_binding["validation_results"][0]
    val_cand2 = res_binding["validation_results"][1]

    # 候选输入顺序扰动：归属必须与顺序无关
    res_binding_rev = run_event_trend_backtest(
        symbol="SOLUSDT", df_1h=df_1h_synth, df_15m=df_15m_synth,
        candidates=[cand_future, cand_valid], funding_events=fund_synth, variant="A"
    )
    by_id_fwd = {v.candidate_id: (v.executed, v.fill_time_ms, v.order_id)
                 for v in res_binding["validation_results"]}
    by_id_rev = {v.candidate_id: (v.executed, v.fill_time_ms, v.order_id)
                 for v in res_binding_rev["validation_results"]}

    # 运行跳空拦截验证
    df_15m_gap = pd.DataFrame([{
        "timestamp": t_exec, "open": 130.0, "high": 131.0, "low": 129.0, "close": 130.0,
        "volume": 50000.0, "quote_volume": 6_500_000.0
    }])
    res_gap = run_event_trend_backtest("SOLUSDT", df_1h_synth, df_15m_gap, [cand_valid], [], variant="A")
    val_gap = res_gap["validation_results"][0]

    # 运行跌回箱体拦截验证
    df_15m_drop = pd.DataFrame([{
        "timestamp": t_exec, "open": 108.0, "high": 109.0, "low": 107.0, "close": 108.5,
        "volume": 50000.0, "quote_volume": 5_400_000.0
    }])
    res_drop = run_event_trend_backtest("SOLUSDT", df_1h_synth, df_15m_drop, [cand_valid], [], variant="A")
    val_drop = res_drop["validation_results"][0]

    adversarial_synthetic_verification = {
        "multi_candidate_unique_binding": {
            "valid_candidate_executed": val_cand1.executed,
            "valid_candidate_qty": val_cand1.executed_qty,
            "valid_candidate_net_pnl": val_cand1.net_pnl,
            "valid_candidate_order_id": val_cand1.order_id,
            "valid_candidate_identity_status": val_cand1.identity_status,
            "future_candidate_executed": val_cand2.executed,
            "future_candidate_canceled_reason": val_cand2.execution_canceled_reason,
            "binding_test_pass": (val_cand1.executed is True and val_cand2.executed is False and val_cand1.executed_qty > 0)
        },
        "candidate_input_order_perturbation": {
            "forward": by_id_fwd,
            "reversed": by_id_rev,
            "order_independent": (by_id_fwd == by_id_rev),
            "test_pass": (by_id_fwd == by_id_rev and by_id_fwd["cand_synth_valid_001"][0] is True)
        },
        "identity_chain": res_binding.get("identity", {}),
        "gap_protection_test": {
            "open_price": 130.0,
            "max_entry_price": cand_valid.max_entry_price,
            "executed": val_gap.executed,
            "canceled_reason": val_gap.execution_canceled_reason,
            "test_pass": (val_gap.executed is False and "gap" in str(val_gap.execution_canceled_reason))
        },
        "drop_into_box_protection_test": {
            "open_price": 108.0,
            "min_entry_price": cand_valid.min_entry_price,
            "executed": val_drop.executed,
            "canceled_reason": val_drop.execution_canceled_reason,
            "test_pass": (val_drop.executed is False and "box" in str(val_drop.execution_canceled_reason))
        }
    }

    # 4. 组装最终 JSON 产物
    final_output = {
        "meta": {
            "title": "WORK-07 三币真实数据事件回测与四大逐字段对账结果",
            "timestamp": int(time.time()),
            "date": "2026-10-17",
            "strategy": "WX_EVENT_TREND_V0_1",
            "strategy_version": "v0.1.0",
            "rules_hash_sha256": "1f1167de35412cb7d1d3608d8069c4108324f7641de7c369bf94882f1679a8ab",
            "governance_status": {
                "work_06_gate": "FAIL (门禁未解除，严禁接入实盘)",
                "work_07_engineering_gate": "NOT_ASSERTED (本轮为第四次独立复核阻断项的整改轮；通过与否由独立复核判定，本报告不自行宣告 PASS)",
                "work_07_research_gate": "FAIL (真实行情 2024H1 样本集下严格门槛产出 0 候选，缺少真实交易统计显著性样本，绝不放宽条件凑样本)"
            },
            "rectification_scope_2026_10_17": {
                "identity_chain": "候选 ID → signal_id → order_id → 成交 全链等值贯穿；删除时间近似/顺序消耗/最近候选/拼接订单号四类兜底",
                "execution_rules": "2R 激活改用实际成交价与实际初始风险 R；接入 10 个自然日到期退出；接入前一根已完成 15m 成交额 0.1% 参与率上限",
                "artifacts": "Run 整套产物原子提交 + 代码/配置/输入/规则四类指纹 + orders/fills/positions/trades/ledger/equity 全链路逐笔证据",
                "pit": "PIT 仍为未证明（pit_proven=False），未伪造发布/到达时间",
                "ui_api_ledger_full_chain": "仍待独立验证（本轮未取得 UI/API 全链路通过证据）"
            },
            "pit_guarantee_statement": {
                "pit_proven": False,
                "note": "无交易所官方 API 发布延迟凭证前，回放将持仓序列对齐时间视为观测可得假设，严格标记为条件假设性研究，不冒称通过 PIT。"
            }
        },
        "four_reconciliation_verifications": reconciliation_results,
        "real_market_playback": real_market_playback,
        "adversarial_synthetic_verification": adversarial_synthetic_verification
    }

    out_path = os.path.join(base_dir, "evidence/Work_三币事件回测与对账结果_2026-10-16.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)

    print(f"\n>>> 成功生成对账与回测总结果文件: {out_path}")
    print("==================================================")


if __name__ == "__main__":
    main()
