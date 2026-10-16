#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WORK-02 合成夹具（确定性，不读库、不联网）。

用途
────
为「抽 Shared Trading Core」提供一份**逐位可复现**的行为基线：
同一份输入 ⇒ 同一份输出 ⇒ 同一份哈希。重构前后各跑一次，哈希相同即证明
「架构拆分未改变行为」。

数据构造（全部由 seed 决定，无随机源之外的不确定性）
──────────────────────────────────────────────────
· 先生成 15m 序列（n_sig × 4 根），再由它**聚合**出 1h 序列 ⇒ 两周期天然一致。
· 价格 = 带漂移的随机游走 + 若干人为「强突破段」，保证唐奇安/威科夫分支被触发。
· OI 分三段不同质量：①前段全缺 ②中段部分缺 ③后段完整可用
  ⇒ 用于 W2-0「OI quality 必须逐行对应」的验收。
· 资金费事件按 8h 边界生成（真实结算口径），带 rate + mark_price。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

# 1h 与 15m 网格同时对齐的基准毫秒（1699999200000 / 3600000 与 / 900000 均为整数）
T0_MS = 1_699_999_200_000
HOUR_MS = 3_600_000
M15_MS = 900_000


def build_fixture(seed: int = 20261016, n_sig: int = 900) -> dict:
    """返回 {'signal_bars', 'intrabar_bars', 'funding_events', 'meta'}。"""
    rng = np.random.default_rng(seed)
    n15 = n_sig * 4

    # ── 15m 价格路径：随机游走 + 人为突破段 ──
    drift = np.zeros(n15)
    vol = np.full(n15, 0.0016)
    drift[300:360] = 0.0022          # 上行突破段
    drift[900:940] = -0.0030         # 下行段（制造 spring / sc 条件）
    drift[1500:1560] = 0.0018
    drift[2200:2240] = -0.0028
    drift[2700:2780] = 0.0020
    drift[3200:3250] = -0.0026
    ret = drift + rng.normal(0.0, 1.0, n15) * vol
    close = 30000.0 * np.exp(np.cumsum(ret))

    open_ = np.empty(n15)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    span = np.abs(rng.normal(0.0, 0.0012, n15)) * close
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    volume = np.abs(rng.normal(1200.0, 420.0, n15)) + 50.0
    # 若干放量 bar（供 SC 分支）
    volume[900:906] *= 4.0
    volume[2200:2206] *= 4.5
    volume[3200:3206] *= 4.2

    t15 = T0_MS + np.arange(n15) * M15_MS
    intrabar = pd.DataFrame({
        "timestamp": t15.astype(np.int64),
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    })

    # ── 由 15m 聚合出 1h ──
    g = np.arange(n15) // 4
    agg = pd.DataFrame({
        "g": g, "timestamp": t15, "open": open_, "high": high,
        "low": low, "close": close, "volume": volume,
    }).groupby("g").agg(
        timestamp=("timestamp", "first"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum")).reset_index(drop=True)
    assert len(agg) == n_sig

    # ── OI：三段不同质量 ──
    oi = 120000.0 + np.cumsum(rng.normal(0.0, 900.0, n_sig))
    oi = np.maximum(oi, 1000.0)
    oi_col = oi.astype(float).copy()
    seg_a = int(n_sig * 0.25)          # 前 25%：全缺
    seg_b = int(n_sig * 0.55)          # 25%~55%：部分缺
    oi_col[:seg_a] = np.nan
    mask_partial = rng.random(seg_b - seg_a) < 0.45
    idx_partial = np.arange(seg_a, seg_b)
    oi_col[idx_partial[mask_partial]] = np.nan
    # 后段（55% 之后）完整可用
    agg["open_interest"] = oi_col

    # ── 资金费事件（8h 边界；真实结算口径）──
    events = []
    k = 1
    while T0_MS + k * 8 * HOUR_MS < int(t15[-1]):
        ev_ms = T0_MS + k * 8 * HOUR_MS
        j = int((ev_ms - T0_MS) // M15_MS)
        mark = float(close[min(max(j, 0), n15 - 1)])
        rate = float(rng.normal(0.0001, 0.00008))
        events.append({"time_ms": ev_ms, "rate": rate, "mark_price": mark})
        k += 1

    return {
        "signal_bars": agg,
        "intrabar_bars": intrabar,
        "funding_events": events,
        "meta": {"seed": seed, "n_sig": n_sig, "n15": n15, "t0_ms": T0_MS,
                 "oi_segments": {"missing": [0, seg_a], "partial": [seg_a, seg_b],
                                 "full": [seg_b, n_sig]},
                 "funding_events": len(events)},
    }


def market_data(fx: dict) -> dict:
    """装配成 `run_portfolio_execution` 的 market_data（单标的 BTCUSDT）。"""
    return {"BTCUSDT": {"signal_bars": fx["signal_bars"],
                        "intrabar_bars": fx["intrabar_bars"],
                        "time_unit": "ms",
                        "funding_events": fx["funding_events"],
                        "funding_time_unit": "ms"}}


def stable_json_hash(obj) -> str:
    """稳定哈希：sort_keys + 紧凑分隔符；NaN/Inf 统一编码为字符串。"""

    def _clean(o):
        if isinstance(o, float):
            if o != o:
                return "__nan__"
            if o in (float("inf"), float("-inf")):
                return "__inf__" if o > 0 else "__ninf__"
            return o
        if isinstance(o, dict):
            return {str(k): _clean(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
        if isinstance(o, (list, tuple)):
            return [_clean(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return _clean(float(o))
        if isinstance(o, (np.bool_,)):
            return bool(o)
        return o

    blob = json.dumps(_clean(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    fx = build_fixture()
    print(json.dumps(fx["meta"], ensure_ascii=False, indent=2))
    print("signal_bars", fx["signal_bars"].shape, "intrabar", fx["intrabar_bars"].shape)
    print("fixture_hash", stable_json_hash({
        "signal_bars": fx["signal_bars"].to_dict("list"),
        "intrabar_bars": fx["intrabar_bars"].to_dict("list"),
        "funding_events": fx["funding_events"]}))
