# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 · 信号与止损核心（新候选，与旧 core 完全隔离）。

信号名（**刻意不复用旧名**）
──────────────────────────
  0 none · 1 spring · 2 sc_reversal · 3 donchian_breakout · 4 lps_pullback

`lps_pullback` = 威科夫 **LPS**（Last Point of Support，突破后回踩确认买点）。
旧 `structural_m2b` 是**条件距离实验**（为绕过 M2 的 3.5ATR 超限拒单而预注册的止损距离
变体），语义不同 ⇒ 本包**不复用**该名，止损模式用 `lps_structural`，历史 M2b 计数
**不得**套到 LPS 上。

相对附件稿的修复
────────────────
  · `compute_stop` 逐参做**类型 + 有限性**校验：bool/str/None/NaN/±inf 一律拒绝；
    非法 `stop_mode` / 非法 `direction` / 非法极值（<=0 或非有限）一律拒绝。
  · `structural_buffer_atr` / `atr_mult` / `price_step` 也做有限性校验（附件未查）。
  · 取整方向按方向分：多头保护价向**更低**取整，空头向**更高**取整。
  · 时间单位由调用方显式给出（`time_unit`），不再从 `max/median` 推断。
  · 因子不可用时**不伪造 0.5**：仅在显式设置门槛（`oi_min_resonance > 0`）时 fail-closed 拒单，
    否则如实放行并携带 `oi_resonance_available=False`。
  · PIT 可得性（施工单 W2-5）**分层**处理，不混为一谈：
    `oi_resonance_available`（数值算得出）≠ `oi_pit_proven`（决策时刻真可见）≠
    `oi_decision_usable`（两者同时成立）。发布延迟 `oi_publication_lag_ms` **未声明**时：
    逐行只**标记** `pit_proven=False`（**不改逐 bar 策略规则**），改由 `formal_run_gate()`
    在**运行级**拒绝正式生产（与 W2-1 的 UNSET 同款口径）；**已声明**时，逐行校验
    「观测 + 延迟 <= bar 收盘」，不可见者逐行 fail-closed（`oi_pit_unproven_fail_closed`）。

同根优先级（2026-10-16 订正）
────────────────────────────
  `fresh breakout 登记 → 因子门禁 → sc_reversal → spring → donchian_breakout →
   lps_pullback →（可选）short`
  · **SC 先于 Spring**：同根同时满足两者时选 SC（不沿用旧顺序）。
  · 突破锚点（`last_bo_*`）只在 **fresh breakout** 当根登记，且**先于因子门禁**；
    后续突破分支**不得**重复覆盖锚点。

输出列
──────
`signal / signal_name / direction / stop_price / structural_ref / stop_mode / stop_atr_mult /
 reject_reason / atr / dc_high_prev / dc_low_prev / exit_low_prev / exit_high_prev /
 exit_low_curr / exit_high_curr / confirm_time` + `factors.compute_oi_factors` 的全部因子列。
`*_curr` 为**当期**（不 shift）通道，收盘后可得；执行层按「收盘确认 → 下一根 15m 生效」使用。

纯函数：不读库、不联网、不下单。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from . import factors as F
from ..trading_core import pit as PIT
from .config import STRATEGY_ID, SIGNAL_FROZEN, SignalParams

SIG_NONE = 0
SIG_SPRING = 1
SIG_SC_REVERSAL = 2
SIG_DONCHIAN_BREAKOUT = 3
SIG_LPS_PULLBACK = 4

SIGNAL_NAMES = {
    SIG_NONE: "none",
    SIG_SPRING: "spring",
    SIG_SC_REVERSAL: "sc_reversal",
    SIG_DONCHIAN_BREAKOUT: "donchian_breakout",
    SIG_LPS_PULLBACK: "lps_pullback",
}

VALID_STOP_MODES = ("uniform", "structural", "lps_structural")
VALID_DIRECTIONS = (1, -1)

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class SignalInputError(ValueError):
    """输入不合法（缺列 / 时间单位非法 / 非有限 OHLCV）。"""


