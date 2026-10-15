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

CURRENT_RUN_KEYS = tuple(wx.ARM_IDS)
LEGACY_KEYS = (strategy_a.KEY, strategy_b.KEY)

# 全量查找表（CLI / 历史查询用）；当前启用集见 CURRENT_RUN_KEYS。
REGISTRY = {**wx.REGISTRY, strategy_a.KEY: strategy_a, strategy_b.KEY: strategy_b}


def get(key):
    return REGISTRY.get(key)


def resolved_config(con, key, version=None):
    """统一配置解析：WX 走生效视图 / 库内登记；旧 A/B 走库内 strategy_configs。"""
    if key in wx.ARM_IDS:
        return wx_config.resolve(con, key, version)
    return common.load_config(con, key, version)


def describe(con, key):
    """返回单个策略的元数据 + 真实三层状态 + 配置状态。

    配置状态三态：已配置 / 待配置 / 配置无效（类型·范围·窗口关系不合法）。
    """
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
        "family": "F_WX" if is_wx else "legacy",
        "enabled_for_current_run": is_wx,
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
        "implementation_status": "implemented" if is_wx else "legacy_readable_only",
        "identity_ok": (resolved.get("identity_ok") if (is_wx and resolved) else None),
        "view_sha256": (resolved.get("view_sha256") if (is_wx and resolved) else None),
        "run_binding": (resolved.get("run_binding") if (is_wx and resolved) else None),
        "exec": (resolved.get("exec") if (is_wx and resolved) else None),
        "gate_status": ({"G01": "pending", "G02": "pending", "G03": "pending",
                         "G04": "pending", "G05": "pending"} if is_wx else None),
        "config": resolved["config"] if resolved else None,
        "note": ("参数由生效参数视图 v2.1 绑定并重算 canonical hash（含执行覆盖）；"
                 "运行放行 = 视图 run_frozen ∧ 身份一致 ∧ G01–G05 全 pass；"
                 "run_frozen=false ⇒ 未运行放行" if is_wx else
                 "历史策略：保留可读，不进入当前启用集（不改名冒充 F_WX）"),
    }


def list_all(con):
    return [describe(con, k) for k in REGISTRY]


def list_current(con):
    """当前启用族（F_WX 四组）。"""
    return [describe(con, k) for k in CURRENT_RUN_KEYS]


def list_legacy(con):
    return [describe(con, k) for k in LEGACY_KEYS]
