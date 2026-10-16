# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 · 执行核心「最小兼容修复」回归测试（2026-10-16）。

覆盖三处修复，均以「修复前会失败 / 修复后成立」的方式断言：

  A. 资金费**毫秒口径**（`_funding_events_ms` / `OpenPosition.last_funding_ms`）
     修复前：`//1000` 截秒比较 ⇒ 开仓后 **2ms** 的真实结算事件被当成「同一秒」而**漏计**。
     修复后：判据为 `last_funding_ms < ev_ms <= t_ms`（原始毫秒）⇒ 该事件必须入账；
             `ev_ms == entry_ms` 仍不入账（半开左端）；纯 seconds 输入仍兼容。

  B. 资金费**起算点 = 入场时刻**
     旧值 `-10**15` ⇒ 新仓入场后第一根 bar 会把**开仓前**的全部历史结算事件一次性入账。

  C. 预热不交易（`run_portfolio_execution(..., trade_start_sec=...)`）
     预热 bar 仍参与指标/因子滚动计算，但其信号**不得**在窗口内开仓，
     也不得经 searchsorted 落到窗口首根 15m。

运行：
  /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python -m pytest \
      evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_fixes.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "策略"))

from WyckoffMaster.oi_resonance_v1 import execution as X   # noqa: E402

MS = 1_000
H = 3_600_000
Q = 900_000

ENTRY_MS = 1_700_000_000_000
ENTRY_SEC = ENTRY_MS // 1000


# ══════════════════════════ A/B. 资金费 ══════════════════════════

def _pos(last_funding_ms):
    return X.OpenPosition(
        symbol="BTCUSDT", direction=1, signal_type=3,
        entry_time=ENTRY_MS, entry_time_sec=ENTRY_SEC,
        entry_price=100.0, qty=1.0, initial_stop=95.0, current_stop=95.0,
        entry_fee=0.05, last_mark_price=100.0, last_funding_ms=last_funding_ms)


def _ms_events(ms_list, rate=0.001, mark=100.0):
    return [{"time_ms": m, "rate": rate, "mark_price": mark} for m in ms_list]


def test_ms_event_2ms_after_entry_is_counted():
    """**核心回归（Codex 报的 P0）**：开仓后 2ms 的结算事件必须入账。

    修复前 `//1000` 把 1791734400002 截成 1791734400 == 入场秒 ⇒ 被 `entry < ev` 排除。
    """
    ev = ENTRY_MS + 2
    pos = _pos(ENTRY_MS)
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=ENTRY_MS + H,
                      open_px=100.0, rate=np.nan,
                      events=_ms_events([ev]), funding_is_assumption=False)
    assert pos.cum_funding_fee == pytest.approx(100.0 * 1.0 * 0.001)
    assert pos.last_funding_ms == ev
    assert pos.funding_gaps == []


def test_ms_event_exactly_at_entry_is_excluded():
    """半开左端：`ev_ms == entry_ms` 不入账（与引擎判据一致）。"""
    pos = _pos(ENTRY_MS)
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=ENTRY_MS + H,
                      open_px=100.0, rate=np.nan,
                      events=_ms_events([ENTRY_MS]), funding_is_assumption=False)
    assert pos.cum_funding_fee == 0.0


def test_ms_event_at_exit_bar_open_is_counted():
    """半开右端闭合：退出 bar 开盘**当刻**的应结事件不得丢。"""
    pos = _pos(ENTRY_MS)
    t_exit = ENTRY_MS + 28_800_000
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=t_exit,
                      open_px=100.0, rate=np.nan,
                      events=_ms_events([t_exit]), funding_is_assumption=False)
    assert pos.cum_funding_fee == pytest.approx(100.0 * 1.0 * 0.001)


def test_pre_entry_events_not_charged():
    """起算点 = 入场毫秒 ⇒ 开仓前事件一律不计（旧 `-1e15` 会把它们全部入账）。"""
    pre = [ENTRY_MS - 86_400_000, ENTRY_MS - 3_600_000]
    post = [ENTRY_MS + 28_800_000, ENTRY_MS + 57_600_000]
    pos = _pos(ENTRY_MS)
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=ENTRY_MS + 86_400_000,
                      open_px=100.0, rate=np.nan,
                      events=_ms_events(pre + post), funding_is_assumption=False)
    assert pos.cum_funding_fee == pytest.approx(2 * 100.0 * 1.0 * 0.001)
    assert pos.last_funding_ms == post[-1]