# ══════════════════════════ 严格数值校验 ══════════════════════════

def finite_number(v, *, name: str, positive: bool = False, nonneg: bool = False):
    """返回 `(value, reason)`。`reason is None` ⇒ 合法。

    拒绝：None / bool / 非数值类型 / NaN / ±inf；`positive` ⇒ 必须 > 0；`nonneg` ⇒ 必须 >= 0。
    """
    if v is None or isinstance(v, bool) or not isinstance(
            v, (int, float, np.integer, np.floating)):
        return None, f"non_numeric_{name}"
    f = float(v)
    if not math.isfinite(f):
        return None, f"non_finite_{name}"
    if positive and f <= 0.0:
        return None, f"nonpositive_{name}"
    if nonneg and f < 0.0:
        return None, f"negative_{name}"
    return f, None


@dataclass(frozen=True)
class StopDecision:
    """止损决定。`accepted=False` ⇒ `stop_price` 为 NaN 且 `reason` 说明拒绝原因。"""
    accepted: bool
    stop_price: float
    reason: str
    stop_dist: float = float("nan")
    raw_stop: float = float("nan")
    rounded_stop: float = float("nan")
    stop_atr_mult: float = float("nan")


def _round_protect(px: float, step: float, direction: int) -> float:
    """保护价取整：多头向**更低**、空头向**更高**（step<=0 不取整）。"""
    if not step or step <= 0:
        return px
    if direction == 1:
        return step * math.floor(px / step + 1e-9)
    return step * math.ceil(px / step - 1e-9)


def compute_stop(entry_ref_price, atr_val, stop_mode, direction: int = 1,
                 atr_mult: float = 3.0, structural_extreme=None,
                 structural_buffer_atr: float = 0.2, price_step: float = 0.0,
                 raise_on_reject: bool = False) -> StopDecision:
    """按**实际成交价** `entry_ref_price` 计算初始保护价。

    调用纪律：`entry_ref_price` 必须是**取整后的真实成交价**（先成交、再算风险/止损）。
    """
    def _rej(reason: str) -> StopDecision:
        if raise_on_reject:
            raise ValueError(reason)
        return StopDecision(False, float("nan"), reason)

    if not isinstance(stop_mode, str) or stop_mode not in VALID_STOP_MODES:
        return _rej(f"illegal_stop_mode:{stop_mode}")
    if direction not in VALID_DIRECTIONS:
        return _rej(f"illegal_direction:{direction}")

    entry, r = finite_number(entry_ref_price, name="entry_ref_price", positive=True)
    if r:
        return _rej(f"invalid_entry:{r}")
    atr, r = finite_number(atr_val, name="atr_val", positive=True)
    if r:
        return _rej(f"invalid_atr:{r}")
    mult, r = finite_number(atr_mult, name="atr_mult", positive=True)
    if r:
        return _rej(f"invalid_atr_mult:{r}")
    buf, r = finite_number(structural_buffer_atr, name="structural_buffer_atr", nonneg=True)
    if r:
        return _rej(f"invalid_structural_buffer:{r}")
    step, r = finite_number(price_step, name="price_step", nonneg=True)
    if r:
        return _rej(f"invalid_price_step:{r}")

    if stop_mode in ("structural", "lps_structural"):
        ext, r = finite_number(structural_extreme, name="structural_extreme", positive=True)
        if r:
            return _rej(f"missing_structural_extreme:{r}")
        raw_stop = ext - buf * atr if direction == 1 else ext + buf * atr
        if raw_stop <= 0.0:
            return _rej("nonpositive_raw_stop")
        if direction == 1 and raw_stop >= entry:
            return _rej("invalid_structural_price_not_below_entry")
        if direction == -1 and raw_stop <= entry:
            return _rej("invalid_structural_price_not_above_entry")
    else:                                            # uniform
        raw_stop = entry - mult * atr if direction == 1 else entry + mult * atr
        if raw_stop <= 0.0:
            return _rej("nonpositive_uniform_stop")

    stop_px = _round_protect(raw_stop, step, direction)
    if stop_px <= 0.0:
        return _rej("nonpositive_stop_after_round")
    dist = abs(entry - stop_px)
    if dist <= 0.0:
        return _rej("zero_stop_distance")
    if direction == 1 and stop_px >= entry:
        return _rej("stop_not_below_entry")
    if direction == -1 and stop_px <= entry:
        return _rej("stop_not_above_entry")
    return StopDecision(True, float(stop_px), "ok", stop_dist=float(dist),
                        raw_stop=float(raw_stop), rounded_stop=float(stop_px),
                        stop_atr_mult=float(dist / atr))


