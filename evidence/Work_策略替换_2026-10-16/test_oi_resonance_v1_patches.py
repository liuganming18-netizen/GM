#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 —— 2026-10-16 补丁组**合成回归测试**。

覆盖（对应用户本轮 4 条补丁 + 3 个已知失败）：
  P1  `exit_low_curr` / `exit_high_curr`（当期、不 shift）与执行层「优先 curr、缺列回退 prev」
      + 「收盘确认 → 下一根 15m 才生效」
  P2  fresh breakout 登记**先于** OI 门禁；连续突破不重锚
  P3  SC 先于 Spring（同根同时满足选 SC）
  P4  `portfolio_open_risk` 改为**入场成本本金风险**（mark 变化不影响；锁利后为 0；long/short 成本方向）
  P5  `factors` 按附件定义：0.50 分项 = **原始 OI 当期 rolling 分位**（`f_oi_level_rank`，不 shift）
  P6  LPS 去掉附件没有的 close_pos 过滤
  F1  `force_close_at_end=False` ⇒ `final_equity` == 权益曲线末值（且为真实 MTM）
  F2  显式时间单位 ⇒ 追加 future bars 不改变既有信号（前缀不变）
  F3  `compute_stop(inf, 2, 'uniform')` 必须拒绝

纯合成：不读库、不联网、不写任何业务状态。运行：
  <codex-python> -B test_oi_resonance_v1_patches.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
if str(ROOT / "策略") not in sys.path:
    sys.path.insert(0, str(ROOT / "策略"))

from WyckoffMaster.oi_resonance_v1 import config as C          # noqa: E402
from WyckoffMaster.oi_resonance_v1 import core as CORE          # noqa: E402
from WyckoffMaster.oi_resonance_v1 import execution as EXEC     # noqa: E402
from WyckoffMaster.oi_resonance_v1 import factors as F          # noqa: E402

T0_MS = 1_700_000_000_000          # 1h 网格起点（对齐 900s 网格）
H_MS = 3_600_000
M15_MS = 900_000
K = 250                            # 突破 bar 下标
WIN_X = C.SIGNAL_FROZEN.donchian_exit_window


# ────────────────────────── 合成数据 ──────────────────────────

def osc_rows(n: int):
    """n 根震荡 1h bar：h=106.0, l=104.0（⇒ 前 20 根最高 106 / 最低 104，ATR≈2）。"""
    rows = []
    for i in range(n):
        o, c = (104.5, 105.5) if i % 2 == 0 else (105.5, 104.5)
        rows.append({"open": o, "high": 106.0, "low": 104.0, "close": c, "volume": 1000.0})
    return rows


def hour_df(rows, oi=None, t0_ms=T0_MS):
    df = pd.DataFrame(rows)
    df.insert(0, "timestamp", [t0_ms + i * H_MS for i in range(len(df))])
    if oi is not None:
        df["open_interest"] = oi
    return df


def lps_scenario(extra=None):
    """震荡 → bar K 突破(108.0) → bar K+1 回踩不破、重新站上 ⇒ lps_pullback。"""
    rows = osc_rows(K)
    rows.append({"open": 106.5, "high": 108.5, "low": 105.0, "close": 108.0, "volume": 1000.0})
    rows.append({"open": 106.4, "high": 107.0, "low": 105.5, "close": 106.8, "volume": 1000.0})
    if extra:
        rows.extend(extra)
    return rows


def q15(n_bars, default=(106.0, 106.5, 105.5, 106.0), overrides=None, t0_ms=T0_MS):
    ov = overrides or {}
    rows = []
    for j in range(n_bars):
        ts = t0_ms + j * M15_MS
        o, h, l, c = ov.get(ts, default)
        rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def pos(**kw):
    base = dict(symbol="X", direction=1, signal_type=1, entry_time=0, entry_time_sec=0,
                entry_price=100.0, qty=1.0, initial_stop=95.0, current_stop=95.0,
                entry_fee=0.05, last_mark_price=100.0)
    base.update(kw)
    return EXEC.OpenPosition(**base)


# ══════════════════════════ P1 当期通道 ══════════════════════════