def test_old_sentinel_would_overcharge():
    """对照：把起算点设回旧哨兵值 ⇒ 开仓前事件会被多扣（证明修复必要性）。"""
    pre = [ENTRY_MS - 86_400_000, ENTRY_MS - 3_600_000]
    post = [ENTRY_MS + 28_800_000, ENTRY_MS + 57_600_000]
    pos = _pos(-10 ** 15)
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=ENTRY_MS + 86_400_000,
                      open_px=100.0, rate=np.nan,
                      events=_ms_events(pre + post), funding_is_assumption=False)
    assert pos.cum_funding_fee == pytest.approx(4 * 100.0 * 1.0 * 0.001)


def test_missing_fields_no_zero_fill():
    """缺 rate/mark ⇒ 记 funding_gap，**不补 0** 冒充成功。"""
    ev_ms = ENTRY_MS + 28_800_000
    pos = _pos(ENTRY_MS)
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=ev_ms,
                      open_px=100.0, rate=np.nan,
                      events=[{"time_ms": ev_ms, "rate": None,
                               "mark_price": None}], funding_is_assumption=False)
    assert pos.cum_funding_fee == 0.0
    assert pos.funding_gaps and "funding_event_missing_fields" in pos.funding_gaps[0]["reason"]
    assert pos.funding_gaps[0]["time_ms"] == ev_ms


def test_pure_seconds_input_still_compatible():
    """既有**纯 seconds 输入**兼容：`time_sec` ⇒ ×1000，且语义与旧实现一致。"""
    pos = _pos(ENTRY_MS)
    evs = [{"time_sec": ENTRY_SEC + 28800, "rate": 0.001, "mark_price": 100.0},
           {"time_sec": ENTRY_SEC - 3600, "rate": 0.001, "mark_price": 100.0}]
    X._settle_funding(pos, 10_000.0, model="settlement_events", t_ms=ENTRY_MS + 86_400_000,
                      open_px=100.0, rate=np.nan, events=X._funding_events_ms(evs),
                      funding_is_assumption=False)
    # 只有 ENTRY_SEC+28800 计入；ENTRY_SEC-3600 早于入场 ⇒ 不计
    assert pos.cum_funding_fee == pytest.approx(100.0 * 1.0 * 0.001)
    # 纯秒输入下「入场秒当刻」的事件仍不入账（与旧行为逐位一致）
    pos2 = _pos(ENTRY_MS)
    X._settle_funding(pos2, 10_000.0, model="settlement_events", t_ms=ENTRY_MS + H,
                      open_px=100.0, rate=np.nan,
                      events=X._funding_events_ms([{"time_sec": ENTRY_SEC, "rate": 0.001,
                                                    "mark_price": 100.0}]),
                      funding_is_assumption=False)
    assert pos2.cum_funding_fee == 0.0


def test_funding_time_ms_field_is_milliseconds():
    """币安口径 `funding_time`（毫秒）原样保留；显式 `time_unit="s"` 时按秒。"""
    a = X._funding_events_ms([{"funding_time": 1791734400002, "rate": 0.001,
                               "mark_price": 100.0}])
    assert a[0]["time_ms"] == 1791734400002
    b = X._funding_events_ms([{"funding_time": 1791734400, "rate": 0.001,
                               "mark_price": 100.0, "time_unit": "s"}])
    assert b[0]["time_ms"] == 1791734400000


