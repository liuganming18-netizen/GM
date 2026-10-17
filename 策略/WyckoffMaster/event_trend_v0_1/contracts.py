# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 核心数据契约。

包含五类标准化核心数据对象与状态定义：
1. RawTrace (原始痕迹)
2. BoxVersion (箱体版本)
3. EventDossier (事件档案)
4. StateTransition (状态迁移流水)
5. CandidateSnapshot (候选快照，生成后不可变冻结)
6. ValidationResult (验证结果，关联候选与撮合账本)

所有对象均支持 to_dict() 和字段级结构化比较，严禁使用随机 ID。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EventState(str, Enum):
    """事件生命周期确定性状态枚举。"""
    BOX = "BOX"                              # 箱体建立，等待扫低
    SWEEP = "SWEEP"                          # 发生有效扫低回收，等待缩量回踩
    RETEST = "RETEST"                        # 发生有效缩量回踩，等待突破
    BREAKOUT_EVAL = "BREAKOUT_EVAL"          # 触发突破穿越，正在评估量能与OI
    CANDIDATE = "CANDIDATE"                  # 确认通过，生成候选快照
    VOLUME_REJECTED = "VOLUME_REJECTED"      # 首次突破量能不足
    OI_REJECTED = "OI_REJECTED"              # 突破时 OI 6h 增长率 < 2%
    OI_UNAVAILABLE = "OI_UNAVAILABLE"        # 突破时 OI 数据缺失或不可用
    MISSED_WITHOUT_RETEST = "MISSED_WITHOUT_RETEST"  # 未回踩已突破上轨
    INVALIDATED = "INVALIDATED"              # 结构破坏失效（跌破止损或连续收盘破L）
    EXPIRED = "EXPIRED"                      # 观察超时归档
    CLOSED = "CLOSED"                        # 交易或流程结束归档


@dataclass(frozen=True)
class BoxVersion:
    """箱体版本（不可变）。建档时基于 42 根 4h K 线确定，后续严禁重画。"""
    version_id: str
    symbol: str
    venue: str
    timeframe: str
    formation_time_ms: int        # 箱体确认收盘时刻 (ms)
    high_h: float                 # 箱体上沿 H
    low_l: float                  # 箱体下沿 L
    atr_a: float                  # 建档时刻 4h Wilder ATR(14)
    efficiency_ratio_er: float    # 42 根 4h 收盘价的方向效率 ER
    box_width_atr: float          # (H - L) / A
    bars_count: int = 42

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawTrace:
    """原始市场异动痕迹。"""
    trace_id: str
    symbol: str
    venue: str
    timeframe: str
    bar_open_ms: int              # K线开盘时刻 (ms)
    bar_close_ms: int             # K线收盘时刻 (ms)
    available_at_ms: int          # 数据市场上可得时刻 (ms)
    trace_type: str               # SWEEP_BAR, RETEST_BAR, BREAKOUT_BAR
    price_low: float
    price_high: float
    price_close: float
    volume: float
    features: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StateTransition:
    """不可变的状态迁移流水，记录数据可得时的状态跃迁与确切证据。"""
    transition_id: str
    event_id: str
    symbol: str
    from_state: str
    to_state: str
    trigger_bar_close_ms: int     # 触发决策的 Bar 收盘时刻 (ms)
    available_at_ms: int          # 决策可得时刻 (ms)
    reason: str
    rule_version: str
    metrics_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EventDossier:
    """事件档案：跨周期的持久化事件实体。"""
    event_id: str
    strategy_version: str
    venue: str
    symbol: str
    created_at_ms: int            # 箱体建档收盘时刻 (ms)
    current_state: EventState
    box: BoxVersion
    hypothesis_direction: str = "LONG"
    
    # 过程关键锚点
    sweep_time_ms: Optional[int] = None
    sweep_low_s: Optional[float] = None
    sweep_volume_vs: Optional[float] = None
    
    retest_time_ms: Optional[int] = None
    retest_low_lr: Optional[float] = None
    
    breakout_time_ms: Optional[int] = None
    breakout_close_price: Optional[float] = None
    breakout_volume: Optional[float] = None
    v20_median_volume: Optional[float] = None
    
    oi_at_breakout: Optional[float] = None
    oi_6h_ago: Optional[float] = None
    oi_growth_6h: Optional[float] = None
    
    termination_reason: Optional[str] = None
    ended_at_ms: Optional[int] = None
    candidate_id: Optional[str] = None
    
    # 状态流水与关联痕迹
    history: List[StateTransition] = field(default_factory=list)
    traces: List[RawTrace] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["current_state"] = self.current_state.value if isinstance(self.current_state, EventState) else str(self.current_state)
        return d


