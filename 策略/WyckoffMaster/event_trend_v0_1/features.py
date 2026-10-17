# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 真实特征计算库。

包含：
1. 4h Wilder ATR(14) 严格递推实现；
2. 42 根 4h 收盘价的方向效率比 ER；
3. 突破前 20 根 1h 成交量中位数 V20；
4. 真实 6 小时持仓数量增长率提取（严禁任何 mock / 假指标）。
"""
from __future__ import annotations

import math
from typing import Optional, Tuple, Union
import numpy as np
import pandas as pd


def compute_wilder_atr_series(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """计算 Wilder ATR(14) 序列。
    
    定义：
    - 首根 TR = high - low
    - 之后 TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    - 前 14 个 TR 的算术平均作为首个 ATR(14) 值 (index = 13)
    - 之后递推: ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period
    """
    n = len(highs)
    if n == 0:
        return np.array([], dtype=np.float64)
    
    tr = np.zeros(n, dtype=np.float64)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
        
    atr = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return atr
        
    # 前 period 个 TR 的算术平均作为初值
    atr[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / float(period)
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / float(period)
        
    return atr


def compute_efficiency_ratio(closes: np.ndarray) -> Optional[float]:
    """计算给定收盘价序列的方向效率比 ER。
    
    公式: abs(last - first) / sum(abs(adjacent difference))
    分母为 0 则返回 None (不建档)。
    """
    if len(closes) < 2:
        return None
    net_change = abs(closes[-1] - closes[0])
    diffs = np.abs(np.diff(closes))
    total_path = np.sum(diffs)
    if total_path <= 1e-12:
        return None
    return float(net_change / total_path)


def compute_v20_median_volume(volumes: np.ndarray, current_idx: int, lookback: int = 20) -> Optional[float]:
    """计算突破小时之前 20 根完整 1h K 线成交量的中位数，不包含突破小时本身。"""
    if current_idx < lookback:
        return None
    window = volumes[current_idx - lookback: current_idx]
    if len(window) < lookback:
        return None
    return float(np.median(window))


def extract_real_oi_growth(
    oi_timestamps: np.ndarray,
    oi_values: np.ndarray,
    target_time_ms: int,
    lookback_ms: int = 6 * 3600 * 1000,
    max_distance_ms: int = 15 * 60 * 1000
) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
    """从真实持仓序列提取 6 小时前与当刻持仓数量及增幅。
    
    规则：
    - OI(T) 是观测时间 <= T 且在 T 已可得的最近值，距 T 不超过 15 分钟；
    - OI(T-6h) 是观测时间 <= T-6h 且已可得的最近值，距 T-6h 不超过 15 分钟；
    - 任一缺失、非正或时点超距，返回 (None, None, None, 'oi_unavailable')；
    - 否则返回 (oi_current, oi_past, growth_ratio, 'ok')。
    """
    if len(oi_timestamps) == 0:
        return None, None, None, "oi_empty"
        
    # 查找 T 时刻最近的可用值
    idx_curr = np.searchsorted(oi_timestamps, target_time_ms, side="right") - 1
    if idx_curr < 0:
        return None, None, None, "oi_curr_missing_before_start"
        
    t_curr = oi_timestamps[idx_curr]
    if abs(target_time_ms - t_curr) > max_distance_ms:
        return None, None, None, f"oi_curr_too_far_{abs(target_time_ms - t_curr)}ms"
        
    val_curr = float(oi_values[idx_curr])
    if val_curr <= 0.0 or math.isnan(val_curr):
        return None, None, None, "oi_curr_non_positive"
        
    # 查找 T - 6h 时刻最近的可用值
    target_past_ms = target_time_ms - lookback_ms
    idx_past = np.searchsorted(oi_timestamps, target_past_ms, side="right") - 1
    if idx_past < 0:
        return None, None, None, "oi_past_missing_before_start"
        
    t_past = oi_timestamps[idx_past]
    if abs(target_past_ms - t_past) > max_distance_ms:
        return None, None, None, f"oi_past_too_far_{abs(target_past_ms - t_past)}ms"
        
    val_past = float(oi_values[idx_past])
    if val_past <= 0.0 or math.isnan(val_past):
        return None, None, None, "oi_past_non_positive"
        
    growth = (val_curr / val_past) - 1.0
    return val_curr, val_past, growth, "ok"
