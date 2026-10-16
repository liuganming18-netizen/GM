# -*- coding: utf-8 -*-
"""策略注册表。

本轮（2026-10-11）加性扩展：
  · **当前启用族** F_WX：`3a/3b/3c/3d`（唐奇安基线 + 威科夫 Spring/SC），由
    `strategies/wx_donchian_wyckoff.py` 的同一确定性核心驱动。
  · **旧 A/B 保留为历史**：仍可读、可查询、可追溯，但 `enabled_for_current_run=False`，
    不进入当前启用集，不得改名冒充 WX。

状态分三层如实展示，互不冒充：
  · policy_status         研究政策是否冻结（v2.1 生效视图）
  · run_status            运行是否冻结（当前恒 pending —— G05 未通过）
  · implementation_status 代码是否已实现（WX 四组 = implemented）
"""
from . import common

# WORK-04B：旧策略彻底退役的单一事实源。
try:
    import legacy_retirement as _RET                      # type: ignore
except Exception:                                         # noqa: BLE001
    import sys as _sys
    from pathlib import Path as _Path
    _bd = str(_Path(__file__).resolve().parents[1])
    if _bd not in _sys.path:
        _sys.path.insert(0, _bd)
    try:
        import legacy_retirement as _RET                  # type: ignore
    except Exception:                                     # noqa: BLE001
        class _RET:                                       # type: ignore
            RETIRED_STRATEGY_KEYS = ("3a", "3b", "3c", "3d", "A", "B")
            RETIRED_AT = "2026-10-16"
            SUCCESSOR_STRATEGY_ID = "WX_OI_RESONANCE_V1"
            RETIREMENT_REASON = "旧 F_WX（3a–3d）与旧 A/B 已彻底退役清除"

# ── 单一「当前唯一策略」元数据（WORK-04B 核心收敛）───────────────────────────
CURRENT_BACKTEST_KEY = "wx_oi_resonance_v1"
CURRENT_BACKTEST_ID = "WX_OI_RESONANCE_V1"
_BACKTEST_CACHE = {"entry": None, "error": None, "loaded": False}


def backtest_entry(key=None):
    """懒加载候选包注册表对象。不可用 ⇒ `(None, 原因)`（不静默降级）。"""
    if _BACKTEST_CACHE["loaded"]:
        return _BACKTEST_CACHE["entry"], _BACKTEST_CACHE["error"]
    _BACKTEST_CACHE["loaded"] = True
    try:
        import sys
        from pathlib import Path
        strat_dir = Path(__file__).resolve().parents[3] / "策略"
        if str(strat_dir) not in sys.path:
            sys.path.insert(0, str(strat_dir))
        from WyckoffMaster.oi_resonance_v1 import registry_entry as _re   # type: ignore
        _BACKTEST_CACHE["entry"] = _re
    except Exception as exc:                                          # noqa: BLE001
        _BACKTEST_CACHE["error"] = f"{type(exc).__name__}: {exc}"
    return _BACKTEST_CACHE["entry"], _BACKTEST_CACHE["error"]


# 当前唯一启用集为新策略 WX_OI_RESONANCE_V1
CURRENT_RUN_KEYS = (CURRENT_BACKTEST_KEY,)
# 旧策略键（3a–3d、A、B）已退役，仅存静态标识，无运行实体
LEGACY_KEYS = ("3a", "3b", "3c", "3d", "A", "B")

_new_mod, _ = backtest_entry()
REGISTRY = {}
if _new_mod is not None:
    REGISTRY[CURRENT_BACKTEST_KEY] = _new_mod


def get(key):
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        mod, _ = backtest_entry()
        return mod
    return None


def resolved_config(con, key, version=None):
    """统一配置解析：新策略走内置冻结配置；旧策略已退役无配置。"""
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        mod, _ = backtest_entry()
        return mod.resolve(con, key, version) if mod else None
    return None