# ══════════════════════════ ATR（Wilder 递归，因果）══════════════════════════

def compute_atr_wilder(high, low, close, period: int = 14) -> np.ndarray:
    """Wilder 递归 ATR。前 `period-1` 位为 NaN（不足不算出，不猜）。"""
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    n = len(c)
    atr = np.full(n, np.nan, dtype=np.float64)
    if n == 0 or period < 1:
        return atr
    tr = np.full(n, np.nan, dtype=np.float64)
    tr[0] = h[0] - l[0]
    if n > 1:
        pc = c[:-1]
        tr[1:] = np.maximum(h[1:] - l[1:],
                            np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    if n < period:
        return atr
    atr[period - 1] = float(np.mean(tr[:period]))
    alpha = 1.0 / period
    for i in range(period, n):
        atr[i] = atr[i - 1] * (1.0 - alpha) + tr[i] * alpha
    return atr


# ══════════════════════════ 输入校验 ══════════════════════════

def validate_signal_input(df: pd.DataFrame, time_unit) -> str:
    """校验列齐全 + 时间单位显式可解析 + OHLCV 有限性。返回规范化后的单位名。"""
    if not isinstance(df, pd.DataFrame):
        raise SignalInputError("input_not_dataframe")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise SignalInputError(f"missing_columns:{missing}")
    unit = F.resolve_time_unit(time_unit)            # 未声明/非法 ⇒ 抛 TimeUnitError
    if len(df) == 0:
        return unit
    for c in ("open", "high", "low", "close", "volume"):
        v = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=np.float64)
        if not np.all(np.isfinite(v)):
            bad = int((~np.isfinite(v)).sum())
            raise SignalInputError(f"non_finite_ohlcv:{c}:{bad}")
    F.to_epoch_seconds(df["timestamp"].to_numpy(), unit)     # 逐值区间校验
    return unit


# ══════════════════════════ 主信号入口 ══════════════════════════