class TestP1CurrentChannel(unittest.TestCase):

    def test_curr_and_prev_columns_both_present_and_correct(self):
        df = hour_df(lps_scenario())
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        for col in ("exit_low_curr", "exit_high_curr", "exit_low_prev", "exit_high_prev"):
            self.assertIn(col, sig.columns)
        low = df["low"].to_numpy(float)
        high = df["high"].to_numpy(float)
        for i in (WIN_X, 60, K, K + 1):
            self.assertAlmostEqual(float(sig["exit_low_curr"][i]),
                                   float(low[i - WIN_X + 1:i + 1].min()), places=10)
            self.assertAlmostEqual(float(sig["exit_high_curr"][i]),
                                   float(high[i - WIN_X + 1:i + 1].max()), places=10)
            self.assertAlmostEqual(float(sig["exit_low_prev"][i]),
                                   float(low[i - WIN_X:i].min()), places=10)
        # 两列必须真的不同（否则测试无鉴别力）
        self.assertTrue((sig["exit_low_curr"] != sig["exit_low_prev"]).any())
        # curr 不含 shift：bar K 处应为 min(low[K-9..K]) = 104.0（含 bar K 自身 low=105 不影响）
        self.assertAlmostEqual(float(sig["exit_low_curr"][K]), 104.0, places=10)

    def test_empty_data_has_curr_columns(self):
        empty = pd.DataFrame({c: pd.Series(dtype=float)
                              for c in ("timestamp", "open", "high", "low", "close", "volume")})
        sig = CORE.compute_signals(empty, C.SIGNAL_FROZEN, "ms")
        self.assertEqual(len(sig), 0)
        for col in ("exit_low_curr", "exit_high_curr", "exit_low_prev", "exit_high_prev"):
            self.assertIn(col, sig.columns)

    def test_execution_prefers_curr_then_falls_back_to_prev(self):
        df = hour_df(lps_scenario())
        bars = q15((K + 4) * 4)
        a1 = EXEC._prep_symbol("X", df, bars, C.SIGNAL_FROZEN, "ms", 0.0)
        self.assertEqual(a1["exit_channel_source"], "exit_low_curr")
        # fallback：用 mock 临时把执行层内部的 compute_signals 换成「缺 curr 列 + prev 列为哨兵值」的帧。
        # 不改变生产接口，也不在生产代码里留「信任已算好帧」的捷径。
        sig_prev = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms").drop(
            columns=["exit_low_curr", "exit_high_curr"])
        sig_prev["exit_low_prev"] = 77.0          # 哨兵：能读到它才说明真的走了 prev 通道
        sig_prev["exit_high_prev"] = 88.0
        with mock.patch.object(EXEC, "compute_signals", return_value=sig_prev):
            a2 = EXEC._prep_symbol("X", df, bars, C.SIGNAL_FROZEN, "ms", 0.0)
        self.assertEqual(a2["exit_channel_source"], "exit_low_prev")
        j = 4 * (K + 1)
        self.assertAlmostEqual(float(a2["exit_low"][j]), 77.0, places=10)
        self.assertAlmostEqual(float(a2["exit_high"][j]), 88.0, places=10)
        # 且真实路径的 curr 值不是哨兵值（否则该断言无鉴别力）
        self.assertNotAlmostEqual(float(a1["exit_low"][j]), 77.0, places=10)

    def test_effective_only_at_next_15m_after_close(self):
        """bar K（开 250h、收 251h）的信号只在 251h 那根 15m 起生效，之前不得出现。"""
        df = hour_df(lps_scenario())
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        self.assertEqual(int(sig["signal"][K]), CORE.SIG_DONCHIAN_BREAKOUT)
        bars = q15((K + 4) * 4)
        arr = EXEC._prep_symbol("X", sig, bars, C.SIGNAL_FROZEN, "ms", 0.0)
        j_confirm = 4 * (K + 1)                       # 251h 那根 15m
        self.assertEqual(int(arr["pending_sig"][j_confirm]), CORE.SIG_DONCHIAN_BREAKOUT)
        self.assertEqual(int(arr["pending_sig"][j_confirm - 1]), CORE.SIG_NONE)
        # 确认时刻 = 信号 bar 收盘（开 + 3600s）
        self.assertEqual(int(arr["timestamp"][j_confirm]), T0_MS + (K + 1) * H_MS)


# ══════════════════════════ P2 fresh breakout / 锚点 ══════════════════════════

