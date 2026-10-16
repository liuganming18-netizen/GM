# -*- coding: utf-8 -*-
"""注册表对接层：把 `WX_OI_RESONANCE_V1` 暴露成注册表可识别的模块对象。

与旧 `strategies/wx_donchian_wyckoff.py` 的 `WxArm` **同协议**（KEY/NAME/TIMEFRAME/
DIRECTION/FAMILY/REQUIRED_CONFIG/VALIDATION/RELATIONS/run），因此 `registry.get /
resolved_config / list_current / describe` 无需为它改结构。

但**身份不同**：本对象不读生效参数视图（视图文件已随旧目录丢失），配置来自本包
内置冻结值（`config.SIGNAL_FROZEN` / `EXEC_FROZEN`），**不覆盖**旧账户冻结参数。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from . import config as C
from . import core as CORE
from . import execution as EXEC

KEY = C.STRATEGY_KEY
NAME = "WX_OI_RESONANCE_V1：OI 共振 + 威科夫/唐奇安候选规则（spring / sc_reversal / donchian_breakout / lps_pullback）"
TIMEFRAME = "1h"
DIRECTION = "long"
FAMILY = C.FAMILY

# 输入 bar 的时间戳单位（币安原始 K 线为毫秒）——**显式声明**，不从 max/median 推断
TIME_UNIT = "ms"

REQUIRED_CONFIG = tuple(sorted(C.SIGNAL_VALIDATION))
VALIDATION = {k: {"type": ("int" if v[2] is int else "float"),
                  "min": v[0], "max": v[1]}
              for k, v in C.SIGNAL_VALIDATION.items() if v[0] is not None or v[1] is not None}


def _rel_window_relations(cfg) -> list[str]:
    errs: list[str] = []
    if cfg.get("donchian_exit_window") is not None and cfg.get("donchian_entry_window") is not None:
        if float(cfg["donchian_exit_window"]) > float(cfg["donchian_entry_window"]):
            errs.append("donchian_exit_window 不得大于 donchian_entry_window")
    if cfg.get("oi_rank_min_days") is not None and cfg.get("oi_rank_days") is not None:
        if float(cfg["oi_rank_min_days"]) > float(cfg["oi_rank_days"]):
            errs.append("oi_rank_min_days 不得大于 oi_rank_days")
    return errs


RELATIONS = [_rel_window_relations]


# ══════════════════════════ 正式运行门禁 ══════════════════════════

FORMAL_M4_REQUIREMENTS = (
    "唯一合格父赢家（同一父规格下唯一通过 G01–G05 的父配置）",
    "共同样本（父子必须在同一时间样本上比较，样本须逐根可核）",
    "同样本父级对照（同一份样本上父级基准的净值/回撤/夏普）",
)
FORMAL_M4_NOT_PASSED_REASONS = (
    "父规格 G01–G05 未全部 pass",
    "唯一合格父赢家未确定",
    "共同样本与同样本父级对照尚未产出",
    "本轮不引入外部数据源 ⇒ 无法补齐 OI/结算事件缺项",
)


def formal_run_gate() -> dict:
    """正式 M4 门禁：未过 ⇒ **禁止正式运行**（本函数只读、不写任何状态）。"""
    return {
        "strategy_id": C.STRATEGY_ID,
        "formal_run_allowed": False,
        "run_frozen": C.RUN_FROZEN,
        "gate_status": dict(C.GATE_STATUS),
        "requirements": list(FORMAL_M4_REQUIREMENTS),
        "blocking_reasons": list(FORMAL_M4_NOT_PASSED_REASONS),
        "ai_role": "AI 只能生成候选；确定性风控有最终权限",
        "live_order_enabled": False,
        "new_entry_switch_default": False,
    }


# ══════════════════════════ 配置解析 ══════════════════════════

def resolve(con, key=KEY, version=None) -> dict:
    """本策略的配置**自带身份**（不依赖已丢失的旧生效视图）。形状与 `wx_config.resolve` 兼容。"""
    if key not in (KEY, C.STRATEGY_ID):
        return None
    cfg = C.signal_params_dict()
    chash = C.SIGNAL_FROZEN.hash()
    errs = C.validate_signal_params()
    gate = formal_run_gate()
    return {
        "version": C.SIGNAL_VERSION,
        "config": cfg,
        "config_hash": chash,
        "registered_config_id": KEY,
        "arm": KEY,
        "kind": "candidate",
        "exec": C.exec_params_dict(),
        "exec_core": EXEC.version_block(),
        "stored_hash": None,
        "frozen": C.RUN_FROZEN,
        "run_frozen": C.RUN_FROZEN,
        "policy_run_frozen": False,
        "source": "oi_resonance_v1_builtin_frozen",
        "view_sha256": None,
        "view_sha_ok": None,
        "hash_check": "ok" if not errs else "invalid",
        "identity_ok": not errs,
        "identity_problems": list(errs),
        "run_binding": gate,
        "oi_weights": dict(C.OI_WEIGHTS),
        "experiment_id": C.EXPERIMENT_ID,
        "not_inherited": list(C.NOT_INHERITED),
        "legacy_archive": C.SUPERSEDED_BY_ARCHIVE,
    }


def interface() -> dict:
    return {
        "strategy_id": C.STRATEGY_ID,
        "strategy_key": KEY,
        "family": FAMILY,
        "timeframe": TIMEFRAME,
        "direction": DIRECTION,
        "time_unit": TIME_UNIT,
        "signal_versions": {"signal": C.SIGNAL_VERSION, "core": C.CORE_VERSION,
                            "exec_core": C.EXEC_CORE_VERSION},
        "experiment_id": C.EXPERIMENT_ID,
        "signal_params_hash": C.SIGNAL_FROZEN.hash(),
        "exec_params_hash": C.EXEC_FROZEN.hash(),
        "oi_weights": dict(C.OI_WEIGHTS),
        "signal_names": dict(CORE.SIGNAL_NAMES),
        "stop_modes": list(CORE.VALID_STOP_MODES),
        "not_inherited": list(C.NOT_INHERITED),
        "formal_run_gate": formal_run_gate(),
        "live_order_enabled": False,
    }


# ══════════════════════════ run（注册表协议）══════════════════════════

def _bars_to_frame(bars: Iterable) -> pd.DataFrame:
    rows = []
    for b in bars:
        g = (b.get if isinstance(b, dict) else (lambda k, _b=b: getattr(_b, k, None)))
        def f(k):
            v = g(k)
            return float(v) if v is not None else float("nan")
        ot = g("open_time")
        ct = g("close_time")
        rows.append({
            "timestamp": int(ot),
            "close_time": (int(ct) if ct is not None else
                           int(ot) + int(C.SIGNAL_FROZEN.signal_bar_seconds) * 1000),
            "open": f("open"), "high": f("high"), "low": f("low"), "close": f("close"),
            "volume": f("volume"),
            "open_interest": (f("open_interest") if g("open_interest") is not None
                              else np.nan),
        })
    return pd.DataFrame(rows)


def _event_uid(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def run(bars, resolved=None, arm_id=None, identity=None, require_identity=False) -> dict:
    """产出事件列表（形状与旧 `wx_donchian_wyckoff.run` 的 events 兼容）。

    `identity` = (exchange, symbol)。`require_identity=True` 且身份缺失 ⇒ 拒绝（status=blocked）。
    """
    exchange, symbol = (identity if identity and len(identity) == 2 else (None, None))
    if require_identity and (exchange is None or symbol is None):
        return {"status": "blocked_missing_identity", "events": [],
                "diagnostics": {"reason": "require_identity_but_identity_missing"}}
    df = _bars_to_frame(bars)
    if len(df) == 0:
        return {"status": "insufficient_data", "events": [],
                "diagnostics": {"reason": "no_bars", "bars": 0}}
    try:
        sig = CORE.compute_signals(df, C.SIGNAL_FROZEN, TIME_UNIT)
    except Exception as exc:                                     # noqa: BLE001
        return {"status": "invalid_input", "events": [],
                "diagnostics": {"reason": f"{type(exc).__name__}: {exc}"}}

    events = []
    for _, r in sig.iterrows():
        s = int(r["signal"])
        if s == CORE.SIG_NONE:
            continue
        close_ms = int(r["timestamp"]) + int(C.SIGNAL_FROZEN.signal_bar_seconds) * 1000
        name = str(r["signal_name"])
        uid = _event_uid(C.STRATEGY_ID, exchange, symbol, TIMEFRAME, close_ms, name)
        direction = "long" if int(r["direction"]) == 1 else "short"
        atr = float(r["atr"]) if math.isfinite(float(r["atr"])) else None
        score = r["oi_resonance_score"]
        events.append({
            "event_time": close_ms, "confirmed_at": close_ms, "direction": direction,
            "strategy_key": KEY, "strategy_version": C.SIGNAL_VERSION,
            "exchange": exchange, "symbol": symbol, "timeframe": TIMEFRAME,
            "evidence": {
                "signal_name": name,
                "execution_label": name,
                "source_masks": [name],
                "event_uid": uid,
                "signal_bar_open_time": int(r["timestamp"]),
                "signal_bar_close_time": close_ms,
                "atr_signal_bar": atr,
                "stop_price": (float(r["stop_price"])
                               if math.isfinite(float(r["stop_price"])) else None),
                "stop_mode": str(r["stop_mode"]),
                "structural_ref": (float(r["structural_ref"])
                                   if math.isfinite(float(r["structural_ref"])) else None),
                "oi_resonance_score": (float(score)
                                       if score is not None and math.isfinite(float(score))
                                       else None),
                "oi_resonance_available": bool(r["oi_resonance_available"]),
                "oi_resonance_quality": str(r["oi_resonance_quality"]),
                "config_hash": C.SIGNAL_FROZEN.hash(),
            },
        })
    counts = sig.attrs.get("signal_counts") or {}
    return {
        "status": "ok",
        "events": events,
        "diagnostics": {
            "strategy_id": C.STRATEGY_ID, "time_unit": TIME_UNIT,
            "bars": int(len(sig)), "signal_counts": counts,
            "reject_counts": sig.attrs.get("reject_counts") or {},
            "dedup_report": sig.attrs.get("dedup_report") or {},
            "oi_factor_meta": sig.attrs.get("oi_factor_meta") or {},
        },
    }


class OiResonanceStrategy:
    """注册表模块对象（与 `WxArm` 同协议）。"""

    def __init__(self):
        self.KEY = KEY
        self.NAME = NAME
        self.TIMEFRAME = TIMEFRAME
        self.DIRECTION = DIRECTION
        self.FAMILY = FAMILY
        self.REQUIRED_CONFIG = REQUIRED_CONFIG
        self.VALIDATION = VALIDATION
        self.RELATIONS = RELATIONS

    def run(self, bars, resolved, arm_id=None, identity=None, require_identity=False):
        return run(bars, resolved, arm_id=arm_id, identity=identity,
                   require_identity=require_identity)

    def resolve(self, con, key=None, version=None):
        return resolve(con, key or self.KEY, version)

    def interface(self):
        return interface()


REGISTRY = {KEY: OiResonanceStrategy()}


def get(key):
    return REGISTRY.get(key)


__all__ = ["KEY", "NAME", "TIMEFRAME", "DIRECTION", "FAMILY", "TIME_UNIT",
           "REQUIRED_CONFIG", "VALIDATION", "RELATIONS", "REGISTRY", "get",
           "run", "resolve", "interface", "formal_run_gate",
           "FORMAL_M4_REQUIREMENTS", "FORMAL_M4_NOT_PASSED_REASONS",
           "OiResonanceStrategy"]
