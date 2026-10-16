# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 核验套件（纯合成样本，**不读任何真实 Holdout 数据**）。

四件套（v3.1 第「四件套核验」节）：
  T1 test_no_lookahead                        —— 截断无前视
  T2 test_anchor_no_overwrite_single_confirm  —— 锚点不覆盖与单次确认消费
  T3 test_stop_limits_m2b_and_below_struct    —— 1.5ATR下限/3.5ATR拒单/M2b缩仓/穿底拒单
  T4 test_step_trail_next_bar_monotonic       —— 台阶下根 15m 生效且不回填

附加（v3.1 规格与接口）：
  T5 test_atr_consistency            —— ATR 口径与 pandas 逐位一致
  T6 test_segments_spec              —— 三段半开 UTC、独立账户、段末强平、重置状态
  T7 test_execution_fee_spec         —— 费用/滑点/720h/15m止损/资金费模型
  T8 test_m2_reject_stats_and_m2b_trigger —— 逐类型超限拒单统计接口与 M2b 触发
  T9 test_forward_gate_m5_m6         —— M5/M6 前向门槛与 30 日口径
  T10 test_config_validity           —— 参数与实验定义合法
  T11 test_experiments_run           —— 历史阶梯只 M0–M4+M2b；M5/M6 前向
  T12 test_holdout_guard             —— 合成时间戳不落入 Holdout；守卫可拒绝

运行：`python -m WyckoffMaster.verify --sha --out <json>`（从 `策略/` 目录）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys

import numpy as np

from . import core, factors, segments as SEG
from .params import FROZEN, validate_params, VERSION, M2_OVER_LIMIT_TRIGGER_RATE
from .experiments import (EXPERIMENTS, HISTORICAL_LADDER, FORWARD_VALIDATION,
                          validate_experiments, experiment_config_hash)

HOUR_MS = 3_600_000


# ══════════════════════════ 合成数据（不读真实数据）══════════════════════════

def synth_bars(n=900, seed=20261015):
    """确定性随机游走 1h bar。时间戳落在 IS 区间内（不触及 Holdout）。"""
    rng = np.random.default_rng(seed)
    price = 100.0
    bars = []
    t0 = 1_700_000_000_000          # 2023-11-14Z，属 IS
    for i in range(n):
        drift = 0.0002 * math.sin(i / 40.0)
        ret = rng.normal(drift, 0.012)
        o = price
        c = max(1e-6, o * (1 + ret))
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.004)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.004)))
        vol = float(abs(rng.normal(1000, 300)) + 1)
        bars.append({"open_time": t0 + i * HOUR_MS,
                     "close_time": t0 + i * HOUR_MS + HOUR_MS - 1,
                     "open": o, "high": hi, "low": lo, "close": c,
                     "volume": vol, "quote_volume": vol * c})
        price = c
    return bars


def _box(n, t0, base=100.0, vol=100.0):
    out = []
    for i in range(n):
        o = base
        c = base + (0.05 if i % 2 else 0.0)
        out.append({"open_time": t0 + i * HOUR_MS, "close_time": t0 + i * HOUR_MS + HOUR_MS - 1,
                    "open": o, "high": base + 0.3, "low": base - 0.3, "close": c,
                    "volume": vol, "quote_volume": vol * c})
    return out


def crafted_spring_breakout_bars():
    """Spring / breakout 触发序列（含零振幅与缺量边界）。"""
    t0 = 1_700_000_000_000
    bars = _box(25, t0)
    bars[10].update(high=bars[10]["open"], low=bars[10]["open"], close=bars[10]["open"])
    bars[11]["volume"] = 0.0
    bars[11]["quote_volume"] = 0.0
    i = 25
    bars.append({"open_time": t0 + i * HOUR_MS, "close_time": t0 + i * HOUR_MS + HOUR_MS - 1,
                 "open": 100.0, "high": 100.35, "low": 99.6, "close": 100.25,
                 "volume": 400.0, "quote_volume": 400.0 * 100.25})
    for k in range(1, 4):
        i = 25 + k
        bars.append({"open_time": t0 + i * HOUR_MS, "close_time": t0 + i * HOUR_MS + HOUR_MS - 1,
                     "open": 100.2, "high": 100.6, "low": 100.1, "close": 100.45,
                     "volume": 120.0, "quote_volume": 120.0 * 100.45})
    for k in range(6):
        ii = 30 + k
        o = 100.5 + k * 0.2
        c = o + 0.5
        bars.append({"open_time": t0 + ii * HOUR_MS, "close_time": t0 + ii * HOUR_MS + HOUR_MS - 1,
                     "open": o, "high": c + 0.1, "low": o - 0.1, "close": c,
                     "volume": 500.0, "quote_volume": 500.0 * c})
    return bars


