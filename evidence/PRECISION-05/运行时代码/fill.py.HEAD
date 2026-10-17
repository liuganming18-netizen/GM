# -*- coding: utf-8 -*-
"""Shared Trading Core · FillAdapter 契约（施工单 W2-6B / W2-7）。

为什么必须拆开
──────────────
`ExecutionPolicy` 决定「**发不发、发什么、发多少**」；
`FillAdapter` 决定「**这笔单最终成交成了什么**」。
两者混在一起时，模拟引擎会顺理成章地被拿去当实盘执行器 —— 那正是本次要根除的。

本模块提供**两个可互换注入**的实现：

  SimulatedBarFillAdapter   bar 级保守假设成交（下一根 bar 开盘 + 滑点 + 取整 + 费率）
  ExchangeFillAdapter       真实交易所回报驱动（ack / fill / avgPrice / fee / funding）
                            **不接受任何 K 线输入**；无交易所客户端即硬拒绝，
                            从结构上不可能「按 K 线假算成交价」。

取整/费率算术**整体搬移**自 `oi_resonance_v1/execution.py`（无第二实现），
因此对既有 Golden 行为保持。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .contracts import (Liquidity, LiveExecutionForbidden, OrderStatus, TradingCoreError)

FILL_CONTRACT_VERSION = "fill_contract_v1.0.0"

SIMULATED_BAR = "simulated_bar"
EXCHANGE_ACK = "exchange_ack"


# ══════════════════════════ 取整（整体搬移，无第二实现）══════════════════════════

def round_buy_price(px: float, step: float) -> float:
    """买入价向**更贵**取整（step<=0 不取整）。"""
    if not step or step <= 0:
        return float(px)
    return float(step * math.ceil(px / step - 1e-9))


def round_sell_price(px: float, step: float) -> float:
    """卖出价向**更便宜**取整。"""
    if not step or step <= 0:
        return float(px)
    return float(step * math.floor(px / step + 1e-9))


def round_qty_down(qty: float, step: float) -> float:
    if not step or step <= 0:
        return float(qty)
    return float(step * math.floor(qty / step + 1e-9))


# ══════════════════════════ 数据结构 ══════════════════════════

@dataclass(frozen=True)
class FillQuote:
    """成交价**报价**（还没成交）。Policy 用它做风险/数量计算。"""
    entry_fill: float
    stop_fill: float
    source: str
    slippage_rate: float
    price_step: float


@dataclass(frozen=True)
class Fill:
    """一笔成交结果（唯一成交事件模型）。"""
    order_id: str
    status: OrderStatus
    fill_price: float
    quantity: float
    fee: float
    fee_rate: float
    liquidity: Liquidity
    filled_at: Optional[int]
    slippage_loss: float = 0.0
    reject_reason: str = ""
    source: str = SIMULATED_BAR
    detail: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class FillAdapter(Protocol):
    """成交适配器协议。三端注入不同实现，其余代码 100% 共享。"""

    kind: str

    def quote_entry(self, *, raw_price: float, direction: int, price_step: float,
                    stop_price: float) -> FillQuote: ...

    def quote_exit(self, *, target_px: float, direction: int,
                   price_step: float, reason: str) -> float: ...

    def fee_rate(self, *, liquidity: Liquidity) -> float: ...


# ══════════════════════════ Backtest / Paper：bar 级模拟成交 ══════════════════════════

class SimulatedBarFillAdapter:
    """bar 级保守成交假设。

    · 下一根执行 bar 的 **open** 作为基准价（施工单「下一根 15m / open」）；
    · 买入向更贵、卖出向更便宜取整（最劣方向）；
    · 滑点按 `slippage_bps` 单边；
    · 费率 maker/taker 分列，默认 taker。

    **不含任何策略/风控判断**，也不读任何账本。
    """

    kind = SIMULATED_BAR

    def __init__(self, *, slippage_bps: float, maker_fee_rate: float,
                 taker_fee_rate: float, default_liquidity: Liquidity = Liquidity.TAKER):
        self.slippage_rate = float(slippage_bps) * 1e-4
        self.maker_fee_rate = float(maker_fee_rate)
        self.taker_fee_rate = float(taker_fee_rate)
        self.default_liquidity = Liquidity(default_liquidity)

    # —— 报价 ——
    def quote_entry(self, *, raw_price: float, direction: int, price_step: float,
                    stop_price: float) -> FillQuote:
        slip = self.slippage_rate
        if direction == 1:
            entry = round_buy_price(raw_price * (1.0 + slip), price_step)
            stop = stop_price * (1.0 - slip)
        else:
            entry = round_sell_price(raw_price * (1.0 - slip), price_step)
            stop = stop_price * (1.0 + slip)
        return FillQuote(entry_fill=float(entry), stop_fill=float(stop),
                         source=SIMULATED_BAR, slippage_rate=slip,
                         price_step=float(price_step))

    def quote_exit(self, *, target_px: float, direction: int,
                   price_step: float, reason: str) -> float:
        slip = self.slippage_rate
        if direction == 1:
            return round_sell_price(target_px * (1.0 - slip), price_step)
        return round_buy_price(target_px * (1.0 + slip), price_step)

    def fee_rate(self, *, liquidity: Liquidity = None) -> float:
        liq = Liquidity(liquidity or self.default_liquidity)
        return self.maker_fee_rate if liq == Liquidity.MAKER else self.taker_fee_rate

    def fill_fee(self, fill_price: float, qty: float, *,
                 liquidity: Liquidity = None) -> float:
        return float(fill_price * qty * self.fee_rate(liquidity=liquidity))

    def identity(self) -> dict:
        return {"kind": self.kind, "slippage_bps": self.slippage_rate * 1e4,
                "maker_fee_rate": self.maker_fee_rate,
                "taker_fee_rate": self.taker_fee_rate,
                "default_liquidity": self.default_liquidity.value}


# ══════════════════════════ Live：真实交易所回报 ══════════════════════════

class ExchangeFillAdapter:
    """真实交易所成交适配器。

    **结构性禁令**：本类不实现「按 K 线推算成交价」。`quote_*` 一律抛
    `LiveExecutionForbidden` —— 于是即便有人误把 Live 接进 bar 循环，也会**立刻炸**，
    而不是悄悄用 K 线假装成交。

    唯一合法入口是 `submit(intent, ack=...)`：`ack` 必须来自交易所真实回报
    （order acknowledgement / fill / avgPrice / fee / funding / rejected / canceled）。
    """

    kind = EXCHANGE_ACK

    def __init__(self, client=None, *, maker_fee_rate: Optional[float] = None,
                 taker_fee_rate: Optional[float] = None):
        self.client = client
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self._acks: Dict[str, dict] = {}

    # —— 结构性拒绝：不得按 K 线假算 ——
    def quote_entry(self, *, raw_price: float, direction: int, price_step: float,
                    stop_price: float) -> FillQuote:
        raise LiveExecutionForbidden(
            "exchange_adapter_cannot_quote_from_bars: 真实成交价只能来自交易所回报")

    def quote_exit(self, *, target_px: float, direction: int,
                   price_step: float, reason: str) -> float:
        raise LiveExecutionForbidden(
            "exchange_adapter_cannot_quote_from_bars: 真实成交价只能来自交易所回报")

    def fee_rate(self, *, liquidity: Liquidity = None) -> float:
        liq = Liquidity(liquidity or Liquidity.TAKER)
        v = self.maker_fee_rate if liq == Liquidity.MAKER else self.taker_fee_rate
        if v is None:
            raise LiveExecutionForbidden("exchange_fee_rate_unset: 费率必须来自交易所账户")
        return float(v)

    # —— 唯一合法入口 ——
    def submit(self, intent, *, ack: Optional[dict] = None) -> Fill:
        """按交易所回报构造 Fill。`ack` 缺失或字段不全 ⇒ 拒绝（不猜、不补 0）。"""
        if self.client is None and ack is None:
            raise LiveExecutionForbidden(
                "live_client_absent: 未接入交易所客户端，拒绝任何成交推断")
        if not isinstance(ack, dict):
            raise TradingCoreError("exchange_ack_required")
        status = str(ack.get("status") or "").upper()
        try:
            st = OrderStatus(status)
        except ValueError as exc:
            raise TradingCoreError(f"unknown_exchange_status:{status}") from exc
        px = ack.get("avg_price", ack.get("fill_price"))
        qty = ack.get("filled_qty", ack.get("quantity"))
        if st in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            if px is None or qty is None:
                raise TradingCoreError("exchange_ack_missing_fill_fields")
            px, qty = float(px), float(qty)
            if not (math.isfinite(px) and px > 0 and math.isfinite(qty) and qty > 0):
                raise TradingCoreError("exchange_ack_nonpositive_fill")
        else:
            px, qty = float("nan"), 0.0
        fee = ack.get("fee")
        liq = Liquidity.MAKER if str(ack.get("liquidity", "")).lower() == "maker" \
            else Liquidity.TAKER
        return Fill(
            order_id=str(ack.get("order_id") or intent.order_id()),
            status=st, fill_price=px, quantity=qty,
            fee=(float(fee) if fee is not None else float("nan")),
            fee_rate=self.fee_rate(liquidity=liq), liquidity=liq,
            filled_at=(int(ack["filled_at"]) if ack.get("filled_at") is not None else None),
            reject_reason=str(ack.get("reject_reason") or ""),
            source=EXCHANGE_ACK,
            detail={"exchange_order_id": ack.get("order_id"),
                    "funding": ack.get("funding")})

    def identity(self) -> dict:
        return {"kind": self.kind,
                "client_attached": self.client is not None,
                "maker_fee_rate": self.maker_fee_rate,
                "taker_fee_rate": self.taker_fee_rate}


__all__ = [
    "FILL_CONTRACT_VERSION", "SIMULATED_BAR", "EXCHANGE_ACK",
    "round_buy_price", "round_sell_price", "round_qty_down",
    "FillQuote", "Fill", "FillAdapter",
    "SimulatedBarFillAdapter", "ExchangeFillAdapter",
]
