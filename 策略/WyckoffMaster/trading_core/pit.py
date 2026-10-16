# -*- coding: utf-8 -*-
"""Shared Trading Core · Point-in-Time（PIT）可得性门禁（施工单 W2-5）。

要解决的问题
────────────
当前 OI 对齐规则是「bar 收盘时刻之前最近一条 OI」（`ts <= bar_close`）。
`ts` 是**观测时刻**，但观测值真正**落库/可被读到**的时刻是 `ts + 发布延迟`。
只比 `ts <= close` 会把「收盘前几毫秒才产生、收盘后才可见」的观测当作已知 ⇒ 乐观读取。

本模块的立场（施工单原文）
──────────────────────────
> 必须明确：数据实际可见时间，而不是只看 event timestamp。
> 如果无法证明 PIT：宁可将该 OI 数据标记为不可用，也不能乐观使用。

因此：

  · `oi_publication_lag_ms` **未声明** ⇒ `oi_pit_proven=False`、
    `oi_decision_usable=False`、`oi_available_at=NaN`（**不猜、不乐观**）；
  · 声明为非负毫秒数 `L` ⇒ 仅当 `ts + L <= bar_close` 时 `oi_pit_proven=True`，
    `oi_available_at = ts + L`。

注意语义分层（不互相冒充）：
  `oi_resonance_available` = **数值是否算得出来**（factors 层，保持原样）
  `oi_pit_proven`          = **该数值在决策时刻是否真的可见**（本模块）
  `oi_decision_usable`     = 两者同时为真 ⇒ 才可用于**决策**

纯函数：不读库、不联网、不下单。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

PIT_CONTRACT_VERSION = "pit_contract_v1.0.0"

C_PIT_PROVEN = "oi_pit_proven"
C_DECISION_USABLE = "oi_decision_usable"
C_AVAILABLE_AT = "oi_available_at"
C_OBSERVED_AT = "oi_observed_at"

# 未声明发布延迟时的原因码
REASON_UNDECLARED = "oi_publication_lag_undeclared"
REASON_NO_OBSERVATION_TIME = "oi_observation_time_absent"
REASON_LATE_OBSERVATION = "oi_observed_after_bar_close"


@dataclass(frozen=True)
class PitPolicy:
    """PIT 策略。`oi_publication_lag_ms=None` ⇒ 未声明 ⇒ 一律不可用。"""
    oi_publication_lag_ms: Optional[int] = None
    bar_seconds: int = 3600

    @property
    def declared(self) -> bool:
        return self.oi_publication_lag_ms is not None

    def identity(self) -> dict:
        return {"pit_contract": PIT_CONTRACT_VERSION,
                "oi_publication_lag_ms": self.oi_publication_lag_ms,
                "declared": self.declared,
                "bar_seconds": self.bar_seconds}


def annotate_oi_pit(df: pd.DataFrame, params=None,
                    policy: Optional[PitPolicy] = None) -> pd.DataFrame:
    """在因子帧上追加 `oi_pit_proven` / `oi_decision_usable` / `oi_available_at`。

    `params`：候选包 `SignalParams`（取 `oi_publication_lag_ms`）。
    `policy`：显式 PIT 策略（优先于 params）。

    观测时刻优先取 `oi_observed_at` 列；缺列时退回 `timestamp + bar_seconds`
    （= bar 收盘，即 as-of 对齐所用的时刻，属**最乐观**假设，因此仍需声明延迟才放行）。
    """
    out = df
    n = len(out)
    if policy is None:
        lag = getattr(params, "oi_publication_lag_ms", None)
        policy = PitPolicy(oi_publication_lag_ms=lag,
                           bar_seconds=int(getattr(params, "signal_bar_seconds", 3600)))

    proven = np.zeros(n, dtype=bool)
    usable = np.zeros(n, dtype=bool)
    avail_at = np.full(n, np.nan, dtype=np.float64)
    reasons = np.array([""] * n, dtype=object)

    if n == 0:
        out[C_PIT_PROVEN] = proven
        out[C_DECISION_USABLE] = usable
        out[C_AVAILABLE_AT] = avail_at
        out["oi_pit_reason"] = reasons
        return out

    ts = pd.to_numeric(out["timestamp"], errors="coerce").to_numpy(np.float64) \
        if "timestamp" in out.columns else np.full(n, np.nan, np.float64)
    close_ms = ts + float(policy.bar_seconds) * 1000.0

    if not policy.declared:
        reasons[:] = REASON_UNDECLARED
        out[C_PIT_PROVEN] = proven
        out[C_DECISION_USABLE] = usable
        out[C_AVAILABLE_AT] = avail_at
        out["oi_pit_reason"] = reasons
        return out

    lag = float(policy.oi_publication_lag_ms)
    if C_OBSERVED_AT in out.columns:
        obs = pd.to_numeric(out[C_OBSERVED_AT], errors="coerce").to_numpy(np.float64)
        have_obs = np.isfinite(obs)
    else:
        obs = close_ms.copy()
        have_obs = np.ones(n, dtype=bool)

    ready = obs + lag
    ok = have_obs & np.isfinite(close_ms) & (ready <= close_ms)
    proven[ok] = True
    avail_at[ok] = ready[ok]
    reasons[~have_obs] = REASON_NO_OBSERVATION_TIME
    reasons[have_obs & ~ok] = REASON_LATE_OBSERVATION

    if "oi_resonance_available" in out.columns:
        raw = out["oi_resonance_available"].to_numpy(bool)
    else:
        raw = np.ones(n, dtype=bool)
    usable = proven & raw

    out[C_PIT_PROVEN] = proven
    out[C_DECISION_USABLE] = usable
    out[C_AVAILABLE_AT] = avail_at
    out["oi_pit_reason"] = reasons
    return out


def pit_summary(df: pd.DataFrame, policy: Optional[PitPolicy] = None) -> dict:
    n = int(len(df))
    if C_PIT_PROVEN not in getattr(df, "columns", []):
        return {"policy": (policy.identity() if policy else None),
                "bars": n, "pit_proven": 0, "decision_usable": 0,
                "note": "未做 PIT 标注"}
    reason_counts: dict = {}
    if "oi_pit_reason" in df.columns:
        reason_counts = {str(k): int(v) for k, v in
                         df["oi_pit_reason"].value_counts().to_dict().items() if str(k)}
    return {
        "policy": (policy.identity() if policy else None),
        "bars": n,
        "pit_proven": int(df[C_PIT_PROVEN].sum()),
        "decision_usable": int(df[C_DECISION_USABLE].sum()),
        "reason_counts": reason_counts,
        "note": ("`oi_resonance_available`=数值算得出；`oi_pit_proven`=决策时刻真的可见；"
                 "`oi_decision_usable`=两者同时成立才可用于决策。未声明发布延迟 ⇒ "
                 "一律 pit_proven=False（宁可标记不可用，不乐观使用）"),
    }


__all__ = ["PIT_CONTRACT_VERSION", "C_PIT_PROVEN", "C_DECISION_USABLE",
           "C_AVAILABLE_AT", "C_OBSERVED_AT", "REASON_UNDECLARED",
           "REASON_NO_OBSERVATION_TIME", "REASON_LATE_OBSERVATION",
           "PitPolicy", "annotate_oi_pit", "pit_summary"]