def crafted_retest_bars():
    t0 = 1_700_000_000_000
    bars = _box(25, t0)
    i = 25
    bars.append({"open_time": t0 + i * HOUR_MS, "close_time": t0 + i * HOUR_MS + HOUR_MS - 1,
                 "open": 100.5, "high": 103.0, "low": 100.2, "close": 101.5,
                 "volume": 600.0, "quote_volume": 600.0 * 101.5})
    i = 26
    bars.append({"open_time": t0 + i * HOUR_MS, "close_time": t0 + i * HOUR_MS + HOUR_MS - 1,
                 "open": 100.4, "high": 101.0, "low": 100.2, "close": 100.8,
                 "volume": 300.0, "quote_volume": 300.0 * 100.8})
    return bars


def crafted_anchor_bars():
    """Spring 母线 25 登记；26 出现新母线但不覆盖；27 首次确认（单次消费）。"""
    t0 = 1_700_000_000_000
    bars = _box(25, t0)                       # 箱体 high 100.3 / low 99.7
    bars.append({"open_time": t0 + 25 * HOUR_MS, "close_time": t0 + 25 * HOUR_MS + HOUR_MS - 1,
                 "open": 100.0, "high": 100.4, "low": 99.5, "close": 100.2,
                 "volume": 400.0, "quote_volume": 400.0 * 100.2})       # A: 登记
    bars.append({"open_time": t0 + 26 * HOUR_MS, "close_time": t0 + 26 * HOUR_MS + HOUR_MS - 1,
                 "open": 99.8, "high": 100.1, "low": 99.6, "close": 99.9,
                 "volume": 400.0, "quote_volume": 400.0 * 99.9})        # B: 不覆盖
    bars.append({"open_time": t0 + 27 * HOUR_MS, "close_time": t0 + 27 * HOUR_MS + HOUR_MS - 1,
                 "open": 100.1, "high": 100.7, "low": 100.0, "close": 100.5,
                 "volume": 200.0, "quote_volume": 200.0 * 100.5})       # C: 首次确认
    bars.append({"open_time": t0 + 28 * HOUR_MS, "close_time": t0 + 28 * HOUR_MS + HOUR_MS - 1,
                 "open": 100.2, "high": 100.3, "low": 100.0, "close": 100.2,
                 "volume": 100.0, "quote_volume": 100.0 * 100.2})       # D: 不应再确认
    return bars


def crafted_retest_overlap_bars():
    """回踩锚点：25 登记；26 又一次过热突破但不覆盖；27 首次回踩确认。"""
    t0 = 1_700_000_000_000
    bars = _box(25, t0)
    bars.append({"open_time": t0 + 25 * HOUR_MS, "close_time": t0 + 25 * HOUR_MS + HOUR_MS - 1,
                 "open": 100.5, "high": 101.0, "low": 100.2, "close": 100.6,
                 "volume": 600.0, "quote_volume": 600.0 * 100.6})       # A: 过热突破→登记
    bars.append({"open_time": t0 + 26 * HOUR_MS, "close_time": t0 + 26 * HOUR_MS + HOUR_MS - 1,
                 "open": 101.2, "high": 101.8, "low": 101.0, "close": 101.3,
                 "volume": 600.0, "quote_volume": 600.0 * 101.3})       # B: 过热突破→不覆盖
    bars.append({"open_time": t0 + 27 * HOUR_MS, "close_time": t0 + 27 * HOUR_MS + HOUR_MS - 1,
                 "open": 100.4, "high": 101.0, "low": 100.5, "close": 100.8,
                 "volume": 300.0, "quote_volume": 300.0 * 100.8})       # C: 回踩确认
    return bars


def crafted_reject_bars():
    """远离结构点的暴涨突破（M0 纯突破 + 结构止损 ⇒ 过远拒单）。"""
    t0 = 1_700_000_000_000
    bars = _box(25, t0)
    bars.append({"open_time": t0 + 25 * HOUR_MS, "close_time": t0 + 25 * HOUR_MS + HOUR_MS - 1,
                 "open": 100.5, "high": 110.2, "low": 100.4, "close": 110.0,
                 "volume": 800.0, "quote_volume": 800.0 * 110.0})
    return bars


# ══════════════════════════ 四件套 ══════════════════════════