class TestP2FreshBreakoutAnchor(unittest.TestCase):

    def _oi(self, n):
        idx = np.arange(n)
        return np.where(idx <= K, 1000.0 - 0.5 * idx, 2000.0)

    def test_fresh_breakout_registered_before_oi_gate_and_pullback_fires(self):
        rows = lps_scenario()
        df = hour_df(rows, oi=self._oi(len(rows)))
        p = C.SIGNAL_FROZEN
        open_sig = CORE.compute_signals(df, p, "ms")           # 门禁关闭
        s_k = float(open_sig["oi_resonance_score"][K])
        s_k1 = float(open_sig["oi_resonance_score"][K + 1])
        # 前提：突破 bar 分数低、回踩 bar 分数高（否则本测试无鉴别力）
        self.assertLess(s_k, 0.6, f"premise failed: score[K]={s_k}")
        self.assertGreaterEqual(s_k1, 0.6, f"premise failed: score[K+1]={s_k1}")

        p_gate = C.SIGNAL_FROZEN.__class__(oi_min_resonance=0.6)
        gated = CORE.compute_signals(df, p_gate, "ms")
        # 突破 bar 被 OI 门禁拒绝（信号为 none）
        self.assertEqual(int(gated["signal"][K]), CORE.SIG_NONE)
        self.assertEqual(str(gated["reject_reason"][K]), "oi_resonance_below_threshold")
        # 但锚点已登记 ⇒ 回踩 bar 仍能给出 lps_pullback
        self.assertEqual(int(gated["signal"][K + 1]), CORE.SIG_LPS_PULLBACK)

    def test_consecutive_breakouts_do_not_reanchor(self):
        rows = osc_rows(K)
        rows.append({"open": 106.5, "high": 108.5, "low": 105.0, "close": 108.0, "volume": 1000.0})
        rows.append({"open": 108.2, "high": 110.0, "low": 107.5, "close": 109.0, "volume": 1000.0})
        rows.append({"open": 106.5, "high": 107.5, "low": 106.2, "close": 106.9, "volume": 1000.0})
        df = hour_df(rows)
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        self.assertEqual(int(sig["signal"][K]), CORE.SIG_DONCHIAN_BREAKOUT)
        self.assertEqual(int(sig["signal"][K + 1]), CORE.SIG_DONCHIAN_BREAKOUT)
        # 若锚点被 K+1 覆盖（level=dc_high_prev[K+1]=108.5 / low=107.5），K+2 不可能满足
        # reclaimed(c>=108.5) 与 held(l>107.5) ⇒ 该断言可鉴别「重锚」缺陷
        self.assertEqual(int(sig["signal"][K + 2]), CORE.SIG_LPS_PULLBACK)


# ══════════════════════════ P3 SC 先于 Spring ══════════════════════════

class TestP3ScBeforeSpring(unittest.TestCase):

    def test_overlapping_bar_selects_sc(self):
        rows = osc_rows(K)
        # 同时满足 Spring（l<104, c>104, c>o）与 SC（放量、长下影、大振幅）
        rows.append({"open": 104.0, "high": 106.0, "low": 102.0, "close": 104.5, "volume": 5000.0})
        df = hour_df(rows)
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        l_i, o_i, c_i, h_i = 102.0, 104.0, 104.5, 106.0
        rng = h_i - l_i
        self.assertLess(l_i, 104.0)                       # spring 前提
        self.assertGreater(c_i, 104.0)
        self.assertGreater(c_i, o_i)
        self.assertGreaterEqual(min(o_i, c_i) - l_i, 0.5 * rng)   # sc 长下影前提
        self.assertEqual(int(sig["signal"][K]), CORE.SIG_SC_REVERSAL)
        self.assertEqual(str(sig["signal_name"][K]), "sc_reversal")

    def test_spring_only_bar_still_fires_spring(self):
        rows = osc_rows(K)
        # 只满足 Spring（不放量、振幅不足）⇒ 应给 spring
        rows.append({"open": 104.0, "high": 104.8, "low": 103.0, "close": 104.5, "volume": 1000.0})
        df = hour_df(rows)
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        self.assertEqual(int(sig["signal"][K]), CORE.SIG_SPRING)


# ══════════════════════════ P4 本金风险口径 ══════════════════════════

