# -*- coding: utf-8 -*-
"""PRECISION-06 隔离复现 · 整改验收草案。

本文件把「最小整改方案」写成了**可执行的验收契约**，但**不实施整改**：

  · `test_FIX_01` / `test_FIX_02` 现在标 `xfail(strict=True)` —— 因为闸门还没实现；
    整改落地后它们应当**转绿**（xfail 自动消失 ⇒ 请删除 marker）。
  · `test_GUARD_*` 是「整改不得破坏」的护栏，现在和整改后都必须**一直是绿的**。

为什么放在 evidence/ 而不是生产测试目录
────────────────────────────────────────
生产测试目录里的用例是「现状契约」；本文件是「待落地契约」。
混在一起会让 `93 passed` 这个门禁数字失去意义（现在 93 全绿，但它不代表缺陷不存在）。

运行：
  pytest evidence/PRECISION-06/隔离复现/test_p06_整改验收草案.py -q \
         --basetemp=evidence/PRECISION-06/tmp_pytest -p no:cacheprovider
预期：2 xfailed, 4 passed
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
from WyckoffMaster.event_trend_v0_1.instrument_spec import (                  # noqa: E402
    APPLICABILITY_UNVERIFIED_HISTORICAL, resolve_instrument_spec,
)
from WyckoffMaster.event_trend_v0_1.tests.test_event_trend_closure import (   # noqa: E402
    HOUR_MS, MIN15_MS, _bars_15m, _candidate,
)
from WyckoffMaster.trading_core.fill import (                                 # noqa: E402
    FillRejected, SimulatedBarFillAdapter,
)


def _adapter(strict: bool = True) -> SimulatedBarFillAdapter:
    return SimulatedBarFillAdapter(slippage_bps=3.0, maker_fee_rate=2e-4,
                                   taker_fee_rate=5e-4, strict=strict)


def _scenario(open_px, stop_px, *, bar_high, bar_low, tail_low, tail_high,
              tag, symbol):
    """非平坦执行 bar + 后续 19 根落在 [tail_low, tail_high]（用来打穿止损）。"""
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
    df_15m.loc[1, ["open", "high", "low", "close"]] = (open_px, bar_high, bar_low, open_px)
    mid = (tail_low + tail_high) / 2.0
    for k in range(2, 21):
        df_15m.loc[k, ["open", "high", "low", "close"]] = (mid, tail_high, tail_low, mid)
    return cand, df_1h, df_15m


# ══════════════════════════ 待落地契约（现在 xfail）══════════════════════════

@pytest.mark.xfail(strict=True, reason="整改未实施：fill.py 无 price_step 与价格的合理性闸门")
def test_FIX_01_step_coarser_than_price_is_rejected():
    """R6：`price_step > raw_price` ⇒ 必须 `FillRejected('price_step_coarser_than_price')`。

    现状：0.0005959 配 0.01 步长，买入取整成 0.01（16.8 倍），放行。
    预期：直接拒绝，并留下 raw_price / price_step / stage 三件套。
    """
    a = _adapter()
    with pytest.raises(FillRejected) as ei:
        a.quote_entry(raw_price=0.0005959, direction=1, price_step="0.01",
                      stop_price=0.0005)
    assert ei.value.reason == "price_step_coarser_than_price"
    assert ei.value.detail["raw_price"] == "0.0005959"
    assert ei.value.detail["price_step"] == "0.01"


@pytest.mark.xfail(strict=True, reason="整改未实施：引擎无「成交价必须落在该 bar 区间」校验")
def test_FIX_02_out_of_range_fill_is_rejected_end_to_end():
    """R7：成交价越出该 bar `[low, high]` 超过滑点允许量 ⇒ 拒绝该腿，运行标记无效。

    现状：COTI 型夹具两条腿都在区间外（0.07 vs high 0.06265；0.06 vs low 0.06177），
          成交照样成立、自检全绿。
    预期：入场腿被拒，`rejected_orders` 出现 `fill_outside_bar_range`，
          `precision.run_valid is False`，`trades_count == 0`。

    注意：reason 字符串属于**待审契约**，验收时以「入场被拒 + 运行无效」为准，
    不要求必须叫这个名字。
    """
    cand, df_1h, df_15m = _scenario(0.06228, 0.0618, bar_high=0.06265,
                                    bar_low=0.06177, tail_low=0.0610,
                                    tail_high=0.0620, tag="coti_fix",
                                    symbol="COTIUSDT")
    res = run_event_trend_backtest("COTIUSDT", df_1h, df_15m, [cand], [],
                                   variant="A", price_step="0.01")
    eng = res["engine_result"]
    reasons = [r.get("reason") for r in eng.get("rejected_orders") or []]
    assert "fill_outside_bar_range" in reasons, f"实际拒绝原因：{reasons}"
    assert eng["trades_count"] == 0
    assert res["precision"]["run_valid"] is False


# ══════════════════════════ 护栏（现在与整改后都必须绿）══════════════════════════

def test_GUARD_01_correct_tick_still_fills_inside_bar():
    """真实 tick（1e-6）下，同一夹具必须成交，且成交价落在该 bar 区间内。

    这条防的是**过度整改**：不能为了消灭越界而把成交一律拒掉。
    """
    cand, df_1h, df_15m = _scenario(0.009718, 0.006779650389958329,
                                    bar_high=0.009721, bar_low=0.009391,
                                    tail_low=0.00670, tail_high=0.00690,
                                    tag="space_tick", symbol="SPACEUSDT")
    res = run_event_trend_backtest("SPACEUSDT", df_1h, df_15m, [cand], [],
                                   variant="A", price_step="0.000001")
    eng = res["engine_result"]
    assert eng["trades_count"] == 1
    t = eng["trades"][0]
    assert 0.009391 <= t["entry_price"] <= 0.009721
    assert 0.00670 <= t["exit_price"] <= 0.00690
    assert res["precision"]["run_valid"] is True


def test_GUARD_02_zero_price_exit_still_rejected():
    """零价防线不得因整改而放宽。"""
    a = _adapter()
    with pytest.raises(FillRejected) as ei:
        a.quote_exit(target_px=0.006779650389958329, direction=1, price_step="0.01",
                     reason="intrabar_stop")
    assert ei.value.reason == "nonpositive_or_nonfinite_exit_fill"


def test_GUARD_03_missing_spec_still_refused():
    """缺规格/缺显式步长仍然必须拒绝运行。"""
    cand, df_1h, df_15m = _scenario(100.0, 99.0, bar_high=100.2, bar_low=99.9,
                                    tail_low=98.5, tail_high=98.7, tag="nospec_fix",
                                    symbol="SOLUSDT")
    with pytest.raises(Exception) as ei:
        run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], [], variant="A")
    assert "instrument_spec_required" in str(ei.value)


def test_GUARD_04_historical_specification_stays_unverified():
    """整改不得把「当前快照」洗成「历史已验证」。

    2024H1 窗口只有单一时点快照 ⇒ 仍必须是 `unverified_historical`、
    `formal_pass_allowed is False`。当前快照里 tick 是多少都不改变这一点。
    """
    sp = resolve_instrument_spec("BNBUSDT", window_start_ms=1704067200000,
                                 window_end_ms=1719705600000)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.formal_pass_allowed is False
    assert sp.blocking is True