def test_no_lookahead() -> dict:
    """T1：前缀信号 == 全序列信号前缀；台阶判定对更长未来不敏感。"""
    bars = synth_bars()
    full = core.compute_signals(bars, FROZEN, "three_buy_points", "structural")
    fields = ["atr", "atr_prev", "don_high", "don_low", "don_exit_low", "vol_ma", "close_pos",
              "spring_entry", "breakout_entry", "retest_entry", "master_entry",
              "struct_low", "ref_stop_price", "ref_target_price"]
    cuts = [60, 120, 200, 333, 500, 700, len(bars) - 1]
    mismatches = []
    for k in cuts:
        pre = core.compute_signals(bars[:k], FROZEN, "three_buy_points", "structural")
        for f in fields:
            if not _arr_equal(getattr(full, f)[:k], getattr(pre, f)):
                mismatches.append({"cut": k, "field": f})
        if full.signal_kind[:k] != pre.signal_kind:
            mismatches.append({"cut": k, "field": "signal_kind"})
        if full.reject_reason[:k] != pre.reject_reason:
            mismatches.append({"cut": k, "field": "reject_reason"})
    # 台阶：第 k 根判定对更长未来不敏感
    closes = [100.0 + i for i in range(11)]
    lows = [c - 0.5 for c in closes]
    trail_ok = True
    for k in range(1, len(closes) + 1):
        a = core.evaluate_trail_stop(closes[:k], lows[:k], 94.0, 100.0, 2.0, FROZEN, "step_swing")
        b = core.evaluate_trail_stop(closes, lows, 94.0, 100.0, 2.0, FROZEN, "step_swing",
                                     upto=k - 1)
        if (a["stop"], a["updated"], a["reason"]) != (b["stop"], b["updated"], b["reason"]):
            trail_ok = False
    return {"passed": len(mismatches) == 0 and trail_ok, "cuts": cuts,
            "mismatches": mismatches, "trail_prefix_insensitive": trail_ok,
            "note": "信号逐位一致 + 台阶判定前缀一致（无前视/无回填）"}


def test_anchor_no_overwrite_single_confirm() -> dict:
    """T2：未决锚点不被新检测覆盖；母线/锚点只确认一次并消费。

    说明：Spring 母线自身会刷新箱体低点（新母线要求跌破此前 20 根低点），
    故 Spring 的「窗口内第二次母线」在结构上必然触发**失效**（跌破锚点低点）而非静默覆盖——
    这本身即「不覆盖」保障。可触发的「重叠忽略」路径由**回踩锚点**演示。
    """
    # —— Spring：登记→（第二次母线被解析为失效/到期，不覆盖）→首次确认→不再确认 ——
    fs = core.compute_signals(crafted_anchor_bars(), FROZEN, "three_buy_points", "structural")
    ts = fs.anchor_trace
    s_reg = [t["bar"] for t in ts if t["kind"] == "spring" and t["action"] == "register"]
    s_conf = [t for t in ts if t["kind"] == "spring" and t["action"] == "confirm"]
    spring_ok = (s_reg == [25] and [c["bar"] for c in s_conf] == [27]
                 and abs(fs.struct_low[27] - 99.5) < 1e-9
                 and not fs.spring_entry[28]
                 and all(c["register_bar"] == 25 for c in s_conf))
    # 单次确认消费：每个 register 最多 1 次 confirm
    per_reg = {}
    for c in ts:
        if c["action"] == "confirm":
            per_reg[(c["kind"], c["register_bar"])] = per_reg.get((c["kind"], c["register_bar"]), 0) + 1
    single = all(v == 1 for v in per_reg.values())

    # —— 回踩：登记→第二次过热突破被忽略（不覆盖）→首次确认 ——
    fr = core.compute_signals(crafted_retest_overlap_bars(), FROZEN,
                              "three_buy_points", "structural")
    tr = fr.anchor_trace
    r_reg = [t for t in tr if t["kind"] == "retest" and t["action"] == "register"]
    r_ovl = [t for t in tr if t["kind"] == "retest" and t["action"] == "overlap_ignored"]
    r_conf = [t for t in tr if t["kind"] == "retest" and t["action"] == "confirm"]
    retest_ok = ([t["bar"] for t in r_reg] == [25]
                 and [t["bar"] for t in r_ovl] == [26]
                 and [t["bar"] for t in r_conf] == [27]
                 and abs(r_ovl[0]["kept_level"] - r_reg[0]["level"]) < 1e-9
                 and abs(fr.struct_low[27] - 100.5) < 1e-9)
    return {"passed": spring_ok and single and retest_ok,
            "spring": {"register_bars": s_reg, "confirm_bars": [c["bar"] for c in s_conf],
                       "struct_low_at_confirm": float(fs.struct_low[27]),
                       "no_reconfirm_at_28": not bool(fs.spring_entry[28])},
            "retest": {"register_bars": [t["bar"] for t in r_reg],
                       "overlap_ignored_bars": [t["bar"] for t in r_ovl],
                       "confirm_bars": [t["bar"] for t in r_conf],
                       "kept_level": r_ovl[0]["kept_level"] if r_ovl else None,
                       "struct_low_at_confirm": float(fr.struct_low[27])},
            "single_confirm_per_register": single,
            "note": "先到先得、不覆盖未决锚点；每根母线/锚点最多确认一次并立即消费"}


