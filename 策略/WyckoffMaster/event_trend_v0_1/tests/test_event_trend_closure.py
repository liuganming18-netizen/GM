# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 严谨回归测试套件。

覆盖独立复核指出的全部缺陷及反例检验：
1. 多候选唯一因果绑定（未来候选绝不成交，真实 qty/net_pnl 绝不掩盖为 0）；
2. **候选输入顺序扰动**不得改变成交归属（WORK-07 第四次复核阻断项 1）；
3. **提前 15 分钟成交必须被拒绝**：成交身份由 candidate_id 贯穿，不靠时间近似；
4. **身份无法解析必须拒绝发单**，不得用最近候选/时间距离兜底；
5. 跳空高开取消（open > 突破收盘 + 0.5A 精确拒绝）；
6. 跌回箱体取消（open < H 精确拒绝）；
7. **实际成交价与实际初始风险 R** 决定 2R 跟踪止损激活（不再用 breakout_close 预计算）；
8. **最长持仓 10 个自然日**到期按下一根 15m 开盘退出；
9. **参与率上限**：订单名义 ≤ 已完成前一根 15m 成交额 0.1%；成交额缺失 fail-closed；
10. SOL 真实数据回放、事件漏斗与去重抗扰；状态持久化与断点重启无损对账；
11. 真实 OI 决策与消融检验（A 组 vs B 组）。
"""
import copy
import glob
import json
import math
import os
import sys
import numpy as np
import pandas as pd
import pytest

base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
sys.path.insert(0, os.path.join(base_dir, "策略"))

from WyckoffMaster.event_trend_v0_1 import (
    EventStateMachine, EventTrendConfig, EventState,
    compute_wilder_atr_series, CandidateSnapshot, ValidationResult,
    run_event_trend_backtest
)
from WyckoffMaster.event_trend_v0_1.engine_adapter import (
    EventTrendExecutionPolicy, EventTrendExecParams, EventTrendSignalParams,
    build_exit_channel_15m
)
from WyckoffMaster.trading_core.fill import FillQuote

HOUR_MS = 3600 * 1000
MIN15_MS = 15 * 60 * 1000
DAY_MS = 24 * HOUR_MS

# 2026-10-17 成交精度修复：适配器不再内置 `price_step=0.01` 默认值。
# 本文件的机制测试统一用 SOLUSDT，其真实 tick 来自 binance exchangeInfo：
#   SOLUSDT  PRICE_FILTER.tickSize = "0.0100"（证据 数据/contracts/旧标的池/
#   exchangeInfo_futures_2026-10-05T045328Z.json，sha256 见 raw_meta.json）
# 因此显式声明 0.01 与修复前数值等价；不声明则适配器**拒绝运行**（刻意如此）。
SOL_TICK = "0.01"


def _run(symbol, *args, **kwargs):
    """本文件专用：显式声明 SOLUSDT 真实 tick，避免退回任何隐含默认步长。"""
    kwargs.setdefault("price_step", SOL_TICK)
    return run_event_trend_backtest(symbol, *args, **kwargs)


# ══════════════════════════ 合成夹具 ══════════════════════════

def _candidate(**overrides) -> CandidateSnapshot:
    base = dict(
        candidate_id="cand_synth_001",
        event_id="ev_synth_001",
        strategy_version="v0.1.0",
        symbol="SOLUSDT",
        venue="binance",
        snapshot_time_ms=1788742800000,
        available_at_ms=1788742800000,
        box_high_h=110.0, box_low_l=100.0, box_atr_a=2.0, box_er=0.15,
        sweep_low_s=99.2, retest_low_lr=100.3, breakout_close=111.0,
        breakout_volume=2500.0, v20_median_volume=1000.0, volume_ratio=2.5,
        oi_breakout=103500.0, oi_6h_ago=100000.0, oi_growth_6h=0.035,
        initial_stop_price=100.1, initial_risk_r=10.9,
        max_entry_price=112.0, min_entry_price=110.0,
        execution_earliest_ms=1788742800000 + MIN15_MS,
    )
    base.update(overrides)
    return CandidateSnapshot.create(**base)


def _bars_15m(start_ms: int, n: int, *, open_px: float, high=None, low=None,
              close=None, volume: float = 50000.0, quote_volume=None,
              step: int = MIN15_MS) -> pd.DataFrame:
    """构造 n 根 15m bar（默认横盘），含 volume 与 quote_volume。"""
    rows = []
    for k in range(n):
        ts = start_ms + k * step
        op = open_px
        hi = high if high is not None else op
        lo = low if low is not None else op
        cl = close if close is not None else op
        rows.append({
            "timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl,
            "volume": volume,
            "quote_volume": (quote_volume if quote_volume is not None
                             else cl * volume),
        })
    return pd.DataFrame(rows)


def _sol_15m_fixture(t_exec: int) -> pd.DataFrame:
    """复刻历史反例的 15m 输入，并**额外补一根已完成的前置 bar**（参与率上限需要）。

    第 0 根 = t_exec 前 15m 的已完成 bar（仅用于参与率上限，成交额充足不构成约束）；
    第 1 根 = 入场 bar（open=111.2 ⇒ entry_fill=111.24，与历史断言一致）；
    第 2..20 根 = 原反例的后续 19 根。
    """
    df = _bars_15m(t_exec - MIN15_MS, 21, open_px=111.2, quote_volume=5_500_000.0)
    df.loc[0, ["open", "high", "low", "close"]] = (111.0, 111.3, 110.9, 111.1)
    df.loc[1, ["open", "high", "low", "close"]] = (111.2, 111.8, 111.0, 111.5)
    for k in range(1, 20):
        df.loc[1 + k, ["open", "high", "low", "close"]] = (
            111.5 + k * 0.5, 111.8 + k * 0.5, 111.3 + k * 0.5, 111.6 + k * 0.5)
    return df


@pytest.fixture(scope="module")
def sol_market_data():
    """真实 SOL 样本（2024H1）的**自洽夹具**。

    夹具输入已固化进测试包 `tests/fixtures/sol_real_2024h1/`，不再指向
    `回测日志/oi_resonance_v1/runs/<某个 run>/inputs/` 这类会被其它工作流
    归档/清理的可变路径（该依赖曾导致测试在运行产物被归档后直接报错）。
    来源 run：`bt_20261016T101649Z_4aa21d94`（sha256 见 FIXTURE_PROVENANCE）。
    """
    fix_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "sol_real_2024h1")
    sb_path = os.path.join(fix_dir, "signal_bars.csv")
    oi_path = os.path.join(fix_dir, "oi_5m.csv")
    ib_path = os.path.join(fix_dir, "intrabar_bars.csv")
    if not all(os.path.exists(p) for p in (sb_path, oi_path, ib_path)):
        pytest.skip("sol_real_2024h1 夹具缺失，跳过依赖真实样本的用例")

    df_sb = pd.read_csv(sb_path)
    df_oi = pd.read_csv(oi_path)
    df_ib = pd.read_csv(ib_path)

    f4h_files = sorted(glob.glob(os.path.join(base_dir, "数据/市场数据/parquet/candles/binance/um/SOLUSDT/4h/*.parquet")))
    df_4h_hist = pd.concat([pd.read_parquet(f) for f in f4h_files], ignore_index=True)
    df_4h_hist = df_4h_hist.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    start_ms = df_sb["timestamp"].min()
    df_4h_pre = df_4h_hist[df_4h_hist["close_time"] <= start_ms].tail(250).copy()

    df_sb["bar_4h_open"] = (df_sb["timestamp"] // (4 * HOUR_MS)) * (4 * HOUR_MS)
    df_4h_curr = df_sb.groupby("bar_4h_open").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "timestamp": "count"
    }).reset_index()
    df_4h_curr = df_4h_curr[df_4h_curr["timestamp"] == 4].rename(columns={"bar_4h_open": "open_time"}).drop(columns=["timestamp"])
    df_4h_curr["close_time"] = df_4h_curr["open_time"] + 4 * HOUR_MS

    df_4h_all = pd.concat([
        df_4h_pre[["open_time", "close_time", "open", "high", "low", "close", "volume"]],
        df_4h_curr[["open_time", "close_time", "open", "high", "low", "close", "volume"]]
    ], ignore_index=True).sort_values("open_time").reset_index(drop=True)

    df_4h_all["atr"] = compute_wilder_atr_series(
        df_4h_all["high"].to_numpy(np.float64),
        df_4h_all["low"].to_numpy(np.float64),
        df_4h_all["close"].to_numpy(np.float64),
        period=14
    )
    return df_sb, df_oi, df_ib, df_4h_all


def _run_state_machine_on_records(records, df_4h_all, oi_ts, oi_vals, symbol="SOLUSDT", enable_oi_filter=True):
    sm = EventStateMachine(config=EventTrendConfig(), symbol=symbol, enable_oi_filter=enable_oi_filter)
    for r in records:
        t_open = int(r["timestamp"])
        t_close = t_open + HOUR_MS
        if t_close % (4 * HOUR_MS) == 0:
            sub_4h = df_4h_all[df_4h_all["close_time"] <= t_close]
            if len(sub_4h) >= 42:
                last_42 = sub_4h.tail(42)
                atr_val = float(sub_4h["atr"].iloc[-1])
                sm.check_and_form_box(
                    bar_4h_close_ms=t_close,
                    history_42_highs=last_42["high"].to_numpy(np.float64),
                    history_42_lows=last_42["low"].to_numpy(np.float64),
                    history_42_closes=last_42["close"].to_numpy(np.float64),
                    current_atr_a=atr_val
                )
        sm.process_1h_bar(
            open_time_ms=t_open, open_p=float(r["open"]), high_p=float(r["high"]),
            low_p=float(r["low"]), close_p=float(r["close"]), volume_v=float(r["volume"]),
            oi_timestamps=oi_ts, oi_values=oi_vals
        )
    return sm


# ══════════════════════════ 真实数据 / 状态机 ══════════════════════════

def test_sol_real_data_idempotence_and_dedup(sol_market_data):
    """【测试项 1】真实 SOL 样本回放、漏斗统计与逐字段幂等去重。"""
    df_sb, df_oi, df_ib, df_4h_all = sol_market_data
    records_normal = df_sb.to_dict("records")
    oi_ts = df_oi["ts"].to_numpy(np.int64)
    oi_vals = df_oi["open_interest"].to_numpy(np.float64)

    sm_a = _run_state_machine_on_records(records_normal, df_4h_all, oi_ts, oi_vals, enable_oi_filter=True)
    st_a = sm_a.export_state()

    sm_a_rep = _run_state_machine_on_records(records_normal, df_4h_all, oi_ts, oi_vals, enable_oi_filter=True)
    st_a_rep = sm_a_rep.export_state()
    assert json.dumps(st_a, sort_keys=True) == json.dumps(st_a_rep, sort_keys=True)

    dups = [copy.deepcopy(records_normal[100])] * 99
    records_dup = records_normal[:101] + dups + records_normal[101:]
    sm_dup = _run_state_machine_on_records(records_dup, df_4h_all, oi_ts, oi_vals, enable_oi_filter=True)
    st_dup = sm_dup.export_state()

    assert json.dumps(st_a["completed_events"], sort_keys=True) == json.dumps(st_dup["completed_events"], sort_keys=True)
    assert json.dumps(st_a["transitions"], sort_keys=True) == json.dumps(st_dup["transitions"], sort_keys=True)
    assert json.dumps(st_a["candidates"], sort_keys=True) == json.dumps(st_dup["candidates"], sort_keys=True)
    assert len(sm_a.completed_events) == 6
    assert len(sm_a.candidates) == 0


def test_crash_recovery_persistence(sol_market_data):
    """【测试项 2】崩溃断点持久化与无损还原。"""
    df_sb, df_oi, df_ib, df_4h_all = sol_market_data
    records = df_sb.to_dict("records")
    oi_ts = df_oi["ts"].to_numpy(np.int64)
    oi_vals = df_oi["open_interest"].to_numpy(np.float64)

    sm_part1 = _run_state_machine_on_records(records[:400], df_4h_all, oi_ts, oi_vals, enable_oi_filter=True)
    saved_snapshot = sm_part1.export_state()

    sm_recovered = EventStateMachine.restore_state(saved_snapshot)
    for r in records[400:]:
        t_open = int(r["timestamp"])
        t_close = t_open + HOUR_MS
        if t_close % (4 * HOUR_MS) == 0:
            sub_4h = df_4h_all[df_4h_all["close_time"] <= t_close]
            if len(sub_4h) >= 42:
                last_42 = sub_4h.tail(42)
                atr_val = float(sub_4h["atr"].iloc[-1])
                sm_recovered.check_and_form_box(
                    bar_4h_close_ms=t_close,
                    history_42_highs=last_42["high"].to_numpy(np.float64),
                    history_42_lows=last_42["low"].to_numpy(np.float64),
                    history_42_closes=last_42["close"].to_numpy(np.float64),
                    current_atr_a=atr_val
                )
        sm_recovered.process_1h_bar(
            open_time_ms=t_open, open_p=float(r["open"]), high_p=float(r["high"]),
            low_p=float(r["low"]), close_p=float(r["close"]), volume_v=float(r["volume"]),
            oi_timestamps=oi_ts, oi_values=oi_vals
        )
    st_recovered = sm_recovered.export_state()

    sm_full = _run_state_machine_on_records(records, df_4h_all, oi_ts, oi_vals, enable_oi_filter=True)
    st_full = sm_full.export_state()

    assert json.dumps(st_full["completed_events"], sort_keys=True) == json.dumps(st_recovered["completed_events"], sort_keys=True)
    assert json.dumps(st_full["transitions"], sort_keys=True) == json.dumps(st_recovered["transitions"], sort_keys=True)


def test_oi_ablation_and_pit_semantics():
    """【测试项 3】合成边界测试：真实 OI 增幅决策消融与 PIT 假设标注。"""
    config = EventTrendConfig()
    t0 = 1788652800000
    high_4h, low_4h, atr_4h = 110.0, 100.0, 2.0

    all_5m_ts = np.arange(t0, t0 + 30 * HOUR_MS, 5 * 60 * 1000, dtype=np.int64)
    t_bo_close = t0 + 23 * HOUR_MS

    vals_low = np.full(len(all_5m_ts), 100000.0, dtype=np.float64)
    idx_bo_close = np.searchsorted(all_5m_ts, t_bo_close, side="right") - 1
    vals_low[idx_bo_close:] = 101000.0  # +1.0% (低 OI)

    vals_high = np.full(len(all_5m_ts), 100000.0, dtype=np.float64)
    vals_high[idx_bo_close:] = 103500.0  # +3.5% (达标 OI)

    def _run_case(oi_vals, enable_oi):
        sm = EventStateMachine(config=config, symbol="SOLUSDT", enable_oi_filter=enable_oi)
        sm.check_and_form_box(
            bar_4h_close_ms=t0, history_42_highs=np.full(42, high_4h),
            history_42_lows=np.full(42, low_4h), history_42_closes=np.array([105.0, 104.0] * 21),
            current_atr_a=atr_4h
        )
        for i in range(1, 20):
            sm.process_1h_bar(t0 + i * HOUR_MS, 105.0, 106.0, 104.0, 105.0, 1000.0, all_5m_ts, oi_vals)
        sm.process_1h_bar(t0 + 20 * HOUR_MS, 101.0, 101.5, 99.2, 100.5, 2000.0, all_5m_ts, oi_vals)
        sm.process_1h_bar(t0 + 21 * HOUR_MS, 100.4, 100.8, 100.3, 100.6, 1200.0, all_5m_ts, oi_vals)
        cand = sm.process_1h_bar(t0 + 22 * HOUR_MS, 105.0, 111.5, 104.8, 111.0, 2500.0, all_5m_ts, oi_vals)
        return sm, cand

    sm_a_low, _ = _run_case(vals_low, enable_oi=True)
    sm_b_low, _ = _run_case(vals_low, enable_oi=False)
    assert len(sm_a_low.candidates) == 0
    assert sm_a_low.completed_events[0].current_state == EventState.OI_REJECTED
    assert len(sm_b_low.candidates) == 1

    sm_a_high, cand = _run_case(vals_high, enable_oi=True)
    assert len(sm_a_high.candidates) == 1
    c = sm_a_high.candidates[0]
    assert c.snapshot_time_ms == t_bo_close
    assert c.execution_earliest_ms == t_bo_close + MIN15_MS
    assert c.pit_proven is False  # 严格标注：无交易所发布凭证时不冒称通过
    assert "observation timestamp" in c.pit_assumption_note


# ══════════════════════════ 身份贯穿反例（阻断项 1）══════════════════════════

def test_adversarial_candidate_unique_binding_and_field_mapping():
    """【测试项 4】多候选唯一关联、未来候选不成交、真实字段读取、真实 order_id 贯穿。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS

    c1 = _candidate(
        candidate_id="cand_test_valid_001", event_id="ev_001",
        snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
        execution_earliest_ms=t_exec,
    )
    # 候选 2（反例）：30 天后的未来候选，在当前输入行情中绝对不可能发生撮合
    t_future_1h_open = t_1h_open + 30 * DAY_MS
    t_future_1h_close = t_future_1h_open + HOUR_MS
    c2_future = _candidate(
        candidate_id="cand_test_future_002", event_id="ev_002",
        snapshot_time_ms=t_future_1h_close, available_at_ms=t_future_1h_close,
        execution_earliest_ms=t_future_1h_close + MIN15_MS,
    )

    df_1h_sim = pd.DataFrame([{
        "timestamp": t_1h_open,
        "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])
    df_15m_sim = _sol_15m_fixture(t_exec)
    funding_events = [{
        "time_ms": t_exec + 4 * HOUR_MS, "rate": 0.0001, "mark_price": 115.0, "rate_type": "settled"
    }]

    res = _run(
        symbol="SOLUSDT", df_1h=df_1h_sim, df_15m=df_15m_sim,
        candidates=[c1, c2_future], funding_events=funding_events, variant="A"
    )

    val_1 = res["validation_results"][0]
    val_2 = res["validation_results"][1]

    # 断言 1：候选 1 成交，真实 qty / net_pnl 读取正确（绝非 0.0）
    # 2026-10-17：原断言写死 `4.4343204` / `42.97898`，是 initial_equity=10000 时代的常数；
    # 而本金唯一事实源 `EventTrendConfig.initial_equity` 现为 2000（配置早已改，测试没跟）
    # ⇒ 该断言在本次修复前就已是**假失败**。改为按配置推导，既保留数值严格性，又不会随配置漂移。
    assert val_1.executed is True
    assert val_1.executed_qty > 0.0
    _cfg = EventTrendConfig()
    _stop_fill = 100.1 * (1.0 - 3.0e-4)
    _unit_risk = abs(111.24 - _stop_fill) + (111.24 + _stop_fill) * 5.0e-4
    _expect_qty = _cfg.initial_equity * _cfg.risk_budget_per_trade / _unit_risk
    assert abs(val_1.executed_qty - _expect_qty) < 1e-9
    assert abs(val_1.net_pnl - res["summary"]["net_pnl"]) < 1e-12
    assert val_1.net_pnl > 0.0
    assert res["summary"]["net_pnl"] > 0.0
    assert val_1.identity_status == "ok"

    # 断言 2：order_id 是共享核心真实产出，可在 orders 中按 order_id / signal_id 溯源
    assert val_1.order_id is not None and str(val_1.order_id).startswith("ord_")
    by_id = {o["order_id"]: o for o in res["orders"]}
    assert val_1.order_id in by_id
    assert by_id[val_1.order_id]["signal_id"] == c1.candidate_id
    assert by_id[val_1.order_id]["status"] == "FILLED"
    # 底层成交记录自身也带身份字段（不再是空）
    tr = [t for t in res["engine_result"]["trades"] if t["signal_id"] == c1.candidate_id]
    assert len(tr) == 1 and tr[0]["order_id"] == val_1.order_id

    # 断言 3：候选 2（未来候选）绝不成交，绝不关联到候选 1 的成交
    assert val_2.executed is False
    assert val_2.fill_time_ms is None
    assert val_2.executed_qty is None
    assert val_2.order_id is None
    assert val_2.identity_status == "not_executed"
    assert res["identity"]["chain_ok"] is True
    assert res["summary"]["order_count"] > 0


def test_adversarial_candidate_input_order_perturbation():
    """【测试项 4b】候选输入顺序扰动：成交归属不得随输入顺序改变。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS

    c_cur = _candidate(candidate_id="cand_cur", event_id="ev_cur",
                       snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                       execution_earliest_ms=t_exec)
    # 复制当前候选并整体后移 900000ms（+15 分钟），赋予新 candidate_id/event_id
    c_shift = _candidate(
        candidate_id="cand_shift", event_id="ev_shift",
        snapshot_time_ms=t_1h_close + 900000,
        available_at_ms=t_1h_close + 900000,
        execution_earliest_ms=t_exec + 900000,
    )

    df_1h_sim = pd.DataFrame([{
        "timestamp": t_1h_open,
        "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])
    df_15m_sim = _sol_15m_fixture(t_exec)

    def _run_order(order):
        r = _run("SOLUSDT", df_1h_sim, df_15m_sim, order, [], variant="A")
        return {v.candidate_id: (v.executed, v.fill_time_ms, v.order_id)
                for v in r["validation_results"]}

    res_ab = _run_order([c_cur, c_shift])
    res_ba = _run_order([c_shift, c_cur])

    # 顺序无关：两种输入顺序下归属完全一致
    assert res_ab == res_ba
    assert res_ab["cand_cur"][0] is True
    assert res_ab["cand_shift"][0] is False
    # 成交时间必须等于候选自己声明的执行时刻（不得提前 15 分钟误绑）
    assert res_ab["cand_cur"][1] == t_exec
    assert res_ab["cand_shift"][1] is None


def test_adversarial_early_fill_must_be_rejected():
    """【测试项 4c】提前 15 分钟成交必须被拒绝：身份链不认「时间近似」。

    反例：候选 `execution_earliest_ms = t_exec + 15m`，行情里 t_exec 处就有一根
    open 合格的 bar。旧实现按 `|成交时刻 − 候选最早入场| <= 900000ms` 匹配，
    会把 t_exec 的成交「提前」绑给这个候选；新实现按 candidate_id 贯穿，
    成交只能发生在候选自己声明的执行时刻。
    """
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS

    df_1h_sim = pd.DataFrame([{
        "timestamp": t_1h_open,
        "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])
    df_15m = _sol_15m_fixture(t_exec)

    # ① 最早入场 = t_exec：成交时刻就是 t_exec
    cand_now = _candidate(candidate_id="cand_now", event_id="ev_now",
                          snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                          execution_earliest_ms=t_exec)
    v_now = _run("SOLUSDT", df_1h_sim, df_15m, [cand_now], [],
                                     variant="A")["validation_results"][0]
    assert v_now.executed is True
    assert v_now.fill_time_ms == t_exec

    # ② 最早入场 = t_exec + 15m：成交必须落在 t_exec + 15m，绝不提前到 t_exec
    cand_later = _candidate(candidate_id="cand_later", event_id="ev_later",
                            snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                            execution_earliest_ms=t_exec + MIN15_MS)
    v_later = _run("SOLUSDT", df_1h_sim, df_15m, [cand_later], [],
                                       variant="A")["validation_results"][0]
    assert v_later.executed is True
    assert v_later.fill_time_ms == t_exec + MIN15_MS
    assert v_later.fill_time_ms != t_exec


def test_missing_identity_must_not_produce_fill():
    """【测试项 4d】身份无法解析时必须拒绝发单，绝不用时间近似/最近候选兜底。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate(candidate_id="cand_known", event_id="ev_known",
                      snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                      execution_earliest_ms=t_exec)

    policy = EventTrendExecutionPolicy(
        signal_params=EventTrendSignalParams(price_step=SOL_TICK),
        exec_params=EventTrendExecParams(),
        strategy_key="WX_EVENT_TREND_V0_1",
        candidates=[cand])

    quote = FillQuote(entry_fill=111.2, stop_fill=100.1 * 0.9997,
                      source="simulated_bar", slippage_rate=3e-4, price_step=0.01)

    # ① 未知 signal_id ⇒ 拒绝，且原因是身份未解析（不是「未发生撮合」这种含糊话）
    dec_unknown = policy.decide_entry(
        symbol="SOLUSDT", signal_type=1, direction=1, raw_price=111.2,
        stop_ref=100.1, quote=quote, positions={}, cash=10000.0, equity_mtm=10000.0,
        signal_id="cand_does_not_exist", order_sequence=0,
        signal_bar_open_ms=t_1h_open)
    assert dec_unknown.accepted is False
    assert "candidate_identity_unresolved" in dec_unknown.reason

    # ② 身份正确但信号 bar 与候选快照不一致 ⇒ 拒绝
    dec_bad_bar = policy.decide_entry(
        symbol="SOLUSDT", signal_type=1, direction=1, raw_price=111.2,
        stop_ref=100.1, quote=quote, positions={}, cash=10000.0, equity_mtm=10000.0,
        signal_id=cand.candidate_id, order_sequence=0,
        signal_bar_open_ms=t_1h_open + HOUR_MS)
    assert dec_bad_bar.accepted is False
    assert "candidate_signal_bar_mismatch" in dec_bad_bar.reason

    # ③ 身份正确 ⇒ 接受，并记录**实际成交价与实际 R**
    dec_ok = policy.decide_entry(
        symbol="SOLUSDT", signal_type=1, direction=1, raw_price=111.2,
        stop_ref=100.1, quote=quote, positions={}, cash=10000.0, equity_mtm=10000.0,
        signal_id=cand.candidate_id, order_sequence=0,
        signal_bar_open_ms=t_1h_open)
    assert dec_ok.accepted is True
    ctx = policy.entry_ctx
    assert ctx["actual_entry_price"] == pytest.approx(111.2)
    assert ctx["actual_initial_risk_r"] == pytest.approx(111.2 - 100.1)
    assert ctx["trigger_level"] == pytest.approx(111.2 + 2.0 * (111.2 - 100.1))


def test_adversarial_gap_and_fell_back_protection():
    """【测试项 5】跳空高开与跌回箱体取消的实际触发验证。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS

    cand = _candidate(candidate_id="cand_test_protect_001", event_id="ev_001",
                      snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                      execution_earliest_ms=t_exec)

    df_1h_sim = pd.DataFrame([{
        "timestamp": t_1h_open,
        "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])

    df_15m_gap = pd.DataFrame([{
        "timestamp": t_exec, "open": 130.0, "high": 131.0, "low": 129.0, "close": 130.0,
        "volume": 50000.0, "quote_volume": 6_500_000.0
    }])
    res_gap = _run("SOLUSDT", df_1h_sim, df_15m_gap, [cand], [], variant="A")
    val_gap = res_gap["validation_results"][0]
    assert val_gap.executed is False
    assert "canceled_gap_above_0.5a" in val_gap.execution_canceled_reason

    df_15m_fell = pd.DataFrame([{
        "timestamp": t_exec, "open": 108.0, "high": 109.0, "low": 107.5, "close": 108.5,
        "volume": 50000.0, "quote_volume": 5_400_000.0
    }])
    res_fell = _run("SOLUSDT", df_1h_sim, df_15m_fell, [cand], [], variant="A")
    val_fell = res_fell["validation_results"][0]
    assert val_fell.executed is False
    assert "canceled_fell_back_into_box" in val_fell.execution_canceled_reason


# ══════════════════════════ 执行规则（阻断项 2）══════════════════════════

def test_trailing_2r_uses_actual_fill_not_breakout_close():
    """【测试项 6】2R 激活门槛必须用**实际成交价 + 实际初始风险 R**。

    旧口径（错误）：trigger = breakout_close + 2×候选暂估 initial_risk_r = 111.0 + 21.8 = 132.8
    新口径（正确）：trigger = 实际成交价 + 2×(实际成交价 − P0) = 111.2 + 22.2 = 133.4
    取 4h 收盘 = 133.0 ⇒ 旧口径会误激活，新口径不得激活。
    """
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate(candidate_id="cand_r2", event_id="ev_r2",
                      snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                      execution_earliest_ms=t_exec)

    policy = EventTrendExecutionPolicy(
        signal_params=EventTrendSignalParams(price_step=SOL_TICK),
        exec_params=EventTrendExecParams(),
        strategy_key="WX_EVENT_TREND_V0_1",
        candidates=[cand])
    quote = FillQuote(entry_fill=111.2, stop_fill=100.1 * 0.9997,
                      source="simulated_bar", slippage_rate=3e-4, price_step=0.01)
    dec = policy.decide_entry(
        symbol="SOLUSDT", signal_type=1, direction=1, raw_price=111.2,
        stop_ref=100.1, quote=quote, positions={}, cash=10000.0, equity_mtm=10000.0,
        signal_id=cand.candidate_id, order_sequence=0, signal_bar_open_ms=t_1h_open)
    assert dec.accepted is True

    old_trigger = cand.breakout_close + 2.0 * cand.initial_risk_r   # 132.8
    new_trigger = 111.2 + 2.0 * (111.2 - 100.1)                     # 133.4
    assert old_trigger < 133.0 < new_trigger

    # 4h 收盘 133.0：未达新触发线 ⇒ 不得激活、不得产生跟踪止损
    assert policy.trail_level(direction=1, exit_low=120.0,
                              exit_high=133.0, current_stop=100.1) is None
    # 4h 收盘 133.5 ≥ 133.4 ⇒ 激活，并上抬到最近 10 根 4h 最低 low
    lvl = policy.trail_level(direction=1, exit_low=120.0,
                             exit_high=133.5, current_stop=100.1)
    assert lvl == pytest.approx(120.0)
    # 只上调不下调
    assert policy.trail_level(direction=1, exit_low=118.0,
                              exit_high=140.0, current_stop=120.0) is None
    # 未成交（无 entry_ctx）时不得产生任何通道
    fresh = EventTrendExecutionPolicy(
        signal_params=EventTrendSignalParams(price_step=SOL_TICK), exec_params=EventTrendExecParams(),
        strategy_key="WX_EVENT_TREND_V0_1", candidates=[cand])
    assert fresh.trail_level(direction=1, exit_low=150.0,
                             exit_high=999.0, current_stop=100.1) is None


def test_max_holding_10_days_expiry_exit():
    """【测试项 7】最长持仓 10 个自然日：到期按下一根 15m 开盘退出。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate(candidate_id="cand_hold", event_id="ev_hold",
                      snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                      execution_earliest_ms=t_exec)

    df_1h_sim = pd.DataFrame([{
        "timestamp": t_1h_open,
        "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])
    # 横盘 11 天，价格始终远离止损（100.1），因此只会因到期退出
    df_15m_sim = _bars_15m(t_exec - MIN15_MS, 11 * 96 + 2, open_px=111.2)

    res = _run("SOLUSDT", df_1h_sim, df_15m_sim, [cand], [], variant="A")
    val = res["validation_results"][0]
    assert val.executed is True
    assert val.exit_reason == "max_holding_expired"
    assert val.exit_time_ms == t_exec + 10 * DAY_MS
    assert val.holding_duration_hours == pytest.approx(240.0)


def test_participation_cap_limits_order_notional():
    """【测试项 8】订单名义 ≤ 已完成前一根 15m 成交额 0.1%。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate(candidate_id="cand_part", event_id="ev_part",
                      snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
                      execution_earliest_ms=t_exec)

    df_1h_sim = pd.DataFrame([{
        "timestamp": t_1h_open,
        "open": 105.0, "high": 111.5, "low": 104.8, "close": 111.0, "volume": 2500.0
    }])

    # 基准：前一根 15m 成交额很大 ⇒ 参与率不构成约束
    big = _bars_15m(t_exec - MIN15_MS, 21, open_px=111.2, quote_volume=5_000_000.0)
    res_big = _run("SOLUSDT", df_1h_sim, big, [cand], [], variant="A")
    qty_big = res_big["validation_results"][0].executed_qty

    # 反例：前一根 15m 成交额只有 50000 ⇒ 上限 = 50 U，数量被压到 50/entry
    # （2026-10-17：原用 100000，但本金事实源已由 10000 改为 2000，风险定量降到
    #   ~0.887 SOL（名义 ~98.6U）⇒ 100U 上限不再构成约束，用例会假失败。
    #   改成 50000 保持「上限必须真的卡住」这一原始意图。）
    small = _bars_15m(t_exec - MIN15_MS, 21, open_px=111.2, quote_volume=50_000.0)
    res_small = _run("SOLUSDT", df_1h_sim, small, [cand], [], variant="A")
    val_small = res_small["validation_results"][0]
    assert val_small.executed is True
    assert val_small.executed_qty < qty_big
    assert val_small.entry_price * val_small.executed_qty <= 50.0 + 1e-6
    # 记录在 decisions 里，可追溯是否触发过参与率收缩
    capped = [d for d in res_small["engine_result"]["decisions"]
              if d.get("phase") == "C_entry"]
    assert capped and capped[0]["participation_capped"] is True

    # 成交额缺失 ⇒ fail-closed 拒绝，不用 0 冒充
    nan_qv = _bars_15m(t_exec - MIN15_MS, 21, open_px=111.2)
    nan_qv.loc[0, "quote_volume"] = np.nan
    res_nan = _run("SOLUSDT", df_1h_sim, nan_qv, [cand], [], variant="A")
    val_nan = res_nan["validation_results"][0]
    assert val_nan.executed is False
    assert val_nan.execution_canceled_reason == "participation_volume_unavailable"


def test_exit_channel_is_strictly_later_than_4h_close():
    """【测试项 9】退出通道只在 4h 收盘**之后**的 15m bar 生效，且入场前的 4h 收盘不参与。"""
    t_exec = 1788744600000
    # 12 根 4h bar：index 10 的 close_time = t_exec，index 11 = t_exec + 4h
    t4 = [t_exec - (10 - j) * 4 * HOUR_MS for j in range(12)]
    lows = [1000.0 + i for i in range(12)]      # roll10[10] = 1001, roll10[11] = 1002
    closes = [2000.0 + i for i in range(12)]    # closes[10] = 2010, closes[11] = 2011
    df_4h = pd.DataFrame({"close_time": t4, "close": closes, "low": lows})

    cand = _candidate(candidate_id="cand_chan", event_id="ev_chan",
                      snapshot_time_ms=t_exec - MIN15_MS,
                      available_at_ms=t_exec - MIN15_MS,
                      execution_earliest_ms=t_exec)
    df_15m = _bars_15m(t_exec - MIN15_MS, 20, open_px=111.2)

    el, eh = build_exit_channel_15m(df_15m, [cand], df_4h)

    # 入场前的 bar 与恰与 4h 收盘同刻的入场 bar 都不得带通道值
    assert np.isnan(el[0]) and np.isnan(eh[0])
    assert np.isnan(el[1]) and np.isnan(eh[1])
    # t_exec + 15m 起才可使用 close_time = t_exec 的 4h bar（严格更晚才生效）
    assert el[2] == pytest.approx(1001.0)
    assert eh[2] == pytest.approx(2010.0)
    # t_exec + 4h + 15m 起切到 close_time = t_exec + 4h 的那根
    idx_after = 1 + 16 + 1
    assert el[idx_after] == pytest.approx(1002.0)
    assert eh[idx_after] == pytest.approx(2011.0)


def test_quote_volume_column_is_required():
    """【测试项 10】缺 quote_volume 必须显式报错，而不是悄悄跳过参与率约束。"""
    from WyckoffMaster.trading_core.contracts import ExecutionInputError
    t_exec = 1788744600000
    cand = _candidate(candidate_id="cand_nv", event_id="ev_nv",
                      snapshot_time_ms=t_exec - MIN15_MS,
                      available_at_ms=t_exec - MIN15_MS,
                      execution_earliest_ms=t_exec)
    df_1h = pd.DataFrame([{
        "timestamp": t_exec - HOUR_MS, "open": 105.0, "high": 111.5,
        "low": 104.8, "close": 111.0, "volume": 2500.0}])
    df_15m = _bars_15m(t_exec - MIN15_MS, 5, open_px=111.2).drop(columns=["quote_volume"])
    with pytest.raises(ExecutionInputError):
        _run("SOLUSDT", df_1h, df_15m, [cand], [], variant="A")
