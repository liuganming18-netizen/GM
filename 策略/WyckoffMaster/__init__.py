# -*- coding: utf-8 -*-
"""WyckoffMaster 顶层包命名空间（WORK-04B 核心收敛）。

架构收敛说明：
  · 旧 v3.1 策略族（core, params, factors, segments, execution, experiments, verify）已在 WORK-04B 彻底退役物理清理。
  · 历史归档不可篡改封存于 `_legacy_archive_v31/`。
  · 本包当前唯一生产策略实现位于 `oi_resonance_v1`。
  · Backtest / Paper / Live 共享交易执行核心位于 `trading_core`。
"""

__version__ = "4.0.0-oi-resonance"

__all__ = [
    "oi_resonance_v1",
    "trading_core",
]