def test_assumption_model_unchanged_cadence():
    """`assumption_fixed_8h` 仍按 8h（UTC 00/08/16）结算，且带假设标记。"""
    # 取一个对齐 28800s 网格、且距入场 >= 8h 的时刻
    t_ms = ((ENTRY_SEC // 28800) + 2) * 28800 * 1000
    assert t_ms - ENTRY_MS >= 28_800_000
    pos = _pos(ENTRY_MS)
    X._settle_funding(pos, 10_000.0, model="assumption_fixed_8h", t_ms=t_ms,
                      open_px=100.0, rate=0.001, events=[],
                      funding_is_assumption=True)
    assert pos.cum_funding_fee == pytest.approx(100.0 * 1.0 * 0.001)


# ══════════════════════════ C. 预热不交易 ══════════════════════════

def _synth(n_warm=60, n_trade=20, breakout_at=None):
    """构造 1h 信号序列 + 4×15m 执行序列；在 `breakout_at` 根放一次唐奇安突破。"""
    t0 = 1_700_000_000_000 // H * H
    rows, bars = [], []
    px = 100.0
    for i in range(n_warm + n_trade):
        # 交替微幅震荡 ⇒ ATR>0（compute_stop 要求 atr>0）
        o = px
        h = px + 1.0
        l = px - 1.0
        c = px + (0.2 if i % 2 == 0 else -0.2)
        if breakout_at is not None and i == breakout_at:
            c = px + 6.0
            h = c + 0.5
        rows.append({"timestamp": t0 + i * H, "open": o, "high": h, "low": l,
                     "close": c, "volume": 1000.0, "open_interest": np.nan})
        # 4 根 15m 与 1h 对齐（open=1h open，close=1h close）
        for k in range(4):
            if k == 0:
                b_o, b_c = o, o + (c - o) * 0.25
            elif k == 3:
                b_o, b_c = rows[-1]["close"], c
            else:
                b_o = b_c = o + (c - o) * (k / 4.0)
            bars.append({"timestamp": t0 + i * H + k * Q,
                         "open": b_o, "high": max(b_o, b_c) + 0.3,
                         "low": min(b_o, b_c) - 0.3, "close": b_c, "volume": 250.0})
        px = c
    return pd.DataFrame(rows), pd.DataFrame(bars)


def test_warmup_no_trade_and_no_leak():
    """trade_start_sec 之前不产生任何开仓；也不把预热信号泄漏到窗口首根 15m。"""
    sig, bar = _synth(breakout_at=30)                     # 信号落在预热段（≥ 信号循环起点 20）
    warm_end_sec = int(sig["timestamp"].iloc[60] / 1000)   # 窗口从第 60 根 1h 开始

    md = {"BTCUSDT": {"signal_bars": sig, "intrabar_bars": bar, "time_unit": "ms"}}
    r_all = X.run_portfolio_execution(md, trade_start_sec=None)
    assert r_all["trades_count"] >= 1, "未加约束时应能成交（对照组）"

    r_win = X.run_portfolio_execution(md, trade_start_sec=warm_end_sec)
    assert r_win["trades_count"] == 0
    assert r_win["decisions"] == []
    assert all(d["timestamp"] >= warm_end_sec * 1000 for d in r_win["decisions"])
    assert r_win["trade_start_sec"] == warm_end_sec


def test_warmup_keeps_indicators_hot():
    """预热 bar 仍参与指标计算：窗口内突破照常成交（不是「一刀切禁交易」）。"""
    sig, bar = _synth(breakout_at=65)                      # 信号落在窗口内
    warm_end_sec = int(sig["timestamp"].iloc[60] / 1000)
    md = {"BTCUSDT": {"signal_bars": sig, "intrabar_bars": bar, "time_unit": "ms"}}
    r = X.run_portfolio_execution(md, trade_start_sec=warm_end_sec)
    assert r["trades_count"] >= 1
    assert all(d["timestamp"] >= warm_end_sec * 1000 for d in r["decisions"])


def test_default_none_is_bitwise_backward_compatible():
    """缺省 trade_start_sec=None 时，与未加参数前行为一致（同一输入两次结果逐位相等）。"""
    sig, bar = _synth(breakout_at=10)
    md = {"BTCUSDT": {"signal_bars": sig, "intrabar_bars": bar, "time_unit": "ms"}}
    a = X.run_portfolio_execution(md)
    b = X.run_portfolio_execution(md, trade_start_sec=None)
    assert a["trades"] == b["trades"]
    assert a["equity_curve"] == b["equity_curve"]
    assert a["trade_start_sec"] is None


def test_entry_decision_carries_signal_bar_ms():
    """开仓决策携带**原始信号 bar 开盘毫秒** ⇒ 账本可联结回原始信号。"""
    sig, bar = _synth(breakout_at=65)
    warm_end_sec = int(sig["timestamp"].iloc[60] / 1000)
    md = {"BTCUSDT": {"signal_bars": sig, "intrabar_bars": bar, "time_unit": "ms"}}
    r = X.run_portfolio_execution(md, trade_start_sec=warm_end_sec)
    entries = [d for d in r["decisions"] if d.get("action") == "entry"]
    assert entries
    assert all(int(d["signal_bar_open_ms"]) > 0 for d in entries)
    # 信号 bar 开盘毫秒必须能在信号帧里找到该信号
    bars = set(int(x) for x in sig["timestamp"])
    assert all(int(d["signal_bar_open_ms"]) in bars for d in entries)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
