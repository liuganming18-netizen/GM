#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**共用执行核心桥**（backend ⇄ 策略窗）。

唯一职责：把 `策略/WyckoffMaster/execution.py`（v3.1 共用执行核心）暴露给后端
（`paper_engine` / `live_worker` / `live_signal_engine` / 只读 API），
使**历史回放与实时模拟使用同一策略、风控与成交逻辑**。

纪律：
  · 本文件**不含任何策略/风控数值**——所有阈值只有一个来源（策略窗 `execution.EXEC_FROZEN`）。
  · 策略包或 numpy 不可用时**不静默降级**：`CORE_AVAILABLE=False` + `IMPORT_ERROR` 如实暴露，
    调用方据此拒单/报缺，而不是退回某套"默认参数"。
  · 只读引用，不写任何库、不下单。
"""
from __future__ import annotations

import sys
from pathlib import Path

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "策略"
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

CORE_AVAILABLE = False
IMPORT_ERROR = None

try:                                                       # noqa: SIM105
    from WyckoffMaster import execution as X                # type: ignore
    CORE_AVAILABLE = True
except Exception as exc:                                    # noqa: BLE001
    X = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if CORE_AVAILABLE:
    CORE_VERSION = X.CORE_VERSION
    STRATEGY_VERSION = X.STRATEGY_VERSION
    LEDGER_MODEL_VERSION = X.LEDGER_MODEL_VERSION
    EXEC_FROZEN = X.EXEC_FROZEN
    DIRECTIONS = X.DIRECTIONS
    FROZEN_EXEC_KEYS = X.FROZEN_EXEC_KEYS
    load_exec_config = X.load_exec_config
    resolve_config = X.resolve_config
    validate_modes = X.validate_modes
    normalize_direction = X.normalize_direction
    decision = X.decision
    round_buy_price = X.round_buy_price
    round_protect_price = X.round_protect_price
    round_qty_down = X.round_qty_down
    size_position = X.size_position
    plan_entry = X.plan_entry
    apply_trail = X.apply_trail
    next_15m_open_at_or_after = X.next_15m_open_at_or_after
    expiry_exit_ms = X.expiry_exit_ms
    expiry_exit_from_bars = X.expiry_exit_from_bars
    drawdown_state = X.drawdown_state
    settle_funding_events = X.settle_funding_events
    order_bar_events = X.order_bar_events
    stop_hit_for_bar = X.stop_hit_for_bar
    ReplayEngine = X.ReplayEngine
    interface = X.interface
    version_block = X.version_block
    EXIT_EVENT_ORDER = X.EXIT_EVENT_ORDER
else:                                                       # pragma: no cover
    CORE_VERSION = None
    STRATEGY_VERSION = None
    LEDGER_MODEL_VERSION = None
    EXEC_FROZEN = None
    DIRECTIONS = ("long", "short")
    FROZEN_EXEC_KEYS = frozenset()
    EXIT_EVENT_ORDER = ("funding_settlement", "expiry_or_drawdown_or_scheduled_exit",
                        "new_entry", "intrabar_stop")

    def _unavailable(*_a, **_kw):
        raise RuntimeError(f"exec_core_unavailable: {IMPORT_ERROR}")

    for _name in ("load_exec_config", "resolve_config", "validate_modes",
                  "normalize_direction", "decision", "round_buy_price",
                  "round_protect_price", "round_qty_down", "size_position",
                  "plan_entry", "apply_trail", "next_15m_open_at_or_after",
                  "expiry_exit_ms", "expiry_exit_from_bars", "drawdown_state",
                  "settle_funding_events", "order_bar_events", "stop_hit_for_bar",
                  "interface", "version_block"):
        globals()[_name] = _unavailable
    ReplayEngine = None


# ── 新候选策略 oi_resonance_v1 加性暴露（不删改任何既有旧导出）──
NEW_CORE_AVAILABLE = False
NEW_IMPORT_ERROR = None
try:
    from WyckoffMaster.oi_resonance_v1 import execution as X_NEW           # type: ignore
    from WyckoffMaster.oi_resonance_v1 import registry_entry as REG_NEW     # type: ignore
    from WyckoffMaster.oi_resonance_v1 import config as CFG_NEW             # type: ignore
    NEW_CORE_AVAILABLE = True
except Exception as exc:                                                    # noqa: BLE001
    X_NEW = None
    REG_NEW = None
    CFG_NEW = None
    NEW_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if NEW_CORE_AVAILABLE:
    run_portfolio_execution = X_NEW.run_portfolio_execution
    new_version_block = X_NEW.version_block
    NEW_STRATEGY_ID = CFG_NEW.STRATEGY_ID
else:
    def _new_unavailable(*_a, **_kw):
        raise RuntimeError(f"new_exec_core_unavailable: {NEW_IMPORT_ERROR}")
    run_portfolio_execution = _new_unavailable
    new_version_block = _new_unavailable
    NEW_STRATEGY_ID = "WX_OI_RESONANCE_V1"


def version_ids() -> dict:
    """供任务三（回测）绑定、任务一（展示）引用的**版本标识块**。"""
    if not CORE_AVAILABLE:
        res = {"core_available": False, "import_error": IMPORT_ERROR,
               "strategy_dir": str(STRATEGY_DIR)}
    else:
        res = version_block()
        res["core_available"] = True
        res["strategy_dir"] = str(STRATEGY_DIR)
    res["new_core_available"] = NEW_CORE_AVAILABLE
    res["new_import_error"] = NEW_IMPORT_ERROR
    res["new_strategy_id"] = NEW_STRATEGY_ID
    if NEW_CORE_AVAILABLE:
        res["new_engine"] = new_version_block()
    return res


def new_entry_switch_default() -> bool:
    """新开仓开关的**默认值**：正式候选未选定/未最终验收 ⇒ 阻断（False）。"""
    return False


__all__ = [
    "CORE_AVAILABLE", "IMPORT_ERROR", "STRATEGY_DIR",
    "CORE_VERSION", "STRATEGY_VERSION", "LEDGER_MODEL_VERSION",
    "EXEC_FROZEN", "DIRECTIONS", "FROZEN_EXEC_KEYS", "EXIT_EVENT_ORDER",
    "load_exec_config", "resolve_config", "validate_modes", "normalize_direction",
    "decision", "round_buy_price", "round_protect_price", "round_qty_down",
    "size_position", "plan_entry", "apply_trail", "next_15m_open_at_or_after",
    "expiry_exit_ms", "expiry_exit_from_bars", "drawdown_state",
    "settle_funding_events", "order_bar_events", "stop_hit_for_bar",
    "ReplayEngine", "interface", "version_block",
    "version_ids", "new_entry_switch_default",
    "NEW_CORE_AVAILABLE", "NEW_IMPORT_ERROR", "X_NEW", "REG_NEW",
    "run_portfolio_execution", "new_version_block", "NEW_STRATEGY_ID",
]