class TestP4CostBasisRisk(unittest.TestCase):

    FEE, SLIP = 0.0005, 0.0003

    def test_long_cost_direction_and_value(self):
        p = pos(direction=1, entry_price=100.0, current_stop=95.0, qty=2.0,
                entry_fee=100.0 * 2.0 * self.FEE)
        got = EXEC.portfolio_open_risk({"X": p}, self.FEE, self.SLIP)
        exp_exit = 95.0 * (1 - self.SLIP)
        exp = (100.0 - exp_exit) * 2.0 + 100.0 * 2.0 * self.FEE + exp_exit * 2.0 * self.FEE
        self.assertAlmostEqual(got, exp, places=10)

    def test_short_cost_direction_and_value(self):
        p = pos(direction=-1, entry_price=100.0, current_stop=105.0, qty=2.0,
                entry_fee=100.0 * 2.0 * self.FEE)
        got = EXEC.portfolio_open_risk({"X": p}, self.FEE, self.SLIP)
        exp_exit = 105.0 * (1 + self.SLIP)
        exp = (exp_exit - 100.0) * 2.0 + 100.0 * 2.0 * self.FEE + exp_exit * 2.0 * self.FEE
        self.assertAlmostEqual(got, exp, places=10)
        # 空头滑点是「更贵出场」⇒ 风险必须大于无滑点情形
        p_noslip = pos(direction=-1, entry_price=100.0, current_stop=105.0, qty=2.0,
                       entry_fee=100.0 * 2.0 * self.FEE)
        self.assertGreater(got, EXEC.portfolio_open_risk({"X": p_noslip}, self.FEE, 0.0))

    def test_last_mark_does_not_change_principal_risk(self):
        base = pos(direction=1, entry_price=100.0, current_stop=95.0, qty=2.0,
                   entry_fee=100.0 * 2.0 * self.FEE, last_mark_price=100.0)
        r0 = EXEC.portfolio_open_risk({"X": base}, self.FEE, self.SLIP)
        prof = pos(direction=1, entry_price=100.0, current_stop=95.0, qty=2.0,
                   entry_fee=100.0 * 2.0 * self.FEE, last_mark_price=300.0)
        loss = pos(direction=1, entry_price=100.0, current_stop=95.0, qty=2.0,
                   entry_fee=100.0 * 2.0 * self.FEE, last_mark_price=60.0)
        self.assertAlmostEqual(EXEC.portfolio_open_risk({"X": prof}, self.FEE, self.SLIP),
                               r0, places=12)
        self.assertAlmostEqual(EXEC.portfolio_open_risk({"X": loss}, self.FEE, self.SLIP),
                               r0, places=12)

    def test_risk_zero_after_trailing_above_entry(self):
        p = pos(direction=1, entry_price=100.0, current_stop=110.0, qty=1.0,
                entry_fee=100.0 * 1.0 * self.FEE, last_mark_price=112.0)
        self.assertEqual(EXEC.portfolio_open_risk({"X": p}, self.FEE, self.SLIP), 0.0)


# ══════════════════════════ P5/P6 因子与 LPS 条件 ══════════════════════════

