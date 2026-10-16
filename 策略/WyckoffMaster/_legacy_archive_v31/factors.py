# -*- coding: utf-8 -*-
"""WyckoffMaster 因子层（v3.1）。

v3.1 变更（相对 v3）：
  · M4 仍用于**本轮历史 IS/Val**：OI 变化自身 90 日分位排序（不变）。
  · M5/M6 **不用于本轮历史选型**，改为**前向验证**：
      M5 = 全网账户比自身 **30 日**分位 > 90% ⇒ 不开多（过滤）。
      M6 = 大户持仓比自身 **30 日** Z-score **降序排序**（平手保持原排序）。
    每币须从**真实前向采集**开始累计 ≥30 自然日**且** ≥90 个唯一、有效、有来源时间的历史
    快照后才激活；未达门槛 M5 不跳过、M6 不改序。
    **已有历史 metrics 不是新前向采集满 30 天的证据。**

纪律：
  · 滚动分位/Z 严格取**自身此前**窗口（不含当前点），窗口按时间（毫秒）。
  · 分位并列处理：pct = (#prior < x + 0.5×#prior == x) / #prior（中位秩）。
  · Z 标准差为 0：Z 记 0.0（中性）并披露。
  · 因子不可用（样本不足/缺事件）⇒ None，不当中性正常值。
  · 全网账户比**不重命名**为散户专属；仍不是巨鲸成本。

本模块纯函数；真实因子序列由「策略数据窗口」交付，本窗不下载数据。
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

from .params import FORWARD_MIN_NATURAL_DAYS, FORWARD_MIN_UNIQUE_SNAPSHOTS, FORWARD_WINDOW_DAYS

DAY_MS = 86_400_000
WINDOW_90D_MS = 90 * DAY_MS
WINDOW_30D_MS = 30 * DAY_MS


def asof_index(times_ms, t) -> int:
    """最后一个 event_time <= t 的下标；无则 -1（严格 asof，不用未来）。"""
    return bisect_right(times_ms, t) - 1


def oi_change_20(oi_times, oi_values, bar_close_times, n_bars: int = 20):
    """逐信号 bar 计算 ΔOI_20 = (OI_now - OI_prev)/OI_prev（M4，跨度 20 根信号 bar）。"""
    out = [None] * len(bar_close_times)
    for i, t in enumerate(bar_close_times):
        if i < n_bars:
            continue
        jn = asof_index(oi_times, t)
        jp = asof_index(oi_times, bar_close_times[i - n_bars])
        if jn < 0 or jp < 0:
            continue
        now, prev = oi_values[jn], oi_values[jp]
        if prev is None or now is None or prev <= 0:
            continue
        out[i] = (now - prev) / prev
    return out


def _prior_window(values, times_ms, i, window_ms):
    t = times_ms[i]
    lo = t - window_ms
    prior = []
    for k in range(i):
        tk = times_ms[k]
        if tk >= t:
            break
        if tk >= lo and values[k] is not None and math.isfinite(values[k]):
            prior.append(values[k])
    return prior


def rolling_pct_rank(values, times_ms, i, window_ms=WINDOW_30D_MS, min_samples: int = 30):
    """i 处值的「自身此前 window_ms」分位（0–1）。并列用中位秩。样本不足/值缺失 ⇒ None。"""
    x = values[i]
    if x is None or not math.isfinite(x):
        return None
    prior = sorted(_prior_window(values, times_ms, i, window_ms))
    if len(prior) < min_samples:
        return None
    lt = bisect_left(prior, x)
    eq = bisect_right(prior, x) - lt
    return (lt + 0.5 * eq) / len(prior)


def rolling_zscore(values, times_ms, i, window_ms=WINDOW_30D_MS, min_samples: int = 30):
    """i 处值的「自身此前 window_ms」Z = (x-mean)/std。std=0 ⇒ 0.0（中性，披露）。"""
    x = values[i]
    if x is None or not math.isfinite(x):
        return None
    prior = _prior_window(values, times_ms, i, window_ms)
    if len(prior) < min_samples:
        return None
    n = len(prior)
    mean = sum(prior) / n
    var = sum((v - mean) ** 2 for v in prior) / n
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0
    return (x - mean) / std


# ── M4（历史）──
def oi_score(oi_pct_rank):
    return 0.0 if oi_pct_rank is None else float(oi_pct_rank)


def m4_rank_key(oi_pct_rank, quote_vol_rank_30d, exchange, symbol):
    """M4 同刻排序键（越小越优先）：(-OI自身90日分位, -30日成交额rank, exchange, symbol)。"""
    return (-oi_score(oi_pct_rank), -float(quote_vol_rank_30d), str(exchange), str(symbol))


# ── 前向激活门槛（M5/M6 共用）──
def forward_gate(natural_days, unique_valid_snapshots) -> dict:
    """每币前向激活门槛：≥30 自然日 **且** ≥90 唯一有效有来源时间快照。"""
    ok = (natural_days is not None and natural_days >= FORWARD_MIN_NATURAL_DAYS
          and unique_valid_snapshots is not None
          and unique_valid_snapshots >= FORWARD_MIN_UNIQUE_SNAPSHOTS)
    if natural_days is None or unique_valid_snapshots is None:
        reason = "gate_inputs_missing"
    elif not ok:
        reason = (f"gate_not_met(days={natural_days}/{FORWARD_MIN_NATURAL_DAYS},"
                  f"snapshots={unique_valid_snapshots}/{FORWARD_MIN_UNIQUE_SNAPSHOTS})")
    else:
        reason = "ok"
    return {"activated": bool(ok), "reason": reason,
            "min_natural_days": FORWARD_MIN_NATURAL_DAYS,
            "min_unique_snapshots": FORWARD_MIN_UNIQUE_SNAPSHOTS,
            "window_days": FORWARD_WINDOW_DAYS}


# ── M5（前向过滤）──
def m5_allows(global_account_ratio_pct_30d, gate_activated: bool):
    """M5：全网账户比自身 **30 日**分位 > 0.90 ⇒ 不开多。

    未达前向门槛 ⇒ **不跳过**（filter 不生效，返回 allow）。
    """
    if not gate_activated:
        return True, "m5_gate_not_met_filter_inactive"
    if global_account_ratio_pct_30d is None:
        return False, "m5_factor_unavailable"
    if global_account_ratio_pct_30d > 0.90:
        return False, "m5_global_account_ratio_30d_pct_gt_0.90"
    return True, "ok"


# ── M6（前向排序）──
def m6_rank_key(top_position_ratio_z_30d, gate_activated: bool):
    """M6：大户持仓比自身 **30 日** Z-score **降序**排序键（越小越优先）。

    未达前向门槛 ⇒ **不改序**（返回 0.0，全部并列 ⇒ 调用方稳定排序保持原序）。
    平手（Z 相同）也返回相同键 ⇒ 稳定排序保持原排序。
    """
    if not gate_activated or top_position_ratio_z_30d is None:
        return 0.0
    return -float(top_position_ratio_z_30d)


def m6_order(items, key_of, gate_activated: bool):
    """稳定排序：按 M6 键降序（Z 大者优先），平手保持传入原序。"""
    if not gate_activated:
        return list(items)
    return sorted(items, key=lambda it: m6_rank_key(key_of(it), True))
