# -*- coding: utf-8 -*-
"""F_WX **真实 Freqtrade Order/Trade 复用适配器**（项目局部、隔离库、不改全局 vendor）。

目的（整改3）：上一轮（整改2）虽然创建了真实 `Order`/`Trade` 行，但把 `wx_execution` 已算好的
`net_pnl` **直接赋值**给 `Trade.close_profit_abs`（只持久化自制结果）。本轮改为**实际驱动
Freqtrade 官方订单更新/盈亏代码路径**：

  · 入场：建 `Trade`（`is_open=True`）+ 真实 `Order(buy, filled)`，调用
    **`Trade.update_trade(order)`**（`freqtrade/persistence/trade_model.py`）由 Freqtrade 自身
    依据订单设置 `open_rate` / `amount` 并 `recalc_trade_from_orders()`。
  · 资金费：`Trade.set_funding_fees(累计资金费)`（正=多头获费、负=多头付费，与 Freqtrade 一致）。
  · 出场：设 `Trade.fee_close`，建真实 `Order(sell, filled)`，再 `Trade.update_trade(order)`
    → Freqtrade 走 `Trade.close()` → `recalc_trade_from_orders(is_closing=True)` →
    `calculate_profit()` 由**真实订单价量 + 双边费 + 资金费**独立算出 `close_profit_abs`。

边界（诚实）：
  · **不修改全局 vendor**、不连交易所、不下单；库为调用方给定的**隔离临时库**。
  · 本适配器**不重算策略规则**（信号/择时/止损仍由 `wx_execution` 定型），但它产出的
    `Trade.close_profit_abs` 是 **Freqtrade 自己**从真实订单算出的，而非复制 `wx_execution`
    的结果；两者的一致性由 `pnl_checks()` 给出，差异即暴露。
  · 这**仍不是** Freqtrade 完整回测主引擎（未调用 `Backtesting` 的撮合/钱包/盘口/强平），
    只是**真实 Order/Trade 持久化 + 真实订单更新/盈亏代码路径**的局部隔离复用；
    诚实标记为 `freqtrade_order_trade_update_adapter`，**不**宣称完整 G04 主引擎对拍。
"""
from __future__ import annotations

from datetime import datetime, timezone

TOL = 1e-6


def _dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc)