def test_stop_limits_m2b_and_below_struct() -> dict:
    """T3：1.5ATR下限 / 3.5ATR拒单(M2) / M2b自然缩仓不拒单 / 穿底拒单。"""
    p = FROZEN
    atr = 2.0
    cases = []
    # 1.5ATR 下限（M2）
    r = core.compute_stop(100.0, 99.2, atr, p, "structural")
    cases.append({"name": "m2_min_1.5atr", "reject": r["reject"],
                  "passed": r["reject"] is None and abs(r["stop_dist"] - 3.0) < 1e-9
                  and r["stop_price"] <= (99.2 - 0.8 * atr) + 1e-9})
    # 3.5ATR 拒单（M2）
    r = core.compute_stop(100.0, 80.0, atr, p, "structural")
    cases.append({"name": "m2_reject_gt_3.5atr", "reject": r["reject"],
                  "passed": r["reject"] == "structural_stop_too_far" and r["stop_price"] is None})
    # 取整后再核（M2）
    r = core.compute_stop(100.0, 96.0, atr, p, "structural", price_step=2.5)
    cases.append({"name": "m2_round_then_recheck", "reject": r["reject"],
                  "passed": r["reject"] == "structural_stop_too_far_after_round"})
    # M2b：同样过远但不拒单，距离 = max(raw_dist, 1.5ATR) = 真实距离（自然缩仓）
    r = core.compute_stop(100.0, 80.0, atr, p, "structural_m2b")
    raw_dist = 100.0 - (80.0 - 0.8 * atr)
    cases.append({"name": "m2b_no_reject_natural_shrink", "reject": r["reject"],
                  "stop_dist": r["stop_dist"], "raw_dist": raw_dist,
                  "passed": (r["reject"] is None
                             and abs(r["stop_dist"] - raw_dist) < 1e-9
                             and r["stop_dist"] > p.max_stop_atr * atr)})
    # M2b：结构很近 ⇒ 下限 1.5ATR
    r = core.compute_stop(100.0, 99.2, atr, p, "structural_m2b")
    cases.append({"name": "m2b_floor_1.5atr",
                  "passed": r["reject"] is None and abs(r["stop_dist"] - 3.0) < 1e-9})
    # 穿底拒单：P_entry <= L_struct（计入 0.05% 滑点后的成交价；M2 与 M2b 均适用）
    f1 = core.evaluate_entry_fill(open_15m=95.9, struct_low=96.0, atr_val=atr, params=p,
                                  stop_mode="structural")
    f2 = core.evaluate_entry_fill(open_15m=95.9, struct_low=96.0, atr_val=atr, params=p,
                                  stop_mode="structural_m2b")
    cases.append({"name": "entry_below_struct_low_m2", "reject": f1["reject"],
                  "entry_price": f1["entry_price"],
                  "passed": f1["reject"] == "entry_below_struct_low"})
    cases.append({"name": "entry_below_struct_low_m2b", "reject": f2["reject"],
                  "passed": f2["reject"] == "entry_below_struct_low"})
    # 恰好等于结构低点：计入滑点后 P_entry > L_struct ⇒ 放行（非拒单）
    f_eq = core.evaluate_entry_fill(open_15m=96.0, struct_low=96.0, atr_val=atr, params=p,
                                    stop_mode="structural")
    cases.append({"name": "entry_equal_struct_after_slippage_allowed",
                  "reject": f_eq["reject"],
                  "passed": f_eq["reject"] is None and f_eq["entry_price"] > 96.0})
    # 开盘已穿止损：在 compute_stop 层直接核验（fill 层因 stop 由同一 open 派生，
    # 结构上 open<=stop 不可达 ⇒ 保留为防御分支）
    r_ots = core.compute_stop(100.0, 96.0, atr, p, "structural", raw_open=90.0)
    cases.append({"name": "open_through_stop_defensive", "reject": r_ots["reject"],
                  "passed": r_ots["reject"] == "open_already_through_stop"})
    # 正常成交：entry>struct，stop 合法
    f3 = core.evaluate_entry_fill(open_15m=100.0, struct_low=96.0, atr_val=atr, params=p,
                                  stop_mode="structural", price_step=0.0)
    cases.append({"name": "normal_fill_ok",
                  "passed": f3["reject"] is None and f3["stop_price"] < f3["entry_price"]})
    # uniform 恒 3ATR；目标 2R
    ru = core.compute_stop(100.0, None, atr, p, "uniform")
    cases.append({"name": "uniform_3atr_target_2r",
                  "passed": ru["reject"] is None and abs(ru["stop_dist"] - 6.0) < 1e-9
                  and abs(ru["target_price"] - (100.0 + 12.0)) < 1e-9})
    return {"passed": all(c["passed"] for c in cases), "cases": cases,
            "note": "M2 过远拒单；M2b 不设上限、按真实距离自然缩仓；穿底拒单 M2/M2b 均适用"}


