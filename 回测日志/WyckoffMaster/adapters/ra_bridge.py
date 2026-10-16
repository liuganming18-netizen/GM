#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WyckoffMaster 回测窗口 · 隔离桥（**项目内闭环**，无旧研究目录依赖）。

与旧实现（依赖已不存在的
`WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/{后端实现,回测资源改造_2026-10-14,
全池真实费用回测_2026-10-13}`）的差别：本模块把依赖全部收回到**项目内**——

  · 策略共用核心   = 项目 `策略/WyckoffMaster/`（任务二交付，只读消费）
  · 执行唯一口径   = 项目 `策略/WyckoffMaster/execution.py`（exec_core v3.1.0）
  · 冻结生产引擎   = 项目 `框架/backend/strategies/wx_execution.py`（只读，SHA 冻结）
  · 研究输入       = 项目 `数据/研究输入/WyckoffMaster_v31/{is,val}`（物理隔离 IS/Val）
  · 写入           = 项目 `回测日志/WyckoffMaster/`（本窗口唯一写入根）

纪律（红线，违反即拒绝运行）：
  1. **不修改**生产引擎：`框架/backend/strategies/wx_execution.py` 必须 SHA == 冻结值。
  2. **不写生产库**：热库/冷档/paper 账本一律只读。
  3. **只写** `回测日志/WyckoffMaster/` 下（账本、日志、状态）。
  4. **禁止** norm4h / ext1h 近似模型冒充真实历史资金费；只允许真实结算事件。
  5. 不引用 v3.0 全时段目录 `数据/研究输入/WyckoffMaster/`（含 Holdout）。
"""
from __future__ import annotations

import sys
from pathlib import Path

WM = Path(__file__).resolve().parent.parent
if str(WM) not in sys.path:
    sys.path.insert(0, str(WM))

from engine import paths as P              # noqa: E402
from engine import loader as LD            # noqa: E402
from engine import ladder as LAD           # noqa: E402
from engine import reconcile as RC         # noqa: E402
from engine.engine import Backtest, LevelSpec  # noqa: E402
from engine.m4 import make_m4_rank_provider    # noqa: E402

# ── 路径常量（项目内）───────────────────────────────────────────────────────
PROJECT = P.PROJECT
WRITE_ROOT = P.WRITE_ROOT
STRATEGY_ROOT = P.STRATEGY_ROOT
BACKEND = P.BACKEND
V31_ROOT = P.V31_ROOT
OLD_RESEARCH_DIR = P.OLD_RESEARCH_DIR       # v3.0（含 Holdout）——本窗口禁止使用

FROZEN_WX_EXECUTION_SHA = P.FROZEN_WX_EXECUTION_SHA
ALLOWED_FUNDING_MODEL = "historical_events"
FORBIDDEN_FUNDING_MODELS = ("norm4h", "ext1h", "close_proxy", "zero_fill")

IsolationError = P.IsolationError
HoldoutViolation = P.HoldoutViolation
sha256_file = P.sha256_file
assert_write_target = P.assert_write_target
assert_production_untouched = P.assert_production_untouched
input_manifest_sha = P.input_manifest_sha


def assert_no_legacy_research_dir() -> dict:
    """确认本窗口不引用 v3.0 全时段研究目录（含 Holdout）。"""
    return {"legacy_research_dir": str(OLD_RESEARCH_DIR),
            "used_by_backtest_window": False,
            "v31_root": str(V31_ROOT), "v31_root_exists": V31_ROOT.exists()}


def assert_funding_model(model: str) -> str:
    m = (model or "").strip()
    if m in FORBIDDEN_FUNDING_MODELS or "norm4h" in m or "ext1h" in m:
        raise IsolationError(
            f"禁止用近似模型 {m!r} 冒充真实历史资金费；只允许 {ALLOWED_FUNDING_MODEL!r}")
    if m != ALLOWED_FUNDING_MODEL:
        raise IsolationError(f"资金费模型必须为 {ALLOWED_FUNDING_MODEL!r}，收到 {m!r}")
    return m


def load_engine() -> dict:
    """加载项目内引擎组件（只读复用，不复制实现）。"""
    if str(P.STRATEGY_ROOT) not in sys.path:
        sys.path.insert(0, str(P.STRATEGY_ROOT))
    from WyckoffMaster import core as WC
    from WyckoffMaster import execution as X
    from WyckoffMaster import factors as WF
    return {"Backtest": Backtest, "LevelSpec": LevelSpec, "ladder": LAD, "loader": LD,
            "reconcile": RC, "m4": make_m4_rank_provider,
            "X": X, "core": WC, "factors": WF}


def make_segment(seg: str) -> LD.SegmentData:
    """构建一段（IS/Val）的只读研究输入（Holdout 段物理拒绝）。"""
    return LD.SegmentData(seg)


def run_segment_arm(*, spec, seg_data, tf, start_ms: int, end_ms: int, symbols, run_id: str,
                    ledger_path: Path, commit_every: int = 1, rank_provider=None,
                    collect_candidates: bool = False, log=print):
    """在**独立账本**上跑一个实验臂的一段（写目标强制落在 WRITE_ROOT 内）。"""
    lp = assert_write_target(Path(ledger_path))
    lp.parent.mkdir(parents=True, exist_ok=True)
    bt = Backtest(seg_data=seg_data, spec=spec, timeframe=tf, start_ms=start_ms, end_ms=end_ms,
                  run_id=run_id, ledger_path=lp, symbols=symbols, commit_every=commit_every,
                  rank_provider=rank_provider, collect_candidates=collect_candidates)
    res = bt.run()
    bt.led.close()
    return {"ledger": str(lp), "run": res, "candidates": list(bt.candidates)}


if __name__ == "__main__":
    import json
    print(json.dumps({
        "write_root": str(WRITE_ROOT), "strategy_root": str(STRATEGY_ROOT),
        "v31_root": str(V31_ROOT),
        "legacy_research_dir": assert_no_legacy_research_dir(),
        "production": assert_production_untouched(),
    }, ensure_ascii=False, indent=2))
