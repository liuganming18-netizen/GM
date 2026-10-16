# -*- coding: utf-8 -*-
"""Shared Trading Core · Order 契约（施工单 W2-7 / W2-8）。

唯一 Order 模型
───────────────
全工程此前**没有**正式 Order 契约：
· `oi_resonance_v1/execution.py` 只有 `decisions` 字典（无 order_id / 无状态机）；
· `backtest_store.new_order_id` 是**落盘层** ID 生成器（输入含 run_id ⇒ 每次不同）；
· `paper_engine` 只有 SQL 表与 `order_status_histogram` 字符串。
⇒ 本模块是**唯一第一份**统一契约，不是第二套。

确定性幂等（W2-8）
──────────────────
**禁止** `uuid4()` / `random()` / `当前时间+random`。本模块一律用
`stable_hash(strategy_key, symbol, signal_id, action, order_sequence)`：

    同一输入重复执行 ⇒ 第一次 → order A；第二次 → **仍识别为 order A**（duplicate=True）
    而不是第一次 → A、第二次 → B。

状态机
──────
`_ALLOWED_TRANSITIONS` 是唯一跳转约束；非法跳转抛 `TradingCoreError`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (Liquidity, OrderStatus, OrderType, PositionSide, Side,
                        TradingCoreError, stable_hash)

ORDER_CONTRACT_VERSION = "order_contract_v1.0.0"

_ALLOWED_TRANSITIONS: Dict[OrderStatus, Tuple[OrderStatus, ...]] = {
    OrderStatus.PENDING: (OrderStatus.SUBMITTED, OrderStatus.REJECTED,
                          OrderStatus.CANCELED),
    OrderStatus.SUBMITTED: (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                            OrderStatus.REJECTED, OrderStatus.CANCELED),
    OrderStatus.PARTIALLY_FILLED: (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                                   OrderStatus.CANCELED),
    OrderStatus.FILLED: (OrderStatus.CLOSED,),
    OrderStatus.CANCELED: (),
    OrderStatus.REJECTED: (),
    OrderStatus.CLOSED: (),
}

TERMINAL_STATUSES = (OrderStatus.FILLED, OrderStatus.CANCELED,
                     OrderStatus.REJECTED, OrderStatus.CLOSED)


def can_transition(src: OrderStatus, dst: OrderStatus) -> bool:
    return dst in _ALLOWED_TRANSITIONS.get(OrderStatus(src), ())


# ══════════════════════════ 确定性 ID ══════════════════════════

def make_client_order_id(strategy_key: str, symbol: str, signal_id: str,
                         action: str, order_sequence: int) -> str:
    return "coid_" + stable_hash(strategy_key, symbol, signal_id, action,
                                 int(order_sequence), length=24)


def make_idempotency_key(strategy_key: str, symbol: str, signal_id: str,
                         action: str, order_sequence: int) -> str:
    return "idem_" + stable_hash(strategy_key, symbol, signal_id, action,
                                 int(order_sequence), length=32)


def make_order_id(strategy_key: str, symbol: str, signal_id: str,
                  action: str, order_sequence: int) -> str:
    return "ord_" + stable_hash(strategy_key, symbol, signal_id, action,
                                int(order_sequence), length=24)


# ══════════════════════════ OrderIntent（发单前意图）══════════════════════════

@dataclass(frozen=True)
class OrderIntent:
    """`ExecutionPolicy` 产出、交给 `FillAdapter` 的**发单意图**。

    只描述「要发什么单」，不含任何成交价/成交结果（那是 FillAdapter 的职责）。
    """
    strategy_key: str
    symbol: str
    side: Side
    position_side: PositionSide
    order_type: OrderType
    quantity: float
    action: str                       # open / exit_stop / exit_gap / exit_eod ...
    phase: str
    signal_id: str
    order_sequence: int
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reference_price: Optional[float] = None
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def client_order_id(self) -> str:
        return make_client_order_id(self.strategy_key, self.symbol, self.signal_id,
                                    self.action, self.order_sequence)

    def idempotency_key(self) -> str:
        return make_idempotency_key(self.strategy_key, self.symbol, self.signal_id,
                                    self.action, self.order_sequence)

    def order_id(self) -> str:
        return make_order_id(self.strategy_key, self.symbol, self.signal_id,
                             self.action, self.order_sequence)


# ══════════════════════════ Order（唯一生命周期模型）══════════════════════════

@dataclass
class Order:
    order_id: str
    client_order_id: str
    idempotency_key: str
    strategy_key: str
    symbol: str
    side: Side
    position_side: PositionSide
    order_type: OrderType
    quantity: float
    status: OrderStatus = OrderStatus.PENDING
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    created_at: Optional[int] = None
    submitted_at: Optional[int] = None
    filled_at: Optional[int] = None
    fill_price: Optional[float] = None
    fee: Optional[float] = None
    reject_reason: str = ""
    action: str = ""
    phase: str = ""
    reason: str = ""
    signal_id: str = ""
    order_sequence: int = 0
    liquidity: Optional[Liquidity] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 状态机 ──
    def transition(self, dst: OrderStatus, *, at: Optional[int] = None,
                   reason: str = "") -> "Order":
        dst = OrderStatus(dst)
        if not can_transition(self.status, dst):
            raise TradingCoreError(
                f"illegal_order_transition:{self.status.value}->{dst.value}")
        self.status = dst
        if dst == OrderStatus.SUBMITTED:
            self.submitted_at = at if at is not None else self.submitted_at
        elif dst in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            self.filled_at = at if at is not None else self.filled_at
        if reason:
            self.reject_reason = reason
        return self

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def identity(self) -> dict:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "idempotency_key": self.idempotency_key,
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "side": self.side.value,
            "position_side": self.position_side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "status": self.status.value,
            "action": self.action,
            "phase": self.phase,
            "signal_id": self.signal_id,
            "order_sequence": self.order_sequence,
        }

    def to_dict(self) -> dict:
        d = dict(self.identity())
        d.update({
            "limit_price": self.limit_price, "stop_price": self.stop_price,
            "created_at": self.created_at, "submitted_at": self.submitted_at,
            "filled_at": self.filled_at, "fill_price": self.fill_price,
            "fee": self.fee, "reject_reason": self.reject_reason,
            "reason": self.reason,
            "liquidity": (self.liquidity.value if self.liquidity else None),
        })
        return d


def order_from_intent(intent: OrderIntent, *, created_at: Optional[int] = None) -> Order:
    return Order(
        order_id=intent.order_id(),
        client_order_id=intent.client_order_id(),
        idempotency_key=intent.idempotency_key(),
        strategy_key=intent.strategy_key, symbol=intent.symbol,
        side=intent.side, position_side=intent.position_side,
        order_type=intent.order_type, quantity=float(intent.quantity),
        status=OrderStatus.PENDING,
        limit_price=intent.limit_price, stop_price=intent.stop_price,
        created_at=created_at, action=intent.action, phase=intent.phase,
        reason=intent.reason, signal_id=intent.signal_id,
        order_sequence=int(intent.order_sequence),
        extra=dict(intent.extra or {}),
    )


# ══════════════════════════ 幂等注册表 ══════════════════════════

@dataclass
class SubmitResult:
    order: Order
    is_duplicate: bool


class OrderRegistry:
    """内存幂等注册表：以 `idempotency_key` 为唯一键。

    同一意图重复提交 ⇒ 返回**同一个 Order 对象**且 `is_duplicate=True`
    （不是新建第二个订单）。**不落盘**；持久化属 Ledger 层（本阶段只定接口）。
    """

    def __init__(self) -> None:
        self._by_key: Dict[str, Order] = {}
        self._by_client_oid: Dict[str, Order] = {}
        self._seq: Dict[Tuple[str, str], int] = {}

    def next_sequence(self, strategy_key: str, symbol: str, signal_id: str) -> int:
        """同一 (策略,标的,信号) 下的发单序号（从 0 起）。"""
        k = (strategy_key, symbol, signal_id)
        n = self._seq.get(k, 0)
        self._seq[k] = n + 1
        return n

    def peek(self, intent: OrderIntent) -> Optional[Order]:
        return self._by_key.get(intent.idempotency_key())

    def submit(self, intent: OrderIntent, *,
               created_at: Optional[int] = None) -> SubmitResult:
        key = intent.idempotency_key()
        if key in self._by_key:
            return SubmitResult(self._by_key[key], True)
        o = order_from_intent(intent, created_at=created_at)
        self._by_key[key] = o
        self._by_client_oid[o.client_order_id] = o
        return SubmitResult(o, False)

    def get(self, idempotency_key: str) -> Optional[Order]:
        return self._by_key.get(idempotency_key)

    def all_orders(self) -> List[Order]:
        return [self._by_key[k] for k in sorted(self._by_key)]

    def __len__(self) -> int:
        return len(self._by_key)


__all__ = [
    "ORDER_CONTRACT_VERSION", "TERMINAL_STATUSES", "can_transition",
    "make_client_order_id", "make_idempotency_key", "make_order_id",
    "OrderIntent", "Order", "order_from_intent", "SubmitResult", "OrderRegistry",
]
