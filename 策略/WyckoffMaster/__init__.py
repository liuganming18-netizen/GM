# -*- coding: utf-8 -*-
"""WyckoffMaster：威科夫·唐奇安合体策略（v2.0 定稿 + v3 修正 + v3.1 修正）。

公开接口：
  · params.MasterParams / FROZEN        —— 冻结参数（阈值不可改）
  · segments.SEGMENTS / spec()          —— v3.1 三段半开 UTC 区间与段执行规格
  · core.compute_signals                —— 三类买点合体信号机（纯函数、无前视）
  · core.compute_stop                   —— uniform / structural / structural_m2b 止损
  · core.evaluate_entry_fill            —— +15m 实际成交判定（含 entry_below_struct_low）
  · core.m2_over_limit_stats / m2b_trigger —— M2 逐类型超限拒单统计与 M2b 触发
  · core.evaluate_trail_stop            —— none / naive_be / step_swing 锁利
  · factors.*                           —— M4 OI 排序、M5/M6 前向因子、前向激活门槛
  · experiments.EXPERIMENTS             —— 阶梯定义（历史 M0–M4+M2b；前向 M5/M6）
  · verify.run_all                      —— v3.1 四件套核验

范围：本包**只做策略规则与实现**，不含数据下载、不含全池回测、不写生产库；
选型阶段不读取/运行 Holdout（见 segments.assert_no_holdout）。
"""
from .params import MasterParams, FROZEN, validate_params, VERSION
from .core import (compute_signals, compute_stop, evaluate_entry_fill, evaluate_trail_stop,
                   m2_over_limit_stats, m2b_trigger, SignalFrame)
from .experiments import (EXPERIMENTS, HISTORICAL_LADDER, FORWARD_VALIDATION,
                          experiment_config_hash, validate_experiments)
from . import factors, segments, verify

__all__ = [
    "MasterParams", "FROZEN", "validate_params", "VERSION",
    "compute_signals", "compute_stop", "evaluate_entry_fill", "evaluate_trail_stop",
    "m2_over_limit_stats", "m2b_trigger", "SignalFrame",
    "EXPERIMENTS", "HISTORICAL_LADDER", "FORWARD_VALIDATION",
    "experiment_config_hash", "validate_experiments",
    "factors", "segments", "verify",
]
