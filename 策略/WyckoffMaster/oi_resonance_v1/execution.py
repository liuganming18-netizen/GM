# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 · 执行**驱动层**（施工单 W2-6）。

WORK-02 之前本文件是「策略 + 风控 + 撮合 + 资金费」四合一的单体。
WORK-02 之后它只做**驱动**：

    装配 market_data → 构造 ExecutionPolicy + FillAdapter → 交给 TradingCore 跑

真正的决策在 `trading_core/policy.py`，成交算术在 `trading_core/fill.py`，
风控在 `trading_core/risk.py`，持仓/成交在 `trading_core/position.py`，
资金费在 `trading_core/funding.py`，账本事件在 `trading_core/ledger.py`，
主循环在 `trading_core/engine.py`。**Backtest / Paper / Live 调用同一份这些模块**。

对外 API **逐字保持不变**（既有调用方零改动）
──────────────────────────────────────────────
`run_portfolio_execution`（含 `trade_start_sec` / `close_open_positions_at_end` 两个
Runtime 选项及其 `close_open_positions_at_end_source` 结果键，与并行施工窗口的契约一致）、
`OpenPosition`、`assert_live_disabled`、`round_buy_price`、`round_sell_price`、
`round_qty_down`、`unrealized_pnl`、`unrealized_net_open`、`portfolio_open_risk`、
`LiveOrderForbidden`、`ExecutionInputError`、`version_block`，
以及既有回归入口 `_settle_funding` / `_funding_events_ms` / `_funding_events_by_sec` /
`_prep_symbol` / `_close_position` / `_max_dd` / `_apply_trail` / `_empty_result`。

真实下单**恒禁用**：本模块不存在任何下单 / 写库 / 联网路径
（`assert_live_disabled()` 为硬闸；`ExchangeFillAdapter` 结构性拒绝按 K 线假算）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from ..trading_core import engine as _engine
from ..trading_core import fill as _fill
from ..trading_core import funding as _funding
from ..trading_core.contracts import (ExecutionInputError,  # noqa: F401
                                      LiveExecutionForbidden)
from ..trading_core.fill import (round_buy_price,  # noqa: F401
                                 round_qty_down, round_sell_price)
from ..trading_core.policy import ExecutionPolicy
from ..trading_core.position import Position  # noqa: F401
from ..trading_core.position import (unrealized_net_open,  # noqa: F401
                                     unrealized_pnl)
from ..trading_core.risk import portfolio_open_risk  # noqa: F401
from . import factors as F
from .config import (EXEC_FROZEN, SIGNAL_FROZEN, SIGNAL_VERSION, STRATEGY_ID,
                     STRATEGY_KEY, ExecParams, SignalParams)
from .core import compute_signals

# ── 兼容别名（**同一类对象**，不是第二套实现）──
OpenPosition = Position
LiveOrderForbidden = LiveExecutionForbidden

# 私有回归入口别名（既有测试按 `X._settle_funding` / `X._funding_events_ms` 调用）
_settle_funding = _funding.settle_funding
_funding_events_ms = _funding.funding_events_ms
_funding_events_by_sec = _funding.funding_events_ms
_max_dd = _engine._max_dd
_close_position = _engine._close_position


def assert_live_disabled() -> None:
    """硬闸：真实下单在本包**不存在实现**，调用即抛。"""
    raise LiveOrderForbidden(
        "live_order_forbidden: WX_OI_RESONANCE_V1 只有合成模拟，无任何真实下单路径")


def _prep_symbol(sym: str, sig_bars: pd.DataFrame, bar_bars: pd.DataFrame,
                 params: SignalParams, unit: str, price_step: float,
                 trade_start_sec: Optional[int] = None) -> dict:
    """单币数组装配（薄包装 ⇒ 转调共享核心 `engine.prep_symbol`）。"""
    return _engine.prep_symbol(sym, sig_bars, bar_bars, params, unit, price_step,
                               compute_signals, F.to_epoch_seconds, trade_start_sec)


def _apply_trail(pos: Position, arr: dict, i: int) -> None:
    """兼容薄包装：把当期锁利通道作用于持仓（决策已迁入 `ExecutionPolicy.trail_level`）。"""
    pol = build_policy()
    lvl = pol.trail_level(direction=pos.direction, exit_low=float(arr["exit_low"][i]),
                          exit_high=float(arr["exit_high"][i]),
                          current_stop=pos.current_stop)
    if lvl is not None:
        pos.current_stop = float(lvl)
        pos.stop_trace.append({"ts": int(arr["timestamp"][i]), "stop": float(lvl),
                               "reason": "trail"})


# ══════════════════════════ 三端共享装配 ══════════════════════════

def build_policy(strategy_params: Optional[SignalParams] = None,
                 exec_cfg: Optional[ExecParams] = None) -> ExecutionPolicy:
    """三端共享的 `ExecutionPolicy`（Backtest / Paper / Live 必须调用同一份）。"""
    return ExecutionPolicy(signal_params=strategy_params or SIGNAL_FROZEN,
                           exec_params=exec_cfg or EXEC_FROZEN,
                           strategy_key=STRATEGY_KEY, timeframe="1h",
                           exec_timeframe="15m")