def test_step_trail_next_bar_monotonic() -> dict:
    """T4：台阶下一根 15m 生效、单调不降、不回填、回看须全属持仓后。"""
    entry_price, entry_atr = 100.0, 2.0
    closes = [100.0 + i for i in range(11)]
    lows = [c - 0.5 for c in closes]
    close_times = [1_700_000_000_000 + i * HOUR_MS for i in range(len(closes))]
    steps = []
    stop = 94.0
    for k in range(1, len(closes) + 1):
        res = core.evaluate_trail_stop(closes[:k], lows[:k], stop, entry_price, entry_atr,
                                       FROZEN, "step_swing")
        steps.append({"k": k, "close_time": close_times[k - 1],
                      "effective_from_open_time": close_times[k - 1] + 1,
                      "stop_before": stop, "stop_after": res["stop"],
                      "updated": res["updated"], "reason": res["reason"],
                      "cand": res["cand"], "swing_low": res["swing_low"]})
        stop = res["stop"]
    monotone = all(s["stop_after"] >= s["stop_before"] - 1e-12 for s in steps)
    next_bar = all(s["effective_from_open_time"] > s["close_time"] for s in steps)
    early5 = core.evaluate_trail_stop(closes[:5], lows[:5], 94.0, entry_price, entry_atr,
                                      FROZEN, "step_swing")
    early6 = core.evaluate_trail_stop(closes[:6], lows[:6], 94.0, entry_price, entry_atr,
                                      FROZEN, "step_swing")
    at7 = core.evaluate_trail_stop(closes[:7], lows[:7], 94.0, entry_price, entry_atr,
                                   FROZEN, "step_swing")
    gate = (early5["reason"] == "insufficient_bars_after_entry"
            and early6["reason"] == "insufficient_bars_after_entry"
            and at7["reason"] != "insufficient_bars_after_entry")
    raised = [s for s in steps if s["updated"]]
    formula_ok = len(raised) > 0
    for s in raised:
        if abs(s["cand"] - (s["swing_low"] - 0.8 * entry_atr)) > 1e-9 \
                or s["cand"] < entry_price + 0.3 * entry_atr:
            formula_ok = False
    # 保本对照：收盘浮盈 >= 1R（R = entry - 初始止损 = 6）后下一根 15m 推至 entry
    be = core.evaluate_trail_stop([100.0, 104.0, 106.5], [99.5, 103.0, 105.5], 94.0, 100.0,
                                  2.0, FROZEN, "naive_be")
    return {"passed": monotone and next_bar and gate and formula_ok and be["updated"]
            and be["stop"] == 100.0,
            "monotone_non_decreasing": monotone, "effective_next_15m": next_bar,
            "min_bars_gate": gate, "raise_formula_ok": formula_ok,
            "raised_count": len(raised), "naive_be_stop": be["stop"],
            "steps": steps,
            "note": "下一根 15m 生效、只升不降、不回填；回看 5 根须全属持仓后"}


# ══════════════════════════ 附加核验 ══════════════════════════

def test_atr_consistency() -> dict:
    bars = synth_bars()
    o = np.array([b["open"] for b in bars], float)
    h = np.array([b["high"] for b in bars], float)
    l = np.array([b["low"] for b in bars], float)
    c = np.array([b["close"] for b in bars], float)
    from . import series as S
    tr = S.true_ranges(o, h, l, c)
    mine = S.ewm_atr(tr, 1.0 / FROZEN.atr_n, FROZEN.atr_n)
    try:
        import pandas as pd
        ref = pd.Series(tr).ewm(alpha=1.0 / FROZEN.atr_n, adjust=False,
                                min_periods=FROZEN.atr_n).mean().to_numpy()
        m = ~np.isnan(mine)
        same = bool(np.allclose(mine[m], ref[m], atol=1e-12, rtol=0)) and \
            bool(np.array_equal(np.isnan(mine), np.isnan(ref)))
        return {"passed": same, "compared_points": int(m.sum()),
                "first_finite_index": int(np.argmax(m)),
                "note": "Wilder 式递归 alpha=1/20, adjust=False, min_periods=20"}
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"pandas 不可用：{exc}"}