def compute_signals(df: pd.DataFrame, params: Optional[SignalParams] = None,
                    time_unit=None, price_step: Optional[float] = None) -> pd.DataFrame:
    """逐 bar 产出信号（长度 = 去重后 bar 数）。

    输出新增列：`signal` / `signal_name` / `direction` / `stop_price` / `structural_ref` /
    `stop_mode` / `stop_atr_mult` / `reject_reason` / `atr` / `dc_high_prev` / `dc_low_prev` /
    `exit_low_prev` / `exit_high_prev` / `confirm_time`，以及 `factors.compute_oi_factors` 的全部因子列。
    """
    p = params or SIGNAL_FROZEN
    unit = validate_signal_input(df, time_unit)
    step = float(p.price_step) if price_step is None else float(price_step)

    work, dedup_report = F.dedup_bars(df)
    out = F.compute_oi_factors(work, p)
    # PIT 可得性门禁（施工单 W2-5）：未声明 OI 发布延迟 ⇒ pit_proven=False（不乐观使用）
    out = PIT.annotate_oi_pit(out, p)
    out = out.reset_index(drop=True)
    n = len(out)
    out.attrs["time_unit"] = unit
    out.attrs["dedup_report"] = dedup_report

    if n == 0:
        for c, v in (("signal", SIG_NONE), ("signal_name", "none"), ("direction", 0),
                     ("stop_price", np.nan), ("structural_ref", np.nan),
                     ("stop_mode", ""), ("stop_atr_mult", np.nan), ("reject_reason", ""),
                     ("atr", np.nan), ("dc_high_prev", np.nan), ("dc_low_prev", np.nan),
                     ("exit_low_prev", np.nan), ("exit_high_prev", np.nan),
                     ("exit_low_curr", np.nan), ("exit_high_curr", np.nan),
                     ("confirm_time", np.nan)):
            out[c] = pd.Series(dtype=object if isinstance(v, str) else float)
        return out

    o = out["open"].to_numpy(np.float64)
    h = out["high"].to_numpy(np.float64)
    l = out["low"].to_numpy(np.float64)
    c = out["close"].to_numpy(np.float64)
    v = out["volume"].to_numpy(np.float64)
    ts_sec = F.to_epoch_seconds(out["timestamp"].to_numpy(), unit)

    atr = compute_atr_wilder(h, l, c, int(p.atr_period))
    out["atr"] = atr

    win_e, win_x = int(p.donchian_entry_window), int(p.donchian_exit_window)
    s_h, s_l, s_v = pd.Series(h), pd.Series(l), pd.Series(v)
    dc_high_prev = s_h.rolling(win_e).max().shift(1).to_numpy(np.float64)
    dc_low_prev = s_l.rolling(win_e).min().shift(1).to_numpy(np.float64)
    exit_low_prev = s_l.rolling(win_x).min().shift(1).to_numpy(np.float64)
    exit_high_prev = s_h.rolling(win_x).max().shift(1).to_numpy(np.float64)
    # **当期**通道（不 shift）：当根 bar 收盘后即可得 ⇒ 供「收盘确认 → 下一根 15m 生效」使用
    exit_low_curr = s_l.rolling(win_x).min().to_numpy(np.float64)
    exit_high_curr = s_h.rolling(win_x).max().to_numpy(np.float64)
    vol_ma_prev = s_v.rolling(win_e).mean().shift(1).to_numpy(np.float64)

    out["dc_high_prev"] = dc_high_prev
    out["dc_low_prev"] = dc_low_prev
    out["exit_low_prev"] = exit_low_prev
    out["exit_high_prev"] = exit_high_prev
    out["exit_low_curr"] = exit_low_curr
    out["exit_high_curr"] = exit_high_curr

    score = out[F.C_SCORE].to_numpy(np.float64)
    avail = out[F.C_AVAILABLE].to_numpy(bool)
    # 因子门槛是否真正参与拒单（UNSET / <=0 ⇒ 不参与）
    oi_gate_enabled = (p.oi_min_resonance is not None
                       and float(p.oi_min_resonance) > 0.0)
    oi_min_resonance = (float(p.oi_min_resonance) if oi_gate_enabled else None)
    # PIT：OI 观测是否**可证明**在 bar 收盘前已可见（施工单 W2-5）
    if PIT.C_PIT_PROVEN in out.columns:
        oi_pit_ok = out[PIT.C_PIT_PROVEN].to_numpy(bool)
    else:
        oi_pit_ok = np.zeros(n, dtype=bool)
    # 发布延迟是否**已声明**：未声明 ⇒ 属**配置缺失**（运行级拒绝），
    # 不据此逐 bar 改策略规则（施工单 W2-5 原文为「标记不可用」，非「逐 bar 拒单」）。
    oi_pit_declared = getattr(p, "oi_publication_lag_ms", None) is not None

    sig = np.zeros(n, np.int64)
    direction = np.zeros(n, np.int64)
    stop_price = np.full(n, np.nan, np.float64)
    structural_ref = np.full(n, np.nan, np.float64)
    stop_mult = np.full(n, np.nan, np.float64)
    stop_mode_col = np.array([""] * n, dtype=object)
    reject = np.array([""] * n, dtype=object)

    last_bo_idx, last_bo_level, last_bo_low = -10 ** 9, np.nan, np.nan
    start = max(win_e, int(p.atr_period))

    def _accept(i, kind, dr, mode, ext, ref_price):
        dec = compute_stop(ref_price, atr[i], mode, dr, float(p.atr_mult),
                           ext, float(p.structural_buffer_atr), step)
        if not dec.accepted:
            reject[i] = dec.reason
            return False
        sig[i] = kind
        direction[i] = dr
        stop_price[i] = dec.stop_price
        stop_mult[i] = dec.stop_atr_mult
        structural_ref[i] = ext if ext is not None else np.nan
        stop_mode_col[i] = mode
        return True

    for i in range(start, n):
        atr_i = atr[i]
        dch, dcl = dc_high_prev[i], dc_low_prev[i]
        if not (math.isfinite(atr_i) and atr_i > 0.0):
            continue
        if not (math.isfinite(dch) and math.isfinite(dcl)):
            continue

        # ⓿ **fresh breakout 登记**——必须在 OI 门禁**之前**：
        #    低 OI / 因子不可用**不得**阻断突破锚点登记（否则后续回踩永远无从判定）。
        #    fresh = 当根突破，且**上一根尚未突破**（含上一根 dc 不可用）⇒ 连续突破不重锚。
        if i >= 1 and c[i] > dch:
            prev_dch = dc_high_prev[i - 1]
            is_fresh = (not math.isfinite(prev_dch)) or (c[i - 1] <= prev_dch)
            if is_fresh:
                last_bo_idx, last_bo_level, last_bo_low = i, float(dch), float(l[i])

        # ① 因子门槛（**仅在显式设置时生效**；不可用 ⇒ fail-closed，不伪造 0.5）
        #    `oi_min_resonance is None` = **UNSET**（未批准生产阈值，施工单 W2-1）
        #    ⇒ 门槛不参与决策（OI 仍照实计算并逐行标注），但正式运行门禁会拒绝。
        if oi_gate_enabled:
            if not avail[i]:
                reject[i] = "oi_factor_unavailable_fail_closed"
                continue
            # PIT（W2-5）：**仅当发布延迟已声明**时，逐行校验「该观测在 bar 收盘前确实可见」；
            # 可见性不成立 ⇒ 逐行 fail-closed（这是**数据**问题，不是配置问题）。
            # 未声明（None）⇒ **不改逐 bar 策略规则**，由 `formal_run_gate()` 在**运行级**拒绝。
            if oi_pit_declared and not oi_pit_ok[i]:
                reject[i] = "oi_pit_unproven_fail_closed"
                continue
            if not (math.isfinite(score[i]) and score[i] >= oi_min_resonance):
                reject[i] = "oi_resonance_below_threshold"
                continue

        # ② SC 反转（**先于 Spring**：同根同时满足时选 SC，不沿用旧优先级）
        rng = h[i] - l[i]
        lower_wick = min(o[i], c[i]) - l[i]
        if (p.enable_sc and math.isfinite(vol_ma_prev[i]) and vol_ma_prev[i] > 0
                and v[i] > float(p.sc_vol_mult) * vol_ma_prev[i]
                and l[i] < dcl
                and rng > float(p.sc_range_atr_mult) * atr_i
                and rng > 0 and lower_wick >= float(p.sc_lower_wick_frac) * rng):
            if _accept(i, SIG_SC_REVERSAL, 1, "structural", float(l[i]), float(c[i])):
                continue

        # ③ Spring：跌破前低后收回，且当根收阳
        if l[i] < dcl and c[i] > dcl and c[i] > o[i]:
            if _accept(i, SIG_SPRING, 1, "structural", float(l[i]), float(c[i])):
                continue

        # ④ 唐奇安突破（锚点已在 ⓿ 登记，此处**不得**重复更新 last_bo_*）
        if c[i] > dch:
            if _accept(i, SIG_DONCHIAN_BREAKOUT, 1, "uniform", None, float(c[i])):
                continue

        # ⑤ LPS 回踩确认（附件条件：触及突破位附近 + 不破前低 + 重新站上突破位且收阳）
        #    **无** close_pos 过滤（附件未要求；此前多加的条件已删除）
        if (1 <= (i - last_bo_idx) <= int(p.pullback_window)
                and math.isfinite(last_bo_level)):
            touched = l[i] <= last_bo_level + float(p.pullback_proximity_atr) * atr_i
            held = l[i] > last_bo_low
            reclaimed = c[i] >= last_bo_level and c[i] > o[i]
            if touched and held and reclaimed:
                pb_low = min(float(l[i]), last_bo_low)
                if _accept(i, SIG_LPS_PULLBACK, 1, "lps_structural", pb_low, float(c[i])):
                    last_bo_idx = -10 ** 9
                    continue

        # ⑥ 空头突破（默认关闭）
        if p.enable_short and c[i] < dcl:
            if _accept(i, SIG_DONCHIAN_BREAKOUT, -1, "uniform", None, float(c[i])):
                continue

    out["signal"] = sig
    out["signal_name"] = [SIGNAL_NAMES[int(x)] for x in sig]
    out["direction"] = direction
    out["stop_price"] = stop_price
    out["structural_ref"] = structural_ref
    out["stop_mode"] = stop_mode_col
    out["stop_atr_mult"] = stop_mult
    out["reject_reason"] = reject
    out["confirm_time"] = ts_sec + int(p.signal_bar_seconds)

    counts = {SIGNAL_NAMES[k]: int((sig == k).sum()) for k in SIGNAL_NAMES if k != SIG_NONE}
    counts["none"] = int((sig == SIG_NONE).sum())
    out.attrs["signal_counts"] = counts
    out.attrs["reject_counts"] = {
        r: int((reject == r).sum()) for r in sorted(set(reject.tolist())) if r}
    out.attrs["oi_factor_meta"] = F.oi_factor_meta(out)
    out.attrs["oi_pit"] = PIT.pit_summary(out)
    out.attrs["oi_gate"] = {
        "oi_min_resonance": oi_min_resonance,
        "status": ("UNSET" if p.oi_min_resonance is None
                   else ("SET_ZERO_NO_GATE" if float(p.oi_min_resonance) <= 0.0 else "SET")),
        "enabled": bool(oi_gate_enabled),
        "pit_declared": bool(oi_pit_declared),
        "note": ("UNSET = 未批准生产阈值 ⇒ 门槛不参与决策，且正式运行门禁拒绝；"
                 "OI 仍逐行标注 quality / pit_proven / decision_usable。"
                 "PIT 未声明 ⇒ 逐行标记 pit_proven=False，**不在逐 bar 改策略规则**，"
                 "由 formal_run_gate 在运行级拒绝（禁止乐观使用）。"),
    }
    return out


