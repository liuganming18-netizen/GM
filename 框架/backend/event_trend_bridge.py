# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 策略 ⇄ 后端桥接模块。

作为最新冻结策略 WX_EVENT_TREND_V0_1 与 Web 接口及执行框架的标准契约桥梁。
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parent
STRATEGY_DIR = PROJECT_ROOT / "策略"
PACKAGE_DIR = STRATEGY_DIR / "WyckoffMaster" / "event_trend_v0_1"
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

STRATEGY_ID = "WX_EVENT_TREND_V0_1"
STRATEGY_KEY = "wx_event_trend_v0_1"
KEY = STRATEGY_KEY
FAMILY = "F_WX_EVENT_TREND"
NAME = "WX_EVENT_TREND_V0_1：低频事件趋势研究（4h箱体 / 1h扫低回踩突破 / 15m撮合）"
TIMEFRAME = "1h"
DIRECTION = "long"
TIME_UNIT = "ms"
CONTRACT_VERSION = "bt_api_v1"

ALLOWED_SIGNAL_TF = ("1h",)
ALLOWED_EXEC_TF = ("15m",)
ALLOWED_EXCHANGE = ("binance",)
ALLOWED_MARKET = ("um",)

SPEC_KEYS = (
    "strategy_id", "exchange", "market", "symbols",
    "signal_timeframe", "exec_timeframe",
    "window_start", "window_end", "window_days", "warmup_bars",
    "params", "note",
)

AVAILABLE = False
IMPORT_ERROR: Optional[str] = None

try:
    from WyckoffMaster.event_trend_v0_1 import EventTrendConfig, EventTrendRunner  # type: ignore
    AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _require() -> None:
    if not AVAILABLE:
        raise RuntimeError(f"event_trend_unavailable: {IMPORT_ERROR}")


def code_hash() -> str:
    """计算最新策略与后端文件的代码哈希。"""
    _require()
    runner = EventTrendRunner()
    fp = runner.code_fingerprint()
    return fp.get("sha256", "0" * 64)


def code_hash_manifest() -> List[Dict[str, Any]]:
    _require()
    runner = EventTrendRunner()
    fp = runner.code_fingerprint()
    return fp.get("files", [])


def version_block() -> Dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_key": STRATEGY_KEY,
        "family": FAMILY,
        "version": "v0.1.0",
        "signal_version": "event_trend_v0_1",
        "core_version": "event_state_machine_v0_1",
        "exec_core_version": "trading_core_shared_v1",
        "contract_version": CONTRACT_VERSION,
        "core_available": AVAILABLE,
        "import_error": IMPORT_ERROR,
    }


def formal_run_gate() -> Dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "formal_run_allowed": False,
        "run_frozen": False,
        "gate_status": {"G01": "pass", "G02": "pass", "G03": "pass", "G04": "pending", "G05": "pending"},
        "requirements": ["真实数据覆盖", "确定性状态迁移与四大对账通过", "共享交易核心真实成交与成本对账", "多标的独立回放验证"],
        "blocking_reasons": ["目前处于候选研究阶段，正式生产运行需通过实盘前置 Gate 审批"],
        "ai_role": "AI 只能生成候选；确定性风控有最终权限",
        "live_order_enabled": False,
        "new_entry_switch_default": False,
    }


PARAMETER_WHITELIST: Dict[str, Dict[str, Any]] = {
    "initial_equity": {"scope": "exec", "min": 100.0, "max": 1000000.0, "type": "float", "default": 2000.0},
    "risk_budget_per_trade": {"scope": "exec", "min": 0.001, "max": 0.05, "type": "float", "default": 0.005},
    "max_active_positions": {"scope": "exec", "min": 1, "max": 10, "type": "int", "default": 3},
    "max_symbol_positions": {"scope": "exec", "min": 1, "max": 3, "type": "int", "default": 1},
    "max_notional_per_position": {"scope": "exec", "min": 0.05, "max": 1.0, "type": "float", "default": 0.30},
    "max_total_notional": {"scope": "exec", "min": 0.1, "max": 3.0, "type": "float", "default": 0.90},
    "taker_fee_bps": {"scope": "exec", "min": 0.0, "max": 50.0, "type": "float", "default": 5.0},
    "slippage_bps": {"scope": "exec", "min": 0.0, "max": 50.0, "type": "float", "default": 3.0},
    "trailing_trigger_r": {"scope": "exec", "min": 1.0, "max": 10.0, "type": "float", "default": 2.0},
    "max_holding_days": {"scope": "exec", "min": 1, "max": 30, "type": "int", "default": 10},
    "box_bars_4h": {"scope": "signal", "min": 20, "max": 120, "type": "int", "default": 42},
    "atr_period_4h": {"scope": "signal", "min": 5, "max": 50, "type": "int", "default": 14},
    "er_threshold": {"scope": "signal", "min": 0.1, "max": 0.9, "type": "float", "default": 0.30},
    "box_min_width_atr": {"scope": "signal", "min": 1.0, "max": 10.0, "type": "float", "default": 2.0},
    "box_max_width_atr": {"scope": "signal", "min": 3.0, "max": 20.0, "type": "float", "default": 8.0},
    "oi_lookback_hours": {"scope": "signal", "min": 1, "max": 24, "type": "int", "default": 6},
    "oi_min_growth_pct": {"scope": "signal", "min": 0.005, "max": 0.20, "type": "float", "default": 0.02},
}


