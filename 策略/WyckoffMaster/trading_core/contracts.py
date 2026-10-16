# -*- coding: utf-8 -*-
"""Shared Trading Core · 契约常量层。

定位
────
本模块只放**跨模块共享的常量、枚举与确定性哈希**，不放任何业务计算。

复用声明（施工单「严格禁止重复造轮子」）
────────────────────────────────────────
· Order 状态枚举：全工程此前**没有**正式定义（实测 `paper_engine` 只有
  `order_status_histogram` 直方图字符串、`backtest_store` 只有 ID 生成器）⇒
  本模块是**唯一第一份**，不是第二套。后续所有端一律 import 这里，不得另建。
· 确定性哈希：与 `oi_resonance_v1/registry_entry._event_uid` 同算法（sha256[:32]），
  此处提升为共享实现；`registry_entry` 改为 import 本函数（同一实现，不并存两份）。

纯常量与纯函数：不读库、不联网、不下单。
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Iterable

CORE_CONTRACT_VERSION = "trading_core_v1.0.0"


# ══════════════════════════ 枚举（唯一一份）══════════════════════════

class OrderStatus(str, Enum):
    """订单状态机。跳转方向由 `order.py` 的 `_ALLOWED_TRANSITIONS` 唯一约束。"""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(str, Enum):
    MARKET = "MARKET"          # 以对手价立即成交（回测按 bar 级假设撮合）
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"   # 保护止损触发后转市价


class ExecPhase(str, Enum):
    """执行相位（顺序即因果顺序，见 engine 主循环）。"""
    A_OPEN = "A_open"          # 开盘：锁利生效 → 资金费结算
    B_GAP = "B_gap"            # 开盘：跳空退出（只用 open）
    C_ENTRY = "C_entry"        # 开盘：新开仓
    D_INTRABAR = "D_intrabar"  # bar 内：止损（含当根新开仓）
    E_CLOSE = "E_close"        # 收盘：盯市与权益
    EOD = "EOD"                # 期末处理


class Liquidity(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


# ══════════════════════════ 错误类型（唯一一份）══════════════════════════

class TradingCoreError(RuntimeError):
    """Shared Trading Core 通用错误。"""


class LiveExecutionForbidden(TradingCoreError):
    """真实下单/真实成交路径未实现或未授权 —— 调用即抛（硬闸）。"""


class ExecutionInputError(ValueError):
    """执行层输入不合法（缺 bar / 缺时间单位 / 单位不一致）。"""


# ══════════════════════════ 确定性哈希 ══════════════════════════

def stable_hash(*parts: Any, length: int = 32) -> str:
    """确定性哈希：`sha256("|".join(str(p)))` 取前 `length` 位十六进制。

    **禁止**用 `uuid4()` / `random()` / 当前时间做幂等键（施工单 W2-8）。
    同一输入重复调用 ⇒ 同一输出；跨进程、跨机器一致。
    """
    blob = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:length]


def canonical_json(obj: Any) -> str:
    """规范 JSON（键排序 + 紧凑分隔符 + NaN/Inf 显式编码）⇒ 可稳定哈希。"""
    return json.dumps(_canon(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _canon(o: Any) -> Any:
    if isinstance(o, float):
        if o != o:
            return "__nan__"
        if o == float("inf"):
            return "__inf__"
        if o == float("-inf"):
            return "__ninf__"
        return o
    if isinstance(o, dict):
        return {str(k): _canon(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_canon(v) for v in o]
    if isinstance(o, (set, frozenset)):
        return sorted(_canon(v) for v in o)
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "identity") and callable(getattr(o, "identity")):
        return _canon(o.identity())
    if isinstance(o, bool):
        return o
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return _canon(float(o))
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return _canon(o.tolist())
    except Exception:                                   # noqa: BLE001
        pass
    return o


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def ordered_unique(seq: Iterable) -> list:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


__all__ = [
    "CORE_CONTRACT_VERSION",
    "OrderStatus", "Side", "PositionSide", "OrderType", "ExecPhase", "Liquidity",
    "TradingCoreError", "LiveExecutionForbidden", "ExecutionInputError",
    "stable_hash", "canonical_json", "payload_hash", "ordered_unique",
]