def test_segments_spec() -> dict:
    """T6：三段半开 UTC、独立账户、段末强平、重置状态、Holdout 红线。"""
    spec = SEG.spec()
    seg = SEG.SEGMENTS
    checks = []
    checks.append(("IS_bounds", seg["IS"]["start_ms"] == SEG.utc_ms(2022, 1, 1)
                   and seg["IS"]["end_ms"] == SEG.utc_ms(2025, 1, 1)))
    checks.append(("Val_bounds", seg["Val"]["start_ms"] == SEG.utc_ms(2025, 1, 1)
                   and seg["Val"]["end_ms"] == SEG.utc_ms(2026, 4, 15)))
    checks.append(("Holdout_bounds", seg["Holdout"]["start_ms"] == SEG.utc_ms(2026, 4, 15)
                   and seg["Holdout"]["end_ms"] == SEG.utc_ms(2026, 10, 15)))
    # 半开：端点归属
    checks.append(("half_open_IS_end_is_Val",
                   SEG.segment_of(SEG.utc_ms(2025, 1, 1)) == "Val"
                   and SEG.segment_of(SEG.utc_ms(2025, 1, 1) - 1) == "IS"))
    checks.append(("half_open_Holdout_start",
                   SEG.segment_of(SEG.utc_ms(2026, 4, 15)) == "Holdout"
                   and SEG.segment_of(SEG.utc_ms(2026, 4, 15) - 1) == "Val"))
    checks.append(("no_gap_no_overlap",
                   seg["IS"]["end_ms"] == seg["Val"]["start_ms"]
                   and seg["Val"]["end_ms"] == seg["Holdout"]["start_ms"]))
    checks.append(("segment_end_rule",
                   SEG.SEGMENT_END_RULE == "segment_end_force_close_last_15m_close"))
    checks.append(("reset_state_complete",
                   set(SEG.SEGMENT_RESET_STATE) ==
                   {"cash", "orders", "positions", "execution_state", "equity_peak"}))
    checks.append(("initial_equity_5000", SEG.INITIAL_EQUITY == 5000.0))
    # Holdout 守卫
    guard_ok = False
    try:
        SEG.assert_no_holdout([SEG.utc_ms(2023, 1, 1), SEG.utc_ms(2024, 1, 1)])
        try:
            SEG.assert_no_holdout([SEG.utc_ms(2026, 5, 1)])
        except ValueError:
            guard_ok = True
    except Exception:  # noqa: BLE001
        guard_ok = False
    checks.append(("holdout_guard_rejects", guard_ok))
    checks.append(("holdout_cutoff_utc", spec["holdout_cutoff_utc"] == "2026-10-15T00:00:00Z"))
    return {"passed": all(v for _, v in checks), "checks": {k: v for k, v in checks},
            "spec": spec, "note": "半开 UTC、独立 5000U、段末最后 15m 收盘强平、段界重置"}


def test_execution_fee_spec() -> dict:
    """T7：费用/滑点 0.05%、720h、止损逐 15m、负费率收入保留、禁冒充 mark。"""
    c = SEG.EXEC_COST
    checks = {
        "fee_each_side_0.05": c["fee_each_side"] == 0.0005,
        "slippage_each_side_0.05": c["slippage_each_side"] == 0.0005,
        "max_holding_720h": c["max_holding_hours"] == 720,
        "participation_1pct": c["entry_participation_prev15m_quote_cap"] == 0.01,
        "stop_checked_15m": c["stop_checked_per"] == "15m",
        "negative_funding_retained": c["negative_funding_income_retained"] is True,
        "forbid_mark_substitutes": set(c["forbidden_funding_mark_substitutes"]) >=
        {"norm4h", "ext1h", "zero_fill", "ordinary_close"},
    }
    return {"passed": all(checks.values()), "checks": checks,
            "note": "M0–M4 及 M2b 全程双边每边 0.05% 费 + 0.05% 滑点 + 全部真实结算资金费"}