def build_fill_adapter(exec_cfg: Optional[ExecParams] = None):
    """默认成交适配器 = `SimulatedBarFillAdapter`（Backtest / Paper 共用）。

    Live 必须由调用方**显式**注入 `ExchangeFillAdapter`；本函数不替 Live 做决定。
    """
    cfg = exec_cfg or EXEC_FROZEN
    return _fill.SimulatedBarFillAdapter(
        slippage_bps=float(cfg.slippage_bps),
        maker_fee_rate=float(getattr(cfg, "maker_fee_rate", 0.0002)),
        taker_fee_rate=float(getattr(cfg, "taker_fee_rate", cfg.fee_rate)))


def build_core(strategy_params: Optional[SignalParams] = None,
               exec_cfg: Optional[ExecParams] = None,
               fill_adapter=None, collect_contracts: bool = True):
    """构造共享核心 `TradingCore`（驱动层的唯一出口）。"""
    pol = build_policy(strategy_params, exec_cfg)
    adapter = fill_adapter if fill_adapter is not None else build_fill_adapter(pol.cfg)
    return _engine.TradingCore(
        policy=pol, fill_adapter=adapter, signal_fn=compute_signals,
        to_epoch_seconds=F.to_epoch_seconds, strategy_key=STRATEGY_KEY,
        strategy_id=STRATEGY_ID, strategy_version=SIGNAL_VERSION,
        timeframe="1h", exec_timeframe="15m",
        collect_contracts=collect_contracts)


# ══════════════════════════ 驱动入口 ══════════════════════════

def run_portfolio_execution(market_data: Dict[str, dict],
                            strategy_params: Optional[SignalParams] = None,
                            exec_cfg: Optional[ExecParams] = None,
                            time_unit=None,
                            trade_start_sec: Optional[int] = None,
                            close_open_positions_at_end: Optional[bool] = None,
                            fill_adapter=None,
                            collect_contracts: bool = True) -> Dict[str, Any]:
    """多币组合合成执行（对外签名与 WORK-02 前**逐字一致**）。

    `market_data[sym]` = {
        "signal_bars": DataFrame（信号周期，已收盘）,
        "intrabar_bars": DataFrame（执行周期，需 timestamp/open/high/low/close）,
        "time_unit": 可选，覆盖顶层声明（多币不一致 ⇒ 拒绝）,
        "funding_events": 可选 [{"time_sec"|"time_ms", "rate", "mark_price"}],
    }

    `trade_start_sec`（可选，默认 None = 旧行为）：只接受 `confirm_time >= trade_start_sec`
    的信号。用于「预热段喂数据但不交易」：预热 bar 仍参与指标/因子滚动计算，其信号不开仓。

    `close_open_positions_at_end`（可选，默认 None）：**段末强平的显式 Runtime 选项**。
      · `None`   ⇒ 采用冻结执行配置（正常生产回测 = True）；
      · `False`  ⇒ **不做**段末强平，期末保留持仓并按真实 MTM 计权益。
    动机（E11 前缀不变性）：`True` 时段末强平是「数据末端」的函数 —— 追加未来数据后末端改变，
    强平随之改变，于是「截止时刻之前的决定」也会在尾部出现差异。显式暴露为 Runtime 选项后，
    前缀不变性用例可传 `False` 做**纯因果**比对，而生产回测仍走 `True`。
    该参数**不进入** `ExecParams` 冻结身份 ⇒ 不影响 `exec_params_hash`、不影响 Golden 口径。

    新增**可选**参数（不改变默认行为）：
      · `fill_adapter`       注入成交适配器（缺省 = `SimulatedBarFillAdapter`）；
      · `collect_contracts`  是否附带 `trading_core` 契约产物（order / ledger 计数）。
    """
    core = build_core(strategy_params, exec_cfg, fill_adapter=fill_adapter,
                      collect_contracts=collect_contracts)
    return core.run(market_data, time_unit=time_unit,
                    trade_start_sec=trade_start_sec,
                    close_open_positions_at_end=close_open_positions_at_end)


def _empty_result(cfg: ExecParams, trade_start_sec: Optional[int] = None,
                  close_at_end: Optional[bool] = None,
                  close_at_end_source: str = "frozen_config") -> dict:
    """空输入结果（保留原签名语义；转调共享核心 `TradingCore._empty_result`）。"""
    pol = build_policy(exec_cfg=cfg)
    core = _engine.TradingCore(
        policy=pol, fill_adapter=build_fill_adapter(cfg), signal_fn=compute_signals,
        to_epoch_seconds=F.to_epoch_seconds, strategy_key=STRATEGY_KEY,
        strategy_id=STRATEGY_ID, strategy_version=SIGNAL_VERSION,
        collect_contracts=True)
    if close_at_end is None:
        return core._empty_result(trade_start_sec, None)
    return core._empty_result(trade_start_sec, bool(close_at_end))


def version_block() -> dict:
    from .config import CORE_VERSION, EXEC_CORE_VERSION
    return {"strategy_id": STRATEGY_ID, "signal_version": SIGNAL_VERSION,
            "core_version": CORE_VERSION, "exec_core_version": EXEC_CORE_VERSION,
            "signal_params_hash": SIGNAL_FROZEN.hash(),
            "exec_params_hash": EXEC_FROZEN.hash()}


__all__ = [
    "LiveOrderForbidden", "ExecutionInputError", "assert_live_disabled",
    "round_buy_price", "round_sell_price", "round_qty_down",
    "OpenPosition", "unrealized_pnl", "unrealized_net_open", "portfolio_open_risk",
    "build_policy", "build_fill_adapter", "build_core",
    "run_portfolio_execution", "version_block",
]
