# -*- coding: utf-8 -*-
"""旧策略（3a–3d / A / B）**退役**的单一事实源（WORK-04 · 2026-10-16）。

背景
────
新候选策略 `WX_OI_RESONANCE_V1`（注册表键 `wx_oi_resonance_v1`）已是**唯一生产候选**。
旧的 `F_WX 3a/3b/3c/3d`（唐奇安威科夫四臂）与旧 `A`/`B` 是**历史策略**，
本模块把它们的**可运行入口**统一标记为「已退役」，避免：

  · 旧策略被当成当前策略运行 / 展示；
  · 旧账本被当成新策略成绩；
  · Paper / Live 在**尚未接新共享核心**时偷偷回退到旧 `exec_bridge`。

纪律（不可回退）
────────────────
1. **只退役「可运行入口」，不删除任何代码 / 账本 / 证据 / 测试 / Golden / 行情数据**。
   旧策略源码与历史 runs 走**可恢复只读归档**（见 `evidence/Work_旧策略退役_2026-10-16/`）。
2. Paper / Live **未接新核心**时一律 `NOT_CONNECTED`：禁止新开仓、禁止真实下单，
   **不回退旧 `exec_bridge`**。
3. 当前回测 / 运行列表**只含新策略** `wx_oi_resonance_v1`；旧 runs 被显式排除并标注归属。
4. `_pool_scope` **不是**旧策略代码：它是 `live_pool` 的**币池依赖**（历史下载口径），
   有工程内替代来源 `数据/contracts/旧标的池/scope_crypto.json` 且被既有测试直接引用
   ⇒ **保留，不退役**（见 `_POOL_SCOPE_DECISION`）。

纯常量 + 纯函数：不读库、不联网、不下单、不写任何文件。
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("DATA_HUB_PROJECT") or BACKEND_DIR.parents[1]).resolve()

# ══════════════════════════ 退役身份 ══════════════════════════

RETIRED_AT = "2026-10-16"

# 旧 `F_WX` 四臂 + 旧 A/B（注册表键）
RETIRED_FWX_KEYS = ("3a", "3b", "3c", "3d")
RETIRED_AB_KEYS = ("A", "B")
RETIRED_STRATEGY_KEYS = RETIRED_FWX_KEYS + RETIRED_AB_KEYS

# 唯一生产候选（新）
SUCCESSOR_STRATEGY_ID = "WX_OI_RESONANCE_V1"
SUCCESSOR_STRATEGY_KEY = "wx_oi_resonance_v1"

RETIREMENT_REASON = (
    "旧 F_WX（3a/3b/3c/3d）与旧 A/B 已退役：新候选 WX_OI_RESONANCE_V1 是唯一生产候选；"
    "旧策略不再进入任何可运行入口（Registry / Backtest / Paper / Live / UI）。"
    "旧源码与历史 runs 以**可恢复只读归档**保留，不得删除、不得改作新策略成绩。"
)

# ══════════════════════════ 归档位置（可恢复只读）══════════════════════════

ARCHIVE_DIR = PROJECT_ROOT / "evidence" / "Work_旧策略退役_2026-10-16"
ARCHIVE_MANIFEST = ARCHIVE_DIR / "归档清单_旧策略_2026-10-16.json"
ARCHIVE_README = ARCHIVE_DIR / "README_归档与恢复.md"
# 旧策略源码所在（**只读保留**，不删除）
LEGACY_SOURCE_DIRS = (
    PROJECT_ROOT / "框架" / "backend" / "strategies",            # 3a–3d / A / B
    PROJECT_ROOT / "策略" / "WyckoffMaster" / "_legacy_archive_v31",  # 旧 v3.1 核心（逐字节封存）
)
# 旧回测 / 复盘 runs（**只读保留**，不进入新策略运行列表）
LEGACY_RUN_DIRS = (
    PROJECT_ROOT / "回测日志" / "WyckoffMaster",
    PROJECT_ROOT / "回测日志" / "ledgers_ra",
)

# ══════════════════════════ `_pool_scope` 退役判定 ══════════════════════════
# 逐个确认替代来源后才可退役；**不以「引用 0」为由盲删**。
_POOL_SCOPE_DECISION = {
    "module": "_pool_scope",
    "retired": False,                    # 保留
    "role": "live_pool 的币池（历史下载口径）依赖，非旧策略代码",
    "current_usage": "live_pool.py 顶部 `import _pool_scope`（缺失则用内置 fallback）",
    "replacement_source": "数据/contracts/旧标的池/scope_crypto.json"
                          "（live_pool._FallbackPoolScope 读取）",
    "hard_dependents": [
        "框架/backend/tests/test_history_integrity.py（直接改 pr._pool_scope.SCOPE_JSON）",
        "框架/backend/tests/test_pool_gate.py（隔离路径依赖同口径）",
    ],
    "reason": "有工程内替代来源且被既有测试直接引用 ⇒ 保留；不得以「引用 0」为由盲删。",
}

# ══════════════════════════ 纯函数 ══════════════════════════


def is_retired(key) -> bool:
    """该注册表键是否属于**已退役**旧策略。"""
    return key in RETIRED_STRATEGY_KEYS


def retired_keys():
    return RETIRED_STRATEGY_KEYS


def pool_scope_decision() -> dict:
    """`_pool_scope` 的退役判定（如实披露：保留 + 替代来源 + 硬依赖）。"""
    return dict(_POOL_SCOPE_DECISION)


def retirement_block() -> dict:
    """统一的「退役」元数据块（供各响应/注册表内嵌）。"""
    return {
        "retired_strategy_keys": list(RETIRED_STRATEGY_KEYS),
        "retired_at": RETIRED_AT,
        "successor_strategy_id": SUCCESSOR_STRATEGY_ID,
        "successor_strategy_key": SUCCESSOR_STRATEGY_KEY,
        "reason": RETIREMENT_REASON,
        "archive_dir": str(ARCHIVE_DIR),
        "archive_manifest": str(ARCHIVE_MANIFEST),
        "recovery": "旧源码/旧 runs 均为**可恢复只读归档**：原件未删、未改；"
                    "按归档清单 SHA 可逐字节核对恢复。",
    }


def paper_live_status() -> dict:
    """Paper / Live 与**新共享核心**的接线状态（当前均未接线 ⇒ NOT_CONNECTED）。

    **当前恒 `connected=False`**：新共享核心只完成 Backtest 一侧；
    Paper / Live 接线属后续施工单。因此本函数**不**因任何运行时条件返回 True，
    避免「看起来接通」的假象。
    """
    return {
        "new_core": SUCCESSOR_STRATEGY_ID,
        "paper": {
            "connected": False,
            "status": "not_connected",
            "reason": "Paper 尚未接新共享核心（SimulatedBarFillAdapter 可复用，接线未做）"
                      "⇒ 禁止新开仓；不回退旧 exec_bridge / 旧 3a–3d 账本模型。",
        },
        "live": {
            "connected": False,
            "status": "not_connected",
            "reason": "Live 尚未接新共享核心（ExchangeFillAdapter 已就位但无交易所客户端）"
                      "⇒ 禁止真实下单；`assert_live_disabled()` 为硬闸，不回退旧 exec_bridge。",
        },
        "note": "Paper / Live 未接新核心期间，任何开仓/下单入口一律 NOT_CONNECTED；"
                "旧 exec_bridge 不得作为回退路径。",
    }


__all__ = [
    "RETIRED_AT", "RETIRED_FWX_KEYS", "RETIRED_AB_KEYS", "RETIRED_STRATEGY_KEYS",
    "SUCCESSOR_STRATEGY_ID", "SUCCESSOR_STRATEGY_KEY", "RETIREMENT_REASON",
    "ARCHIVE_DIR", "ARCHIVE_MANIFEST", "ARCHIVE_README",
    "LEGACY_SOURCE_DIRS", "LEGACY_RUN_DIRS",
    "is_retired", "retired_keys", "pool_scope_decision", "retirement_block",
    "paper_live_status",
]
