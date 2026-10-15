# -*- coding: utf-8 -*-
"""WyckoffMaster 参数冻结（v3.1）。

来源与边界：
  · 权威 = `docs/WyckoffMaster_v3.1_执行补充与窗口交接_2026-10-15.md`（v3.1 正式开工令，
    覆盖 2026-10-15 上一版施工单中与之冲突部分）。
  · v3.1 相对 v3 的强制变更（本模块据此实现，不得回退）：
      (1) 半开 UTC 三段：IS/Val/Holdout 见 `segments.py`；三段各自 5000U、零仓零挂单起算，
          段末用**本段最后 15m 收盘**强平（非下一段开盘）。
      (2) 结构止损上限：M2 = D>3.5ATR **拒单**；若任一类型 IS+Val 合并超限率 > 30%，
          自动加跑预注册 **M2b**：D = max(P_entry-(L_struct-0.8ATR), 1.5ATR)，不因 >3.5ATR 拒单，
          数量按 0.5% 权益 / 实际距离**自然缩小**。
      (3) 穿底拒单：使用结构低点的候选若 P_entry <= L_struct ⇒ 拒单原因严格
          `entry_below_struct_low`（M2/M2b 均适用）。
      (4) 本轮历史阶梯**只跑 M0–M4 及条件 M2b**；M5/M6 改为**前向验证**
          （M5 30 日分位>90% 过滤；M6 30 日 Z 降序排序），每币 ≥30 自然日且 ≥90 唯一有效
          有来源时间快照才激活。
  · 本文件只放**冻结常量与校验**，不含计算逻辑；不改任何阈值。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields

VERSION = "3.1"

# ── 模式（不是阈值）──
ENTRY_MODES = ("donchian_only", "three_buy_points")
STOP_MODES = ("uniform", "structural", "structural_m2b")
TRAIL_MODES = ("none", "naive_be", "step_swing")
RANK_MODES = ("quote_vol_30d_median", "oi_change_90d_pct")
# v3.1：M5/M6 仅前向验证；历史阶梯不使用
FACTOR_FILTERS = (
    "none",
    "global_account_ratio_30d_pct_gt_0.90",      # M5（前向）
    "top_position_ratio_30d_z_desc_rank",        # M6（前向，排序）
)

# ── M2 / M2b 触发 ──
M2_OVER_LIMIT_TRIGGER_RATE = 0.30   # 任一类型 IS+Val 合并超限拒单率 > 30% ⇒ 加跑 M2b

# ── M5/M6 前向激活门槛（v3.1）──
FORWARD_WINDOW_DAYS = 30            # 自身 30 日滚动
FORWARD_MIN_NATURAL_DAYS = 30       # 每币 ≥30 自然日
FORWARD_MIN_UNIQUE_SNAPSHOTS = 90   # 且 ≥90 唯一、有效、有来源时间快照


@dataclass(frozen=True)
class MasterParams:
    """合体策略冻结参数（v2.0 修正定稿 + v3 修正 + v3.1 修正）。阈值不变。"""

    # —— 唐奇安 / 均量 / ATR ——
    don_entry: int = 20
    don_exit: int = 10
    atr_n: int = 20
    vol_n: int = 20

    # —— Spring ——
    tr_atr_mult: float = 4.0
    spring_vol_mult: float = 1.5
    spring_close_pos: float = 0.50
    spring_depth_atr: float = 0.60
    spring_confirm_bars: int = 3

    # —— 突破 / 过热 / 回踩 ——
    breakout_vol_mult: float = 1.5
    overheat_range_atr: float = 2.5
    overheat_ext_atr: float = 1.2
    retest_window_bars: int = 5

    # —— 止损 / 目标 ——
    uniform_stop_atr: float = 3.0
    wick_buffer_atr: float = 0.8
    min_stop_atr: float = 1.5     # 下限（不足只向更低处扩大；M2b 亦以此为下限）
    max_stop_atr: float = 3.5     # 上限（M2 超限拒单；M2b 不设此上限）
    target_rr: float = 2.0        # entry + 2R，仅参考

    # —— 台阶锁利 ——
    trail_activate_atr: float = 2.0
    trail_lookback_bars: int = 5
    trail_min_bars_after_entry: int = 5
    trail_min_above_entry_atr: float = 0.3

    # —— 保本对照 ——
    be_trigger_r: float = 1.0

    # —— 到期 ——
    max_holding_hours: int = 720

    def to_dict(self) -> dict:
        return asdict(self)

    def identity(self) -> dict:
        return {k: getattr(self, k) for k in sorted(f.name for f in fields(self))}


# 只读单例：所有实验共享同一套阈值（禁止在实验中改阈值，只允许切换模式）。
FROZEN = MasterParams()

VALIDATION = {
    "don_entry": (1, None, int), "don_exit": (1, None, int),
    "atr_n": (2, None, int), "vol_n": (2, None, int),
    "tr_atr_mult": (0.0, None, float), "spring_vol_mult": (0.0, None, float),
    "spring_close_pos": (0.0, 1.0, float), "spring_depth_atr": (0.0, None, float),
    "spring_confirm_bars": (1, None, int), "breakout_vol_mult": (0.0, None, float),
    "overheat_range_atr": (0.0, None, float), "overheat_ext_atr": (0.0, None, float),
    "retest_window_bars": (1, None, int), "uniform_stop_atr": (0.0, None, float),
    "wick_buffer_atr": (0.0, None, float), "min_stop_atr": (0.0, None, float),
    "max_stop_atr": (0.0, None, float), "target_rr": (0.0, None, float),
    "trail_activate_atr": (0.0, None, float), "trail_lookback_bars": (1, None, int),
    "trail_min_bars_after_entry": (1, None, int), "trail_min_above_entry_atr": (0.0, None, float),
    "be_trigger_r": (0.0, None, float), "max_holding_hours": (1, None, int),
}


def validate_params(p: MasterParams = FROZEN) -> list[str]:
    errs: list[str] = []
    for name, (lo, hi, kind) in VALIDATION.items():
        v = getattr(p, name)
        if kind is int and (not isinstance(v, int) or isinstance(v, bool)):
            errs.append(f"{name} 必须为 int（得到 {v!r}）")
            continue
        if kind is float and isinstance(v, bool):
            errs.append(f"{name} 必须为 float（得到 {v!r}）")
            continue
        fv = float(v)
        if lo is not None and fv < lo:
            errs.append(f"{name} 必须 >= {lo}（得到 {fv}）")
        if hi is not None and fv > hi:
            errs.append(f"{name} 必须 <= {hi}（得到 {fv}）")
    if p.min_stop_atr > p.max_stop_atr:
        errs.append(f"min_stop_atr({p.min_stop_atr}) 不得大于 max_stop_atr({p.max_stop_atr})")
    return errs