class TestP5P6FactorsAndLps(unittest.TestCase):

    def test_level_rank_is_raw_oi_rolling_rank_not_chg_rank_no_shift(self):
        n = 400
        rows = osc_rows(n)
        oi = 1000.0 + np.arange(n) * 3.0
        oi[-1] = oi[-2] - 150.0            # 末根回落 ⇒ 使「shift 与否」「chg-rank 与否」可鉴别
        df = hour_df(rows, oi=oi)
        p = C.SIGNAL_FROZEN
        out = F.compute_oi_factors(df, p)
        self.assertIn(F.C_OI_CHG_20, out.columns)   # 独立观察列仍在
        w, mp = F._rank_window(p)
        s = pd.Series(oi)
        expect = s.rolling(window=w, min_periods=mp).rank(pct=True)
        got = out[F.C_RANK_LEVEL]
        last = n - 1
        self.assertAlmostEqual(float(got.iloc[last]), float(expect.iloc[last]), places=10)
        # ① 不得 shift(1)：末根分位(≈0.87) ≠ 上一根分位(1.0)
        self.assertAlmostEqual(float(expect.shift(1).iloc[last]), 1.0, places=10)
        self.assertNotAlmostEqual(float(got.iloc[last]),
                                  float(expect.shift(1).iloc[last]), places=6)
        # ② 不得是「OI 变化率分位」：末根 chg<0 ⇒ chg-rank ≈ 0，与 level-rank(≈0.87) 不同
        chg = s.pct_change(int(p.oi_chg_window))
        chg_rank = chg.rolling(window=w, min_periods=mp).rank(pct=True)
        self.assertLess(float(chg_rank.iloc[last]), 0.05)
        self.assertNotAlmostEqual(float(got.iloc[last]), float(chg_rank.iloc[last]), places=6)

    def test_weights_and_naming(self):
        self.assertEqual(C.OI_WEIGHTS,
                         {"f_oi_level_rank": 0.50, "f_oi_price_div_rank": 0.25,
                          "f_oi_mom_flux_rank": 0.25})
        self.assertNotIn("m4", "".join(C.OI_WEIGHTS).lower())

    def test_missing_oi_gives_nan_not_half(self):
        df = hour_df(lps_scenario())                 # 无 open_interest 列
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        self.assertTrue(sig["oi_resonance_score"].isna().all())
        self.assertFalse(sig["oi_resonance_available"].any())
        self.assertFalse((sig["oi_resonance_score"] == 0.5).any())
        self.assertTrue((sig["oi_resonance_quality"] == F.QUALITY_MISSING_OI).all())

    def test_lps_has_no_extra_close_pos_filter(self):
        """构造「满足附件条件但收盘位置偏低」的回踩 ⇒ 仍应给出 lps_pullback。"""
        rows = osc_rows(K)
        rows.append({"open": 106.5, "high": 108.5, "low": 105.0, "close": 108.0, "volume": 1000.0})
        # 回踩 bar：l=105.5(>105.0 不破)、c=106.5(>=106.0 站上)、c>o；
        # 但 (c-l)/(h-l) = (106.5-105.5)/(107.0-105.5) = 0.667 < 0.5? 不，需构造 <0.5
        rows.append({"open": 106.2, "high": 107.2, "low": 105.5, "close": 106.3, "volume": 1000.0})
        df = hour_df(rows)
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        rng = 107.2 - 105.5
        self.assertLess((106.3 - 105.5) / rng, 0.5)      # 收盘位置 < 0.5
        self.assertEqual(int(sig["signal"][K + 1]), CORE.SIG_LPS_PULLBACK)


# ══════════════════════════ F1/F2/F3 已知失败 ══════════════════════════

