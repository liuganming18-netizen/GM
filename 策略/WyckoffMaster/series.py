# -*- coding: utf-8 -*-
"""纯 numpy 因果序列工具（WyckoffMaster）。

设计纪律（与既有 `wx_donchian_wyckoff.py` 同口径，可独立复核）：
  · 全部为**因果**函数：`out[i]` 只依赖 `arr[0..i]`（严格不含 i 之后）。
  · 前置基线一律取「截至 i-1 的 n 根」= pandas `arr.rolling(n).<stat>().shift(1)` 的等价物。
  · ATR 用**递归** `y_i = (1-alpha)*y_{i-1} + alpha*tr_i`，`y_0 = tr_0`，前 `min_periods-1`
    位返回 NaN（不足不算出，不猜）。对应 pandas `ewm(alpha=1/20, adjust=False,
    min_periods=20).mean()`。**不**使用 EMA span 形式。
  · 零振幅 / 非有限一律 NaN（拒绝制造信号），不填 0。
"""
from __future__ import annotations

import math

import numpy as np


def as_float(a) -> np.ndarray:
    return np.asarray(a, dtype=float)


def prev_window(arr: np.ndarray, n: int) -> np.ndarray:
    """返回形如 (m, n) 的窗口矩阵：第 i 行 = arr[i-n : i]（严格 i 之前 n 根）。

    不足 n 根的行整行 NaN。等价 `arr.rolling(n).apply(...).shift(1)` 的窗口视图。
    """
    arr = as_float(arr)
    m = len(arr)
    out = np.full((m, n), np.nan)
    for i in range(n, m):
        out[i] = arr[i - n:i]
    return out


def rolling_max_prev(arr, n) -> np.ndarray:
    if n <= 0:
        return np.full(len(as_float(arr)), np.nan)
    w = prev_window(arr, n)
    out = np.full(w.shape[0], np.nan)
    valid = ~np.all(np.isnan(w), axis=1)
    if valid.any():
        with np.errstate(all="ignore"):
            out[valid] = np.nanmax(w[valid], axis=1)
    return out


def rolling_min_prev(arr, n) -> np.ndarray:
    if n <= 0:
        return np.full(len(as_float(arr)), np.nan)
    w = prev_window(arr, n)
    out = np.full(w.shape[0], np.nan)
    valid = ~np.all(np.isnan(w), axis=1)
    if valid.any():
        with np.errstate(all="ignore"):
            out[valid] = np.nanmin(w[valid], axis=1)
    return out


def rolling_mean_prev(arr, n) -> np.ndarray:
    if n <= 0:
        return np.full(len(as_float(arr)), np.nan)
    w = prev_window(arr, n)
    out = np.full(w.shape[0], np.nan)
    valid = ~np.all(np.isnan(w), axis=1)
    if valid.any():
        with np.errstate(all="ignore"):
            out[valid] = np.nanmean(w[valid], axis=1)
    return out


def ewm_atr(trs, alpha: float, min_periods: int) -> np.ndarray:
    """Wilder 式递归平滑（adjust=False）的 ATR。

    与既有核心 `ewm_atr` 逐位等价；前 `min_periods-1` 位为 NaN。
    """
    trs = as_float(trs)
    out = np.full(len(trs), np.nan)
    if len(trs) == 0:
        return out
    y = float(trs[0])
    for i, x in enumerate(trs):
        if i > 0:
            y = (1.0 - alpha) * y + alpha * float(x)
        if i + 1 >= min_periods:
            out[i] = y
    return out


def true_ranges(o, h, l, c) -> np.ndarray:
    """TR：TR[0] = h0-l0；TR[i] = max(h-l, |h-c_{i-1}|, |l-c_{i-1}|)。"""
    h, l, c = as_float(h), as_float(l), as_float(c)
    m = len(h)
    tr = np.full(m, np.nan)
    for i in range(m):
        hl = h[i] - l[i]
        if i == 0:
            tr[i] = hl
        else:
            pc = c[i - 1]
            tr[i] = max(hl, abs(h[i] - pc), abs(l[i] - pc))
    return tr


def close_pos(h, l, c) -> np.ndarray:
    """收盘位置 (close-low)/(high-low)。零振幅 / 非有限 ⇒ NaN（拒绝，不猜）。"""
    h, l, c = as_float(h), as_float(l), as_float(c)
    m = len(h)
    out = np.full(m, np.nan)
    for i in range(m):
        rng = h[i] - l[i]
        if not math.isfinite(rng) or rng <= 0:
            continue
        pos = (c[i] - l[i]) / rng
        out[i] = pos if math.isfinite(pos) else np.nan
    return out


def shift(a, n) -> np.ndarray:
    a = as_float(a)
    out = np.full(len(a), np.nan)
    if n == 0:
        return a.copy()
    if n > 0:
        out[n:] = a[:-n]
    else:
        out[:n] = a[-n:]
    return out
