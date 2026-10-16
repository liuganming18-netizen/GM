# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 回测引擎 · 路径与隔离纪律（回测窗口唯一写入根）。

与旧实现（adapters/ra_bridge.py）的差别：旧实现依赖已不存在的
`WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/{后端实现,回测资源改造_2026-10-14,
全池真实费用回测_2026-10-13}`；本模块把依赖全部收回到**项目内**：

  · 策略共用核心   = 项目 `策略/WyckoffMaster/`（任务二交付，只读消费）
  · 执行原语       = 项目 `框架/backend/strategies/wx_execution.py`（只读复用）
  · 研究输入       = 项目 `数据/研究输入/WyckoffMaster_v31/{is,val}`（物理隔离 IS/Val）
  · 写入           = 项目 `回测日志/WyckoffMaster/`（本窗口唯一写入根）

不再引用旧研究目录（v3.0 全时段 `数据/研究输入/WyckoffMaster/`，含 Holdout）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
WM = PROJECT / "回测日志" / "WyckoffMaster"
WRITE_ROOT = WM

STRATEGY_ROOT = PROJECT / "策略"          # 含 WyckoffMaster 包
BACKEND = PROJECT / "框架" / "backend"    # 含 strategies/wx_execution.py
V31_ROOT = PROJECT / "数据" / "研究输入" / "WyckoffMaster_v31"

OLD_RESEARCH_DIR = PROJECT / "数据" / "研究输入" / "WyckoffMaster"  # v3.0，含 Holdout，禁止用于选型

# 本窗口只写这些子目录
WRITE_SUBDIRS = ("engine", "adapters", "scripts", "config", "state", "ledgers", "logs", "evidence")

# v3.1 冻结区间（与 策略/WyckoffMaster/segments.py、config/frozen_segments.json 一致）
SEGMENTS = {
    "IS": ("2022-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
    "Val": ("2025-01-01T00:00:00Z", "2026-04-15T00:00:00Z"),
    "Holdout": ("2026-04-15T00:00:00Z", "2026-10-15T00:00:00Z"),
}
SELECTION_SEGMENTS = ("IS", "Val")
HOLDOUT_START_MS = 1776211200000     # 2026-04-15T00:00:00Z
HOLDOUT_END_MS = 1792022400000       # 2026-10-15T00:00:00Z

# 执行费用（v3.1 冻结：双边每边 0.05% 手续费 + 0.05% 滑点）
FEE_EACH_SIDE = 0.0005
SLIPPAGE_EACH_SIDE = 0.0005
DETAIL_MS = 900_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

# 冻结生产引擎 SHA（防止他窗改写执行原语）
FROZEN_WX_EXECUTION_SHA = "0ede9ce19c7cec9d18398572e393ef65ef4c04a6aa5ff2b41f8ad022c83dbcf1"


class IsolationError(RuntimeError):
    """隔离纪律被违反。"""


class HoldoutViolation(RuntimeError):
    """触及 Holdout 红线。"""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_write_target(p: Path) -> Path:
    """任何写目标必须落在 WRITE_ROOT 内，否则拒绝。"""
    rp = Path(p).resolve()
    root = WRITE_ROOT.resolve()
    if root not in rp.parents and rp != root:
        raise IsolationError(f"写目标越界（只允许写 {root}）：{rp}")
    return rp


def assert_segment_not_holdout(seg: str) -> None:
    if seg not in SELECTION_SEGMENTS:
        raise HoldoutViolation(
            f"选型阶段只允许段 {SELECTION_SEGMENTS}，收到 {seg!r}（Holdout 仅在最终锁定后单次运行）")


def assert_no_holdout_ts(ts_ms_iter) -> None:
    bad = [int(t) for t in ts_ms_iter if HOLDOUT_START_MS <= int(t) < HOLDOUT_END_MS]
    if bad:
        raise HoldoutViolation(
            f"数据触及 Holdout 区间 [{HOLDOUT_START_MS},{HOLDOUT_END_MS})：{len(bad)} 点，首个 {bad[0]}")


def assert_production_untouched() -> dict:
    """核验生产执行引擎 SHA 未被改写（防他窗改动执行语义）。"""
    p = BACKEND / "strategies" / "wx_execution.py"
    if not p.exists():
        raise IsolationError(f"缺生产执行引擎：{p}")
    got = sha256_file(p)
    ok = (got == FROZEN_WX_EXECUTION_SHA)
    if not ok:
        raise IsolationError(
            "生产执行引擎 wx_execution.py SHA 与冻结值不符，拒绝运行：\n"
            f"  expect {FROZEN_WX_EXECUTION_SHA}\n  got    {got}")
    return {"wx_execution_sha256": got, "wx_execution_sha_ok": ok}


def load_v31_manifest() -> dict:
    return json.loads((V31_ROOT / "manifest_v31.json").read_text(encoding="utf-8"))


def input_manifest_sha() -> str:
    return sha256_file(V31_ROOT / "manifest_v31.json")


if __name__ == "__main__":
    print(json.dumps({
        "write_root": str(WRITE_ROOT),
        "strategy_root": str(STRATEGY_ROOT),
        "v31_root": str(V31_ROOT),
        "v31_manifest_sha256": input_manifest_sha(),
        "production": assert_production_untouched(),
    }, ensure_ascii=False, indent=2))