class TestKnownFailures(unittest.TestCase):

    def test_force_close_false_returns_real_mtm_equal_to_curve_tail(self):
        rows = osc_rows(K)
        rows.append({"open": 106.5, "high": 108.5, "low": 105.0, "close": 108.0, "volume": 1000.0})
        df = hour_df(rows)
        ts_confirm = T0_MS + (K + 1) * H_MS
        ov = {ts_confirm: (107.9, 108.5, 107.0, 108.0)}
        n15 = (K + 4) * 4
        rows15 = []
        for j in range(n15):
            ts = T0_MS + j * M15_MS
            if ts in ov:
                o, h, l, c = ov[ts]
            elif ts > ts_confirm:
                o, h, l, c = 112.0, 121.0, 111.0, 120.0     # 之后一路上行
            else:
                o, h, l, c = 106.0, 106.5, 105.5, 106.0
            rows15.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c})
        bars15 = pd.DataFrame(rows15)
        cfg = C.ExecParams(force_close_at_end=False)
        r = EXEC.run_portfolio_execution(
            {"X": {"signal_bars": df, "intrabar_bars": bars15}}, C.SIGNAL_FROZEN, cfg, "ms")
        self.assertEqual(r["trades_count"], 0)
        self.assertEqual(r["open_positions_remaining"], ["X"])
        tail = r["equity_curve"][-1]["total_equity"]
        self.assertAlmostEqual(r["final_equity"], tail, places=9)      # F1：不再出现 100000 vs 曲线末值
        self.assertNotAlmostEqual(r["final_equity"], r["initial_equity"], places=2)
        self.assertGreater(r["final_equity"], r["initial_equity"])
        self.assertTrue(r["reconcile"]["equity_curve_tail_matches_final"])

    def test_explicit_time_unit_prefix_invariance(self):
        rows = lps_scenario()
        df = hour_df(rows)
        full = CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")
        for k in (K + 1, K + 2, len(df)):
            pre = CORE.compute_signals(df.iloc[:k].reset_index(drop=True), C.SIGNAL_FROZEN, "ms")
            self.assertTrue((full["signal"].to_numpy()[:k]
                             == pre["signal"].to_numpy()[:k]).all(),
                            f"prefix invariance broken at k={k}")
        first_full = int(np.argmax(full["signal"].to_numpy() != 0))
        first_pre = int(np.argmax(CORE.compute_signals(
            df.iloc[:K + 1].reset_index(drop=True), C.SIGNAL_FROZEN, "ms")["signal"]
            .to_numpy() != 0))
        self.assertEqual(first_full, first_pre)         # F2：首个信号时刻不因追加未来 bar 而改变

    def test_time_unit_undeclared_rejected_and_mixed_rejected(self):
        df = hour_df(lps_scenario())
        with self.assertRaises(F.TimeUnitError):
            CORE.compute_signals(df, C.SIGNAL_FROZEN, None)
        with self.assertRaises(F.TimeUnitError):
            F.assert_single_time_unit({"A": "ms", "B": "s"})

    def test_seconds_milliseconds_microseconds_equivalent(self):
        rows = lps_scenario()
        base = hour_df(rows)
        s_ms = CORE.compute_signals(base, C.SIGNAL_FROZEN, "ms")["signal"].to_numpy()
        s_s = CORE.compute_signals(base.assign(timestamp=base["timestamp"] // 1000),
                                   C.SIGNAL_FROZEN, "s")["signal"].to_numpy()
        s_us = CORE.compute_signals(base.assign(timestamp=base["timestamp"] * 1000),
                                    C.SIGNAL_FROZEN, "us")["signal"].to_numpy()
        self.assertTrue((s_ms == s_s).all())
        self.assertTrue((s_ms == s_us).all())

    def test_compute_stop_rejects_inf_nan_and_illegal_args(self):
        cases = [
            (float("inf"), 2.0, "uniform", {}),
            (100.0, float("nan"), "uniform", {}),
            (100.0, float("inf"), "uniform", {}),
            (100.0, 2.0, "bogus_mode", {}),
            (100.0, 2.0, "uniform", {"direction": 0}),
            (100.0, 2.0, "uniform", {"atr_mult": float("nan")}),
            (100.0, 2.0, "structural", {"structural_extreme": float("inf")}),
            (100.0, 2.0, "structural", {"structural_extreme": None}),
            (100.0, 2.0, "structural", {"structural_extreme": -1.0}),
            (True, 2.0, "uniform", {}),
            ("100", 2.0, "uniform", {}),
            (100.0, 2.0, "uniform", {"price_step": float("nan")}),
        ]
        for entry, atr, mode, kw in cases:
            d = CORE.compute_stop(entry, atr, mode, **kw)
            self.assertFalse(d.accepted, f"must reject: {entry},{atr},{mode},{kw}")
            self.assertTrue(math.isnan(d.stop_price))
            self.assertTrue(d.reason and d.reason != "ok")
        ok = CORE.compute_stop(100.0, 2.0, "uniform")
        self.assertTrue(ok.accepted)
        self.assertAlmostEqual(ok.stop_price, 94.0, places=10)


# ══════════════════════════ 执行层：当根止损 / 跳空因果 ══════════════════════════

class TestExecutionCausality(unittest.TestCase):

    def _scenario(self, confirm_bar, nxt_bar=None):
        rows = osc_rows(K)
        rows.append({"open": 106.5, "high": 108.5, "low": 105.0, "close": 108.0, "volume": 1000.0})
        df = hour_df(rows)
        ts_confirm = T0_MS + (K + 1) * H_MS
        n15 = (K + 4) * 4
        rows15 = []
        for j in range(n15):
            ts = T0_MS + j * M15_MS
            if ts == ts_confirm:
                o, h, l, c = confirm_bar
            elif nxt_bar and ts == ts_confirm + M15_MS:
                o, h, l, c = nxt_bar
            else:
                o, h, l, c = 106.0, 106.5, 105.5, 106.0
            rows15.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c})
        return df, pd.DataFrame(rows15)

    def test_entry_bar_checks_stop_same_bar(self):
        df, bars15 = self._scenario((107.9, 108.2, 101.0, 103.0))
        r = EXEC.run_portfolio_execution(
            {"X": {"signal_bars": df, "intrabar_bars": bars15}},
            C.SIGNAL_FROZEN, C.EXEC_FROZEN, "ms")
        self.assertEqual(r["trades_count"], 1)
        t = r["trades"][0]
        self.assertEqual(t["exit_reason"], "intrabar_stop")
        self.assertEqual(t["entry_time"], t["exit_time"])          # 当根开、当根止

    def test_gap_open_exit_uses_open_not_low(self):
        df, bars15 = self._scenario((107.9, 108.5, 107.0, 108.0),
                                    nxt_bar=(90.0, 91.0, 80.0, 85.0))
        r = EXEC.run_portfolio_execution(
            {"X": {"signal_bars": df, "intrabar_bars": bars15}},
            C.SIGNAL_FROZEN, C.EXEC_FROZEN, "ms")
        self.assertEqual(r["trades_count"], 1)
        t = r["trades"][0]
        self.assertEqual(t["exit_reason"], "gap_open_stop")
        slip = C.EXEC_FROZEN.slippage_bps * 1e-4
        self.assertAlmostEqual(t["exit_price"], EXEC.round_sell_price(90.0 * (1 - slip), 0.0),
                               places=10)
        self.assertGreater(t["exit_price"], 85.0)                  # 不得用未来 low(80) 定价
        phases = {d["phase"] for d in r["decisions"] if d.get("action") == "exit"}
        self.assertIn("B_open_gap", phases)

    def test_entry_fee_once_and_equity_identities(self):
        df, bars15 = self._scenario((107.9, 108.5, 107.0, 108.0),
                                    nxt_bar=(90.0, 91.0, 80.0, 85.0))
        r = EXEC.run_portfolio_execution(
            {"X": {"signal_bars": df, "intrabar_bars": bars15}},
            C.SIGNAL_FROZEN, C.EXEC_FROZEN, "ms")
        rec = r["reconcile"]
        self.assertTrue(rec["cash_identity_ok"])
        self.assertTrue(rec["equity_identity_ok"])
        self.assertTrue(rec["equity_curve_tail_matches_final"])
        self.assertAlmostEqual(rec["sum_entry_fee"],
                               sum(t["entry_fee"] for t in r["trades"]), places=10)
        for t in r["trades"]:
            self.assertAlmostEqual(
                t["net_pnl"],
                t["gross_pnl"] - t["entry_fee"] - t["exit_fee"] - t["funding_fee"], places=10)
            self.assertAlmostEqual(t["fees"], t["entry_fee"] + t["exit_fee"], places=12)

    def test_funding_settlement_events_fail_closed(self):
        df, bars15 = self._scenario((107.9, 108.5, 107.0, 108.0))
        ev_bad = [{"time_sec": (T0_MS + (K + 1) * H_MS) // 1000 + 60,
                   "rate": None, "mark_price": 108.0}]
        r = EXEC.run_portfolio_execution(
            {"X": {"signal_bars": df, "intrabar_bars": bars15,
                   "funding_events": ev_bad}},
            C.SIGNAL_FROZEN, C.EXEC_FROZEN, "ms")
        self.assertFalse(r["funding_is_assumption"])
        self.assertTrue(r["funding_gaps"], "缺 rate 必须记 funding_gap（不补 0）")
        self.assertEqual(r["trades"][0]["funding_fee"], 0.0)

    def test_funding_assumption_model_is_labelled(self):
        df, bars15 = self._scenario((107.9, 108.5, 107.0, 108.0))
        bars15 = bars15.assign(funding_rate=0.0001)
        cfg = C.ExecParams(funding_model="assumption_fixed_8h")
        r = EXEC.run_portfolio_execution(
            {"X": {"signal_bars": df, "intrabar_bars": bars15}}, C.SIGNAL_FROZEN, cfg, "ms")
        self.assertTrue(r["funding_is_assumption"])
        self.assertEqual(r["funding_model"], "assumption_fixed_8h")


