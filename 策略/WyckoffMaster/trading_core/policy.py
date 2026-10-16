# -*- coding: utf-8 -*-
"""Shared Trading Core · ExecutionPolicy（施工单 W2-6A）。

职责（**只有这些**）
────────────────────
    Signal → 是否发 Order → 何时发 Order → 目标数量 → 订单类型 → 止损/退出规则

**不含**任何交易所成交逻辑：成交价一律由注入的 `FillAdapter` 提供
（`FillQuote`）。因此同一份 `ExecutionPolicy` 可以被 Backtest / Paper / Live
**共同调用**，而三端只是在 `FillAdapter` 上分叉。

三端共享声明
────────────
`Backtest` / `Paper` / `Live` 必须使用**同一份** `ExecutionPolicy`（物理同一模块）。
本类不做 IO、不读库、不下单、不持有任何账户状态 —— 输入状态由调用方传入。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import ExecPhase, OrderType, PositionSide, Side
from .fill import FillQuote, round_qty_down
from .order import OrderIntent
from .position import Position
from .risk import RiskConstraints, RiskDecision, check_entry

POLICY_CONTRACT_VERSION = "execution_policy_v1.0.0"

# 退出原因码（与既有引擎逐字一致）
EXIT_GAP = "gap_open_stop"
EXIT_INTRABAR = "intrabar_stop"
EXIT_EOD = "force_close_eod"

# 入场动作名
ACTION_OPEN = "open"
ACTION_EXIT_GAP = "exit_gap"
ACTION_EXIT_STOP = "exit_stop"
ACTION_EXIT_EOD = "exit_eod"


@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    reason: str = ""
    intent: Optional[OrderIntent] = None
    risk: Optional[RiskDecision] = None
    phase: str = ExecPhase.C_ENTRY.value


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str = ""
    target_px: float = float("nan")
    phase: str = ""
    order_type: OrderType = OrderType.STOP_MARKET
    intent: Optional[OrderIntent] = None


class ExecutionPolicy:
    """三端共享的执行策略（纯决策）。"""

    def __init__(self, *, signal_params, exec_params,
                 strategy_key: str, timeframe: str = "1h",
                 exec_timeframe: str = "15m"):
        self.p = signal_params
        self.cfg = exec_params
        self.strategy_key = strategy_key
        self.timeframe = timeframe
        self.exec_timeframe = exec_timeframe
        self.price_step = float(signal_params.price_step)
        self.constraints = RiskConstraints.from_exec_params(exec_params)

    # ══════════════════════ 订单类型（统一声明，不散落）══════════════════════
    def entry_order_type(self) -> OrderType:
        return OrderType.MARKET

    def exit_order_type(self) -> OrderType:
        return OrderType.STOP_MARKET

    # ══════════════════════ 入场 ══════════════════════
    def entry_priority(self, score: Optional[float]) -> float:
        """同根多信号排序键（越小越先）。与既有引擎 `-(r if finite else -1.0)` 一致。"""
        r = -1.0 if score is None or not math.isfinite(float(score)) else float(score)
        return -r

    def decide_entry(self, *, symbol: str, signal_type: int, direction: int,
                     raw_price: float, stop_ref: float, quote: FillQuote,
                     positions: Dict[str, Position], cash: float, equity_mtm: float,
                     signal_id: str, order_sequence: int,
                     signal_bar_open_ms: Optional[int] = None,
                     signal_score: Optional[float] = None) -> EntryDecision:
        """风控 + 目标数量 → 发单意图。`quote` 由 FillAdapter 提供（本类不造价）。"""
        d = check_entry(positions=positions, symbol=symbol, direction=direction,
                        stop_ref=stop_ref, entry_fill=quote.entry_fill, cash=cash,
                        equity_mtm=equity_mtm, constraints=self.constraints,
                        fee_rate=self.constraints.fee_rate)
        if not d.accepted:
            return EntryDecision(False, d.reason, risk=d)
        # 注意：**不**对数量取整。既有引擎 `run_portfolio_execution` 相位 C 直接使用
        # 风控算出的 qty（未做 step 取整）⇒ 此处保持逐位一致，不引入行为变化。
        qty = float(d.quantity)
        if qty <= 0 or not math.isfinite(qty):
            return EntryDecision(False, "nonpositive_qty", risk=d)
        ps = PositionSide.LONG if direction == 1 else PositionSide.SHORT
        side = Side.BUY if direction == 1 else Side.SELL
        intent = OrderIntent(
            strategy_key=self.strategy_key, symbol=symbol, side=side,
            position_side=ps, order_type=self.entry_order_type(),
            quantity=float(qty), action=ACTION_OPEN,
            phase=ExecPhase.C_ENTRY.value, signal_id=signal_id,
            order_sequence=int(order_sequence),
            stop_price=float(stop_ref), reference_price=float(raw_price),
            reason="entry",
            extra={"signal_type": int(signal_type), "direction": int(direction),
                   "signal_score": (None if signal_score is None
                                    else float(signal_score)),
                   "signal_bar_open_ms": signal_bar_open_ms,
                   "entry_fill": float(quote.entry_fill),
                   "stop_fill": float(quote.stop_fill),
                   "unit_risk": float(d.unit_risk)})
        return EntryDecision(True, "", intent=intent, risk=d)

    # ══════════════════════ 锁利通道（trailing）══════════════════════
    def trail_level(self, *, direction: int, exit_low: float,
                    exit_high: float, current_stop: float) -> Optional[float]:
        """当期锁利通道 → 新保护价（多头只上抬、空头只下压；否则 None）。"""
        if direction == 1:
            lvl = float(exit_low)
            if math.isfinite(lvl) and lvl > current_stop:
                return lvl
            return None
        lvl = float(exit_high)
        if math.isfinite(lvl) and lvl < current_stop:
            return lvl
        return None

    # ══════════════════════ 退出 ══════════════════════
    def decide_gap_exit(self, *, pos: Position, bar_open: float,
                        symbol: str) -> ExitDecision:
        """相位 B：跳空退出（**只用 open**，不得用未来 low/high 提前释放资金）。"""
        op = float(bar_open)
        gap = ((pos.direction == 1 and op <= pos.current_stop)
               or (pos.direction == -1 and op >= pos.current_stop))
        if not gap:
            return ExitDecision(False)
        return ExitDecision(True, EXIT_GAP, target_px=op,
                            phase=ExecPhase.B_GAP.value,
                            order_type=self.exit_order_type())

    def decide_intrabar_exit(self, *, pos: Position, high: float, low: float,
                             symbol: str) -> ExitDecision:
        """相位 D：bar 内止损（**含当根新开仓**）。"""
        if pos.direction == 1:
            hit = float(low) <= pos.current_stop
        else:
            hit = float(high) >= pos.current_stop
        if not hit:
            return ExitDecision(False)
        return ExitDecision(True, EXIT_INTRABAR, target_px=float(pos.current_stop),
                            phase=ExecPhase.D_INTRABAR.value,
                            order_type=self.exit_order_type())

    def close_open_positions_at_end(self, runtime: Optional[bool] = None) -> bool:
        """期末是否强平（施工单 W2-3 的显式开关）。

        `runtime=None` ⇒ 采用冻结配置；`True/False` ⇒ 显式 Runtime 覆盖。
        冻结配置读取顺序：`cfg.close_open_positions_at_end` → `cfg.force_close_at_end`
        （后者是 WORK-02 之前的字段名，保留为兼容别名）。
        """
        if runtime is not None:
            return bool(runtime)
        return bool(getattr(self.cfg, "close_open_positions_at_end",
                            getattr(self.cfg, "force_close_at_end", True)))

    def close_at_end_source(self, runtime: Optional[bool] = None) -> str:
        return "frozen_config" if runtime is None else "runtime_override"

    def decide_eod_exit(self, *, pos: Position, last_close: float,
                        symbol: str) -> ExitDecision:
        return ExitDecision(True, EXIT_EOD, target_px=float(last_close),
                            phase=ExecPhase.EOD.value,
                            order_type=self.exit_order_type())

    # ══════════════════════ 身份 ══════════════════════
    def identity(self) -> dict:
        return {
            "policy_contract": POLICY_CONTRACT_VERSION,
            "strategy_key": self.strategy_key,
            "timeframe": self.timeframe,
            "exec_timeframe": self.exec_timeframe,
            "price_step": self.price_step,
            "entry_order_type": self.entry_order_type().value,
            "exit_order_type": self.exit_order_type().value,
            "close_open_positions_at_end": self.close_open_positions_at_end(),
            "enable_trailing_exit": bool(getattr(self.cfg, "enable_trailing_exit", True)),
            "risk": self.constraints.identity(),
            "signal_params_hash": (self.p.hash() if hasattr(self.p, "hash") else None),
            "exec_params_hash": (self.cfg.hash() if hasattr(self.cfg, "hash") else None),
        }


__all__ = [
    "POLICY_CONTRACT_VERSION", "EXIT_GAP", "EXIT_INTRABAR", "EXIT_EOD",
    "ACTION_OPEN", "ACTION_EXIT_GAP", "ACTION_EXIT_STOP", "ACTION_EXIT_EOD",
    "EntryDecision", "ExitDecision", "ExecutionPolicy",
]