def test_m2_reject_stats_and_m2b_trigger() -> dict:
    """T8：逐类型超限拒单统计接口 + M2b 触发（>30%）。"""
    atr = 2.0
    def rec(kind, struct_low, entry=100.0):
        return {"kind": kind, "entry_price": entry, "struct_low": struct_low, "atr": atr}
    over = 80.0        # raw_dist = 21.6 > 7 ⇒ 超限
    within = 96.0      # raw_dist = 5.6 <= 7 ⇒ 正常
    invalid = 1.0      # raw_stop = -0.6 <= 0 ⇒ 不计入分母
    is_records = [rec("spring", within), rec("spring", over),
                  rec("breakout", within), rec("breakout", over),
                  rec("retest", within), rec("retest", invalid)]
    val_records = [rec("spring", within), rec("spring", over),
                   rec("breakout", within)]
    st_is = core.m2_over_limit_stats(is_records)
    st_val = core.m2_over_limit_stats(val_records)
    trig = core.m2b_trigger(st_is, st_val, M2_OVER_LIMIT_TRIGGER_RATE)
    # spring: IS(2) + Val(2) = 4 den, over 2 ⇒ rate 0.5 > 0.30 ⇒ 触发
    # 无效结构不计入分母（retest invalid 被排除）
    ok_den = (st_is["by_type"]["retest"]["denominator"] == 1
              and st_is["by_type"]["retest"]["over_limit"] == 0)
    ok_trigger = trig["triggered"] and abs(trig["by_type"]["spring"]["rate"] - 0.5) < 1e-9
    # N/A 情形：0 分母
    st_zero = core.m2_over_limit_stats([])
    ok_na = st_zero["total"]["na"] is True and st_zero["total"]["rate"] is None
    # 不触发：全部 <= 30%
    st_low = core.m2_over_limit_stats([rec("spring", within)] * 10)
    trig_low = core.m2b_trigger(st_low, st_zero, M2_OVER_LIMIT_TRIGGER_RATE)
    ok_low = trig_low["triggered"] is False
    return {"passed": ok_den and ok_trigger and ok_na and ok_low,
            "is_by_type": st_is["by_type"], "val_by_type": st_val["by_type"],
            "trigger": trig, "na_case": st_zero["total"],
            "note": "分母=到达超限判断且具备有效结构与执行价的唯一候选；0 分母记 N/A；"
                    "任一类型 IS+Val 合并 >30% 触发 M2b"}


def test_forward_gate_m5_m6() -> dict:
    """T9：M5/M6 前向门槛（≥30 自然日且 ≥90 唯一快照）、30 日口径、M6 Z 降序稳定平手。"""
    g1 = factors.forward_gate(29, 200)
    g2 = factors.forward_gate(30, 89)
    g3 = factors.forward_gate(30, 90)
    gate_ok = (not g1["activated"]) and (not g2["activated"]) and g3["activated"]
    m5_inactive = factors.m5_allows(0.95, gate_activated=False)[0] is True
    m5_block = factors.m5_allows(0.95, gate_activated=True)[0] is False
    m5_pass = factors.m5_allows(0.50, gate_activated=True)[0] is True
    m6_nogate = factors.m6_rank_key(1.5, gate_activated=False) == 0.0
    m6_key = abs(factors.m6_rank_key(1.5, gate_activated=True) + 1.5) < 1e-9
    # 稳定排序：Z 降序，平手保持原序
    items = [("a", 1.0), ("b", 2.0), ("c", 1.0), ("d", 0.5)]
    ordered = factors.m6_order(items, lambda it: it[1], gate_activated=True)
    order_ok = [x[0] for x in ordered] == ["b", "a", "c", "d"]
    # 未激活 ⇒ 不改序
    same = factors.m6_order(items, lambda it: it[1], gate_activated=False)
    same_ok = [x[0] for x in same] == ["a", "b", "c", "d"]
    # 30 日窗口常量
    window_ok = factors.WINDOW_30D_MS == 30 * factors.DAY_MS
    return {"passed": gate_ok and m5_inactive and m5_block and m5_pass and m6_nogate
            and m6_key and order_ok and same_ok and window_ok,
            "gate_cases": {"29d_200": g1["activated"], "30d_89": g2["activated"],
                           "30d_90": g3["activated"]},
            "m6_order": [x[0] for x in ordered], "m6_no_gate_order": [x[0] for x in same],
            "note": "M5 30 日分位>90% 过滤；M6 30 日 Z 降序排序；未达门槛 M5 不跳过、M6 不改序"}


def test_config_validity() -> dict:
    param_errs = validate_params(FROZEN)
    exp_errs = validate_experiments()
    return {"passed": not param_errs and not exp_errs,
            "param_errors": param_errs, "experiment_errors": exp_errs,
            "historical_ladder": list(HISTORICAL_LADDER),
            "forward_validation": list(FORWARD_VALIDATION)}