def signal_summary(df: pd.DataFrame) -> dict:
    """信号层摘要（供回执引用）。"""
    if "signal" not in df.columns:
        return {"bars": int(len(df)), "note": "no_signal_column"}
    return {
        "bars": int(len(df)),
        "signal_counts": dict(df.attrs.get("signal_counts") or {}),
        "reject_counts": dict(df.attrs.get("reject_counts") or {}),
        "dedup_report": dict(df.attrs.get("dedup_report") or {}),
        "oi_factor_meta": dict(df.attrs.get("oi_factor_meta") or {}),
        "oi_pit": dict(df.attrs.get("oi_pit") or {}),
        "oi_gate": dict(df.attrs.get("oi_gate") or {}),
        "time_unit": df.attrs.get("time_unit"),
        "strategy_id": STRATEGY_ID,
    }


__all__ = [
    "SIG_NONE", "SIG_SPRING", "SIG_SC_REVERSAL", "SIG_DONCHIAN_BREAKOUT", "SIG_LPS_PULLBACK",
    "SIGNAL_NAMES", "VALID_STOP_MODES", "VALID_DIRECTIONS", "REQUIRED_COLUMNS",
    "SignalInputError", "StopDecision", "finite_number", "compute_stop",
    "compute_atr_wilder", "validate_signal_input", "compute_signals", "signal_summary",
]
