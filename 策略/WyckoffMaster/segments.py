# -*- coding: utf-8 -*-
"""v3.1 冻结时间段与段执行规格（半开 UTC 区间）。

来源：`docs/WyckoffMaster_v3.1_执行补充与窗口交接_2026-10-15.md` 第 1、5、6 条。

纪律：
  · 半开区间 [start, end)：`start <= t < end`。
  · 三段**各自** 5000 USDT、**零仓零挂单**起算；段界重置 cash / orders / positions /
    execution_state / equity_peak。
  · 段末未平仓用**本段最后 15m 收盘**强平（计不利滑点、费用、应结资金费），
    **不用下一段开盘**。
  · 起点前仅指标预热（只读、不交易）；是否沿用预热期形成的有效信号锚点须一致，
    且**不得产生段前开仓**。
  · 本模块只定义规格常量与判定函数，不读任何数据。

Holdout 红线：Holdout 段在本模块中**只有边界定义**，选型阶段不得读取/运行。
"""
from __future__ import annotations

from datetime import datetime, timezone

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MIN15_MS = 900_000


def utc_ms(y, mo, d) -> int:
    return int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp() * 1000)


# 冻结三段（半开 UTC）
SEGMENTS = {
    "IS": {"start_ms": utc_ms(2022, 1, 1), "end_ms": utc_ms(2025, 1, 1)},
    "Val": {"start_ms": utc_ms(2025, 1, 1), "end_ms": utc_ms(2026, 4, 15)},
    "Holdout": {"start_ms": utc_ms(2026, 4, 15), "end_ms": utc_ms(2026, 10, 15)},
}
SEGMENT_ORDER = ("IS", "Val", "Holdout")

INITIAL_EQUITY = 5000.0
# 段界必须重置的状态
SEGMENT_RESET_STATE = ("cash", "orders", "positions", "execution_state", "equity_peak")
# 段末强平口径
SEGMENT_END_RULE = "segment_end_force_close_last_15m_close"

# 执行费用/时钟（v3.1 第 6 条：双边每边手续费 0.05%、滑点 0.05%，全部真实结算资金费）
EXEC_COST = {
    "fee_each_side": 0.0005,
    "slippage_each_side": 0.0005,
    "max_holding_hours": 720,
    "entry_participation_prev15m_quote_cap": 0.01,
    "stop_checked_per": "15m",              # 止损逐 15m 检查，非仅 1h/4h 收盘
    "negative_funding_income_retained": True,
    "forbidden_funding_mark_substitutes": ("norm4h", "ext1h", "zero_fill", "ordinary_close"),
}


def segment_of(t_ms: int) -> str | None:
    """返回 t（毫秒，UTC）所属段名；不在任何段内返回 None。半开区间。"""
    for name in SEGMENT_ORDER:
        s = SEGMENTS[name]
        if s["start_ms"] <= t_ms < s["end_ms"]:
            return name
    return None


def segment_bounds(name: str) -> tuple[int, int]:
    s = SEGMENTS[name]
    return s["start_ms"], s["end_ms"]


def is_holdout(t_ms: int) -> bool:
    """Holdout 红线判定：任何落在 Holdout 区间的时间点都返回 True。"""
    s = SEGMENTS["Holdout"]
    return s["start_ms"] <= t_ms < s["end_ms"]


def assert_no_holdout(times_ms) -> None:
    """选型阶段守卫：若任一时间点落在 Holdout 区间则抛错（物理拒绝）。"""
    bad = [t for t in times_ms if is_holdout(int(t))]
    if bad:
        raise ValueError(f"HOLDOUT_GUARD: 选型阶段禁止触及 Holdout 区间，收到 {len(bad)} 个点，"
                         f"首个 {bad[0]}")


def segment_length_days(name: str) -> float:
    s = SEGMENTS[name]
    return (s["end_ms"] - s["start_ms"]) / DAY_MS


def spec() -> dict:
    return {
        "version": "3.1",
        "segments": {k: {"start_ms": v["start_ms"], "end_ms": v["end_ms"],
                         "start_utc": datetime.fromtimestamp(v["start_ms"] / 1000, timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "end_utc": datetime.fromtimestamp(v["end_ms"] / 1000, timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "length_days": segment_length_days(k)}
                     for k, v in SEGMENTS.items()},
        "initial_equity": INITIAL_EQUITY,
        "segment_reset_state": list(SEGMENT_RESET_STATE),
        "segment_end_rule": SEGMENT_END_RULE,
        "exec_cost": EXEC_COST,
        "holdout_cutoff_utc": datetime.fromtimestamp(
            SEGMENTS["Holdout"]["end_ms"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "holdout_cutoff_beijing": "2026-10-15 08:00 (UTC+8)",
    }