def test_experiments_run() -> dict:
    bars = synth_bars()
    per = {}
    for k, e in EXPERIMENTS.items():
        fr = core.compute_signals(bars, FROZEN, e.entry_mode, e.stop_mode)
        per[k] = {"entry_mode": e.entry_mode, "stop_mode": e.stop_mode,
                  "historical": e.historical, "conditional": e.conditional,
                  "master_count": fr.diagnostics["master_count"],
                  "reject_count": fr.diagnostics["reject_count"],
                  "reject_breakdown": fr.diagnostics["reject_breakdown"]}
    hist = [k for k in per if per[k]["historical"]]
    fwd = [k for k in per if not per[k]["historical"]]
    ok = (set(hist) == set(HISTORICAL_LADDER) and set(fwd) == set(FORWARD_VALIDATION)
          and "M5" not in hist and "M6" not in hist)
    return {"passed": ok, "per_experiment": per, "historical": hist, "forward": fwd,
            "note": "历史阶梯只 M0–M4+M2b；M5/M6 为前向验证"}


def test_holdout_guard_synthetic() -> dict:
    """T12：本核验全部使用合成样本；所有合成时间戳不落入 Holdout。"""
    stamps = set()
    for b in synth_bars() + crafted_spring_breakout_bars() + crafted_retest_bars() \
            + crafted_anchor_bars() + crafted_reject_bars():
        stamps.add(int(b["open_time"]))
    try:
        SEG.assert_no_holdout(sorted(stamps))
        ok = True
        err = None
    except ValueError as exc:
        ok = False
        err = str(exc)
    return {"passed": ok, "synthetic_timestamps": len(stamps), "error": err,
            "note": "纯合成；未读任何真实 Holdout 数据（本套件不打开任何行情文件）"}


# ══════════════════════════ 主流程 ══════════════════════════

def run_all() -> dict:
    checks = {
        "T1_test_no_lookahead": test_no_lookahead(),
        "T2_test_anchor_no_overwrite_single_confirm": test_anchor_no_overwrite_single_confirm(),
        "T3_test_stop_limits_m2b_and_below_struct": test_stop_limits_m2b_and_below_struct(),
        "T4_test_step_trail_next_bar_monotonic": test_step_trail_next_bar_monotonic(),
        "T5_test_atr_consistency": test_atr_consistency(),
        "T6_test_segments_spec": test_segments_spec(),
        "T7_test_execution_fee_spec": test_execution_fee_spec(),
        "T8_test_m2_reject_stats_and_m2b_trigger": test_m2_reject_stats_and_m2b_trigger(),
        "T9_test_forward_gate_m5_m6": test_forward_gate_m5_m6(),
        "T10_test_config_validity": test_config_validity(),
        "T11_test_experiments_run": test_experiments_run(),
        "T12_test_holdout_guard_synthetic": test_holdout_guard_synthetic(),
    }
    all_passed = all(c["passed"] for c in checks.values())
    return {
        "version": VERSION,
        "all_passed": all_passed,
        "checks": checks,
        "experiment_config_hash": experiment_config_hash(),
        "params": FROZEN.to_dict(),
        "segments": SEG.spec(),
        "environment": {"python": sys.version.split()[0], "numpy": np.__version__,
                        "platform": platform.platform()},
        "data_read_disclosure": "本套件仅使用合成样本，未打开任何真实行情/因子文件，"
                                "未读取 Holdout 区间数据。",
    }


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_bundle_sha(module_dir) -> dict:
    files = sorted(f for f in os.listdir(module_dir) if f.endswith(".py") or f == "README.md")
    per, parts = {}, []
    for f in files:
        s = _sha256_file(os.path.join(module_dir, f))
        per[f] = s
        parts.append(f"{f}:{s}")
    return {"bundle_sha256": hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest(),
            "per_file": per}


def _arr_equal(a, b, atol=0.0):
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        return False
    fa, fb = a.astype(float), b.astype(float)
    if not np.array_equal(np.isnan(fa), np.isnan(fb)):
        return False
    m = ~np.isnan(fa)
    if atol == 0.0:
        return bool(np.array_equal(fa[m], fb[m]))
    return bool(np.allclose(fa[m], fb[m], atol=atol, rtol=0))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--sha", action="store_true")
    args = ap.parse_args(argv)
    res = run_all()
    if args.sha:
        res["code_sha"] = code_bundle_sha(os.path.dirname(os.path.abspath(__file__)))
    text = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if res["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
