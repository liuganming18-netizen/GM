# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 策略模块根包。"""
from .config import EventTrendConfig
from .contracts import (BoxVersion, CandidateSnapshot, EventDossier, EventState,
                        RawTrace, StateTransition, ValidationResult)
from .features import (compute_efficiency_ratio, compute_v20_median_volume,
                       compute_wilder_atr_series, extract_real_oi_growth)
from .state_machine import EventStateMachine
from .engine_adapter import build_signal_frame_from_candidates, run_event_trend_backtest
from .runner import EventTrendRunner

__version__ = "0.1.0"
__strategy_id__ = "WX_EVENT_TREND_V0_1"
__all__ = [
    "EventTrendConfig",
    "BoxVersion",
    "CandidateSnapshot",
    "EventDossier",
    "EventState",
    "RawTrace",
    "StateTransition",
    "ValidationResult",
    "compute_wilder_atr_series",
    "compute_efficiency_ratio",
    "compute_v20_median_volume",
    "extract_real_oi_growth",
    "EventStateMachine",
    "build_signal_frame_from_candidates",
    "run_event_trend_backtest",
    "EventTrendRunner",
]
