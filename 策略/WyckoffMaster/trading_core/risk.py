# -*- coding: utf-8 -*-
"""Shared Trading Core · Risk 契约（施工单 W2-6A / W2-9）。

唯一风控校验器
──────────────
`portfolio_open_risk` **整体搬移**自 `oi_resonance_v1/execution.py`（无第二实现）；
`check_entry` 把原相位 C 内联的风险判定**逐式搬移**，判据顺序与阈值一字不改，
因此对既有 Golden 是**行为保持**的。

本金风险口径（不可回退）
────────────────────────
long  : exp_exit = stop×(1−slip)；loss = (entry − exp_exit) × qty
short : exp_exit = stop×(1+slip)；loss = (exp_exit − entry) × qty
total += max(0, loss + entry_fee + exp_exit × qty × fee_rate)

**不得**用 `max/min(entry, last_mark)` 把浮盈计入本金风险。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .position import Position

RISK_CONTRACT_VERSION = "risk_contract_v1.0.0"

# 拒绝原因码（与既有引擎逐字一致，不得改写）
R_DUPLICATE_SYMBOL = "duplicate_symbol_active"
R_MAX_CONCURRENT = "max_concurrent_trades_reached"
R_INVALID_STOP = "invalid_signal_stop"
R_GAP_BEYOND_STOP = "open_gapped_beyond_stop"
R_NONPOSITIVE_EQUITY = "nonpositive_equity"
R_ZERO_UNIT_RISK = "zero_unit_risk"
R_PORTFOLIO_RISK = "portfolio_risk_cap_exceeded"
R_PORTFOLIO_MARGIN = "portfolio_margin_cap_exceeded"
R_NONPOSITIVE_QTY = "nonpositive_qty"


@dataclass(frozen=True)
class RiskConstraints:
    """风控约束（唯一来源：`ExecParams`；此处只做只读快照，不另立参数）。"""
    risk_per_trade: float
    max_portfolio_risk: float
    max_portfolio_margin_pct: float
    max_concurrent_trades: int
    max_leverage_per_symbol: float
    fee_rate: float
    slippage_rate: float

    @classmethod
    def from_exec_params(cls, cfg) -> "RiskConstraints":
        return cls(
            risk_per_trade=float(cfg.risk_per_trade),
            max_portfolio_risk=float(cfg.max_portfolio_risk),
            max_portfolio_margin_pct=float(getattr(cfg, "max_portfolio_margin_pct", 0.40)),
            max_concurrent_trades=int(getattr(cfg, "max_concurrent_trades", 3)),
            max_leverage_per_symbol=float(cfg.max_leverage_per_symbol),
            fee_rate=float(cfg.fee_rate),
            slippage_rate=float(cfg.slippage_bps) * 1e-4,
        )

    def identity(self) -> dict:
        return {
            "risk_per_trade": self.risk_per_trade,
            "max_portfolio_risk": self.max_portfolio_risk,
            "max_portfolio_margin_pct": self.max_portfolio_margin_pct,
            "max_concurrent_trades": self.max_concurrent_trades,
            "max_leverage_per_symbol": self.max_leverage_per_symbol,
            "fee_rate": self.fee_rate,
            "slippage_rate": self.slippage_rate,
        }


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str = ""
    quantity: float = 0.0
    unit_risk: float = float("nan")
    equity_mtm: float = float("nan")
    current_open_risk: float = float("nan")
    available_notional: float = float("nan")
    detail: Dict[str, Any] = field(default_factory=dict)


def portfolio_open_risk(positions: Dict[str, Position], fee_rate: float,
                        slip_rate: float) -> float:
    """组合**本金风险**（入场成本口径）：按「入场价 → 止损价」的损失算，**不含浮盈**。"""
    tot = 0.0
    for p in positions.values():
        if p.direction == 1:
            exp_exit = p.current_stop * (1.0 - slip_rate)
            loss = (p.entry_price - exp_exit) * p.qty
        else:
            exp_exit = p.current_stop * (1.0 + slip_rate)
            loss = (exp_exit - p.entry_price) * p.qty
        tot += max(0.0, loss + p.entry_fee + exp_exit * p.qty * fee_rate)
    return float(tot)


def portfolio_notional(positions: Dict[str, Position]) -> float:
    return float(sum(p.qty * p.last_mark_price for p in positions.values()))


def check_entry(*, positions: Dict[str, Position], symbol: str, direction: int,
                stop_ref: float, entry_fill: float, cash: float,
                equity_mtm: float, constraints: RiskConstraints,
                fee_rate: float) -> RiskDecision:
    """入场风控 + 目标数量（**逐式搬移**原相位 C，判据顺序不可改）。

    `equity_mtm` 由调用方按 `cash + unrealized_pnl(positions, fee_rate)` 提供，
    以保证与既有引擎完全同源（不在本函数内重复计算，避免第二套口径）。
    """
    c = constraints
    slip = c.slippage_rate

    if symbol in positions:
        return RiskDecision(False, R_DUPLICATE_SYMBOL)
    if len(positions) >= int(c.max_concurrent_trades):
        return RiskDecision(False, R_MAX_CONCURRENT)
    if not math.isfinite(stop_ref) or stop_ref <= 0:
        return RiskDecision(False, R_INVALID_STOP)
    if (direction == 1 and entry_fill <= stop_ref) or \
            (direction == -1 and entry_fill >= stop_ref):
        return RiskDecision(False, R_GAP_BEYOND_STOP)

    mtm = float(equity_mtm)
    if mtm <= 0:
        return RiskDecision(False, R_NONPOSITIVE_EQUITY, equity_mtm=mtm)

    stop_fill = (stop_ref * (1.0 - slip) if direction == 1
                 else stop_ref * (1.0 + slip))
    unit_risk = abs(entry_fill - stop_fill) + (entry_fill + stop_fill) * fee_rate
    if unit_risk <= 0:
        return RiskDecision(False, R_ZERO_UNIT_RISK, equity_mtm=mtm)

    cur_risk = portfolio_open_risk(positions, fee_rate, slip)
    if (c.max_portfolio_risk * mtm - cur_risk) < c.risk_per_trade * mtm:
        return RiskDecision(False, R_PORTFOLIO_RISK, unit_risk=unit_risk,
                            equity_mtm=mtm, current_open_risk=cur_risk)

    cur_notional = portfolio_notional(positions)
    max_allowed_notional = mtm * c.max_portfolio_margin_pct * c.max_leverage_per_symbol
    avail_notional = max(0.0, max_allowed_notional - cur_notional)
    if avail_notional <= 0:
        return RiskDecision(False, R_PORTFOLIO_MARGIN, unit_risk=unit_risk,
                            equity_mtm=mtm, current_open_risk=cur_risk,
                            available_notional=avail_notional)

    qty = min(c.risk_per_trade * mtm / unit_risk,
              mtm * c.max_leverage_per_symbol / entry_fill,
              avail_notional / entry_fill)
    if qty <= 0 or not math.isfinite(qty):
        return RiskDecision(False, R_NONPOSITIVE_QTY, unit_risk=unit_risk,
                            equity_mtm=mtm, current_open_risk=cur_risk,
                            available_notional=avail_notional)

    return RiskDecision(True, "", quantity=float(qty), unit_risk=float(unit_risk),
                        equity_mtm=mtm, current_open_risk=cur_risk,
                        available_notional=avail_notional,
                        detail={"stop_fill": float(stop_fill),
                                "notional": float(qty * entry_fill),
                                "risk_usd": float(qty * unit_risk)})


__all__ = [
    "RISK_CONTRACT_VERSION",
    "R_DUPLICATE_SYMBOL", "R_MAX_CONCURRENT", "R_INVALID_STOP", "R_GAP_BEYOND_STOP",
    "R_NONPOSITIVE_EQUITY", "R_ZERO_UNIT_RISK", "R_PORTFOLIO_RISK",
    "R_PORTFOLIO_MARGIN", "R_NONPOSITIVE_QTY",
    "RiskConstraints", "RiskDecision", "portfolio_open_risk", "portfolio_notional",
    "check_entry",
]