def describe(con, key):
    """返回单个策略的元数据 + 真实三层状态 + 配置状态。

    配置状态三态：已配置 / 待配置 / 配置无效（类型·范围·窗口关系不合法）。
    """
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        return describe_current_backtest(con)

    if key in LEGACY_KEYS:
        return {
            "strategy_key": key,
            "name": f"Legacy Strategy {key} (Retired)",
            "timeframe": "1h",
            "direction": "long",
            "family": "legacy_retired",
            "enabled_for_current_run": False,
            "retired": True,
            "runnable": False,
            "retired_at": getattr(_RET, "RETIRED_AT", "2026-10-16"),
            "successor_strategy_id": getattr(_RET, "SUCCESSOR_STRATEGY_ID", "WX_OI_RESONANCE_V1"),
            "status": "retired",
            "config_status": "已退役清理",
            "missing_config": [],
            "config_errors": ["WORK-04B: 策略已彻底退役，禁止运行"],
            "required_config": [],
            "config_version": None,
            "config_hash": None,
            "config_source": None,
            "frozen": False,
            "policy_status": "retired",
            "run_status": "retired",
            "implementation_status": "deleted_in_work04b",
            "note": "WORK-04B: 旧策略已退役并从源码中物理清理，仅保留历史审计凭据，不可作为生产策略启动。",
        }
    return None


def list_all(con):
    keys = list(CURRENT_RUN_KEYS) + list(LEGACY_KEYS)
    return [describe(con, k) for k in keys if k]


def list_current(con):
    """当前启用族（新策略 WX_OI_RESONANCE_V1）。"""
    return [describe(con, k) for k in CURRENT_RUN_KEYS]


def list_legacy(con):
    """历史追溯族（旧 F_WX 与旧 A/B）。"""
    return [describe(con, k) for k in LEGACY_KEYS]



# ══════════════════════════ 单一「当前回测策略」元数据（2026-10-16 加性）══════════
# 用户授权：**单一策略真实回测**接入。当前唯一回测候选 = `WX_OI_RESONANCE_V1`
# （`策略/WyckoffMaster/oi_resonance_v1`），注册表键 `wx_oi_resonance_v1`。
#
# 与旧族的关系（**不互相冒充**）：
#   · 旧 F_WX 3a/3b/3c/3d 仍是「当前启用族」（`CURRENT_RUN_KEYS`），本函数**不改**它；
#   · 旧 get / resolved_config / describe / list_all / list_current / list_legacy **语义不变**
#     （历史兼容保留）；
#   · 本函数只**新增**一份「当前回测策略」元数据，供回测接口与回执引用。
#
# 候选包依赖 numpy/pandas ⇒ **懒加载 + 失败如实报缺**，绝不在导入期拖垮后端。
CURRENT_BACKTEST_KEY = "wx_oi_resonance_v1"
CURRENT_BACKTEST_ID = "WX_OI_RESONANCE_V1"
_BACKTEST_CACHE = {"entry": None, "error": None, "loaded": False}


def backtest_entry(key=None):
    """懒加载候选包注册表对象。不可用 ⇒ `(None, 原因)`（不静默降级）。"""
    if _BACKTEST_CACHE["loaded"]:
        return _BACKTEST_CACHE["entry"], _BACKTEST_CACHE["error"]
    _BACKTEST_CACHE["loaded"] = True
    try:
        import sys
        from pathlib import Path
        strat_dir = Path(__file__).resolve().parents[3] / "策略"
        if str(strat_dir) not in sys.path:
            sys.path.insert(0, str(strat_dir))
        from WyckoffMaster.oi_resonance_v1 import registry_entry as _re   # type: ignore
        _BACKTEST_CACHE["entry"] = _re
    except Exception as exc:                                          # noqa: BLE001
        _BACKTEST_CACHE["error"] = f"{type(exc).__name__}: {exc}"
    return _BACKTEST_CACHE["entry"], _BACKTEST_CACHE["error"]