class FreqtradeEngine:
    """把研究成交**驱动真实 Freqtrade Order/Trade 代码路径**的隔离适配器。

    用法：
        eng = FreqtradeEngine("/tmp/xxx/freqtrade_research.sqlite3")
        run_arm(..., ft_engine=eng)
        eng.trades()        # 读回真实 Trade/Order 行
        eng.pnl_checks()    # Freqtrade 自算 close_profit_abs vs wx_execution net_pnl
    """

    def __init__(self, db_path, *, strategy="F_WX", timeframe=15, fee_default=0.0005):
        # 延迟导入：未安装 Freqtrade 的环境（仅跑合成反例）不受影响。
        from freqtrade.persistence import models as ft_models
        from freqtrade.enums import TradingMode

        self.db_path = str(db_path)
        self.Trade = ft_models.Trade
        self.Order = ft_models.Order
        self.TradingMode = TradingMode
        self.strategy = strategy
        self.timeframe = int(timeframe)
        self.fee_default = float(fee_default)
        ft_models.init_db(f"sqlite:///{self.db_path}")
        self._open = {}          # position_id -> Trade
        self._entry_order = {}   # position_id -> Order
        self._seq = 0
        self._pnl_checks = []
        self.update_path_used = []   # 记录真实调用路径，供证据使用

    # ── 写：入场 / 资金费 / 出场 ────────────────────────────────────────
    def open_position(self, pos, ts, price, fee_cost, slip_cost):
        Trade, Order = self.Trade, self.Order
        self._seq += 1
        fee_rate = (fee_cost / (price * pos.quantity)) if (price and pos.quantity) else self.fee_default
        t = Trade(pair=f"{pos.symbol}/USDT:USDT", exchange=pos.exchange, is_short=False,
                  amount=float(pos.quantity), open_rate=float(price), open_date=_dt(ts),
                  fee_open=float(fee_rate), fee_open_cost=float(fee_cost),
                  stake_amount=float(pos.notional), leverage=1.0,
                  trading_mode=self.TradingMode.FUTURES, is_open=True,
                  base_currency=pos.symbol[:-4] if pos.symbol.endswith("USDT") else pos.symbol,
                  stake_currency="USDT", strategy=self.strategy, enter_tag="wx",
                  stop_loss=float(pos.stop_price), initial_stop_loss=float(pos.stop_price),
                  timeframe=self.timeframe, exit_reason=None)
        Trade.session.add(t)
        Trade.commit()
        o = Order(ft_trade_id=t.id, ft_order_side="buy", ft_pair=t.pair, ft_is_open=False,
                  ft_amount=float(pos.quantity), ft_price=float(price),
                  order_id=f"wx-entry-{self._seq}-{t.id}", status="closed",
                  order_type="market", side="buy", symbol=t.pair,
                  amount=float(pos.quantity), filled=float(pos.quantity), remaining=0.0,
                  average=float(price), cost=float(price) * float(pos.quantity),
                  order_date=_dt(ts), order_filled_date=_dt(ts))
        Order.session.add(o)
        Trade.commit()
        # 真实 Freqtrade 订单更新路径：由其自身设置 open_rate/amount 并重算开仓价值。
        t.update_trade(o)
        self.update_path_used.append(
            {"call": "Trade.update_trade(entry_order)", "trade_id": t.id,
             "open_rate": t.open_rate, "amount": t.amount,
             "open_trade_value": t.open_trade_value})
        self._open[pos.position_id] = t
        self._entry_order[pos.position_id] = o

    def close_position(self, pos, ts, price, fee_cost, slip_cost, reason):
        Trade, Order = self.Trade, self.Order
        t = self._open.get(pos.position_id)
        if t is None:
            return
        self._seq += 1
        fee_rate = (fee_cost / (price * pos.quantity)) if (price and pos.quantity) else self.fee_default
        # 双边费与资金费进入 Freqtrade 自身盈亏口径。
        t.fee_close = float(fee_rate)
        t.fee_close_cost = float(fee_cost)
        t.exit_reason = reason
        # 资金费：Freqtrade 约定 funding_fees 正=多头获费、负=多头付费；与 wx_execution 同号。
        t.set_funding_fees(float(pos.funding))
        Trade.commit()
        o = Order(ft_trade_id=t.id, ft_order_side="sell", ft_pair=t.pair, ft_is_open=False,
                  ft_amount=float(pos.quantity), ft_price=float(price),
                  order_id=f"wx-exit-{self._seq}-{t.id}", status="closed",
                  order_type="market", side="sell", symbol=t.pair,
                  amount=float(pos.quantity), filled=float(pos.quantity), remaining=0.0,
                  average=float(price), cost=float(price) * float(pos.quantity),
                  order_date=_dt(ts), order_filled_date=_dt(ts))
        Order.session.add(o)
        Trade.commit()
        # 真实 Freqtrade 出场路径：update_trade → close() → recalc_trade_from_orders(is_closing)
        # → calculate_profit()，由 Freqtrade 自身从真实订单算 close_profit_abs。
        t.update_trade(o)
        ft_pnl = t.close_profit_abs
        wx_pnl = pos.net_pnl
        diff = (None if (ft_pnl is None or wx_pnl is None) else abs(float(ft_pnl) - float(wx_pnl)))
        self._pnl_checks.append({
            "position_id": pos.position_id, "symbol": pos.symbol, "trade_id": t.id,
            "ft_close_profit_abs": ft_pnl, "wx_net_pnl": wx_pnl,
            "abs_diff": diff, "within_tol": (diff is not None and diff <= TOL),
            "ft_funding_fees": t.funding_fees, "ft_fee_open": t.fee_open,
            "ft_fee_close": t.fee_close, "exit_reason": reason,
            "computed_by": "freqtrade.Trade.recalc_trade_from_orders/calculate_profit"})
        self.update_path_used.append(
            {"call": "Trade.update_trade(exit_order)", "trade_id": t.id,
             "is_open": bool(t.is_open), "close_profit_abs": ft_pnl,
             "funding_fees": t.funding_fees})

    # ── 读：真实 Trade / Order 行 ───────────────────────────────────────
    def trades(self):
        rows = []
        for t in self.Trade.session.query(self.Trade).order_by(self.Trade.id).all():
            rows.append({
                "ft_trade_id": t.id, "pair": t.pair, "exchange": t.exchange,
                "is_open": bool(t.is_open), "amount": t.amount,
                "open_rate": t.open_rate, "close_rate": t.close_rate,
                "open_date": t.open_date, "close_date": t.close_date,
                "fee_open": t.fee_open, "fee_close": t.fee_close,
                "fee_open_cost": t.fee_open_cost, "fee_close_cost": t.fee_close_cost,
                "exit_reason": t.exit_reason, "stop_loss": t.stop_loss,
                "leverage": t.leverage, "trading_mode": str(t.trading_mode),
                "realized_profit": t.realized_profit,
                "close_profit_abs": t.close_profit_abs,
                "funding_fees": t.funding_fees,
                "enter_tag": t.enter_tag, "strategy": t.strategy, "timeframe": t.timeframe,
            })
        return rows

    def orders(self):
        rows = []
        for o in self.Order.session.query(self.Order).order_by(self.Order.id).all():
            rows.append({
                "ft_order_id": o.id, "ft_trade_id": o.ft_trade_id, "order_id": o.order_id,
                "ft_order_side": o.ft_order_side, "status": o.status, "amount": o.amount,
                "filled": o.filled, "average": o.average, "cost": o.cost,
                "order_date": o.order_date, "order_filled_date": o.order_filled_date,
            })
        return rows

    def pnl_checks(self):
        """Freqtrade 自算 PnL 与 wx_execution net_pnl 的逐笔对照。"""
        return list(self._pnl_checks)

    def version(self):
        try:
            import freqtrade
            return getattr(freqtrade, "__version__", "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"
