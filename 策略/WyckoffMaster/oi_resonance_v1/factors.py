# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 · 因子层（新候选，独立于旧 M4/M5/M6）。

修复要点（相对附件稿）
──────────────────────
1. **时间单位必须显式**：附件用 `max(timestamps)` / `median(diff)` 推断秒/毫秒/微秒，
   于是「同一序列的前缀」与「全量」可能被判成不同单位（前缀不变性被破坏）。
   本模块改为：单位由调用方**显式声明**，逐值做区间校验；未声明直接拒绝；
   多币单位不一致**拒绝**（不静默归一）。
2. **缺 OI / 预热不足不得伪造 0.5**：附件在 `open_interest` 缺失时直接写
   `oi_resonance_score = 0.5`（把「不知道」伪装成「中性正常值」）。本模块改为
   `score = NaN` + `available = False` + `quality = <原因>`。
3. **保留原始分项 / 权重 / available / quality**：三个分项各自留原始值与分位值，
   权重以 `config.OI_WEIGHTS` 显式给出，**不**冒充旧 M4/M5/M6。
4. **分项按附件定义**（2026-10-16 纠正）：
   · 0.50 分项 = **原始 `open_interest`** 的 `rolling(window, min_periods).rank(pct=True)`，
     命名 `f_oi_level_rank`；**不是**「OI 变化率的分位」，**也不整体 `shift(1)`**
     （当根收盘后该值已可得，不是未来函数）。
   · 0.25 分项 `f_oi_price_div_rank` / `f_oi_mom_flux_rank` 同样取**各自当期** rolling 分位。
   · `oi_chg_20` 保留为**独立观察列**，不参与共振分数。
   · NaN 一律保留，不用 0 / 0.5 顶替。

纯函数：不读库、不联网、不下单。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .config import OI_WEIGHTS, EPOCH_SEC_MAX, EPOCH_SEC_MIN, TIME_UNITS, UNIT_SCALE

# 列名（**新名**，刻意不叫 m4/m5/m6）
C_OI_CHG_20 = "oi_chg_20"            # **独立观察列**（不参与共振分数）
C_RAW_DIV = "f_oi_price_div"
C_RAW_FLUX = "f_oi_mom_flux"
C_RANK_LEVEL = "f_oi_level_rank"     # 0.50 分项 = 原始 OI 当期 rolling 分位（附件第 72 行）
C_RANK_DIV = "f_oi_price_div_rank"   # 0.25 分项
C_RANK_FLUX = "f_oi_mom_flux_rank"   # 0.25 分项
C_SCORE = "oi_resonance_score"
C_AVAILABLE = "oi_resonance_available"
C_QUALITY = "oi_resonance_quality"

OBSERVATION_COMPONENTS = (C_OI_CHG_20,)
RAW_COMPONENTS = (C_RAW_DIV, C_RAW_FLUX)
RANK_COMPONENTS = (C_RANK_LEVEL, C_RANK_DIV, C_RANK_FLUX)

QUALITY_OK = "ok"
QUALITY_MISSING_OI = "missing_open_interest"
QUALITY_INSUFFICIENT = "insufficient_history"
QUALITY_PARTIAL = "partial_insufficient_history"


class TimeUnitError(ValueError):
    """时间单位缺失 / 非法 / 与数据不符 / 多币不一致。"""


# ══════════════════════════ 时间单位（显式）══════════════════════════

def resolve_time_unit(time_unit) -> str:
    """校验显式声明的单位。未声明 / 非法 ⇒ 拒绝（**不推断**）。"""
    if time_unit is None:
        raise TimeUnitError("time_unit_undeclared")
    u = str(time_unit).strip().lower()
    if u not in TIME_UNITS:
        raise TimeUnitError(f"illegal_time_unit:{time_unit}")
    return u


def to_epoch_seconds(timestamps, time_unit) -> np.ndarray:
    """把声明单位的时间戳转成 epoch 秒（int64）。

    **前缀不变性**：`to_epoch_seconds(ts, u)[:k] == to_epoch_seconds(ts[:k], u)`
    对任意 k 成立（单位是显式常量，不依赖 max/median）。逐值区间校验可捕捉
    「声明错单位」（例如毫秒数据被声明为秒 ⇒ 落到公元 5 万年）。
    """
    u = resolve_time_unit(time_unit)
    arr = np.asarray(timestamps)
    if arr.size == 0:
        return np.asarray([], dtype=np.int64)
    if arr.dtype.kind not in ("i", "u", "f"):
        raise TimeUnitError("non_numeric_timestamp")
    a = arr.astype(np.float64)
    if not np.all(np.isfinite(a)):
        raise TimeUnitError("non_finite_timestamp")
    if np.any(a != np.floor(a)):
        raise TimeUnitError("non_integer_timestamp")
    sec = (a / UNIT_SCALE[u]).astype(np.int64)
    if np.any(sec < EPOCH_SEC_MIN) or np.any(sec > EPOCH_SEC_MAX):
        raise TimeUnitError(
            f"timestamp_out_of_range_for_unit:{u}"
            f"(min_sec={int(sec.min())},max_sec={int(sec.max())})")
    return sec


