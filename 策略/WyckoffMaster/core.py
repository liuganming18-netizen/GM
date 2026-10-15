# -*- coding: utf-8 -*-
"""WyckoffMaster 纯核心：三类买点合体信号机 + 止损 + 台阶锁利（v3）。

范围与纪律：
  · 纯函数：输入 = 已收盘信号周期 bar 序列（1h 或 4h）+ 冻结参数；输出 = 事件与价格。
    **不读库、不联网、不下单、不持有账户状态**。
  · 只消费已收盘 bar；全部前置基线取 `shift(1)`（严格更早的 bar）。
    例外（施工单第3条）：**当根**收盘 ATR 用于止损（收盘可用）；持仓锁利用**入场信号
    冻结 ATR**（entry_atr）。
  · 零振幅 / 缺量 / NaN **不得**制造信号（close_pos 为 NaN 即拒绝）。
  · 同根冲突顺序固定 **Spring -> breakout -> retest**；母线最多确认一次；
    未决锚点不被覆盖（先到先得，直至确认/超时/失效）。
  · v3：结构止损距离 > 3.5×ATR **拒单**（不 clip）；距离 < 1.5×ATR 只向更低处扩大。
  · v3：台阶锁利在信号周期收盘判定、**下一根 15m** 起生效，绝不回填、不下移。

执行层（取整 / 滑点 / 资金费 / 回撤停机 / 15m 成交）不在本模块，由既有引擎
`wx_execution.py`（研究等价版 `ra_engine.py`）承担；本模块只产出信号 bar 的 ATR、
结构低点、参考止损/目标价与拒单原因。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import series as S
from .params import MasterParams, FROZEN


# ══════════════════════════ 结果容器 ══════════════════════════

@dataclass
class SignalFrame:
    """逐 bar 信号输出（长度 = 输入 bar 数，全部 numpy 数组）。"""
    atr: np.ndarray                 # 当根收盘可用 ATR（止损用）
    atr_prev: np.ndarray            # 上一根 ATR（前置阈值用）
    don_high: np.ndarray            # 前 20 根最高（shift1）
    don_low: np.ndarray             # 前 20 根最低（shift1）
    don_exit_low: np.ndarray        # 前 10 根最低（shift1）
    vol_ma: np.ndarray              # 前 20 根均量（shift1）
    close_pos: np.ndarray
    is_tr: np.ndarray               # 箱体（Spring 前提）
    spring_entry: np.ndarray
    breakout_entry: np.ndarray
    retest_entry: np.ndarray
    master_entry: np.ndarray
    struct_low: np.ndarray          # 结构参考低点（uniform 模式为 NaN）
    ref_stop_price: np.ndarray      # 信号阶段参考止损（含 v3 拒单语义，NaN=拒单/无信号）
    ref_target_price: np.ndarray    # entry + 2R（仅参考）
    signal_kind: list               # "spring"/"breakout"/"retest"/""
    reject_reason: list             # 信号阶段结构止损拒单原因（"" = 无）
    anchor_trace: list              # 锚点轨迹（register/confirm/invalidate/expire/overlap_ignored）
    diagnostics: dict

    def events(self) -> list[dict]:
        """把逐 bar 输出压成事件列表（仅 master_entry=True 的 bar）。"""
        out = []
        for i in range(len(self.master_entry)):
            if not self.master_entry[i]:
                continue
            out.append({
                "bar_index": i,
                "signal_kind": self.signal_kind[i],
                "atr_signal_bar": float(self.atr[i]) if np.isfinite(self.atr[i]) else None,
                "atr_prev": float(self.atr_prev[i]) if np.isfinite(self.atr_prev[i]) else None,
                "struct_low": float(self.struct_low[i]) if np.isfinite(self.struct_low[i]) else None,
                "ref_stop_price": float(self.ref_stop_price[i])
                if np.isfinite(self.ref_stop_price[i]) else None,
                "ref_target_price": float(self.ref_target_price[i])
                if np.isfinite(self.ref_target_price[i]) else None,
                "don_high": float(self.don_high[i]) if np.isfinite(self.don_high[i]) else None,
                "don_low": float(self.don_low[i]) if np.isfinite(self.don_low[i]) else None,
                "reject_reason": self.reject_reason[i],
            })
        return out


# ══════════════════════════ 止损 / 目标 ══════════════════════════

def compute_stop(entry_px: float, struct_low, atr_val: float, params: MasterParams = FROZEN,
                 stop_mode: str = "structural", price_step: float = 0.0,
                 raw_open=None) -> dict:
    """按入场价计算初始止损价 / 距离 / 2R 目标，并给出 v3 拒单原因。

    返回 dict：{stop_price, stop_dist, target_price, reject, raw_stop, rounded_stop,
                dist_after_round, stop_atr_mult}

    规则（与施工单「策略实现冻结」第 4 条 + v3.1 第 2 条逐字一致）：
      · uniform：D = uniform_stop_atr × ATR。
      · structural（M2）：
          raw_stop = struct_low − wick_buffer_atr × ATR
          raw_dist = entry_px − raw_stop
          · raw_dist < min_stop_atr×ATR ⇒ 只向**更低处**扩大到 min（stop = entry − min×ATR）
          · raw_dist > max_stop_atr×ATR ⇒ **拒单**（v3，不 clip 拉回）
          · 否则 D = raw_dist
        取整（round_protect_price，向更低）后**再核**：若取整后距离 > max×ATR ⇒ 拒单。
        raw_stop <= 0 ⇒ 拒单；原始开盘已穿止损（raw_open <= stop）⇒ 拒单。
      · structural_m2b（M2b，v3.1 条件加跑）：
          D = max(P_entry − (L_struct − 0.8×ATR), 1.5×ATR)，**不因 >3.5ATR 拒单**；
          仍要求合法正止损、正常精度、真实风险（数量按 0.5% 权益/实际距离自然缩小，
          缩仓由回测窗口执行）。
      绝不把止损抬回结构点上方（raw_stop 恒 < struct_low，扩大时只会更低）。
    """
    p = params
    out = {"stop_price": None, "stop_dist": None, "target_price": None,
           "reject": None, "raw_stop": None, "rounded_stop": None,
           "dist_after_round": None, "stop_atr_mult": None}
    if not math.isfinite(atr_val) or atr_val <= 0:
        out["reject"] = "nonpositive_atr"
        return out
    if not math.isfinite(entry_px) or entry_px <= 0:
        out["reject"] = "nonpositive_entry"
        return out

    if stop_mode == "uniform" or struct_low is None or not math.isfinite(struct_low):
        dist = p.uniform_stop_atr * atr_val
        raw_stop = entry_px - dist
    else:
        # structural（M2）与 structural_m2b（M2b）共用同一 raw_stop
        raw_stop = float(struct_low) - p.wick_buffer_atr * atr_val
        out["raw_stop"] = raw_stop
        if raw_stop <= 0:
            out["reject"] = "nonpositive_raw_stop"
            return out
        raw_dist = entry_px - raw_stop
        lo = p.min_stop_atr * atr_val
        hi = p.max_stop_atr * atr_val
        if stop_mode == "structural_m2b":
            # v3.1：M2b 不设 3.5ATR 上限、不因过远拒单；距离取 max(raw_dist, 1.5ATR)，
            # 数量按 0.5% 权益 / 实际距离自然缩小（缩仓在回测窗口执行）。
            dist = max(raw_dist, lo)
        elif raw_dist > hi:
            # v3：过远拒单，不 clip 拉回（v2 的 np.clip 已被覆盖）
            out["reject"] = "structural_stop_too_far"
            return out
        elif raw_dist < lo:
            # 只向更低处扩大到下限：stop 更低 ⇒ 距离更大；raw_stop 之上不动
            dist = lo
        else:
            dist = raw_dist

    stop_px = entry_px - dist
    stop_px = _round_protect(stop_px, price_step)
    out["rounded_stop"] = stop_px
    if stop_px <= 0:
        out["reject"] = "nonpositive_stop_after_round"
        return out
    dist_after = entry_px - stop_px
    out["dist_after_round"] = dist_after
    if stop_mode == "structural" and dist_after > p.max_stop_atr * atr_val + 1e-12:
        # 向下取整只会让 stop 更低 ⇒ 距离只会更大；取整后仍超上限即拒单
        out["reject"] = "structural_stop_too_far_after_round"
        return out
    if stop_px >= entry_px:
        out["reject"] = "stop_not_below_entry"
        return out
    if raw_open is not None and math.isfinite(raw_open) and raw_open <= stop_px:
        out["reject"] = "open_already_through_stop"
        return out

    out["stop_price"] = stop_px
    out["stop_dist"] = dist_after
    out["target_price"] = entry_px + p.target_rr * dist_after
    out["stop_atr_mult"] = dist_after / atr_val
    return out


def _round_protect(px: float, step: float) -> float:
    """保护价（多头止损）向**更低**方向取整；step<=0 不取整（镜像执行层口径）。"""
    if not step or step <= 0:
        return px
    return step * math.floor(px / step + 1e-9)


def _round_buy(px: float, step: float) -> float:
    """买价向**更贵**方向取整（镜像执行层 round_buy_price）。step<=0 不取整。"""
    if not step or step <= 0:
        return px
    return step * math.ceil(px / step - 1e-9)


# ══════════════════════════ +15m 实际成交判定（v3.1 第 3 条）══════════════════════════

def evaluate_entry_fill(*, open_15m, struct_low, atr_val, params: MasterParams = FROZEN,
                        stop_mode: str = "structural", price_step: float = 0.0,
                        slippage: float = 0.0005) -> dict:
    """+15m 实际成交判定：**只用该时刻 open + 已知信号结构 + 滑点/取整**。

    绝不用同根 15m 的未来 low/close 决定是否入场。顺序：
      ① P_entry = round_buy(open_15m × (1+滑点), price_step)
      ② 若使用结构低点且 P_entry <= L_struct ⇒ 拒单 `entry_below_struct_low`（M2/M2b 均适用）
      ③ 按 stop_mode 计算初始止损（uniform / structural / structural_m2b）
      ④ 若 open_15m <= stop ⇒ 拒单 `open_already_through_stop`（原 open<=initial_stop 拒单继续）

    返回 {entry_price, entry_raw, stop_price, stop_dist, target_price, reject, reason}
    """
    out = {"entry_price": None, "entry_raw": None, "stop_price": None,
           "stop_dist": None, "target_price": None, "reject": None}
    if open_15m is None or not math.isfinite(open_15m) or open_15m <= 0:
        out["reject"] = "missing_or_nonpositive_open"
        return out
    entry_raw = open_15m * (1.0 + slippage)
    entry = _round_buy(entry_raw, price_step)
    out["entry_raw"] = entry_raw
    out["entry_price"] = entry
    uses_struct = stop_mode in ("structural", "structural_m2b") and \
        struct_low is not None and math.isfinite(struct_low)
    if uses_struct and entry <= float(struct_low):
        out["reject"] = "entry_below_struct_low"
        return out
    r = compute_stop(entry, struct_low if uses_struct else None, atr_val, params,
                     stop_mode, price_step)
    if r["reject"]:
        out["reject"] = r["reject"]
        return out
    stop = r["stop_price"]
    if open_15m <= stop:
        out["reject"] = "open_already_through_stop"
        return out
    out["stop_price"] = stop
    out["stop_dist"] = r["stop_dist"]
    out["target_price"] = r["target_price"]
    return out


# ══════════════════════════ M2 逐类型超限拒单统计（v3.1 第 2 条）══════════════════════════

def m2_over_limit_stats(records, params: MasterParams = FROZEN) -> dict:
    """按 spring/breakout/retest 统计 D_stop > 3.5ATR 超限拒单计数与比例。

    `records`：可迭代的**唯一候选信号**记录，每项含
      kind（"spring"/"breakout"/"retest"）、entry_price（执行价）、struct_low、atr。
    **分母** = 到达结构止损超限判断、具备**有效结构**（struct_low 有限且 raw_stop>0）
    与**有效执行价/ATR** 的唯一候选数。数量为 0 记 N/A（不触发）。
    超限事件需由调用方另留逐笔键，不与穿底/精度/资金不足混算（本函数只做计数）。

    返回 {by_type: {kind: {denominator, over_limit, rate, na}}, total: {...}}
    """
    by: dict = {}
    for r in records:
        kind = r.get("kind")
        e, s, a = r.get("entry_price"), r.get("struct_low"), r.get("atr")
        d = by.setdefault(kind, {"denominator": 0, "over_limit": 0})
        if e is None or s is None or a is None:
            continue
        if not (math.isfinite(e) and math.isfinite(s) and math.isfinite(a)) or a <= 0:
            continue
        raw_stop = float(s) - params.wick_buffer_atr * float(a)
        if raw_stop <= 0:
            continue                       # 无有效结构，不计入分母
        d["denominator"] += 1
        if (float(e) - raw_stop) > params.max_stop_atr * float(a):
            d["over_limit"] += 1
    tot = {"denominator": 0, "over_limit": 0}
    for k, d in by.items():
        d["rate"] = (d["over_limit"] / d["denominator"]) if d["denominator"] > 0 else None
        d["na"] = d["denominator"] == 0
        tot["denominator"] += d["denominator"]
        tot["over_limit"] += d["over_limit"]
    tot["rate"] = (tot["over_limit"] / tot["denominator"]) if tot["denominator"] > 0 else None
    tot["na"] = tot["denominator"] == 0
    return {"by_type": by, "total": tot}


def m2b_trigger(stats_is: dict, stats_val: dict,
                threshold: float = 0.30) -> dict:
    """任一类型 **IS+Val 合并**超限拒单率 > 30% ⇒ 触发预注册 M2b。

    入参为两次 `m2_over_limit_stats` 的结果（须为**去重后的唯一候选**）。
    返回 {triggered, threshold, by_type:{kind:{denominator, over_limit, rate, na}}}
    """
    kinds = set(stats_is.get("by_type", {})) | set(stats_val.get("by_type", {}))
    per = {}
    triggered = False
    for k in sorted(kinds):
        di = stats_is.get("by_type", {}).get(k, {}).get("denominator", 0)
        oi = stats_is.get("by_type", {}).get(k, {}).get("over_limit", 0)
        dv = stats_val.get("by_type", {}).get(k, {}).get("denominator", 0)
        ov = stats_val.get("by_type", {}).get(k, {}).get("over_limit", 0)
        den, over = di + dv, oi + ov
        rate = (over / den) if den > 0 else None
        per[k] = {"denominator": den, "over_limit": over, "rate": rate, "na": den == 0}
        if rate is not None and rate > threshold:
            triggered = True
    return {"triggered": triggered, "threshold": threshold, "by_type": per}


# ══════════════════════════ 信号状态机 ══════════════════════════

def compute_signals(bars, params: MasterParams = FROZEN,
                    entry_mode: str = "three_buy_points",
                    stop_mode: str = "structural",
                    price_step: float = 0.0) -> SignalFrame:
    """主信号入口。

    bars：可迭代的「已收盘」信号周期 bar；每根需含
      open_time, close_time, open, high, low, close, volume, quote_volume（dict 或对象）。
    entry_mode：
      · "donchian_only"     = M0 纯 20 突破（close > 前20最高，严格）
      · "three_buy_points"  = M1+ 三类买点合体
    stop_mode："uniform" | "structural"（仅影响参考止损/拒单，不影响入场判定）
    price_step：历史价格最小变动（0 = 不取整）。
    """
    rows = [_norm_bar(b) for b in bars]
    m = len(rows)
    o = np.array([r["open"] for r in rows], float)
    h = np.array([r["high"] for r in rows], float)
    l = np.array([r["low"] for r in rows], float)
    c = np.array([r["close"] for r in rows], float)
    v = np.array([r["volume"] for r in rows], float)
    p = params

    tr = S.true_ranges(o, h, l, c)
    atr = S.ewm_atr(tr, 1.0 / p.atr_n, p.atr_n)
    atr_prev = S.shift(atr, 1)
    vol_ma = S.rolling_mean_prev(v, p.vol_n)
    don_high = S.rolling_max_prev(h, p.don_entry)
    don_low = S.rolling_min_prev(l, p.don_entry)
    don_exit_low = S.rolling_min_prev(l, p.don_exit)
    cp = S.close_pos(h, l, c)

    width = don_high - don_low
    is_tr = np.where(np.isfinite(width) & np.isfinite(atr_prev),
                     width < (p.tr_atr_mult * atr_prev), False)

    # —— 原始条件（严格不等号；NaN 一律 False，不制造信号）——
    raw_spring = _all(
        is_tr,
        l < don_low,
        c > don_low,
        cp >= p.spring_close_pos,
        _gt(v, p.spring_vol_mult * vol_ma),
        (don_low - l) < (p.spring_depth_atr * atr_prev),
    )
    raw_breakout = _all(
        c > don_high,
        c > o,
        _gt(v, p.breakout_vol_mult * vol_ma),
    )
    overheated = _any(
        (h - l) > (p.overheat_range_atr * atr_prev),
        (c - don_high) > (p.overheat_ext_atr * atr_prev),
        cp < 0.60,
    )

    spring_entry = np.zeros(m, bool)
    breakout_entry = np.zeros(m, bool)
    retest_entry = np.zeros(m, bool)
    struct_low = np.full(m, np.nan)
    ref_stop = np.full(m, np.nan)
    ref_target = np.full(m, np.nan)
    kind = [""] * m
    reject = [""] * m
    trace = []

    sp_idx, sp_low, sp_mid = -1, np.nan, np.nan
    bo_idx, bo_level = -1, np.nan

    don_only = (entry_mode == "donchian_only")

    for i in range(m):
        cur_atr = atr[i] if np.isfinite(atr[i]) else atr_prev[i]
        if not np.isfinite(cur_atr) or cur_atr <= 0:
            continue

        # —— M0 纯突破：不做 Spring / 回踩，仅严格 close > 前20最高 ——
        if don_only:
            if bool(_finite_gt(c[i], don_high[i])):
                breakout_entry[i] = True
                kind[i] = "donchian_breakout"
                struct_low[i] = don_high[i]
                _fill_ref(i, c[i], don_high[i], cur_atr, p, stop_mode, price_step,
                          ref_stop, ref_target, reject)
            continue

        # Step 1：已有未决 Spring 锚点（失效先于确认；严格不等号）
        if sp_idx >= 0:
            if (i - sp_idx) > p.spring_confirm_bars:
                trace.append({"bar": i, "kind": "spring", "action": "expire",
                              "anchor_low": sp_low})
                sp_idx = -1
            elif l[i] < sp_low:
                trace.append({"bar": i, "kind": "spring", "action": "invalidate",
                              "anchor_low": sp_low, "reason": "low_below_anchor_low"})
                sp_idx = -1
            elif c[i] > sp_mid:
                spring_entry[i] = True
                kind[i] = "spring"
                struct_low[i] = sp_low
                _fill_ref(i, c[i], sp_low, cur_atr, p, stop_mode, price_step,
                          ref_stop, ref_target, reject)
                trace.append({"bar": i, "kind": "spring", "action": "confirm",
                              "anchor_low": sp_low, "mid": sp_mid,
                              "register_bar": sp_idx})
                sp_idx = -1
            elif bool(raw_spring[i]):
                # 未决锚点存在时的新母线不覆盖旧锚点（先到先得）
                trace.append({"bar": i, "kind": "spring", "action": "overlap_ignored",
                              "kept_anchor_low": sp_low, "kept_register_bar": sp_idx})

        # 仅当无未决 Spring 锚点、且当根未确认时，才登记新 Spring 母线（不覆盖旧锚点）
        if sp_idx < 0 and not spring_entry[i] and bool(raw_spring[i]):
            sp_idx = i
            sp_low = l[i]
            sp_mid = 0.5 * (don_high[i] + don_low[i])
            trace.append({"bar": i, "kind": "spring", "action": "register",
                          "anchor_low": sp_low, "mid": sp_mid})

        # Step 2：当根未触发 Spring，才评估 breakout / retest（冲突顺序 Spring 优先）
        if not spring_entry[i]:
            if bool(raw_breakout[i]) and not bool(overheated[i]):
                breakout_entry[i] = True
                kind[i] = "breakout"
                struct_low[i] = don_high[i]
                _fill_ref(i, c[i], don_high[i], cur_atr, p, stop_mode, price_step,
                          ref_stop, ref_target, reject)
                bo_idx = -1
            else:
                if bo_idx >= 0:
                    if (i - bo_idx) > p.retest_window_bars:
                        trace.append({"bar": i, "kind": "retest", "action": "expire",
                                      "level": bo_level})
                        bo_idx = -1
                    elif l[i] < (bo_level - 0.8 * cur_atr):
                        trace.append({"bar": i, "kind": "retest", "action": "invalidate",
                                      "level": bo_level, "reason": "low_below_level_buffer"})
                        bo_idx = -1
                    elif (l[i] <= bo_level + 0.5 * cur_atr and c[i] > bo_level
                          and c[i] > o[i] and np.isfinite(cp[i]) and cp[i] >= 0.50):
                        retest_entry[i] = True
                        kind[i] = "retest"
                        struct_low[i] = l[i]          # 统一：回踩确认当根低点
                        _fill_ref(i, c[i], l[i], cur_atr, p, stop_mode, price_step,
                                  ref_stop, ref_target, reject)
                        trace.append({"bar": i, "kind": "retest", "action": "confirm",
                                      "level": bo_level, "register_bar": bo_idx})
                        bo_idx = -1
                    elif bool(raw_breakout[i]) and bool(overheated[i]):
                        trace.append({"bar": i, "kind": "retest", "action": "overlap_ignored",
                                      "kept_level": bo_level, "kept_register_bar": bo_idx})
                if bo_idx < 0 and not retest_entry[i] and \
                        bool(raw_breakout[i]) and bool(overheated[i]):
                    bo_idx = i
                    bo_level = don_high[i]
                    trace.append({"bar": i, "kind": "retest", "action": "register",
                                  "level": bo_level})

    master = spring_entry | breakout_entry | retest_entry
    diag = {
        "entry_mode": entry_mode, "stop_mode": stop_mode, "bars": m,
        "spring_count": int(spring_entry.sum()),
        "breakout_count": int(breakout_entry.sum()),
        "retest_count": int(retest_entry.sum()),
        "master_count": int(master.sum()),
        "reject_count": int(sum(1 for r in reject if r)),
        "reject_breakdown": _count(reject),
        "anchor_trace_counts": _count(t["action"] for t in trace),
        "atr_rule": "alpha=1/atr_n, adjust=False, min_periods=atr_n（Wilder 式递归）",
        "baseline_rule": "全部前置基线 shift(1)；当根收盘 ATR 用于止损；锁利用入场冻结 ATR",
        "conflict_order": "Spring -> breakout -> retest",
    }
    return SignalFrame(atr, atr_prev, don_high, don_low, don_exit_low, vol_ma, cp, is_tr,
                       spring_entry, breakout_entry, retest_entry, master, struct_low,
                       ref_stop, ref_target, kind, reject, trace, diag)


def _fill_ref(i, entry_px, struct_low_i, cur_atr, p, stop_mode, price_step,
              ref_stop, ref_target, reject):
    r = compute_stop(entry_px, struct_low_i, cur_atr, p, stop_mode, price_step)
    if r["reject"]:
        reject[i] = r["reject"]
        return
    ref_stop[i] = r["stop_price"]
    ref_target[i] = r["target_price"]


def _norm_bar(b) -> dict:
    if isinstance(b, dict):
        g = lambda k: b[k]  # noqa: E731
    else:
        g = lambda k: getattr(b, k)  # noqa: E731
    def f(k):
        val = g(k)
        return float(val) if val is not None else float("nan")
    return {"open": f("open"), "high": f("high"), "low": f("low"), "close": f("close"),
            "volume": f("volume"), "quote_volume": f("quote_volume"),
            "open_time": int(g("open_time")),
            "close_time": int(g("close_time")) if g("close_time") is not None else None}


def _all(*conds) -> np.ndarray:
    out = np.ones(len(conds[0]), bool)
    for x in conds:
        out &= np.asarray(x, bool)
    return out


def _any(*conds) -> np.ndarray:
    out = np.zeros(len(conds[0]), bool)
    for x in conds:
        out |= np.asarray(x, bool)
    return out


def _gt(a, b) -> np.ndarray:
    a, b = S.as_float(a), S.as_float(b)
    return np.where(np.isfinite(a) & np.isfinite(b), a > b, False)


def _finite_gt(a, b) -> bool:
    return math.isfinite(a) and math.isfinite(b) and a > b


def _count(items) -> dict:
    d: dict = {}
    for x in items:
        if x:
            d[x] = d.get(x, 0) + 1
    return d


# ══════════════════════════ 台阶锁利 / 保本 ══════════════════════════

def evaluate_trail_stop(closes, lows, current_stop, entry_price, entry_atr,
                        params: MasterParams = FROZEN, mode: str = "step_swing",
                        upto: int = None) -> dict:
    """在信号周期 bar 收盘后调用，返回**下一根 15m bar 开盘起生效**的止损。

    closes / lows：自**入场信号 bar** 起（含入场 bar，index 0）到**当前评估 bar**（含）
      的收盘价 / 最低价序列。**只读截至当前已收盘**，绝不使用未来 bar。
    upto：显式指定评估 bar 的下标（默认最后一根）。用于「截断无前视」核验：
      对同一序列，`upto=k` 的判定必须与仅传前缀 `closes[:k+1]` 的判定逐位相同。

    返回 {stop, updated, reason, cand, swing_low}。
    · mode="none"       ：不变
    · mode="naive_be"   ：收盘浮盈 >= be_trigger_r × R 且收盘确认 ⇒ 推至 entry_price
    · mode="step_swing" ：创持仓期收盘新高 且 浮盈 >= trail_activate_atr×entry_atr；
                          回看前 trail_lookback_bars 根已闭合低点（须属持仓后）− 0.8×ATR；
                          须 > 当前止损 且 >= entry + trail_min_above_entry_atr×ATR；
                          且至少 trail_min_bars_after_entry 根完整持仓信号 bar 后才评估。
    **单调不降**：候选 <= 当前止损一律不动（不下移）；绝不回填到过去 bar。
    """
    p = params
    idx = (len(closes) - 1) if upto is None else int(upto)
    if idx < 0:
        idx = 0
    closes = closes[:idx + 1]
    lows = lows[:idx + 1]
    n = len(closes)
    res = {"stop": current_stop, "updated": False, "reason": "hold",
           "cand": None, "swing_low": None}
    if mode == "none" or n < 2:
        return res
    cur_close = float(closes[-1])

    if mode == "naive_be":
        init_risk = max(entry_price - current_stop, p.min_stop_atr * entry_atr)
        if init_risk > 0 and (cur_close - entry_price) >= p.be_trigger_r * init_risk \
                and entry_price > current_stop:
            res.update(stop=entry_price, updated=True, reason="naive_be_breakeven")
        return res

    if mode == "step_swing":
        # v3：回看前 trail_lookback_bars 根必须**全部属于持仓后**（index >= 1），
        # 且至少 trail_min_bars_after_entry 根完整持仓 bar。二者取严：
        #   (n-1) = 评估 bar 与入场 bar 的距离（根数）。
        #   回看窗口 [n-1-k, n-1) 全属持仓后 ⇒ n-1-k >= 1 ⇒ n-1 >= k+1。
        k = p.trail_lookback_bars
        gate = max(p.trail_min_bars_after_entry, k + 1)
        if (n - 1) < gate:
            res["reason"] = "insufficient_bars_after_entry"
            return res
        prev_max_close = float(np.max(closes[:-1]))
        if not (cur_close > prev_max_close):
            res["reason"] = "no_new_close_high"
            return res
        if (cur_close - entry_price) < p.trail_activate_atr * entry_atr:
            res["reason"] = "profit_below_activation"
            return res
        # 回看前 k 根**已闭合**低点（全部属持仓后：index >= 1）
        lo_slice = lows[max(1, n - 1 - k):n - 1]
        if len(lo_slice) < k:
            res["reason"] = "insufficient_prior_lows"
            return res
        swing_low = float(np.min(lo_slice))
        cand = swing_low - p.wick_buffer_atr * entry_atr
        res["swing_low"] = swing_low
        res["cand"] = cand
        min_valid = entry_price + p.trail_min_above_entry_atr * entry_atr
        if cand > current_stop and cand >= min_valid:
            res.update(stop=cand, updated=True, reason="step_swing_raise")
        elif cand <= current_stop:
            res["reason"] = "candidate_not_above_current_stop"
        else:
            res["reason"] = "candidate_below_entry_plus_buffer"
        return res

    return res