def describe_current_backtest(con=None):
    """单一当前回测策略元数据（只读；候选包不可用 ⇒ 如实标 unavailable）。"""
    mod, err = backtest_entry()
    if mod is None:
        return {
            "strategy_key": CURRENT_BACKTEST_KEY, "strategy_id": CURRENT_BACKTEST_ID,
            "available": False, "import_error": err,
            "note": "候选策略包不可用（缺 numpy/pandas 或包路径变动）⇒ 回测接口将拒建，"
                    "不退回默认参数",
            "inherits_legacy_results": False,
            "legacy_archive": "策略/WyckoffMaster/_legacy_archive_v31",
            "runnable": False, "retired": False,
        }
    iface = mod.interface()
    return {
        "strategy_key": mod.KEY,
        "strategy_id": iface["strategy_id"],
        "name": mod.NAME,
        "family": mod.FAMILY,
        "timeframe": mod.TIMEFRAME,
        "direction": mod.DIRECTION,
        "time_unit": mod.TIME_UNIT,
        "available": True,
        "signal_versions": iface["signal_versions"],
        "experiment_id": iface["experiment_id"],
        "signal_params_hash": iface["signal_params_hash"],
        "exec_params_hash": iface["exec_params_hash"],
        "oi_weights": iface["oi_weights"],
        "signal_names": iface["signal_names"],
        "stop_modes": iface["stop_modes"],
        "formal_run_gate": iface["formal_run_gate"],
        "live_order_enabled": False,
        "inherits_legacy_results": False,
        "not_inherited": iface["not_inherited"],
        "legacy_archive": "策略/WyckoffMaster/_legacy_archive_v31",
        "runnable": True,
        "retired": False,
        "role": "当前**唯一可运行回测策略**；旧 F_WX 3a–3d 与旧 A/B 已退役"
                "（runnable=False，见 list_legacy / retirement_summary）",
        "note": "正式 M4 门禁未过（run_frozen=False）⇒ 本策略只做**独立候选研究**，"
                "不得冒称 M4 已通过、不得继承 3a–3d / M0–M6 成绩",
    }


def is_runnable(key) -> bool:
    """该注册表键当前是否**可运行**。旧 3a–3d / A / B 一律 `False`（WORK-04 退役）。"""
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        mod, _ = backtest_entry()
        return mod is not None
    return False


def guard_runnable(key):
    """可运行性硬闸：旧策略 / 未知键 ⇒ 返回**拒绝原因字符串**；可运行 ⇒ `None`。

    调用方（回测创建 / 驱动）必须先过此闸，避免旧策略被当成可运行策略启动。
    """
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        mod, err = backtest_entry()
        if mod is None:
            return f"候选策略包不可用：{err}"
        return None
    if key in LEGACY_KEYS:
        return (f"策略 {key!r} 已退役（WORK-04，{getattr(_RET, 'RETIRED_AT', '')}）"
                f"⇒ 不可运行；唯一可运行策略为 "
                f"{getattr(_RET, 'SUCCESSOR_STRATEGY_ID', 'WX_OI_RESONANCE_V1')}")
    return f"未知策略键 {key!r}（不在注册表）"


def retirement_block() -> dict:
    """**轻量**退役元数据（不查库、不触发候选包加载）——供 `/api/v1/strategies` 等内嵌。"""
    return {
        "retired_strategy_keys": list(_RET.RETIRED_STRATEGY_KEYS),
        "retired_at": getattr(_RET, "RETIRED_AT", None),
        "successor_strategy_id": getattr(_RET, "SUCCESSOR_STRATEGY_ID", None),
        "successor_strategy_key": CURRENT_BACKTEST_KEY,
        "current_runnable_keys": list(CURRENT_RUN_KEYS),
        "legacy_readonly_keys": list(LEGACY_KEYS),
    }


def retirement_summary(con=None) -> dict:
    """退役汇总（供 API / UI 展示「新旧归属」）。"""
    return {
        "retired_strategy_keys": list(_RET.RETIRED_STRATEGY_KEYS),
        "retired_at": getattr(_RET, "RETIRED_AT", None),
        "successor_strategy_id": getattr(_RET, "SUCCESSOR_STRATEGY_ID", None),
        "successor_strategy_key": CURRENT_BACKTEST_KEY,
        "current_runnable_keys": list(CURRENT_RUN_KEYS),
        "legacy_readonly": list(LEGACY_KEYS),
        "legacy": list_legacy(con),
        "note": "旧策略已退役：不再进入任何可运行入口；旧源码/旧 runs 以可恢复只读归档保留。",
    }


def list_backtest(con=None):
    """当前回测策略清单（当前仅一项）。"""
    return [describe_current_backtest(con)]