def assert_single_time_unit(units_by_symbol: Dict[str, Any]) -> str:
    """多币单位一致性：全部相同才放行；否则拒绝（**不静默归一**）。"""
    declared = {str(sym): resolve_time_unit(u) for sym, u in units_by_symbol.items()}
    if not declared:
        raise TimeUnitError("no_symbol_time_unit_declared")
    uniq = set(declared.values())
    if len(uniq) > 1:
        bad = {s: u for s, u in declared.items()}
        raise TimeUnitError(f"mixed_time_units_across_symbols:{bad}")
    return next(iter(uniq))


# ══════════════════════════ 同币去重 ══════════════════════════

def dedup_bars(df: pd.DataFrame, *, time_col: str = "timestamp") -> tuple[pd.DataFrame, dict]:
    """同一 symbol 内按时间戳去重（保留首现）。

    返回 `(deduped, report)`；report 含 `duplicates_dropped` 与
    `conflicting_duplicates`（同一时间戳但 OHLCV 不一致的组数）。**不**用后值覆盖前值。
    """
    if time_col not in df.columns or len(df) == 0:
        return df, {"duplicates_dropped": 0, "conflicting_duplicates": 0}
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    dup_mask = df.duplicated(subset=[time_col], keep=False)
    n_dup_rows = int(dup_mask.sum())
    conflicting = 0
    if n_dup_rows and cols:
        grp = df.loc[dup_mask].groupby(time_col, sort=False)[cols].nunique()
        conflicting = int((grp > 1).any(axis=1).sum())
    out = df.drop_duplicates(subset=[time_col], keep="first").reset_index(drop=True)
    return out, {"duplicates_dropped": int(len(df) - len(out)),
                 "conflicting_duplicates": conflicting}


# ══════════════════════════ OI 因子 ══════════════════════════

def _rank_window(params) -> tuple[int, int]:
    w = max(int(params.oi_chg_window) + 1, int(params.oi_rank_days) * int(params.bars_per_day))
    mp = min(w, max(20, int(params.bars_per_day) * int(params.oi_rank_min_days)))
    return int(w), int(mp)