def parameter_whitelist() -> Dict[str, Dict[str, Any]]:
    return dict(PARAMETER_WHITELIST)


def validate_params(params: Optional[dict]) -> Tuple[Dict[str, Any], List[str]]:
    errs: List[str] = []
    clean: Dict[str, Any] = {}
    if not params:
        return clean, errs
    if not isinstance(params, dict):
        return {}, ["params 必须是字典对象"]
    for k, v in params.items():
        if k not in PARAMETER_WHITELIST:
            errs.append(f"参数不在白名单：{k}")
            continue
        spec = PARAMETER_WHITELIST[k]
        try:
            if spec["type"] == "int":
                val = int(v)
            else:
                val = float(v)
        except (ValueError, TypeError):
            errs.append(f"{k} 必须为合法数值（收到 {v!r}）")
            continue
        if spec.get("min") is not None and val < spec["min"]:
            errs.append(f"{k} 必须 >= {spec['min']}（收到 {val}）")
        if spec.get("max") is not None and val > spec["max"]:
            errs.append(f"{k} 必须 <= {spec['max']}（收到 {val}）")
        clean[k] = val
    return clean, errs


def validate_run_spec(spec: Optional[dict]) -> Tuple[Dict[str, Any], List[str]]:
    errs: List[str] = []
    if not isinstance(spec, dict):
        return {}, ["请求必须是 JSON 对象"]
    
    clean: Dict[str, Any] = {}
    sid = spec.get("strategy_id") or STRATEGY_ID
    if sid != STRATEGY_ID:
        errs.append(f"策略 ID 不匹配：当前仅支持 {STRATEGY_ID}，收到 {sid!r}")
    clean["strategy_id"] = STRATEGY_ID

    exchange = spec.get("exchange") or "binance"
    if exchange not in ALLOWED_EXCHANGE:
        errs.append(f"交易所 {exchange!r} 不受支持（当前仅允许 {ALLOWED_EXCHANGE}）")
    clean["exchange"] = exchange

    market = spec.get("market") or "um"
    if market not in ALLOWED_MARKET:
        errs.append(f"市场类型 {market!r} 不受支持（当前仅允许 {ALLOWED_MARKET}）")
    clean["market"] = market

    symbols = spec.get("symbols") or ["BTCUSDT"]
    if isinstance(symbols, str):
        symbols = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not isinstance(symbols, list) or not symbols:
        errs.append("symbols 必须是非空列表")
    clean["symbols"] = [str(s).upper() for s in (symbols or [])]

    clean["signal_timeframe"] = "1h"
    clean["exec_timeframe"] = "15m"

    clean_params, p_errs = validate_params(spec.get("params"))
    errs.extend(p_errs)
    clean["params"] = clean_params
    
    # 默认本金 2000 USDT
    clean["initial_equity"] = float(clean_params.get("initial_equity", 2000.0))
    clean["window_start"] = spec.get("window_start")
    clean["window_end"] = spec.get("window_end")
    clean["note"] = spec.get("note", "WX_EVENT_TREND_V0_1 标准回测")

    return clean, errs


def interface() -> Dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_key": STRATEGY_KEY,
        "name": NAME,
        "family": FAMILY,
        "timeframe": TIMEFRAME,
        "direction": DIRECTION,
        "time_unit": TIME_UNIT,
        "signal_versions": {"signal": "event_trend_v0_1", "core": "event_state_machine_v0_1", "exec_core": "trading_core_shared_v1"},
        "experiment_id": "exp_event_trend_v0_1",
        "signal_params_hash": code_hash()[:16],
        "exec_params_hash": code_hash()[:16],
        "oi_weights": {"oi_growth_pct": 1.0},
        "signal_names": {"1": "breakout_long"},
        "stop_modes": ["wilder_atr_trailing", "initial_stop"],
        "formal_run_gate": formal_run_gate(),
        "not_inherited": ["旧 WX_OI_RESONANCE_V1 / 3a-3d 的任何历史净值与夏普"],
    }
