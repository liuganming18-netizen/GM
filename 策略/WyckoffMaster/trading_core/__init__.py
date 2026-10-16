# -*- coding: utf-8 -*-
"""Shared Trading Core —— Backtest / Paper / Live 共享的**唯一**交易核心契约。

分层
────
    contracts.py  常量 · 枚举 · 确定性哈希 · 错误类型
    signal.py     Signal 契约（四时点分离）
    order.py      Order 模型 · 状态机 · 确定性幂等
    risk.py       唯一风控校验器（本金风险口径）
    policy.py     ExecutionPolicy（只决策，不含成交逻辑）
    fill.py       FillAdapter 协议 + Simulated / Exchange 两个实现
    position.py   Position / Trade（原 OpenPosition 搬移，非第二套）
    funding.py    资金费结算（settlement_events / 显式假设模型）
    ledger.py     LedgerEvent 类型 + 幂等写入协议
    engine.py     TradingCore 主循环

三端唯一区别在 `FillAdapter` 与数据泵；策略/风控/撮合/账本 100% 共享。
"""
from __future__ import annotations

from .contracts import (CORE_CONTRACT_VERSION, ExecPhase, ExecutionInputError,
                        Liquidity, LiveExecutionForbidden, OrderStatus, OrderType,
                        PositionSide, Side, TradingCoreError, canonical_json,
                        payload_hash, stable_hash)
from .engine import ENGINE_CONTRACT_VERSION, TradingCore, prep_symbol
from .fill import (EXCHANGE_ACK, FILL_CONTRACT_VERSION, SIMULATED_BAR,
                   ExchangeFillAdapter, Fill, FillAdapter, FillQuote,
                   SimulatedBarFillAdapter, round_buy_price, round_qty_down,
                   round_sell_price)
from .funding import (FUNDING_CONTRACT_VERSION, funding_events_ms, settle_funding)
from .ledger import (LEDGER_CONTRACT_VERSION, LEDGER_EVENT_TYPES, LedgerEvent,
                     LedgerWriter, InMemoryLedger, make_event_id)
from .order import (ORDER_CONTRACT_VERSION, Order, OrderIntent, OrderRegistry,
                    SubmitResult, make_client_order_id, make_idempotency_key,
                    make_order_id, order_from_intent)
from .policy import (POLICY_CONTRACT_VERSION, EntryDecision, ExitDecision,
                     ExecutionPolicy)
from .position import (POSITION_CONTRACT_VERSION, Position, PositionBook, Trade,
                       unrealized_net_open, unrealized_pnl)
from .risk import (RISK_CONTRACT_VERSION, RiskConstraints, RiskDecision,
                   check_entry, portfolio_notional, portfolio_open_risk)
from .signal import (SIGNAL_CONTRACT_VERSION, SignalEvent, build_signal_events,
                     make_signal_id, resolve_trade_time)

__all__ = [
    "CORE_CONTRACT_VERSION", "ENGINE_CONTRACT_VERSION", "SIGNAL_CONTRACT_VERSION",
    "ORDER_CONTRACT_VERSION", "RISK_CONTRACT_VERSION", "POLICY_CONTRACT_VERSION",
    "FILL_CONTRACT_VERSION", "POSITION_CONTRACT_VERSION", "FUNDING_CONTRACT_VERSION",
    "LEDGER_CONTRACT_VERSION",
    # contracts
    "OrderStatus", "Side", "PositionSide", "OrderType", "ExecPhase", "Liquidity",
    "TradingCoreError", "LiveExecutionForbidden", "ExecutionInputError",
    "stable_hash", "canonical_json", "payload_hash",
    # signal
    "SignalEvent", "build_signal_events", "make_signal_id", "resolve_trade_time",
    # order
    "Order", "OrderIntent", "OrderRegistry", "SubmitResult", "order_from_intent",
    "make_client_order_id", "make_idempotency_key", "make_order_id",
    # risk
    "RiskConstraints", "RiskDecision", "check_entry", "portfolio_open_risk",
    "portfolio_notional",
    # policy
    "ExecutionPolicy", "EntryDecision", "ExitDecision",
    # fill
    "FillAdapter", "FillQuote", "Fill", "SimulatedBarFillAdapter",
    "ExchangeFillAdapter", "SIMULATED_BAR", "EXCHANGE_ACK",
    "round_buy_price", "round_sell_price", "round_qty_down",
    # position / trade
    "Position", "PositionBook", "Trade", "unrealized_pnl", "unrealized_net_open",
    # funding
    "funding_events_ms", "settle_funding",
    # ledger
    "LedgerEvent", "LedgerWriter", "InMemoryLedger", "LEDGER_EVENT_TYPES",
    "make_event_id",
    # engine
    "TradingCore", "prep_symbol",
]
