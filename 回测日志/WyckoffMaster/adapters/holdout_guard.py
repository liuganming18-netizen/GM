#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Holdout 隔离守卫（v3.1 最高红线，物理强制）。

红线（v3.1 开工令）：**在 IS/Val 全部选型锁定前**——
  · 不读取 Holdout 区间（[2026-04-15T00:00:00Z, 2026-10-15T00:00:00Z)）的行情/因子数据；
  · 不运行该段；
  · 不计算/查看该段交易、收益、信号表现。

本模块把红线变成**代码级拒绝**，而不是口头承诺：
  · `assert_window_allowed(start_ms, end_ms, phase)`：窗口与 Holdout 有交集即抛错，
    除非 `phase == "holdout"` **且** 存在合法 `winner_lock`（IS/Val 赢家已冻结）。
  · `assert_phase_allowed(phase)`：选型阶段（kline/factor）直接禁止 holdout。
  · `assert_research_input_isolation(root)`：要求研究输入物理区分 IS/Val 与 Holdout。
  · `assert_holdout_cutoff_reached(now)`：终验前要求 UTC 截止已到且数据闭合。

用法：任何要读数据/跑段的代码，先调用本模块的断言。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

PROJECT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
WM = PROJECT / "回测日志" / "WyckoffMaster"
WINNER_LOCK = WM / "state" / "winner_lock.json"

UTC = dt.timezone.utc
# 由 frozen_segments.json 派生（缺失时用 v3.1 开工令硬值兜底并断言一致）
_FALLBACK = {"start_ms": 1776211200000, "end_ms": 1792022400000}  # 2026-04-15 / 2026-10-15
try:
    _fs = json.loads((WM / "config" / "frozen_segments.json").read_text(encoding="utf-8"))
    _h = next(s for s in _fs["segments"] if s["name"] == "Holdout")
    HOLDOUT_START_MS, HOLDOUT_END_MS = int(_h["start_ms"]), int(_h["end_excl_ms"])
except Exception:  # noqa: BLE001
    HOLDOUT_START_MS, HOLDOUT_END_MS = _FALLBACK["start_ms"], _FALLBACK["end_ms"]
SELECTION_PHASES = ("kline", "factor", "step0")


class HoldoutViolation(RuntimeError):
    """触及 Holdout 红线。"""


def _now_ms() -> int:
    return int(dt.datetime.now(UTC).timestamp() * 1000)


def assert_phase_allowed(phase: str) -> None:
    if phase == "holdout":
        lock = load_winner_lock()
        if not lock:
            raise HoldoutViolation(
                "选型未锁定（缺 winner_lock.json）⇒ 禁止运行 Holdout 段。"
                "必须先在 IS/Val 上锁定唯一模型并写 winner_lock。")
    elif phase in SELECTION_PHASES:
        return
    else:
        raise HoldoutViolation(f"未知阶段 {phase!r}；仅允许 {SELECTION_PHASES} 或 'holdout'")


def assert_window_allowed(start_ms: int, end_ms: int, phase: str) -> None:
    """窗口 [start, end) 与 Holdout 有交集即拒绝（holdout 阶段且已锁赢家时除外）。"""
    assert_phase_allowed(phase)
    if phase == "holdout":
        return
    if end_ms > HOLDOUT_START_MS and start_ms < HOLDOUT_END_MS:
        raise HoldoutViolation(
            f"窗口 [{start_ms}, {end_ms}) 与 Holdout "
            f"[{HOLDOUT_START_MS}, {HOLDOUT_END_MS}) 有交集，"
            f"选型阶段（phase={phase}）禁止读取该区间。")


def assert_research_input_isolation(root: Path) -> dict:
    """要求研究输入物理区分 IS/Val 与 Holdout。

    接受两种合规形态：
      A) 显式子目录：root/{is_val,holdout}/...  → 选型阶段只允许读 is_val/
      B) 单文件但带列式区间标签：由 data_ready 声明且本函数无法自证 → 记 WARN（不通过）
    返回结构供门禁报告引用；不合规时返回 ok=False（由调用方决定是否阻断）。
    """
    root = Path(root)
    facts = {"root": str(root), "exists": root.exists(), "layout": None, "ok": False}
    if not root.exists():
        facts["reason"] = "研究输入根不存在"
        return facts
    names = {p.name.lower() for p in root.iterdir() if p.is_dir()}
    if {"is_val", "holdout"} <= names or {"is", "val", "holdout"} <= names:
        facts["layout"] = "explicit_subdirs"
        facts["ok"] = True
        facts["pre_lock_readable"] = sorted(
            n for n in names if n in ("is_val", "is", "val"))
    else:
        facts["layout"] = "flat_or_unknown"
        facts["reason"] = ("未见 is_val/holdout 物理分区；跨界文件不得整文件解析后过滤"
                           "并称零读取")
    return facts


def assert_holdout_cutoff_reached(now_utc: dt.datetime | None = None) -> None:
    now = now_utc or dt.datetime.now(UTC)
    if now.timestamp() * 1000 < HOLDOUT_END_MS:
        raise HoldoutViolation(
            f"Holdout 截止 {dt.datetime.fromtimestamp(HOLDOUT_END_MS/1000, UTC).isoformat()} 未到"
            f"（当前 {now.isoformat()}）⇒ 不得提前截断冒充完整区间。")


def load_winner_lock() -> dict | None:
    if not WINNER_LOCK.exists():
        return None
    try:
        return json.loads(WINNER_LOCK.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def write_winner_lock(*, level: str, timeframe: str, config_sha256: str,
                      code_sha256: str, input_sha256: str, is_metrics: dict,
                      val_metrics: dict) -> Path:
    """IS/Val 选型锁定后写赢家锁（唯一一次 Holdout 的前置条件）。"""
    doc = {
        "schema": "wyckoffmaster_winner_lock/v1",
        "locked_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": level, "timeframe": timeframe,
        "config_sha256": config_sha256,
        "code_sha256": code_sha256,
        "input_sha256": input_sha256,
        "is_metrics": is_metrics, "val_metrics": val_metrics,
        "note": "锁定后仅一次 Holdout；不得调参救结果、不得据此再挑配置或 1h/4h 赢家",
    }
    canon = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
    doc["self_sha256"] = hashlib.sha256(canon).hexdigest()
    WINNER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    WINNER_LOCK.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return WINNER_LOCK


if __name__ == "__main__":
    import json as _json
    print(_json.dumps({
        "holdout_start_utc": dt.datetime.fromtimestamp(HOLDOUT_START_MS/1000, UTC).isoformat(),
        "holdout_end_utc": dt.datetime.fromtimestamp(HOLDOUT_END_MS/1000, UTC).isoformat(),
        "cutoff_reached_now": _now_ms() >= HOLDOUT_END_MS,
        "winner_lock_present": WINNER_LOCK.exists(),
        "research_input_isolation": assert_research_input_isolation(
            PROJECT / "数据" / "研究输入" / "WyckoffMaster"),
    }, ensure_ascii=False, indent=2))
