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
from . import common, strategy_a, strategy_b
from . import wx_donchian_wyckoff as wx
from . import wx_config

# ── 单一「当前回测策略」元数据（2026-10-16 策略替换）─────────────────────────
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


# 当前启用集切换为新策略 WX_OI_RESONANCE_V1（2026-10-16 任务三对接）
CURRENT_RUN_KEYS = (CURRENT_BACKTEST_KEY,)
# 旧 F_WX（3a–3d）降为 legacy 只读，与旧 A/B 共同保留为历史追溯，不改名冒充新策略
LEGACY_KEYS = tuple(wx.ARM_IDS) + (strategy_a.KEY, strategy_b.KEY)

# 全量查找表（CLI / 历史查询用）
_new_mod, _ = backtest_entry()
REGISTRY = {
    **wx.REGISTRY,
    strategy_a.KEY: strategy_a,
    strategy_b.KEY: strategy_b,
}
if _new_mod is not None:
    REGISTRY[CURRENT_BACKTEST_KEY] = _new_mod


def get(key):
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        mod, _ = backtest_entry()
        return mod
    return REGISTRY.get(key)


def resolved_config(con, key, version=None):
    """统一配置解析：新策略走内置冻结配置；WX 走生效视图/库内登记；旧 A/B 走库内 strategy_configs。"""
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        mod, _ = backtest_entry()
        return mod.resolve(con, key, version) if mod else None
    if key in wx.ARM_IDS:
        return wx_config.resolve(con, key, version)
    return common.load_config(con, key, version)


def describe(con, key):
    """返回单个策略的元数据 + 真实三层状态 + 配置状态。

    配置状态三态：已配置 / 待配置 / 配置无效（类型·范围·窗口关系不合法）。
    """
    if key in (CURRENT_BACKTEST_KEY, CURRENT_BACKTEST_ID):
        return describe_current_backtest(con)

    mod = REGISTRY.get(key)
    if mod is None:
        return None
    resolved = resolved_config(con, key, None)
    status, missing, errors = common.validate_config(
        resolved, mod.REQUIRED_CONFIG,
        getattr(mod, "VALIDATION", None), getattr(mod, "RELATIONS", None))
    if resolved and resolved.get("hash_check") == "drift":
        status = common.STATUS_INVALID
        errors = list(errors) + ["身份漂移（视图 SHA/登记表/逐配置生效身份或 DB 参数不一致）：拒绝运行"]
        errors = errors + list(resolved.get("identity_problems") or [])
    if status == common.STATUS_INVALID:
        config_status = common.STATUS_INVALID
        run_status = "invalid_config"
    elif status == common.STATUS_OK:
        config_status = "已配置"
        run_status = "configured"
    else:
        config_status = common.STATUS_PENDING
        run_status = "pending_config"

    is_wx = key in wx.ARM_IDS
    view, vsha = wx_config.load_view() if is_wx else (None, None)
    return {
        "strategy_key": key,
        "name": mod.NAME,
        "timeframe": mod.TIMEFRAME,
        "direction": mod.DIRECTION,
        "family": "F_WX_legacy" if is_wx else "legacy",
        "enabled_for_current_run": False,   # 旧 F_WX 降为 legacy 只读
        "status": run_status,
        "config_status": config_status,
        "missing_config": missing,
        "config_errors": errors,
        "required_config": list(mod.REQUIRED_CONFIG),
        "config_version": resolved["version"] if resolved else None,
        "config_hash": resolved["config_hash"] if resolved else None,
        "config_source": resolved.get("source") if resolved else None,
        "frozen": bool(resolved["frozen"]) if resolved else False,
        # 三层真实状态（不互相冒充）
        "policy_status": ("frozen" if (is_wx and view and view.get("research_policy_frozen"))
                          else ("unknown" if is_wx else "n/a")),
        "run_status": ("frozen" if (is_wx and resolved and resolved.get("run_frozen"))
                       else ("pending" if is_wx else "n/a")),
        "implementation_status": "legacy_readable_only",
        "identity_ok": (resolved.get("identity_ok") if (is_wx and resolved) else None),
        "view_sha256": (resolved.get("view_sha256") if (is_wx and resolved) else None),
        "run_binding": (resolved.get("run_binding") if (is_wx and resolved) else None),
        "exec": (resolved.get("exec") if (is_wx and resolved) else None),
        "gate_status": ({"G01": "pending", "G02": "pending", "G03": "pending",
                         "G04": "pending", "G05": "pending"} if is_wx else None),
        "config": resolved["config"] if resolved else None,
        "note": "旧 F_WX 策略族（3a–3d）：已降为 legacy 只读追溯，不进入当前启用集",
    }


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
        "role": "当前**回测**策略（唯一）；旧 F_WX 3a–3d 仍为当前启用族，语义不变",
        "note": "正式 M4 门禁未过（run_frozen=False）⇒ 本策略只做**独立候选研究**，"
                "不得冒称 M4 已通过、不得继承 3a–3d / M0–M6 成绩",
    }


def list_backtest(con=None):
    """当前回测策略清单（当前仅一项）。"""
    return [describe_current_backtest(con)]
