# -*- coding: utf-8 -*-
"""Shared Trading Core · Ledger 事件契约（施工单 W2-10）。

本阶段**只定义事件接口与幂等语义**，不做大规模迁移：
`paper_engine.py` 的旧账本**一个字节都不删**，等下一施工单再迁移。

统一事件类型（施工单原文 12 类）
────────────────────────────────
  GENESIS_INIT · SIGNAL_ACCEPTED · SIGNAL_REJECTED · ORDER_CREATED · ORDER_SUBMITTED
  ORDER_FILLED · ORDER_CANCELED · FUNDING_FEE · POSITION_OPENED · POSITION_REDUCED
  POSITION_CLOSED · FEE_CHARGED

每个事件必备：`event_id / event_type / event_time / strategy_key / symbol / payload`。
`event_id` 由**确定性哈希**产生 ⇒ 重复投递同一事件只会入库一次（幂等）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .contracts import TradingCoreError, payload_hash, stable_hash

LEDGER_CONTRACT_VERSION = "ledger_contract_v1.0.0"

GENESIS_INIT = "GENESIS_INIT"
SIGNAL_ACCEPTED = "SIGNAL_ACCEPTED"
SIGNAL_REJECTED = "SIGNAL_REJECTED"
ORDER_CREATED = "ORDER_CREATED"
ORDER_SUBMITTED = "ORDER_SUBMITTED"
ORDER_FILLED = "ORDER_FILLED"
ORDER_CANCELED = "ORDER_CANCELED"
FUNDING_FEE = "FUNDING_FEE"
POSITION_OPENED = "POSITION_OPENED"
POSITION_REDUCED = "POSITION_REDUCED"
POSITION_CLOSED = "POSITION_CLOSED"
FEE_CHARGED = "FEE_CHARGED"

LEDGER_EVENT_TYPES = (
    GENESIS_INIT, SIGNAL_ACCEPTED, SIGNAL_REJECTED, ORDER_CREATED, ORDER_SUBMITTED,
    ORDER_FILLED, ORDER_CANCELED, FUNDING_FEE, POSITION_OPENED, POSITION_REDUCED,
    POSITION_CLOSED, FEE_CHARGED,
)


def make_event_id(event_type: str, strategy_key: str, symbol: str,
                  event_time: Optional[int], payload: Any, sequence: int = 0) -> str:
    """确定性 event_id（同一事件重复投递 ⇒ 同一 ID ⇒ 幂等去重）。"""
    return "evt_" + stable_hash(event_type, strategy_key, symbol,
                                event_time, payload_hash(payload),
                                int(sequence), length=32)


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    event_type: str
    event_time: Optional[int]
    strategy_key: str
    symbol: str
    payload: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    @staticmethod
    def build(event_type: str, *, strategy_key: str, symbol: str,
              event_time: Optional[int], payload: Optional[dict] = None,
              sequence: int = 0) -> "LedgerEvent":
        if event_type not in LEDGER_EVENT_TYPES:
            raise TradingCoreError(f"unknown_ledger_event_type:{event_type}")
        pl = dict(payload or {})
        return LedgerEvent(
            event_id=make_event_id(event_type, strategy_key, symbol, event_time,
                                   pl, sequence),
            event_type=event_type, event_time=event_time,
            strategy_key=strategy_key, symbol=symbol, payload=pl, sequence=sequence)

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "event_type": self.event_type,
                "event_time": self.event_time, "strategy_key": self.strategy_key,
                "symbol": self.symbol, "payload": self.payload,
                "sequence": int(self.sequence)}


@runtime_checkable
class LedgerWriter(Protocol):
    def emit(self, event: LedgerEvent) -> bool:
        """写入事件；返回 True = 新写入，False = 幂等命中（已存在）。"""
        ...


class InMemoryLedger:
    """内存账本（幂等）。落盘/迁移属下一施工单，本阶段只验证接口语义。"""

    def __init__(self) -> None:
        self._events: List[LedgerEvent] = []
        self._seen: Dict[str, LedgerEvent] = {}

    def emit(self, event: LedgerEvent) -> bool:
        if event.event_id in self._seen:
            return False
        self._seen[event.event_id] = event
        self._events.append(event)
        return True

    def events(self) -> List[LedgerEvent]:
        return list(self._events)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self._events:
            out[e.event_type] = out.get(e.event_type, 0) + 1
        return out

    def __len__(self) -> int:
        return len(self._events)


__all__ = [
    "LEDGER_CONTRACT_VERSION",
    "GENESIS_INIT", "SIGNAL_ACCEPTED", "SIGNAL_REJECTED", "ORDER_CREATED",
    "ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_CANCELED", "FUNDING_FEE",
    "POSITION_OPENED", "POSITION_REDUCED", "POSITION_CLOSED", "FEE_CHARGED",
    "LEDGER_EVENT_TYPES", "make_event_id", "LedgerEvent", "LedgerWriter",
    "InMemoryLedger",
]