# ══════════════════════════ 其余既有门槛 ══════════════════════════

class TestOtherGates(unittest.TestCase):

    def test_non_finite_ohlcv_rejected(self):
        df = hour_df(lps_scenario())
        df.loc[30, "close"] = float("nan")
        with self.assertRaises(CORE.SignalInputError):
            CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")

    def test_dedup_same_symbol(self):
        df = hour_df(lps_scenario())
        dup = pd.concat([df.iloc[:40], df.iloc[[39]], df.iloc[40:]], ignore_index=True)
        out, rep = F.dedup_bars(dup)
        self.assertEqual(rep["duplicates_dropped"], 1)
        self.assertEqual(len(out), len(df))

    def test_missing_columns_rejected(self):
        df = hour_df(lps_scenario()).drop(columns=["volume"])
        with self.assertRaises(CORE.SignalInputError):
            CORE.compute_signals(df, C.SIGNAL_FROZEN, "ms")

    def test_live_order_hard_forbidden(self):
        with self.assertRaises(EXEC.LiveOrderForbidden):
            EXEC.assert_live_disabled()

    def test_registry_entry_run_shape(self):
        from WyckoffMaster.oi_resonance_v1 import registry_entry as RE
        df = hour_df(lps_scenario())
        bars = [{"open_time": int(r.timestamp), "close_time": int(r.timestamp) + H_MS,
                 "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                 "volume": r.volume} for r in df.itertuples()]
        res = RE.run(bars, None, identity=("binance", "X"), require_identity=True)
        self.assertEqual(res["status"], "ok")
        names = {e["evidence"]["signal_name"] for e in res["events"]}
        self.assertIn("donchian_breakout", names)
        self.assertIn("lps_pullback", names)
    def test_registry_wiring_current_and_legacy(self):
        backend_dir = ROOT / "框架" / "backend"
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        from strategies import registry as REG
        curr = REG.list_current(None)
        self.assertEqual(len(curr), 1)
        self.assertEqual(curr[0]["strategy_id"], "WX_OI_RESONANCE_V1")
        self.assertEqual(curr[0]["strategy_key"], "wx_oi_resonance_v1")
        leg = REG.list_legacy(None)
        leg_keys = {x["strategy_key"] for x in leg}
        for arm in ("3a", "3b", "3c", "3d"):
            self.assertIn(arm, leg_keys)
        mod = REG.get("wx_oi_resonance_v1")
        self.assertIsNotNone(mod)
        self.assertTrue(hasattr(mod, "run"))

    def test_exec_bridge_additive_core(self):
        backend_dir = ROOT / "框架" / "backend"
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        import exec_bridge as EB
        self.assertTrue(EB.CORE_AVAILABLE)
        self.assertTrue(EB.NEW_CORE_AVAILABLE)
        self.assertEqual(EB.NEW_STRATEGY_ID, "WX_OI_RESONANCE_V1")
        v = EB.version_ids()
        self.assertTrue(v["new_core_available"])
        self.assertTrue(callable(EB.run_portfolio_execution))

    def test_live_signal_engine_new_entry(self):
        backend_dir = ROOT / "框架" / "backend"
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        import live_signal_engine as LSE
        self.assertTrue(LSE.NEW_STRATEGY_AVAILABLE)
        self.assertEqual(LSE.CURRENT_CALC_STRATEGY, "WX_OI_RESONANCE_V1")
        ev, diag = LSE.oi_resonance_events_for("BTCUSDT", [])
        self.assertEqual(diag["status"], "insufficient_data")
        self.assertEqual(diag["strategy_id"], "WX_OI_RESONANCE_V1")

    def test_legacy_archive_manifest_verifiable(self):
        import hashlib
        arch_dir = ROOT / "策略" / "WyckoffMaster" / "_legacy_archive_v31"
        man_path = arch_dir / "ARCHIVE_MANIFEST.json"
        self.assertTrue(man_path.exists())
        doc = json.loads(man_path.read_text(encoding="utf-8"))
        entries = doc.get("entries") or []
        self.assertGreaterEqual(len(entries), 10)
        for item in entries:
            fname = item["file"]
            fpath = arch_dir / fname
            self.assertTrue(fpath.exists(), f"归档文件缺失：{fname}")
            sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
            self.assertEqual(sha, item.get("archive_sha256"), f"归档文件 SHA 不符：{fname}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

