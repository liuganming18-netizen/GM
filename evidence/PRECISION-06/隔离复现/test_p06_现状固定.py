# -*- coding: utf-8 -*-
"""PRECISION-06 隔离复现 · 现状固定（characterization tests）。

⚠️ 重要：本文件断言的是**当前实现的真实行为**，不是「正确行为」。
   · 现在全绿 = 缺陷仍在（strict 不检查成交价是否落在 K 线区间内）；
   · 整改落地后，`test_CURRENT_*` 应当**转红** —— 那正是整改生效的信号。
   · 因此本文件放在 `evidence/PRECISION-06/隔离复现/`，不进生产测试目录，
     也不参与 93 项门禁。

每个用例的数值都取自真实 run（见 docstring 里的 run_id / order_id），
但 K 线是**合成夹具**（只为复现边界条件），因此结论只针对「算法行为」，
不构成对历史行情的回放。

运行：
  pytest evidence/PRECISION-06/隔离复现/test_p06_现状固定.py -q \
         --basetemp=evidence/PRECISION-06/tmp_pytest -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "策略") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "策略"))

from WyckoffMaster.event_trend_v0_1 import run_event_trend_backtest            # noqa: E402
from WyckoffMaster.event_trend_v0_1.tests.test_event_trend_closure import (   # noqa: E402
    HOUR_MS, MIN15_MS, _bars_15m, _candidate,
)
from WyckoffMaster.trading_core.fill import (                                 # noqa: E402
    FillRejected, SimulatedBarFillAdapter,
)

SLIP_BPS = 3.0      # = run 里 inputs.json → params.slippage_bps
TAKER_BPS = 5.0     # = params.taker_fee_bps


def _adapter(strict: bool) -> SimulatedBarFillAdapter:
    return SimulatedBarFillAdapter(slippage_bps=SLIP_BPS, maker_fee_rate=2e-4,
                                   taker_fee_rate=TAKER_BPS * 1e-4, strict=strict)


def _scenario(open_px: float, stop_px: float, *, bar_high: float, bar_low: float,
              tail_low: float | None = None, tag: str = "p06",
              symbol: str = "SOLUSDT"):
    """一根**非平坦**的执行 bar（open/high/low/close 各不相同）+ 后续 19 根。

    非平坦是必须的：项目既有夹具 `_bars_15m` 默认 O=H=L=C，
    任何「成交价越出区间」在这类夹具里都**看不出来**。
    """
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate(
        candidate_id=f"cand_{tag}", event_id=f"ev_{tag}", symbol=symbol,
        snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
        execution_earliest_ms=t_exec,
        box_high_h=open_px * 0.9, box_low_l=open_px * 0.8,
        breakout_close=open_px, initial_stop_price=stop_px,
        max_entry_price=open_px * 2.0, min_entry_price=open_px * 0.5,
    )
    df_1h = pd.DataFrame([{"timestamp": t_1h_open, "open": open_px, "high": open_px,
                           "low": open_px, "close": open_px, "volume": 1e6}])
    df_15m = _bars_15m(t_exec - MIN15_MS, 21, open_px=open_px, quote_volume=5_000_000.0)
    # 执行 bar（index 1）：给真实的高低点
    df_15m.loc[1, ["open", "high", "low", "close"]] = (open_px, bar_high, bar_low, open_px)
    if tail_low is not None:      # 后续 bar 打穿止损
        for k in range(2, 21):
            df_15m.loc[k, ["open", "high", "low", "close"]] = (
                tail_low, tail_low * 1.002, tail_low, tail_low)
    return cand, df_1h, df_15m


# ══════════════════════════ 1. 越界成交被放行（SPACE 型）══════════════════════════

def test_CURRENT_01_entry_above_bar_high_is_accepted():
    """SPACEUSDT 入场：0.009718 × 1.0003 = 0.00972092 → ceil(0.01) = 0.01。

    该 bar 的 high 只有 0.009721，成交价 0.01 比 high 高 2.87%。
    **strict=True 仍然放行**（R1–R5 全过），因为没有任何规则看 bar 区间。
    真实来源：run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451 / ord_61ca901294bdf932068e2b25
    """
    a = _adapter(strict=True)
    q = a.quote_entry(raw_price=0.009718, direction=1, price_step="0.01",
                      stop_price=0.006779650389958329)
    assert q.entry_fill == 0.01
    assert q.entry_fill > 0.009721           # 越出该 bar 的 high
    assert q.audit["post_round_price"] == "0.01"
    assert q.audit["post_slip_price"].startswith("0.0097209")


def test_CURRENT_02_exit_below_bar_low_is_accepted():
    """AIGENSYNUSDT 出场：止损 0.0189935 × 0.9997 = 0.01898781 → floor(0.01) = 0.01。

    该 bar 的 low 是 0.01899，成交价 0.01 比 low 低 47.3%。
    **strict 放行**（取整后 0.01 > 0，R5 不触发）。
    真实来源：run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703 / ord_1781728fdf1e5ea4c6248ec8
    """
    a = _adapter(strict=True)
    px = a.quote_exit(target_px=0.018993512924076944, direction=1, price_step="0.01",
                      reason="intrabar_stop")
    assert px == 0.01
    assert px < 0.01899
    assert a.last_exit_audit["round_delta"].startswith("-0.008987")


def test_CURRENT_03_step_coarser_than_price_is_accepted():
    """TAGUSDT 型：价格 0.0005959，步长 0.01 —— **步长是价格的 16.8 倍**。

    买入取整直接把价格放大到 0.01（16.8 倍），strict 依然放行。
    真实来源：run_WX_EVENT_TREND_V0_1_TAGUSDT_1792196833
    """
    a = _adapter(strict=True)
    q = a.quote_entry(raw_price=0.0005959, direction=1, price_step="0.01",
                      stop_price=0.0005)
    assert q.entry_fill == 0.01
    assert q.entry_fill / 0.0005959 > 16


# ══════════════════════════ 2. 真正被拦下的那一条 ══════════════════════════

def test_CURRENT_04_zero_price_exit_is_rejected():
    """SPACEUSDT 出场：止损 0.00677965 × 0.9997 = 0.00677762 → floor(0.01) = **0.0**。

    这是 8 条越界腿里**唯一**被拦下的一条（R5：取整后必须 > 0）。
    拦下它的原因是「价格变成 0」，不是「越出区间」——两者碰巧同时成立。
    真实来源：run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451 / ord_15bcc436236283cb00408881
    """
    a = _adapter(strict=True)
    with pytest.raises(FillRejected) as ei:
        a.quote_exit(target_px=0.006779650389958329, direction=1, price_step="0.01",
                     reason="intrabar_stop")
    assert ei.value.reason == "nonpositive_or_nonfinite_exit_fill"
    assert ei.value.detail["post_round_price"] == "0"


def test_CURRENT_05_legacy_mode_has_no_gate_at_all():
    """`strict=False`（run 当时唯一行为）连零价都不拦 —— 直接产出 0.0。"""
    a = _adapter(strict=False)
    px = a.quote_exit(target_px=0.006779650389958329, direction=1, price_step="0.01",
                      reason="intrabar_stop")
    assert px == 0.0


# ══════════════════════════ 3. 端到端：越界成交能过全部自检 ══════════════════════════

def test_CURRENT_06_end_to_end_two_out_of_range_legs_pass_all_selfchecks():
    """COTI 型端到端：两条腿都在 K 线区间外，成交仍成立，且自检全绿。

    入场 0.06228 → 0.07（bar high 0.06265）；出场止损 0.0618 → 0.06（bar low 0.0610）。
    期望（现状）：trades_count == 1、run_valid == True、
                 identity.chain_ok == True、reconcile.cash_identity_ok == True。
    也就是说：**物理上不可能的成交价，能一路通过身份链与现金恒等式自检。**
    """
    cand, df_1h, df_15m = _scenario(0.06228, 0.0618, bar_high=0.06265,
                                    bar_low=0.06177, tail_low=0.0610, tag="coti",
                                    symbol="COTIUSDT")
    res = run_event_trend_backtest("COTIUSDT", df_1h, df_15m, [cand], [],
                                   variant="A", price_step="0.01")
    eng = res["engine_result"]
    assert eng["trades_count"] == 1
    t = eng["trades"][0]
    assert t["entry_price"] == 0.07 and t["entry_price"] > 0.06265      # 越界
    assert t["exit_price"] == 0.06 and t["exit_price"] < 0.06177       # 越界
    assert res["precision"]["run_valid"] is True
    assert res["identity"]["chain_ok"] is True
    assert eng["reconcile"]["cash_identity_ok"] is True


def test_CURRENT_07_end_to_end_zero_exit_keeps_position_open():
    """SPACE 型端到端：入场 0.01 越界被接受；出场取整成 0 → 拒绝 → 仓位仍未平。"""
    cand, df_1h, df_15m = _scenario(0.009718, 0.006779650389958329,
                                    bar_high=0.009721, bar_low=0.009391,
                                    tail_low=0.0065, tag="space",
                                    symbol="SPACEUSDT")
    res = run_event_trend_backtest("SPACEUSDT", df_1h, df_15m, [cand], [],
                                   variant="A", price_step="0.01")
    eng = res["engine_result"]
    assert eng["trades_count"] == 0
    assert eng["open_positions_remaining"]            # 仓位未平
    assert res["precision"]["run_valid"] is False
    assert any("exit" in r for r in res["precision"]["invalid_reasons"])
    # 入场腿的越界成交确实进了订单流
    entries = [o for o in res["orders"] if o.get("action") == "open"]
    assert entries and entries[0]["fill_price"] == 0.01
    assert entries[0]["fill_price"] > 0.009721


# ══════════════════════════ 4. 对照：该拦的确实拦了 ══════════════════════════

def test_CURRENT_08_missing_spec_and_step_is_refused():
    """既无 instrument_spec 也无显式 price_step ⇒ 拒绝运行（不得回退 0.01）。"""
    cand, df_1h, df_15m = _scenario(100.0, 99.0, bar_high=100.2, bar_low=99.9, tag="nospec")
    with pytest.raises(Exception) as ei:
        run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], [], variant="A")
    assert "instrument_spec_required" in str(ei.value)


def test_CURRENT_09_normal_fill_inside_bar_is_untouched():
    """正常成交：价格 100.00、步长 0.01、bar [99.9, 100.2] ⇒ 成交 100.01，落在区间内。

    这条是「整改不得误伤正常成交」的基线。
    """
    cand, df_1h, df_15m = _scenario(100.0, 99.0, bar_high=100.2, bar_low=99.9,
                                    tail_low=98.5, tag="normal")
    res = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], [],
                                   variant="A", price_step="0.01")
    eng = res["engine_result"]
    assert eng["trades_count"] == 1
    t = eng["trades"][0]
    assert t["entry_price"] == 100.03          # 100.00 × 1.0003 = 100.03
    assert 99.9 <= t["entry_price"] <= 100.2
    assert res["precision"]["run_valid"] is True


def test_CURRENT_10_flat_bar_fixture_cannot_reveal_out_of_range():
    """既有夹具 `_bars_15m` 默认 O=H=L=C ⇒ 越界在夹具层面**不可见**。

    0.004 的价格配 0.01 步长，成交 0.01 其实是「高于 bar high 150%」，
    但夹具里 high 也是 0.004，测试只能通过「出场变 0」间接发现问题。
    """
    df = _bars_15m(0, 3, open_px=0.004)
    assert (df["high"] == df["low"]).all()
    assert (df["open"] == df["high"]).all()