@dataclass(frozen=True)
class CandidateSnapshot:
    """候选快照：在突破收盘确认并通过量能与 OI 检验后生成，生成后严格不可变。"""
    candidate_id: str
    event_id: str
    strategy_version: str
    symbol: str
    venue: str
    
    snapshot_time_ms: int         # 必须等于 1h 突破 K 线收盘时刻 (ms)
    available_at_ms: int          # 决策确切可得时刻 (ms)，>= snapshot_time_ms
    
    # 箱体与价格特征
    box_high_h: float
    box_low_l: float
    box_atr_a: float
    box_er: float
    
    sweep_low_s: float
    retest_low_lr: float
    breakout_close: float
    breakout_volume: float
    v20_median_volume: float
    volume_ratio: float           # breakout_volume / v20_median_volume
    
    # OI 真实指标 (严禁 mock)
    oi_breakout: float
    oi_6h_ago: float
    oi_growth_6h: float           # oi_breakout / oi_6h_ago - 1.0
    
    # 风险与执行指导
    initial_stop_price: float     # P0 = Lr - 0.1A
    initial_risk_r: float         # 暂估 R = breakout_close - P0 (开盘成交时根据实际买价重算)
    max_entry_price: float        # breakout_close + 0.5A (超过则跳空取消)
    min_entry_price: float        # box_high_h (低于则取消)
    
    # 执行约束时点 (下一根 15m 开盘)
    execution_earliest_ms: int    # 严格更晚的 15m 开盘时间 (snapshot_time_ms + 15m)
    
    # PIT 时点说明（规则 §2 要求：无官方发布时点凭证时标记为假设性研究）
    pit_proven: bool = False
    pit_assumption_note: str = "Based on observation timestamp without verified exchange publication latency certificate"
    
    missing_fields: List[str] = field(default_factory=list)
    snapshot_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(cls, **kwargs) -> CandidateSnapshot:
        # 自动计算快照哈希
        kw = dict(kwargs)
        kw.pop("snapshot_hash", None)
        raw_repr = json.dumps(kw, sort_keys=True, default=str)
        h = hashlib.sha256(raw_repr.encode("utf-8")).hexdigest()
        return cls(**kw, snapshot_hash=h)


@dataclass
class ValidationResult:
    """验证结果：关联候选快照与执行账本，记录单次事件的撮合、成本与 MFE/MAE。"""
    validation_id: str
    candidate_id: str
    event_id: str
    symbol: str
    strategy_version: str
    run_variant: str              # 'A' (完整), 'B' (关OI), 'C' (成本翻倍)
    
    executed: bool                # 是否真实产生成交订单
    execution_canceled_reason: Optional[str] = None

    # 身份贯穿结论（不可用时间近似/拼接订单号替代）：
    #   ok                    —— 候选 → signal_id → order_id → 成交 全链一致
    #   order_missing_for_trade —— 有成交但找不到对应开仓订单（内部不一致，必须暴露）
    #   not_executed          —— 未成交（原因见 execution_canceled_reason）
    identity_status: str = "ok"
    
    order_id: Optional[str] = None
    fill_time_ms: Optional[int] = None
    entry_price: Optional[float] = None
    executed_qty: Optional[float] = None
    actual_stop_price: Optional[float] = None
    actual_r: Optional[float] = None
    
    exit_time_ms: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    holding_duration_hours: Optional[float] = None
    
    # 成本明细
    fee_paid: float = 0.0
    slippage_cost: float = 0.0
    funding_cost: float = 0.0
    
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    net_pnl_r: float = 0.0
    
    mfe: float = 0.0              # 最大有利偏移 (Max Favorable Excursion)
    mae: float = 0.0              # 最大不利偏移 (Max Adverse Excursion)
    mfe_r: float = 0.0
    mae_r: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