def _current_pct_rank(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    """**当期**窗口分位（附件定义：`rolling(window, min_periods).rank(pct=True)`）。

    **不 shift**：当根 bar 收盘后该值即已可得（不是未来函数）。此前的实现把它整体
    `shift(1)` 并改算「OI 变化率」的分位，属于对附件的偏离，已按附件定义纠正。
    """
    return s.rolling(window=window, min_periods=min_periods).rank(pct=True)


def compute_oi_factors(df: pd.DataFrame, params) -> pd.DataFrame:
    """计算 OI 共振因子（**严格按附件定义**）。缺 OI / 预热不足 ⇒ NaN + available=False。"""
    out = df.copy()
    n = len(out)
    for c in (C_OI_CHG_20, *RAW_COMPONENTS, *RANK_COMPONENTS, C_SCORE):
        out[c] = np.nan
    out[C_AVAILABLE] = False
    out[C_QUALITY] = QUALITY_MISSING_OI

    if n == 0:
        out[C_QUALITY] = QUALITY_INSUFFICIENT
        return out

    if "open_interest" not in out.columns:
        return out                                   # 全列 NaN + available=False

    oi = pd.to_numeric(out["open_interest"], errors="coerce").astype(np.float64)
    close = pd.to_numeric(out["close"], errors="coerce").astype(np.float64)
    vol = pd.to_numeric(out["volume"], errors="coerce").astype(np.float64)
    oi = oi.where(np.isfinite(oi))
    close = close.where(np.isfinite(close))
    vol = vol.where(np.isfinite(vol))

    window, min_periods = _rank_window(params)
    k = int(params.oi_chg_window)

    # ① 独立观察列 oi_chg_20（**不参与共振分数**，仅如实记录）
    oi_lag = oi.shift(k)
    out[C_OI_CHG_20] = pd.Series(
        np.where((oi_lag > 0) & oi.notna() & oi_lag.notna(),
                 (oi - oi_lag) / oi_lag, np.nan), index=out.index)

    # ② 价量背离：sign(价格收益) × OI 收益（`fill_method=None`：缺值不 pad，如实留 NaN）
    m5 = int(params.oi_price_div_window)
    ret_c = close.pct_change(m5, fill_method=None)
    ret_oi = oi.pct_change(m5, fill_method=None)
    div = pd.Series(np.where(np.isfinite(ret_c) & np.isfinite(ret_oi),
                             np.sign(ret_c) * ret_oi, np.nan), index=out.index)

    # ③ 动能通量：OI 收益 × clip(量比, 0, clip)
    m6 = int(params.oi_mom_flux_window)
    vw = int(params.oi_vol_ma_window)
    vol_ma = vol.rolling(window=vw, min_periods=max(2, vw // 4)).mean()
    ratio = pd.Series(np.where((vol_ma > 0) & vol.notna(),
                               vol / vol_ma, np.nan), index=out.index)
    ratio = ratio.clip(lower=0.0, upper=float(params.oi_vol_ratio_clip))
    ret_oi6 = oi.pct_change(m6, fill_method=None)
    flux = pd.Series(np.where(np.isfinite(ret_oi6) & np.isfinite(ratio),
                              ret_oi6 * ratio, np.nan), index=out.index)

    out[C_RAW_DIV] = div
    out[C_RAW_FLUX] = flux

    # ④ 三个分项**各自**的当期 rolling 分位（0.50 分项直接对**原始 OI** 取分位）
    out[C_RANK_LEVEL] = _current_pct_rank(oi, window, min_periods)
    out[C_RANK_DIV] = _current_pct_rank(div, window, min_periods)
    out[C_RANK_FLUX] = _current_pct_rank(flux, window, min_periods)

    r_level = out[C_RANK_LEVEL]
    r_div = out[C_RANK_DIV]
    r_flux = out[C_RANK_FLUX]
    score = (OI_WEIGHTS[C_RANK_LEVEL] * r_level
             + OI_WEIGHTS[C_RANK_DIV] * r_div
             + OI_WEIGHTS[C_RANK_FLUX] * r_flux)
    out[C_SCORE] = score
    avail = score.notna() & np.isfinite(score.astype(np.float64))
    out[C_AVAILABLE] = avail

    all_nan = (~avail).all()
    any_nan = (~avail).any()
    if all_nan:
        out[C_QUALITY] = QUALITY_INSUFFICIENT
    elif any_nan:
        out[C_QUALITY] = QUALITY_PARTIAL
    else:
        out[C_QUALITY] = QUALITY_OK
    return out


def oi_factor_meta(df: pd.DataFrame) -> dict:
    """因子可用性/质量披露（供回执与注册表描述引用）。"""
    if C_AVAILABLE not in df.columns:
        return {"weights": dict(OI_WEIGHTS), "available": 0, "total": int(len(df)),
                "quality": {QUALITY_MISSING_OI: int(len(df))}}
    q = df[C_QUALITY].value_counts().to_dict() if len(df) else {}
    return {
        "weights": dict(OI_WEIGHTS),
        "observation_components": list(OBSERVATION_COMPONENTS),
        "components_raw": list(RAW_COMPONENTS),
        "components_rank": list(RANK_COMPONENTS),
        "available": int(df[C_AVAILABLE].sum()),
        "total": int(len(df)),
        "quality": {str(k): int(v) for k, v in q.items()},
        "note": ("缺 OI / 预热不足 ⇒ score=NaN 且 available=False（不写 0.5）；"
                 "分项按附件定义取**当期** rolling 分位（不 shift1）；"
                 "0.50 分项对**原始 OI** 取分位（f_oi_level_rank）；"
                 "oi_chg_20 仅为独立观察列；权重仅 0.50/0.25/0.25 一项来源"),
    }


__all__ = [
    "TimeUnitError", "resolve_time_unit", "to_epoch_seconds", "assert_single_time_unit",
    "dedup_bars", "compute_oi_factors", "oi_factor_meta",
    "C_OI_CHG_20", "C_RAW_DIV", "C_RAW_FLUX",
    "C_RANK_LEVEL", "C_RANK_DIV", "C_RANK_FLUX",
    "C_SCORE", "C_AVAILABLE", "C_QUALITY",
    "OBSERVATION_COMPONENTS", "RAW_COMPONENTS", "RANK_COMPONENTS",
    "QUALITY_OK", "QUALITY_MISSING_OI", "QUALITY_INSUFFICIENT", "QUALITY_PARTIAL",
]
