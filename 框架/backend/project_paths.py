# -*- coding: utf-8 -*-
"""工程内部路径**唯一解析器**（W1 幽灵依赖清理 · 2026-10-16）。

纪律（不可回退）
────────────────
1. **只解析工程内部路径**：全部由 `PROJECT_ROOT`（= 本文件 `parents[2]`）或**显式环境变量**
   推导；**禁止**出现工程外绝对路径（旧工作区 / 外部检出目录 / 外部工作树推导）。
2. **配置驱动**：外部注入一律走环境变量（`MARKET_DB` / `MARKET_ARCHIVE` /
   `PAPER_LEDGER_DIR` / `EFFECTIVE_VIEW` / `RESEARCH_INPUT_DIR`）；缺省值 = 工程内正式路径。
3. **不静默 fallback**：本模块**只**做「路径解析」，不做「文件存在性兜底」。
   需要 fail-closed 的调用方（账本、视图）必须自己判 `exists()` 并**显式**声明缺失态。
4. **外部研究输入已迁入工程**：`数据/研究输入/外部迁移_2026-10-16/`（来源为原外部工作树，
   2026-10-16 整体复制，45 MB，逐字节未改）。原外部目录**不再是依赖**。
5. **冻结参数生效视图 v2.2 已丢失**（随旧工作区一并删除，全盘无副本）。
   此处只给出「若恢复应放哪」的工程内正式路径，**不伪造内容**。
   调用方读不到 ⇒ 必须显式标注 `source=fallback`/`unavailable`，不得冒充生效视图。

纯路径常量与纯函数：不读库、不联网、不下单。
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
FRAMEWORK_DIR = BACKEND_DIR.parent
PROJECT_ROOT = Path(os.environ.get("DATA_HUB_PROJECT") or FRAMEWORK_DIR.parent).resolve()


def _env_path(name: str):
    """环境变量 → 绝对 Path；未设置 / 空串 ⇒ None（**不猜默认**）。"""
    v = os.environ.get(name)
    if not v:
        return None
    return Path(v).expanduser().resolve()


def internal(*parts) -> Path:
    """工程内路径拼接（唯一入口，禁止调用方自行 `../../` 推导）。"""
    return PROJECT_ROOT.joinpath(*parts)


# ══════════════════════════ 行情数据（热库 / 冷档）══════════════════════════

MARKET_DB = (_env_path("MARKET_DB")
             or internal("数据", "市场数据", "db", "market_v2.sqlite3"))
MARKET_ARCHIVE = (_env_path("MARKET_ARCHIVE")
                  or internal("数据", "市场数据", "parquet"))

# ══════════════════════════ 纸面账本（缺库 ⇒ 调用方 fail-closed）═════════════

PAPER_LEDGER_DIR = (_env_path("PAPER_LEDGER_DIR")
                    or internal("数据", "paper"))

# ══════════════════════════ 研究输入（原外部依赖的工程内落点）════════════════

RESEARCH_INPUT_DIR = (_env_path("RESEARCH_INPUT_DIR")
                      or internal("数据", "研究输入"))
# 2026-10-16 由外部检出目录（原外部工作树）整体迁入
EXTERNAL_MIGRATION_DIR = RESEARCH_INPUT_DIR / "外部迁移_2026-10-16"

# 本次离线决策（375 研究池 / 逐日准入资格 / 保守执行近似规格）
LEGACY_DECISION_DIR = EXTERNAL_MIGRATION_DIR / "本次离线决策_2026-10-12"
# Work 派工（118 新候选日历等待确认件等）
LEGACY_DISPATCH_DIR = EXTERNAL_MIGRATION_DIR / "Work派工_2026-10-11"
# 策略与回测规则（v2 规格 / 研究参数 v2）
LEGACY_RULES_DIR = EXTERNAL_MIGRATION_DIR / "策略与回测规则_2026-10-11"
# 项目闭环审查（含 v2.1 生效视图，仅历史追溯）
LEGACY_REVIEW_DIR = EXTERNAL_MIGRATION_DIR / "项目闭环审查_2026-10-11"

# ══════════════════════════ 冻结参数生效视图（已丢失，不伪造）════════════════
# 唯一合法来源是 `F_WX_v2.2_project_closure_user_5000_shared` 生效视图。
# 该文件随旧工作区删除，**全盘无副本** ⇒ 这里只声明「若恢复应放哪」。
EFFECTIVE_VIEW_RELPATH = Path(
    "最新唐奇安威科夫接通与收尾_2026-10-11") / "5000U收尾与回测条件终验6" / (
    "10_研究参数生效视图_v2_2_user5000_shared.json")
EFFECTIVE_VIEW = (_env_path("EFFECTIVE_VIEW")
                  or RESEARCH_INPUT_DIR / EFFECTIVE_VIEW_RELPATH)
EFFECTIVE_VIEW_MISSING_NOTE = (
    "冻结参数生效视图 v2.2 已随旧工作区一并删除，"
    "全盘无副本；不得伪造、不得以库内值冒充。")

__all__ = [
    "BACKEND_DIR", "FRAMEWORK_DIR", "PROJECT_ROOT", "internal",
    "MARKET_DB", "MARKET_ARCHIVE", "PAPER_LEDGER_DIR",
    "RESEARCH_INPUT_DIR", "EXTERNAL_MIGRATION_DIR",
    "LEGACY_DECISION_DIR", "LEGACY_DISPATCH_DIR", "LEGACY_RULES_DIR", "LEGACY_REVIEW_DIR",
    "EFFECTIVE_VIEW", "EFFECTIVE_VIEW_RELPATH", "EFFECTIVE_VIEW_MISSING_NOTE",
]
