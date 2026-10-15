# -*- coding: utf-8 -*-
"""M0–M6 单变量阶梯实验定义（v3.1）。

v3.1 变更：
  · 本轮**历史阶梯只跑 M0–M4 及条件 M2b**（`historical=True`）。
  · M5/M6 改为**前向验证**（`historical=False`），不参与本轮历史选型。
  · M2b 为**条件加跑**（`conditional=True`）：仅当 M2 任一类型 IS+Val 合并超限拒单率
    > 30% 时触发；与 M2 同一父配置/数据/时间/排序/费用，只改距离上限。

每次只动**一个维度**；阈值一律沿用 params.FROZEN，实验中只允许切换模式。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .params import FROZEN, ENTRY_MODES, STOP_MODES, TRAIL_MODES, RANK_MODES, FACTOR_FILTERS


@dataclass(frozen=True)
class Experiment:
    exp_id: str
    entry_mode: str
    stop_mode: str
    trail_mode: str
    rank_mode: str
    factor_filter: str
    parent: str
    single_variable: str
    question: str
    needs_factors: bool
    historical: bool          # 本轮历史阶梯是否运行
    conditional: bool         # 是否条件加跑（仅 M2b）

    def to_dict(self) -> dict:
        return asdict(self)


EXPERIMENTS = {
    "M0": Experiment(
        "M0", "donchian_only", "uniform", "none", "quote_vol_30d_median", "none",
        "", "(基线)", "纯 20 突破 + 3ATR 固定止损 + 不移止损 的原始基准",
        False, True, False),
    "M1": Experiment(
        "M1", "three_buy_points", "uniform", "none", "quote_vol_30d_median", "none",
        "M0", "entry_mode", "单测买点：三类买点整体是否优于纯突破", False, True, False),
    "M2": Experiment(
        "M2", "three_buy_points", "structural", "none", "quote_vol_30d_median", "none",
        "M1", "stop_mode", "单测初始止损：结构止损（含过远拒单）是否优于固定 3ATR",
        False, True, False),
    "M2b": Experiment(
        "M2b", "three_buy_points", "structural_m2b", "none", "quote_vol_30d_median", "none",
        "M2", "stop_cap(M2b)", "条件加跑：M2 超限率>30% 时按真实距离自然缩仓、不拒单",
        False, True, True),
    "M3_BE": Experiment(
        "M3_BE", "three_buy_points", "structural", "naive_be", "quote_vol_30d_median", "none",
        "M2/M2b 胜出者", "trail_mode(naive_be)", "单测锁利：收盘 >=1R 后推保本 的对照臂",
        False, True, False),
    "M3_Step": Experiment(
        "M3_Step", "three_buy_points", "structural", "step_swing", "quote_vol_30d_median", "none",
        "M2/M2b 胜出者", "trail_mode(step_swing)", "单测锁利：台阶上移是否比推保本更少被震出",
        False, True, False),
    "M4": Experiment(
        "M4", "three_buy_points", "structural", "step_swing", "oi_change_90d_pct", "none",
        "M2/M3 胜出者", "rank_mode", "单测 OI 排序：优先选 OI 变化 90 日分位高的币是否有正增量",
        True, True, False),
    "M5": Experiment(
        "M5", "three_buy_points", "structural", "step_swing", "oi_change_90d_pct",
        "global_account_ratio_30d_pct_gt_0.90", "M4",
        "factor_filter(M5)", "前向验证：全网账户比自身 30 日分位>90% 不开多（需前向门槛）",
        True, False, False),
    "M6": Experiment(
        "M6", "three_buy_points", "structural", "step_swing", "oi_change_90d_pct",
        "top_position_ratio_30d_z_desc_rank", "M5",
        "factor_filter(M6)", "前向验证：大户持仓比自身 30 日 Z 降序排序（需前向门槛，平手保持原序）",
        True, False, False),
}

HISTORICAL_LADDER = tuple(k for k, v in EXPERIMENTS.items() if v.historical)
FORWARD_VALIDATION = tuple(k for k, v in EXPERIMENTS.items() if not v.historical)
STEP_CHILDREN = ("M3_BE", "M3_Step")


def validate_experiments() -> list[str]:
    errs = []
    for k, e in EXPERIMENTS.items():
        if e.entry_mode not in ENTRY_MODES:
            errs.append(f"{k}: entry_mode 非法 {e.entry_mode}")
        if e.stop_mode not in STOP_MODES:
            errs.append(f"{k}: stop_mode 非法 {e.stop_mode}")
        if e.trail_mode not in TRAIL_MODES:
            errs.append(f"{k}: trail_mode 非法 {e.trail_mode}")
        if e.rank_mode not in RANK_MODES:
            errs.append(f"{k}: rank_mode 非法 {e.rank_mode}")
        if e.factor_filter not in FACTOR_FILTERS:
            errs.append(f"{k}: factor_filter 非法 {e.factor_filter}")
        if e.parent and e.parent not in EXPERIMENTS and "/" not in e.parent:
            errs.append(f"{k}: 父配置 {e.parent} 不存在")
    # v3.1：M5/M6 必须为前向验证，不得进入历史阶梯
    if EXPERIMENTS["M5"].historical or EXPERIMENTS["M6"].historical:
        errs.append("M5/M6 必须为前向验证（historical=False）")
    # M2b 必须为条件加跑且只改距离上限
    if not EXPERIMENTS["M2b"].conditional or EXPERIMENTS["M2b"].stop_mode != "structural_m2b":
        errs.append("M2b 必须为 conditional 且 stop_mode=structural_m2b")
    # M6 必须为 Z 降序排序（不是 Z>=0 过滤）
    if EXPERIMENTS["M6"].factor_filter != "top_position_ratio_30d_z_desc_rank":
        errs.append("M6 必须为 top_position_ratio_30d_z_desc_rank")
    return errs


def experiment_config_hash() -> str:
    import hashlib
    import json
    payload = {k: v.to_dict() for k, v in sorted(EXPERIMENTS.items())}
    payload["_params"] = FROZEN.identity()
    payload["_historical_ladder"] = list(HISTORICAL_LADDER)
    payload["_forward_validation"] = list(FORWARD_VALIDATION)
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
