# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 —— 新候选策略包（2026-10-16 落地）。

与同目录下的旧实现（`../core.py` / `../execution.py` / `../params.py`）**完全隔离**：
  · 旧实现是 `F_WX 3a/3b/3c/3d` 当前所依赖的 `WyckoffMaster v3.1` 核心；
    本包**不改其一个字节**，也不复用其任何成绩。
  · 替换前的旧实现已在 `../_legacy_archive_v31/` 逐字节封存（附 SHA256 与 Git diff 证据）。
  · 本包不读、不写任何账本 / 行情库 / 状态 JSON / manifest；无联网、无下单路径。

公开接口：
  · `config`        —— 独立版本化的实验配置（STRATEGY_ID / EXPERIMENT_ID / 参数哈希）
  · `factors`       —— OI 共振因子（显式时间单位；raw 分项 + 权重 + available/quality）
  · `core`          —— 信号机与止损（新信号名 `lps_pullback`；严格数值校验）
  · `execution`     —— 合成组合执行（开盘相位先于 bar 内事件；费用/权益恒等式）
  · `registry_entry`—— 注册表协议对象（KEY/NAME/.../run/resolve/interface）
"""
from . import config, core, execution, factors, registry_entry  # noqa: F401
from .config import (EXEC_FROZEN, EXPERIMENT_ID, FAMILY, NOT_INHERITED, OI_WEIGHTS,
                     SIGNAL_FROZEN, SIGNAL_VERSION, STRATEGY_ID, STRATEGY_KEY,
                     config_identity)
from .core import (SIG_DONCHIAN_BREAKOUT, SIG_LPS_PULLBACK, SIG_NONE, SIG_SC_REVERSAL,
                   SIG_SPRING, SIGNAL_NAMES, StopDecision, compute_signals, compute_stop)
from .execution import run_portfolio_execution, version_block

__all__ = [
    "config", "factors", "core", "execution", "registry_entry",
    "STRATEGY_ID", "STRATEGY_KEY", "FAMILY", "SIGNAL_VERSION", "EXPERIMENT_ID",
    "SIGNAL_FROZEN", "EXEC_FROZEN", "OI_WEIGHTS", "NOT_INHERITED", "config_identity",
    "SIG_NONE", "SIG_SPRING", "SIG_SC_REVERSAL", "SIG_DONCHIAN_BREAKOUT",
    "SIG_LPS_PULLBACK", "SIGNAL_NAMES", "StopDecision", "compute_stop", "compute_signals",
    "run_portfolio_execution", "version_block",
]
