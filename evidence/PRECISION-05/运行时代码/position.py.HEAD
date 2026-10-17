# -*- coding: utf-8 -*-
"""Shared Trading Core · Position / Trade 契约（施工单 W2-9）。

复用声明
────────
本模块**不是**第二套持仓模型：`Position` 就是原 `oi_resonance_v1/execution.py`
的 `OpenPosition`（字段名、顺序、默认值**逐字保留**），只是搬到共享层，
并让 `execution.OpenPosition` 成为它的**别名**（同一类对象，不是副本）。
`unrealized_pnl` / `unrealized_net_open` 同样**整体搬移**，无第二实现。

接口边界（W2-9）
────────────────
    Fill → Position → Trade → LedgerEvent
Paper / Live **不得**自己算 Position / Equity / Fee，也不得维护另一套 Order。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import PositionSide

POSITION_CONTRACT_VERSION = "position_contract_v1.0.0"


@dataclass
class Position:
    """持仓（= 原 `OpenPosition`，字段逐字保留，保证既有调用方零改动）。"""

    symbol: str
    direction: int
    signal_type: int
    entry_time: int
    entry_time_sec: int
    entry_price: float
    qty: float
    initial_stop: float
    current_stop: float
    entry_fee: float
    last_mark_price: float
    # 上次资金费结算时刻（**毫秒**；入场时初始化为入场 bar 开盘毫秒）。
    # 判据 `last_funding_ms < ev_ms <= t_ms` ⇒ 只结算**持仓区间内**的真实结算事件。
    last_funding_ms: int = -10 ** 15
    cum_funding_fee: float = 0.0
    funding_gaps: List[dict] = field(default_factory=list)
    entry_slip_loss: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    # 保护止损轨迹（**引擎实况记录**，供图表真实取点；不造点）：
    #   [{"ts": 毫秒, "stop": 保护价, "reason": "init"|"trail"}]
    stop_trace: List[dict] = field(default_factory=list)
    # 该仓由哪个订单/信号开立（W2-9 溯源链：Ledger → Trade → Order → Signal）
    order_id: Optional[str] = None
    signal_id: Optional[str] = None
    signal_bar_open_ms: Optional[int] = None

    @property
    def position_side(self) -> PositionSide:
        return PositionSide.LONG if int(self.direction) == 1 else PositionSide.SHORT

    def unrealized_gross(self) -> float:
        return (self.last_mark_price - self.entry_price) * self.qty * self.direction

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": int(self.direction),
            "signal_type": int(self.signal_type),
            "entry_time": int(self.entry_time), "entry_time_sec": int(self.entry_time_sec),
            "entry_price": self.entry_price, "qty": self.qty,
            "initial_stop": self.initial_stop, "current_stop": self.current_stop,
            "entry_fee": self.entry_fee, "last_mark_price": self.last_mark_price,
            "last_funding_ms": int(self.last_funding_ms),
            "cum_funding_fee": self.cum_funding_fee,
            "entry_slip_loss": self.entry_slip_loss, "mfe": self.mfe, "mae": self.mae,
            "stop_trace": list(self.stop_trace),
            "order_id": self.order_id, "signal_id": self.signal_id,
            "signal_bar_open_ms": self.signal_bar_open_ms,
        }


@dataclass(frozen=True)
class Trade:
    """标准化成交记录（一次完整开→平）。

    `maker_fee` / `taker_fee` 严格分项；`slippage_loss` 同时给 U 额与来源目标价。
    """
    trade_id: str
    symbol: str
    direction: int
    signal_type: int
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    fees: float
    funding_fee: float
    slippage_loss: float
    net_pnl: float
    mfe: float = 0.0
    mae: float = 0.0
    exit_reason: str = ""
    order_id: Optional[str] = None
    signal_id: Optional[str] = None
    funding_gaps: List[dict] = field(default_factory=list)
    stop_trace: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id, "symbol": self.symbol,
            "direction": int(self.direction), "signal_type": int(self.signal_type),
            "entry_time": int(self.entry_time), "exit_time": int(self.exit_time),
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "qty": self.qty, "gross_pnl": self.gross_pnl,
            "entry_fee": self.entry_fee, "exit_fee": self.exit_fee, "fees": self.fees,
            "funding_fee": self.funding_fee, "slippage_loss": self.slippage_loss,
            "net_pnl": self.net_pnl, "mfe": self.mfe, "mae": self.mae,
            "exit_reason": self.exit_reason, "order_id": self.order_id,
            "signal_id": self.signal_id, "funding_gaps": list(self.funding_gaps),
            "stop_trace": list(self.stop_trace),
        }


# ══════════════════════════ 未实现盈亏（整体搬移，无第二实现）══════════════════════════

def unrealized_pnl(positions: Dict[str, Position], fee_rate: float) -> float:
    """未实现盈亏 = Σ[(mark − entry)×qty×dir − 预估**出场**费用]（入场费已在现金中扣过）。"""
    tot = 0.0
    for p in positions.values():
        gross = (p.last_mark_price - p.entry_price) * p.qty * p.direction
        tot += gross - p.last_mark_price * p.qty * fee_rate
    return float(tot)


def unrealized_net_open(positions: Dict[str, Position], fee_rate: float) -> float:
    """未实现**净**盈亏（再扣一次入场费与已结算资金费），用于权益恒等式对账。"""
    tot = 0.0
    for p in positions.values():
        gross = (p.last_mark_price - p.entry_price) * p.qty * p.direction
        tot += (gross - p.entry_fee - p.cum_funding_fee
                - p.last_mark_price * p.qty * fee_rate)
    return float(tot)


class PositionBook:
    """持仓集合（薄封装）。快照导出/导入供 Paper/Live 重启幂等（本阶段只定接口）。"""

    def __init__(self) -> None:
        self._by_symbol: Dict[str, Position] = {}

    def get(self, symbol: str) -> Optional[Position]:
        return self._by_symbol.get(symbol)

    def put(self, pos: Position) -> None:
        self._by_symbol[pos.symbol] = pos

    def pop(self, symbol: str) -> Optional[Position]:
        return self._by_symbol.pop(symbol, None)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._by_symbol

    def __len__(self) -> int:
        return len(self._by_symbol)

    def symbols(self) -> List[str]:
        return sorted(self._by_symbol)

    def values(self):
        return self._by_symbol.values()

    def snapshot(self) -> List[dict]:
        return [self._by_symbol[s].to_dict() for s in self.symbols()]

    @classmethod
    def from_snapshot(cls, rows: List[dict]) -> "PositionBook":
        book = cls()
        for r in rows:
            d = dict(r)
            d.pop("position_side", None)
            book.put(Position(**d))
        return book


__all__ = [
    "POSITION_CONTRACT_VERSION", "Position", "Trade", "PositionBook",
    "unrealized_pnl", "unrealized_net_open",
]
