# -*- coding: utf-8 -*-
"""Shared Trading Core · 资金费结算（整体搬移，无第二实现）。

模型
────
`settlement_events`（默认，fail-closed）：消费真实结算事件 `(time_ms, rate, mark_price)`，
判据 `last_funding_ms < ev_ms <= t_ms`（**毫秒**，不截秒）；缺 rate/mark ⇒ 记 `funding_gap`，
**不补 0**。
`assumption_fixed_8h`：**显式假设模型**（结果恒带 `funding_is_assumption=True`）。

搬移自 `oi_resonance_v1/execution.py`，算术逐字保留。
"""
from __future__ import annotations

import math
from typing import List, Optional

from .position import Position

FUNDING_CONTRACT_VERSION = "funding_contract_v1.0.0"

FUNDING_MODEL_SETTLEMENT = "settlement_events"
FUNDING_MODEL_ASSUMPTION = "assumption_fixed_8h"


def funding_events_ms(events, default_unit: Optional[str] = None) -> List[dict]:
    """规范化资金费事件 → **毫秒**（保留原始精度，不截秒）。

    接受（按优先级）：
      · `time_ms` / `funding_time_ms`：毫秒，原样保留；
      · `time_sec`：秒 ⇒ ×1000；
      · `funding_time` / `time`：币安口径**毫秒**；仅当事件自带 `time_unit="s"`
        或调用方 `default_unit="s"` 时按秒 ×1000。
    未识别 / 缺时间字段 ⇒ 跳过（**不猜单位、不补 0**）。返回按 `time_ms` 升序。
    """
    out: List[dict] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        unit = ev.get("time_unit") or default_unit
        t_ms: Optional[int] = None
        if ev.get("time_ms") is not None:
            t_ms = int(round(float(ev["time_ms"])))
        elif ev.get("funding_time_ms") is not None:
            t_ms = int(round(float(ev["funding_time_ms"])))
        elif ev.get("time_sec") is not None:
            t_ms = int(round(float(ev["time_sec"]) * 1000.0))
        else:
            for key in ("funding_time", "time"):
                if ev.get(key) is not None:
                    v = float(ev[key])
                    t_ms = int(round(v * 1000.0)) if unit == "s" else int(round(v))
                    break
        if t_ms is None:
            continue
        out.append({"time_ms": t_ms, "time_sec": t_ms // 1000,
                    "rate": ev.get("rate"), "mark_price": ev.get("mark_price")})
    out.sort(key=lambda x: x["time_ms"])
    return out


# 兼容旧名（旧内部调用点已全部更新；保留别名避免既有引用断裂）
_funding_events_ms = funding_events_ms
_funding_events_by_sec = funding_events_ms


def settle_funding(pos: Position, cash: float, *, model: str, t_ms: int,
                   open_px: float, rate, events: List[dict],
                   funding_is_assumption: bool) -> float:
    """相位 A②：资金费结算。缺 rate/mark ⇒ 记 `funding_gap`，**不补 0**。"""
    if model == FUNDING_MODEL_ASSUMPTION:
        t_sec = int(t_ms // 1000)
        if t_sec % 28800 != 0 or (t_ms - pos.last_funding_ms) < 28800_000:
            return cash
        if rate is None or not math.isfinite(float(rate)):
            pos.funding_gaps.append({"time_ms": int(t_ms), "time_sec": t_sec,
                                     "reason": "funding_rate_missing_assumption_model"})
            pos.last_funding_ms = int(t_ms)
            return cash
        cost = (open_px * pos.qty) * float(rate) * pos.direction
        pos.cum_funding_fee += cost
        pos.last_funding_ms = int(t_ms)
        return cash - cost

    # settlement_events：消费真实结算事件（毫秒）
    for ev in events or []:
        ev_ms = int(ev.get("time_ms"))
        if not (pos.last_funding_ms < ev_ms <= int(t_ms)):
            continue
        r, m = ev.get("rate"), ev.get("mark_price")
        ok_r = r is not None and isinstance(r, (int, float)) and not isinstance(r, bool) \
            and math.isfinite(float(r))
        ok_m = m is not None and isinstance(m, (int, float)) and not isinstance(m, bool) \
            and math.isfinite(float(m)) and float(m) > 0
        if not (ok_r and ok_m):
            pos.funding_gaps.append({
                "time_ms": ev_ms, "time_sec": ev_ms // 1000,
                "reason": ("funding_event_missing_fields:"
                           + ("rate" if not ok_r else "") + ("mark" if not ok_m else "")),
                "rate": r, "mark_price": m})
            pos.last_funding_ms = ev_ms
            continue
        cost = (float(m) * pos.qty) * float(r) * pos.direction
        pos.cum_funding_fee += cost
        pos.last_funding_ms = ev_ms
        cash -= cost
    return cash


# 兼容旧私有名（既有回归测试按 `X._settle_funding` 调用）
_settle_funding = settle_funding

__all__ = ["FUNDING_CONTRACT_VERSION", "FUNDING_MODEL_SETTLEMENT",
           "FUNDING_MODEL_ASSUMPTION", "funding_events_ms", "settle_funding",
           "_funding_events_ms", "_funding_events_by_sec", "_settle_funding"]
