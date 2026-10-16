# -*- coding: utf-8 -*-
"""Shared Trading Core · 主循环（施工单 W2-6 / W2-9）。

一段话定位
──────────
本模块是 Backtest / Paper / Live **共享的同一份**交易循环：
`Signal → Risk → Order → ExecutionPolicy → FillAdapter → Fill → Position → Trade → LedgerEvent`。

策略无关性
──────────
信号生成通过 `signal_fn` 注入（Backtest 用历史 bar，Paper/Live 用实时 bar），
成交通过 `fill_adapter` 注入（Backtest/Paper 用 `SimulatedBarFillAdapter`，
Live 用 `ExchangeFillAdapter`）。本模块自身**不 import 任何具体策略**。

相位顺序（因果，不可换）
────────────────────────
  A 开盘：锁利生效 → 资金费结算
  B 开盘：跳空退出（**只用 open**）
  C 开盘：新开仓（取整后成交价 → 再算风险/止损）
  D bar 内：止损（**含当根新开仓**）
  E 收盘：盯市与权益
  EOD：期末（由 `close_open_positions_at_end` 显式决定，见 W2-3）

本模块的数值算术**整体搬移**自 `oi_resonance_v1/execution.py`，
并由改造前基线（`evidence/Work_策略核心收敛_2026-10-16/基线_改造前.json`）逐位校验。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .contracts import (ExecPhase, Liquidity, OrderStatus, PositionSide, Side,
                        stable_hash)
from .fill import FillAdapter, SimulatedBarFillAdapter
from .funding import funding_events_ms, settle_funding
from .ledger import (FEE_CHARGED, FUNDING_FEE, GENESIS_INIT, ORDER_CREATED,
                     ORDER_FILLED, ORDER_SUBMITTED, POSITION_CLOSED,
                     POSITION_OPENED, SIGNAL_ACCEPTED, SIGNAL_REJECTED,
                     LedgerEvent, LedgerWriter)
from .order import Order, OrderRegistry
from .policy import (ACTION_EXIT_EOD, ACTION_EXIT_GAP, ACTION_EXIT_STOP,
                     EXIT_EOD, EXIT_GAP, EXIT_INTRABAR, ExecutionPolicy,
                     EntryDecision)
from .position import Position, unrealized_net_open, unrealized_pnl

ENGINE_CONTRACT_VERSION = "trading_engine_v1.0.0"

SIG_NONE = 0


# ══════════════════════════ 单币数组装配 ══════════════════════════

def prep_symbol(sym: str, sig_bars: pd.DataFrame, bar_bars: pd.DataFrame,
                params, unit: str, price_step: float, signal_fn: Callable,
                to_epoch_seconds: Callable,
                trade_start_sec: Optional[int] = None) -> dict:
    """装配单币数组。信号由注入的 `signal_fn` 现算（不信任「已算好帧」）。"""
    from .contracts import ExecutionInputError

    if bar_bars is None or len(bar_bars) == 0:
        raise ExecutionInputError(f"missing_intrabar_bars:{sym}")
    for c in ("timestamp", "open", "high", "low", "close"):
        if c not in bar_bars.columns:
            raise ExecutionInputError(f"missing_intrabar_column:{sym}:{c}")

    sig = signal_fn(sig_bars, params, unit, price_step)
    t15_raw = bar_bars["timestamp"].to_numpy(np.int64)
    t15_sec = to_epoch_seconds(t15_raw, unit)

    o15 = bar_bars["open"].to_numpy(np.float64)
    h15 = bar_bars["high"].to_numpy(np.float64)
    l15 = bar_bars["low"].to_numpy(np.float64)
    c15 = bar_bars["close"].to_numpy(np.float64)
    if not all(np.all(np.isfinite(x)) for x in (o15, h15, l15, c15)):
        raise ExecutionInputError(f"non_finite_intrabar_ohlc:{sym}")

    n15 = len(t15_raw)
    pend_sig = np.zeros(n15, np.int64)
    pend_dir = np.zeros(n15, np.int64)
    pend_stop = np.full(n15, np.nan, np.float64)
    pend_res = np.full(n15, np.nan, np.float64)
    pend_sig_bar = np.full(n15, -1, np.int64)
    pend_sig_id = np.array([""] * n15, dtype=object)
    conf_exit_low = np.full(n15, np.nan, np.float64)
    conf_exit_high = np.full(n15, np.nan, np.float64)

    if len(sig):
        tsig_confirm = sig["confirm_time"].to_numpy(np.int64)
        sig_bar_ms = sig["timestamp"].to_numpy(np.int64)
        s_arr = sig["signal"].to_numpy(np.int64)
        d_arr = sig["direction"].to_numpy(np.int64)
        st_arr = sig["stop_price"].to_numpy(np.float64)
        sc_col = "oi_resonance_score"
        rs_arr = (sig[sc_col].to_numpy(np.float64) if sc_col in sig.columns
                  else np.full(len(sig), np.nan, np.float64))
        lo_col = "exit_low_curr" if "exit_low_curr" in sig.columns else "exit_low_prev"
        hi_col = "exit_high_curr" if "exit_high_curr" in sig.columns else "exit_high_prev"
        el_arr = sig[lo_col].to_numpy(np.float64)
        eh_arr = sig[hi_col].to_numpy(np.float64)
        idx = np.searchsorted(t15_sec, tsig_confirm, side="left")
        for k in range(len(tsig_confirm)):
            j = int(idx[k])
            if j >= n15:
                continue
            # 预热不交易：`confirm_time < trade_start_sec` 的信号与通道值一律丢弃。
            if trade_start_sec is not None and int(tsig_confirm[k]) < int(trade_start_sec):
                continue
            if s_arr[k] != SIG_NONE:
                pend_sig[j] = s_arr[k]
                pend_dir[j] = d_arr[k]
                pend_stop[j] = st_arr[k]
                pend_res[j] = rs_arr[k]
                pend_sig_bar[j] = int(sig_bar_ms[k])
                pend_sig_id[j] = f"{sym}|{int(sig_bar_ms[k])}|{int(s_arr[k])}"
            conf_exit_low[j] = el_arr[k]
            conf_exit_high[j] = eh_arr[k]

    return {
        "symbol": sym,
        "timestamp": t15_raw,
        "timestamp_sec": t15_sec,
        "open": o15, "high": h15, "low": l15, "close": c15,
        "funding_rate": (bar_bars["funding_rate"].to_numpy(np.float64)
                         if "funding_rate" in bar_bars.columns
                         else np.full(n15, np.nan, np.float64)),
        "pending_sig": pend_sig, "pending_dir": pend_dir,
        "pending_stop": pend_stop, "pending_res": pend_res,
        "pending_sig_bar": pend_sig_bar, "pending_sig_id": pend_sig_id,
        "exit_low": pd.Series(conf_exit_low).ffill().to_numpy(np.float64),
        "exit_high": pd.Series(conf_exit_high).ffill().to_numpy(np.float64),
        "exit_channel_source": (lo_col if len(sig) else None),
        "signal_frame": sig,
    }


# ══════════════════════════ 主循环 ══════════════════════════

class TradingCore:
    """共享交易核心：组合 `ExecutionPolicy` + `FillAdapter` + 账本。"""

    def __init__(self, *, policy: ExecutionPolicy, fill_adapter: FillAdapter,
                 signal_fn: Callable, to_epoch_seconds: Callable,
                 strategy_key: str, strategy_id: str, strategy_version: str,
                 timeframe: str = "1h", exec_timeframe: str = "15m",
                 ledger: Optional[LedgerWriter] = None,
                 collect_contracts: bool = True):
        self.policy = policy
        self.adapter = fill_adapter
        self.signal_fn = signal_fn
        self.to_epoch_seconds = to_epoch_seconds
        self.strategy_key = strategy_key
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version
        self.timeframe = timeframe
        self.exec_timeframe = exec_timeframe
        self.ledger = ledger
        self.collect_contracts = collect_contracts
        self.registry = OrderRegistry()
        self._orders: List[Order] = []
        self._ledger_counts: Dict[str, int] = {}

    # ── 账本 ──
    def _emit(self, event_type: str, *, symbol: str, event_time: Optional[int],
              payload: dict) -> None:
        self._ledger_counts[event_type] = self._ledger_counts.get(event_type, 0) + 1
        if self.ledger is not None:
            self.ledger.emit(LedgerEvent.build(
                event_type, strategy_key=self.strategy_key, symbol=symbol,
                event_time=event_time, payload=payload,
                sequence=self._ledger_counts[event_type] - 1))

    # ── 发单：意图 → 订单 → 成交价 ──
    def _submit(self, intent, *, ts: int) -> Tuple[Order, bool]:
        res = self.registry.submit(intent, created_at=ts)
        o = res.order
        if not res.is_duplicate:
            self._orders.append(o)
            self._emit(ORDER_CREATED, symbol=o.symbol, event_time=ts,
                       payload={"order_id": o.order_id, "action": o.action,
                                "phase": o.phase, "signal_id": o.signal_id,
                                "quantity": o.quantity,
                                "client_order_id": o.client_order_id})
            o.transition(OrderStatus.SUBMITTED, at=ts)
            self._emit(ORDER_SUBMITTED, symbol=o.symbol, event_time=ts,
                       payload={"order_id": o.order_id, "action": o.action})
        return o, res.is_duplicate

    def _fill_order(self, o: Order, *, price: float, qty: float, fee: float,
                    ts: int, liquidity: Liquidity, detail: Optional[dict] = None) -> None:
        o.transition(OrderStatus.FILLED, at=ts)
        o.fill_price = float(price)
        o.fee = float(fee)
        o.liquidity = Liquidity(liquidity)
        self._emit(ORDER_FILLED, symbol=o.symbol, event_time=ts,
                   payload={"order_id": o.order_id, "action": o.action,
                            "fill_price": float(price), "quantity": float(qty),
                            "fee": float(fee), "liquidity": o.liquidity.value,
                            "detail": dict(detail or {})})

    # ── 主入口 ──
    def run(self, market_data: Dict[str, dict], *,
            time_unit=None, trade_start_sec: Optional[int] = None,
            close_open_positions_at_end: Optional[bool] = None) -> Dict[str, Any]:
        from .contracts import ExecutionInputError, LiveExecutionForbidden

        p = self.policy.p
        cfg = self.policy.cfg
        if getattr(cfg, "live_order_enabled", False):
            raise LiveExecutionForbidden("live_order_enabled_must_be_false")
        if not market_data:
            return self._empty_result(trade_start_sec, close_open_positions_at_end)

        declared = {s: (d.get("time_unit") or time_unit)
                    for s, d in market_data.items()}
        unit = _assert_single_time_unit(declared)

        slip = float(cfg.slippage_bps) * 1e-4
        taker_fee = float(getattr(cfg, "taker_fee_rate", cfg.fee_rate))
        maker_fee = float(getattr(cfg, "maker_fee_rate", 0.0002))
        fee = taker_fee
        step = float(p.price_step)
        enable_trail = bool(getattr(cfg, "enable_trailing_exit", True))
        funding_is_assumption = (cfg.funding_model == "assumption_fixed_8h")

        arrays: Dict[str, dict] = {}
        for sym in sorted(market_data):
            d = market_data[sym]
            if "signal_bars" not in d or d["signal_bars"] is None:
                raise ExecutionInputError(f"missing_signal_bars:{sym}")
            arrays[sym] = prep_symbol(sym, d["signal_bars"], d.get("intrabar_bars"),
                                      p, unit, step, self.signal_fn,
                                      self.to_epoch_seconds, trade_start_sec)

        timeline = np.unique(np.concatenate([arrays[s]["timestamp"] for s in arrays]))
        cursors = {s: 0 for s in arrays}
        lengths = {s: len(arrays[s]["timestamp"]) for s in arrays}

        cash = float(cfg.initial_equity)
        self._emit(GENESIS_INIT, symbol="*", event_time=(int(timeline[0])
                   if len(timeline) else None),
                   payload={"initial_equity": cash,
                            "funding_model": cfg.funding_model,
                            "fee_model": {"maker": maker_fee, "taker": taker_fee},
                            "slippage_bps": float(cfg.slippage_bps)})

        open_positions: Dict[str, Position] = {}
        closed: List[dict] = []
        decisions: List[dict] = []
        rejected: List[dict] = []
        equity_curve: List[dict] = []
        funding_gaps_all: List[dict] = []
        sum_gross = sum_entry_fee = sum_exit_fee = 0.0

        for t_now in timeline:
            active = []
            for sym, arr in arrays.items():
                i = cursors[sym]
                if i < lengths[sym] and int(arr["timestamp"][i]) == int(t_now):
                    active.append((sym, i))
                    cursors[sym] = i + 1
            active.sort()
            if not active:
                continue

            # ── 相位 A：开盘（锁利生效 → 资金费结算）──
            for sym, i in active:
                pos = open_positions.get(sym)
                if pos is None:
                    continue
                arr = arrays[sym]
                pos.last_mark_price = float(arr["open"][i])
                if enable_trail:
                    lvl = self.policy.trail_level(
                        direction=pos.direction, exit_low=float(arr["exit_low"][i]),
                        exit_high=float(arr["exit_high"][i]),
                        current_stop=pos.current_stop)
                    if lvl is not None:
                        pos.current_stop = float(lvl)
                        pos.stop_trace.append({"ts": int(arr["timestamp"][i]),
                                               "stop": float(lvl), "reason": "trail"})
                evs = funding_events_ms(market_data[sym].get("funding_events"),
                                        market_data[sym].get("funding_time_unit"))
                before = cash
                cash = settle_funding(
                    pos, cash, model=cfg.funding_model, t_ms=int(arr["timestamp"][i]),
                    open_px=float(arr["open"][i]), rate=arr["funding_rate"][i],
                    events=evs, funding_is_assumption=funding_is_assumption)
                if cash != before:
                    self._emit(FUNDING_FEE, symbol=sym, event_time=int(arr["timestamp"][i]),
                               payload={"delta": float(cash - before),
                                        "cum_funding_fee": float(pos.cum_funding_fee)})

            # ── 相位 B：跳空退出（**只用 open**）──
            for sym, i in active:
                pos = open_positions.get(sym)
                if pos is None:
                    continue
                arr = arrays[sym]
                op = float(arr["open"][i])
                dec = self.policy.decide_gap_exit(pos=pos, bar_open=op, symbol=sym)
                if not dec.should_exit:
                    continue
                fill = self.adapter.quote_exit(target_px=dec.target_px,
                                               direction=pos.direction,
                                               price_step=step, reason=dec.reason)
                intent = self._exit_intent(pos, action=ACTION_EXIT_GAP,
                                           phase=ExecPhase.B_GAP.value,
                                           reason=dec.reason, symbol=sym,
                                           target=dec.target_px, ts=int(t_now))
                o, dup = self._submit(intent, ts=int(t_now))
                if dup:
                    continue
                exit_fee = self.adapter.fill_fee(fill, pos.qty)
                cash, rec = _close_position(pos, cash, fill, exit_fee, fee, int(t_now),
                                            dec.reason, open_positions, sym,
                                            raw_target_px=op, order=o)
                self._fill_order(o, price=fill, qty=pos.qty, fee=exit_fee,
                                 ts=int(t_now), liquidity=Liquidity.TAKER,
                                 detail={"reason": dec.reason})
                self._emit(POSITION_CLOSED, symbol=sym, event_time=int(t_now),
                           payload={"trade_id": _trade_id(rec),
                                    "exit_reason": dec.reason,
                                    "net_pnl": rec["net_pnl"]})
                self._emit(FEE_CHARGED, symbol=sym, event_time=int(t_now),
                           payload={"side": "exit", "fee": float(exit_fee)})
                closed.append(rec)
                sum_gross += rec["gross_pnl"]
                sum_exit_fee += rec["exit_fee"]
                decisions.append({"timestamp": int(t_now), "symbol": sym,
                                  "phase": "B_open_gap", "action": "exit",
                                  "reason": dec.reason, "price": fill})

            # ── 相位 C：新开仓（用本 bar 的 open；取整后再算风险/止损）──
            if bool(getattr(cfg, "allow_new_entries", True)):
                cands = []
                for sym, i in active:
                    arr = arrays[sym]
                    if arr["pending_sig"][i] != SIG_NONE:
                        r = arr["pending_res"][i]
                        cands.append((self.policy.entry_priority(
                            float(r) if np.isfinite(r) else None), sym, i))
                cands.sort(key=lambda x: (x[0], x[1]))
                for _, sym, i in cands:
                    arr = arrays[sym]
                    direction = int(arr["pending_dir"][i])
                    sig_type = int(arr["pending_sig"][i])
                    raw_open = float(arr["open"][i])
                    stop_ref = float(arr["pending_stop"][i])
                    sig_id = str(arr["pending_sig_id"][i]) or _signal_id(
                        sym, int(arr["pending_sig_bar"][i]), sig_type)
                    score = float(arr["pending_res"][i])
                    score = score if math.isfinite(score) else None
                    quote = self.adapter.quote_entry(raw_price=raw_open,
                                                     direction=direction,
                                                     price_step=step,
                                                     stop_price=stop_ref)
                    mtm = cash + unrealized_pnl(open_positions, fee)
                    seq = self.registry.next_sequence(self.strategy_key, sym, sig_id)
                    dec = self.policy.decide_entry(
                        symbol=sym, signal_type=sig_type, direction=direction,
                        raw_price=raw_open, stop_ref=stop_ref, quote=quote,
                        positions=open_positions, cash=cash, equity_mtm=mtm,
                        signal_id=sig_id, order_sequence=seq,
                        signal_bar_open_ms=int(arr["pending_sig_bar"][i]),
                        signal_score=score)
                    if not dec.accepted:
                        rejected.append({"timestamp": int(t_now), "symbol": sym,
                                         "phase": "C_entry", "reason": dec.reason})
                        self._emit(SIGNAL_REJECTED, symbol=sym, event_time=int(t_now),
                                   payload={"signal_id": sig_id, "reason": dec.reason,
                                            "phase": "C_entry"})
                        continue
                    entry_fill = float(quote.entry_fill)
                    o, dup = self._submit(dec.intent, ts=int(t_now))
                    if dup:
                        continue
                    e_fee = self.adapter.fill_fee(entry_fill, dec.intent.quantity)
                    cash -= e_fee
                    sum_entry_fee += e_fee
                    entry_slip_loss = abs(entry_fill - raw_open) * dec.intent.quantity
                    pos = Position(
                        symbol=sym, direction=direction, signal_type=sig_type,
                        entry_time=int(t_now), entry_time_sec=int(arr["timestamp_sec"][i]),
                        entry_price=entry_fill, qty=dec.intent.quantity,
                        initial_stop=stop_ref, current_stop=stop_ref,
                        entry_fee=e_fee, last_mark_price=entry_fill,
                        entry_slip_loss=entry_slip_loss,
                        last_funding_ms=int(arr["timestamp"][i]),
                        order_id=o.order_id, signal_id=sig_id,
                        signal_bar_open_ms=int(arr["pending_sig_bar"][i]))
                    open_positions[sym] = pos
                    if enable_trail:
                        lvl = self.policy.trail_level(
                            direction=direction, exit_low=float(arr["exit_low"][i]),
                            exit_high=float(arr["exit_high"][i]), current_stop=stop_ref)
                        if lvl is not None:
                            pos.current_stop = float(lvl)
                            pos.stop_trace.append({"ts": int(arr["timestamp"][i]),
                                                   "stop": float(lvl), "reason": "trail"})
                    pos.stop_trace.insert(0, {"ts": int(arr["timestamp"][i]),
                                              "stop": float(stop_ref), "reason": "init"})
                    self._fill_order(o, price=entry_fill, qty=dec.intent.quantity,
                                     fee=e_fee, ts=int(t_now), liquidity=Liquidity.TAKER,
                                     detail={"signal_id": sig_id})
                    self._emit(POSITION_OPENED, symbol=sym, event_time=int(t_now),
                               payload={"order_id": o.order_id, "signal_id": sig_id,
                                        "entry_price": entry_fill,
                                        "qty": dec.intent.quantity,
                                        "stop_price": stop_ref})
                    self._emit(FEE_CHARGED, symbol=sym, event_time=int(t_now),
                               payload={"side": "entry", "fee": float(e_fee)})
                    self._emit(SIGNAL_ACCEPTED, symbol=sym, event_time=int(t_now),
                               payload={"signal_id": sig_id, "signal_type": sig_type,
                                        "direction": direction,
                                        "oi_resonance_score": score})
                    decisions.append({"timestamp": int(t_now), "symbol": sym,
                                      "phase": "C_entry", "action": "entry",
                                      "signal_type": sig_type,
                                      "direction": direction, "price": entry_fill,
                                      "stop_price": stop_ref,
                                      "qty": dec.intent.quantity,
                                      "entry_fee": e_fee,
                                      "signal_bar_open_ms": int(arr["pending_sig_bar"][i])})

            # ── 相位 D：bar 内止损（**含当根新开仓**）──
            for sym, i in active:
                pos = open_positions.get(sym)
                if pos is None:
                    continue
                arr = arrays[sym]
                hi, lo, cl = (float(arr["high"][i]), float(arr["low"][i]),
                              float(arr["close"][i]))
                if pos.direction == 1:
                    pos.mfe = max(pos.mfe, (hi - pos.entry_price) / pos.entry_price)
                    pos.mae = max(pos.mae, (pos.entry_price - lo) / pos.entry_price)
                else:
                    pos.mfe = max(pos.mfe, (pos.entry_price - lo) / pos.entry_price)
                    pos.mae = max(pos.mae, (hi - pos.entry_price) / pos.entry_price)
                dec = self.policy.decide_intrabar_exit(pos=pos, high=hi, low=lo, symbol=sym)
                if dec.should_exit:
                    fill = self.adapter.quote_exit(target_px=dec.target_px,
                                                   direction=pos.direction,
                                                   price_step=step, reason=dec.reason)
                    intent = self._exit_intent(pos, action=ACTION_EXIT_STOP,
                                               phase=ExecPhase.D_INTRABAR.value,
                                               reason=dec.reason, symbol=sym,
                                               target=dec.target_px, ts=int(t_now))
                    o, dup = self._submit(intent, ts=int(t_now))
                    if dup:
                        continue
                    exit_fee = self.adapter.fill_fee(fill, pos.qty)
                    cash, rec = _close_position(pos, cash, fill, exit_fee, fee,
                                                int(t_now), dec.reason, open_positions,
                                                sym, raw_target_px=pos.current_stop,
                                                order=o)
                    self._fill_order(o, price=fill, qty=pos.qty, fee=exit_fee,
                                     ts=int(t_now), liquidity=Liquidity.TAKER,
                                     detail={"reason": dec.reason})
                    self._emit(POSITION_CLOSED, symbol=sym, event_time=int(t_now),
                               payload={"trade_id": _trade_id(rec),
                                        "exit_reason": dec.reason,
                                        "net_pnl": rec["net_pnl"]})
                    self._emit(FEE_CHARGED, symbol=sym, event_time=int(t_now),
                               payload={"side": "exit", "fee": float(exit_fee)})
                    closed.append(rec)
                    sum_gross += rec["gross_pnl"]
                    sum_exit_fee += rec["exit_fee"]
                    decisions.append({"timestamp": int(t_now), "symbol": sym,
                                      "phase": "D_intrabar", "action": "exit",
                                      "reason": dec.reason, "price": fill})
                else:
                    pos.last_mark_price = cl

            # ── 相位 E：盯市与权益 ──
            un = unrealized_pnl(open_positions, fee)
            equity = cash + un
            equity_curve.append({
                "timestamp": int(t_now), "cash_equity": cash, "unrealized_pnl": un,
                "total_equity": equity, "open_positions_count": len(open_positions),
                "open_risk_pct": (_portfolio_open_risk(open_positions, fee, slip) / equity
                                  if equity > 0 else 0.0),
            })

        # ── 期末（W2-3：由 close_open_positions_at_end 显式决定）──
        last_ts = int(timeline[-1]) if len(timeline) else None
        close_at_end = self.policy.close_open_positions_at_end(close_open_positions_at_end)
        if close_at_end and open_positions:
            for sym in sorted(list(open_positions)):
                pos = open_positions[sym]
                arr = arrays[sym]
                last_close = float(arr["close"][-1])
                dec = self.policy.decide_eod_exit(pos=pos, last_close=last_close, symbol=sym)
                fill = self.adapter.quote_exit(target_px=dec.target_px,
                                               direction=pos.direction,
                                               price_step=step, reason=dec.reason)
                intent = self._exit_intent(pos, action=ACTION_EXIT_EOD,
                                           phase=ExecPhase.EOD.value,
                                           reason=dec.reason, symbol=sym,
                                           target=dec.target_px, ts=int(last_ts))
                o, dup = self._submit(intent, ts=int(last_ts))
                exit_fee = self.adapter.fill_fee(fill, pos.qty)
                cash, rec = _close_position(pos, cash, fill, exit_fee, fee, int(last_ts),
                                            dec.reason, open_positions, sym,
                                            raw_target_px=last_close, order=o)
                self._fill_order(o, price=fill, qty=pos.qty, fee=exit_fee,
                                 ts=int(last_ts), liquidity=Liquidity.TAKER,
                                 detail={"reason": dec.reason})
                self._emit(POSITION_CLOSED, symbol=sym, event_time=int(last_ts),
                           payload={"trade_id": _trade_id(rec),
                                    "exit_reason": dec.reason,
                                    "net_pnl": rec["net_pnl"]})
                self._emit(FEE_CHARGED, symbol=sym, event_time=int(last_ts),
                           payload={"side": "exit", "fee": float(exit_fee)})
                closed.append(rec)
                sum_gross += rec["gross_pnl"]
                sum_exit_fee += rec["exit_fee"]
                decisions.append({"timestamp": int(last_ts), "symbol": sym,
                                  "phase": "EOD", "action": "force_close_eod",
                                  "reason": dec.reason, "price": fill})
            if equity_curve:
                equity_curve[-1].update({"cash_equity": cash, "unrealized_pnl": 0.0,
                                         "total_equity": cash, "open_positions_count": 0,
                                         "open_risk_pct": 0.0})

        un_final = unrealized_pnl(open_positions, fee)
        final_equity = cash + un_final
        if equity_curve:
            equity_curve[-1]["cash_equity"] = cash
            equity_curve[-1]["unrealized_pnl"] = un_final
            equity_curve[-1]["total_equity"] = final_equity
            equity_curve[-1]["open_positions_count"] = len(open_positions)

        for pos in open_positions.values():
            funding_gaps_all.extend({"symbol": pos.symbol, **g} for g in pos.funding_gaps)
        for rec in closed:
            for g in rec.get("funding_gaps") or []:
                funding_gaps_all.append({"symbol": rec["symbol"], **g})

        net_closed = sum(r["net_pnl"] for r in closed)
        un_net_open = unrealized_net_open(open_positions, fee)
        sum_slip = float(sum(r.get("slippage_loss", 0.0) for r in closed))
        total_funding = float(sum(r["funding_fee"] for r in closed)
                              + sum(p.cum_funding_fee for p in open_positions.values()))
        recon = {
            "initial_equity": float(cfg.initial_equity),
            "final_cash": float(cash),
            "sum_gross_pnl": float(sum_gross),
            "sum_entry_fee": float(sum_entry_fee),
            "sum_exit_fee": float(sum_exit_fee),
            "sum_fees": float(sum_entry_fee + sum_exit_fee),
            "sum_slippage_loss": sum_slip,
            "sum_funding_fee": total_funding,
            "funding_settlement_model": cfg.funding_model,
            "fee_model": {"tier": "Binance_VIP0", "maker_fee_rate": maker_fee,
                          "taker_fee_rate": taker_fee},
            "risk_model": {
                "risk_per_trade": float(cfg.risk_per_trade),
                "max_concurrent_trades": int(getattr(cfg, "max_concurrent_trades", 3)),
                "max_portfolio_margin_pct": float(getattr(cfg,
                                                          "max_portfolio_margin_pct", 0.40)),
                "max_leverage": float(cfg.max_leverage_per_symbol),
            },
            "sum_net_pnl_closed": float(net_closed),
            "unrealized_net_open": float(un_net_open),
            "final_equity": float(final_equity),
            "expected_cash": float(cfg.initial_equity + sum_gross - sum_entry_fee
                                   - sum_exit_fee - total_funding),
            "cash_identity_ok": abs(cash - (cfg.initial_equity + sum_gross - sum_entry_fee
                                            - sum_exit_fee - total_funding)) < 1e-6,
            "equity_identity_ok": abs((final_equity - cfg.initial_equity)
                                      - (net_closed + un_net_open)) < 1e-6,
            "equity_curve_tail_matches_final": (not equity_curve) or
                abs(equity_curve[-1]["total_equity"] - final_equity) < 1e-9,
        }

        out = {
            "strategy_id": self.strategy_id,
            "time_unit": unit,
            "trade_start_sec": (None if trade_start_sec is None else int(trade_start_sec)),
            "funding_model": cfg.funding_model,
            "funding_is_assumption": bool(funding_is_assumption),
            "initial_equity": float(cfg.initial_equity),
            "final_cash": float(cash),
            "final_equity": float(final_equity),
            "open_positions_remaining": sorted(open_positions),
            "net_return_pct": ((final_equity - cfg.initial_equity) / cfg.initial_equity
                               * 100.0 if cfg.initial_equity else None),
            "trades_count": len(closed),
            "trades": closed,
            "decisions": decisions,
            "rejected_orders": rejected,
            "equity_curve": equity_curve,
            "funding_gaps": funding_gaps_all,
            "reconcile": recon,
            "max_drawdown_pct": _max_dd(equity_curve),
            # W2-3：段末强平的显式 Runtime 选项（与并行施工窗口的契约逐字一致）
            "close_open_positions_at_end": bool(close_at_end),
            "close_open_positions_at_end_source":
                self.policy.close_at_end_source(close_open_positions_at_end),
        }
        if self.collect_contracts:
            out["trading_core"] = self.contract_block()
        return out

    # ── 契约产物（加性；不参与既有字段的数值判据）──
    def contract_block(self) -> dict:
        return {
            "engine_contract": ENGINE_CONTRACT_VERSION,
            "policy": self.policy.identity(),
            "fill_adapter": (self.adapter.identity()
                             if hasattr(self.adapter, "identity") else
                             {"kind": getattr(self.adapter, "kind", "unknown")}),
            "orders": [o.to_dict() for o in self._orders],
            "orders_count": len(self._orders),
            "registry_unique_keys": len(self.registry),
            "ledger_event_counts": dict(sorted(self._ledger_counts.items())),
            "ledger_events_total": int(sum(self._ledger_counts.values())),
        }

    def _exit_intent(self, pos: Position, *, action: str, phase: str, reason: str,
                     symbol: str, target: float, ts: int):
        from .order import OrderIntent
        ps = PositionSide.LONG if pos.direction == 1 else PositionSide.SHORT
        side = Side.SELL if pos.direction == 1 else Side.BUY
        sig_id = pos.signal_id or f"{symbol}|exit"
        seq = self.registry.next_sequence(self.strategy_key, symbol, f"{sig_id}:{action}")
        return OrderIntent(
            strategy_key=self.strategy_key, symbol=symbol, side=side,
            position_side=ps, order_type=self.policy.exit_order_type(),
            quantity=float(pos.qty), action=action, phase=phase,
            signal_id=sig_id, order_sequence=seq,
            stop_price=float(pos.current_stop), reference_price=float(target),
            reason=reason, extra={"exit_target_px": float(target)})

    def _empty_result(self, trade_start_sec: Optional[int] = None,
                      close_open_positions_at_end: Optional[bool] = None) -> dict:
        cfg = self.policy.cfg
        close_at_end = self.policy.close_open_positions_at_end(close_open_positions_at_end)
        out = {
            "strategy_id": self.strategy_id, "time_unit": None,
            "trade_start_sec": (None if trade_start_sec is None else int(trade_start_sec)),
            "funding_model": cfg.funding_model,
            "funding_is_assumption": (cfg.funding_model == "assumption_fixed_8h"),
            "initial_equity": float(cfg.initial_equity),
            "final_cash": float(cfg.initial_equity),
            "final_equity": float(cfg.initial_equity),
            "open_positions_remaining": [], "net_return_pct": 0.0,
            "trades_count": 0, "trades": [], "decisions": [], "rejected_orders": [],
            "equity_curve": [], "funding_gaps": [],
            "max_drawdown_pct": 0.0,
            "reconcile": {"initial_equity": float(cfg.initial_equity),
                          "final_cash": float(cfg.initial_equity),
                          "final_equity": float(cfg.initial_equity),
                          "cash_identity_ok": True, "equity_identity_ok": True,
                          "equity_curve_tail_matches_final": True},
            "close_open_positions_at_end": bool(close_at_end),
            "close_open_positions_at_end_source":
                self.policy.close_at_end_source(close_open_positions_at_end),
        }
        if self.collect_contracts:
            out["trading_core"] = self.contract_block()
        return out


# ══════════════════════════ 内部工具 ══════════════════════════

def _signal_id(sym: str, bar_ms: int, sig_type: int) -> str:
    return f"{sym}|{int(bar_ms)}|{int(sig_type)}"



def _trade_id(rec: dict) -> str:
    """账本事件用的确定性 trade_id（**不写入 `trades` 输出**，故不改变既有结果形状）。

    与 `backtest_store.new_trade_id` 的 run-scoped ID 不同：这里只用于账本事件内部联结。
    """
    return "trd_" + stable_hash(rec.get("symbol"), int(rec.get("entry_time", -1)),
                                int(rec.get("exit_time", -1)),
                                rec.get("exit_reason", ""), length=24)


def _assert_single_time_unit(units_by_symbol: Dict[str, Any]) -> str:
    from .contracts import ExecutionInputError
    declared = {str(s): _resolve_unit(u) for s, u in units_by_symbol.items()}
    if not declared:
        raise ExecutionInputError("no_symbol_time_unit_declared")
    uniq = set(declared.values())
    if len(uniq) > 1:
        raise ExecutionInputError(f"mixed_time_units_across_symbols:{declared}")
    return next(iter(uniq))


def _resolve_unit(u) -> str:
    from .contracts import ExecutionInputError
    if u is None:
        raise ExecutionInputError("time_unit_undeclared")
    s = str(u).strip().lower()
    if s not in ("s", "ms", "us"):
        raise ExecutionInputError(f"illegal_time_unit:{u}")
    return s


def _portfolio_open_risk(positions: Dict[str, Position], fee_rate: float,
                         slip_rate: float) -> float:
    from .risk import portfolio_open_risk
    return portfolio_open_risk(positions, fee_rate, slip_rate)


def _close_position(pos: Position, cash: float, fill: float, exit_fee: float,
                    fee: float, ts: int, reason: str,
                    store: Dict[str, Position], sym: str,
                    raw_target_px: Optional[float] = None,
                    order: Optional[Order] = None) -> Tuple[float, dict]:
    """平仓记账（**入场费不重复计**：入场时已在现金中扣过一次）。"""
    gross = (fill - pos.entry_price) * pos.qty * pos.direction
    cash += gross - exit_fee
    target_px = raw_target_px if raw_target_px is not None else fill
    exit_slip_loss = abs(fill - target_px) * pos.qty
    total_slip_loss = getattr(pos, "entry_slip_loss", 0.0) + exit_slip_loss
    rec = {
        "symbol": sym, "direction": pos.direction, "signal_type": pos.signal_type,
        "entry_time": int(pos.entry_time), "exit_time": int(ts),
        "entry_price": pos.entry_price, "exit_price": fill, "qty": pos.qty,
        "gross_pnl": gross, "entry_fee": pos.entry_fee, "exit_fee": exit_fee,
        "fees": pos.entry_fee + exit_fee, "funding_fee": pos.cum_funding_fee,
        "slippage_loss": total_slip_loss,
        "net_pnl": gross - pos.entry_fee - exit_fee - pos.cum_funding_fee,
        "mfe": pos.mfe, "mae": pos.mae, "exit_reason": reason,
        "funding_gaps": list(pos.funding_gaps),
        "stop_trace": list(pos.stop_trace),
    }
    del store[sym]
    return cash, rec


def _max_dd(equity_curve) -> float:
    if len(equity_curve) < 2:
        return 0.0
    eq = np.array([p["total_equity"] for p in equity_curve], dtype=np.float64)
    peaks = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - eq) / peaks, 0.0)
    return float(np.max(dd) * 100.0)


__all__ = ["ENGINE_CONTRACT_VERSION", "SIG_NONE", "prep_symbol", "TradingCore"]
