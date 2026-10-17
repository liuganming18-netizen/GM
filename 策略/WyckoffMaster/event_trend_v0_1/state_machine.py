# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 确定性事件状态机。

具备：
1. 严格因果时序：输入去重，收盘决策，下一根 15m 开盘执行；
2. 真实指标：Wilder ATR(14)、ER、V20、真实 OI 6h 增幅；
3. 状态持久化与无损还原（export_state / restore_state）；
4. 逐字段完全幂等；
5. A/B 消融开关（enable_oi_filter）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from .config import EventTrendConfig
from .contracts import (BoxVersion, CandidateSnapshot, EventDossier, EventState,
                        RawTrace, StateTransition)
from .features import (compute_efficiency_ratio, compute_v20_median_volume,
                       compute_wilder_atr_series, extract_real_oi_growth)


class EventStateMachine:
    """确定性低频事件状态机。"""

    def __init__(self, config: Optional[EventTrendConfig] = None, symbol: str = "SOLUSDT", enable_oi_filter: bool = True):
        self.config = config or EventTrendConfig()
        self.symbol = symbol
        self.enable_oi_filter = enable_oi_filter
        self.venue = self.config.venue
        
        # 活跃事件与已完结事件档案
        self.active_event: Optional[EventDossier] = None
        self.completed_events: List[EventDossier] = []
        self.candidates: List[CandidateSnapshot] = []
        self.transitions: List[StateTransition] = []
        self.traces: List[RawTrace] = []
        
        # 冷却控制
        self.cooling_until_ms: int = 0
        
        # 输入去重集合 (保证重复 bar 绝不进入特征滑窗)
        self.processed_1h_bar_opens: Set[int] = set()
        
        # 滚动 1h 历史序列 (已去重)
        self.h1_open_times: List[int] = []
        self.h1_close_times: List[int] = []
        self.h1_opens: List[float] = []
        self.h1_highs: List[float] = []
        self.h1_lows: List[float] = []
        self.h1_closes: List[float] = []
        self.h1_volumes: List[float] = []
        
        # 辅助计数：连续收盘低于 L 的根数
        self.consecutive_closes_below_l: int = 0

    def export_state(self) -> Dict[str, Any]:
        """导出完整内部状态，支持冷热重启。"""
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "enable_oi_filter": self.enable_oi_filter,
            "cooling_until_ms": self.cooling_until_ms,
            "processed_1h_bar_opens": sorted(list(self.processed_1h_bar_opens)),
            "h1_open_times": list(self.h1_open_times),
            "h1_close_times": list(self.h1_close_times),
            "h1_opens": list(self.h1_opens),
            "h1_highs": list(self.h1_highs),
            "h1_lows": list(self.h1_lows),
            "h1_closes": list(self.h1_closes),
            "h1_volumes": list(self.h1_volumes),
            "consecutive_closes_below_l": self.consecutive_closes_below_l,
            "active_event": self.active_event.to_dict() if self.active_event else None,
            "completed_events": [e.to_dict() for e in self.completed_events],
            "candidates": [c.to_dict() for c in self.candidates],
            "transitions": [t.to_dict() for t in self.transitions],
            "traces": [tr.to_dict() for tr in self.traces],
        }

    @classmethod
    def restore_state(cls, state_dict: Dict[str, Any], config: Optional[EventTrendConfig] = None) -> EventStateMachine:
        """从字典无损还原状态机实体。"""
        cfg = config or EventTrendConfig()
        sm = cls(config=cfg, enable_oi_filter=state_dict.get("enable_oi_filter", True))
        sm.symbol = state_dict.get("symbol", "SOLUSDT")
        sm.venue = state_dict.get("venue", cfg.venue)
        sm.cooling_until_ms = state_dict.get("cooling_until_ms", 0)
        sm.processed_1h_bar_opens = set(state_dict.get("processed_1h_bar_opens", []))
        sm.h1_open_times = list(state_dict.get("h1_open_times", []))
        sm.h1_close_times = list(state_dict.get("h1_close_times", []))
        sm.h1_opens = list(state_dict.get("h1_opens", []))
        sm.h1_highs = list(state_dict.get("h1_highs", []))
        sm.h1_lows = list(state_dict.get("h1_lows", []))
        sm.h1_closes = list(state_dict.get("h1_closes", []))
        sm.h1_volumes = list(state_dict.get("h1_volumes", []))
        sm.consecutive_closes_below_l = state_dict.get("consecutive_closes_below_l", 0)

        def _dossier_from_dict(d: dict) -> EventDossier:
            box = BoxVersion(**d["box"])
            hist = [StateTransition(**h) for h in d.get("history", [])]
            trcs = [RawTrace(**tr) for tr in d.get("traces", [])]
            return EventDossier(
                event_id=d["event_id"],
                strategy_version=d["strategy_version"],
                venue=d["venue"],
                symbol=d["symbol"],
                created_at_ms=d["created_at_ms"],
                current_state=EventState(d["current_state"]),
                box=box,
                hypothesis_direction=d.get("hypothesis_direction", "LONG"),
                sweep_time_ms=d.get("sweep_time_ms"),
                sweep_low_s=d.get("sweep_low_s"),
                sweep_volume_vs=d.get("sweep_volume_vs"),
                retest_time_ms=d.get("retest_time_ms"),
                retest_low_lr=d.get("retest_low_lr"),
                breakout_time_ms=d.get("breakout_time_ms"),
                breakout_close_price=d.get("breakout_close_price"),
                breakout_volume=d.get("breakout_volume"),
                v20_median_volume=d.get("v20_median_volume"),
                oi_at_breakout=d.get("oi_at_breakout"),
                oi_6h_ago=d.get("oi_6h_ago"),
                oi_growth_6h=d.get("oi_growth_6h"),
                termination_reason=d.get("termination_reason"),
                ended_at_ms=d.get("ended_at_ms"),
                candidate_id=d.get("candidate_id"),
                history=hist,
                traces=trcs
            )

        if state_dict.get("active_event"):
            sm.active_event = _dossier_from_dict(state_dict["active_event"])
        else:
            sm.active_event = None

        sm.completed_events = [_dossier_from_dict(d) for d in state_dict.get("completed_events", [])]
        sm.candidates = [CandidateSnapshot(**c) for c in state_dict.get("candidates", [])]
        sm.transitions = [StateTransition(**t) for t in state_dict.get("transitions", [])]
        sm.traces = [RawTrace(**tr) for tr in state_dict.get("traces", [])]

        return sm


    def _make_transition(self, event: EventDossier, to_state: EventState,
                         trigger_bar_close_ms: int, reason: str,
                         metrics: Dict[str, Any]) -> StateTransition:
        """记录状态迁移并追加到事件历史。"""
        from_st = event.current_state.value if isinstance(event.current_state, EventState) else str(event.current_state)
        to_st = to_state.value if isinstance(to_state, EventState) else str(to_state)
        
        t_id = f"tr_{event.event_id}_{len(event.history) + 1:03d}_{to_st}"
        trans = StateTransition(
            transition_id=t_id,
            event_id=event.event_id,
            symbol=self.symbol,
            from_state=from_st,
            to_state=to_st,
            trigger_bar_close_ms=trigger_bar_close_ms,
            available_at_ms=trigger_bar_close_ms,
            reason=reason,
            rule_version=self.config.strategy_version,
            metrics_snapshot=metrics
        )
        event.current_state = to_state
        event.history.append(trans)
        self.transitions.append(trans)
        return trans

    def _terminate_event(self, event: EventDossier, end_state: EventState,
                         close_ms: int, reason: str, metrics: Dict[str, Any]):
        """终止当前事件并开启 24 小时冷却。"""
        self._make_transition(event, end_state, close_ms, reason, metrics)
        event.termination_reason = reason
        event.ended_at_ms = close_ms
        self.completed_events.append(event)
        self.active_event = None
        self.consecutive_closes_below_l = 0
        # 冷却 24 小时
        self.cooling_until_ms = close_ms + self.config.cooling_hours * 3600 * 1000

    def check_and_form_box(self, bar_4h_close_ms: int,
                           history_42_highs: np.ndarray,
                           history_42_lows: np.ndarray,
                           history_42_closes: np.ndarray,
                           current_atr_a: float) -> Optional[EventDossier]:
        """检查并尝试建立 4h 箱体档案。"""
        # 1. 冷却期与已有活跃事件检查
        if self.active_event is not None:
            return None
        if bar_4h_close_ms < self.cooling_until_ms:
            return None
            
        # 2. 检查 42 根数据与 ATR 有效性
        if len(history_42_highs) < self.config.box_bars_4h or current_atr_a <= 0.0 or np.isnan(current_atr_a):
            return None
            
        high_h = float(np.max(history_42_highs))
        low_l = float(np.min(history_42_lows))
        width_atr = (high_h - low_l) / current_atr_a
        
        # 3. 箱体宽度限制: 2A ~ 8A
        if not (self.config.box_min_width_atr <= width_atr <= self.config.box_max_width_atr):
            return None
            
        # 4. 方向效率比 ER <= 0.30
        er = compute_efficiency_ratio(history_42_closes)
        if er is None or er > self.config.er_threshold:
            return None
            
        # 5. 确定性生成 Event ID
        event_id = f"ev_{self.config.strategy_name}_{self.venue}_{self.symbol}_{bar_4h_close_ms}"
        version_id = f"box_{event_id}_v1"
        
        box = BoxVersion(
            version_id=version_id,
            symbol=self.symbol,
            venue=self.venue,
            timeframe=self.config.timeframe_env,
            formation_time_ms=bar_4h_close_ms,
            high_h=high_h,
            low_l=low_l,
            atr_a=current_atr_a,
            efficiency_ratio_er=er,
            box_width_atr=width_atr,
            bars_count=self.config.box_bars_4h
        )
        
        event = EventDossier(
            event_id=event_id,
            strategy_version=self.config.strategy_version,
            venue=self.venue,
            symbol=self.symbol,
            created_at_ms=bar_4h_close_ms,
            current_state=EventState.BOX,
            box=box,
            hypothesis_direction="LONG"
        )
        
        # 记录建档迁移
        trans = StateTransition(
            transition_id=f"tr_{event_id}_001_BOX",
            event_id=event_id,
            symbol=self.symbol,
            from_state="NONE",
            to_state=EventState.BOX.value,
            trigger_bar_close_ms=bar_4h_close_ms,
            available_at_ms=bar_4h_close_ms,
            reason="4h箱体蓄势成立(ER<=0.30, 2A<=宽度<=8A)",
            rule_version=self.config.strategy_version,
            metrics_snapshot={
                "high_h": high_h, "low_l": low_l, "atr_a": current_atr_a,
                "er": er, "width_atr": width_atr
            }
        )
        event.history.append(trans)
        self.transitions.append(trans)
        
        self.active_event = event
        self.consecutive_closes_below_l = 0
        return event

    def process_1h_bar(
        self,
        open_time_ms: int,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
        volume_v: float,
        oi_timestamps: np.ndarray,
        oi_values: np.ndarray
    ) -> Optional[CandidateSnapshot]:
        """处理一根 1h K 线并推动状态演化。
        
        严格因果：输入去重，先判断失效再判断升级。
        返回生成的候选快照（若当根突破确认通过）。
        """
        # 1. 严格输入去重（杜绝重复 bar 污染特征滑窗）
        if open_time_ms in self.processed_1h_bar_opens:
            return None
        self.processed_1h_bar_opens.add(open_time_ms)
        
        close_time_ms = open_time_ms + 3600 * 1000
        
        # 维护滚动历史
        self.h1_open_times.append(open_time_ms)
        self.h1_close_times.append(close_time_ms)
        self.h1_opens.append(open_p)
        self.h1_highs.append(high_p)
        self.h1_lows.append(low_p)
        self.h1_closes.append(close_p)
        self.h1_volumes.append(volume_v)
        
        current_idx = len(self.h1_closes) - 1
        
        # 没有活跃事件则无需处理
        event = self.active_event
        if event is None:
            return None
            
        # 建档时刻之前或同刻的 1h bar 不得推动该事件
        if close_time_ms <= event.created_at_ms:
            return None
            
        box = event.box
        H = box.high_h
        L = box.low_l
        A = box.atr_a
        
        # 2. 检查全局连续破 L 计数
        if close_p < L:
            self.consecutive_closes_below_l += 1
        else:
            self.consecutive_closes_below_l = 0
            
        # 3. 检查总体 14 天超时
        if close_time_ms - event.created_at_ms > self.config.breakout_max_total_hours * 3600 * 1000:
            self._terminate_event(
                event, EventState.EXPIRED, close_time_ms,
                "事件总体观察超时(>14天)", {"elapsed_hours": (close_time_ms - event.created_at_ms) / 3600000}
            )
            return None

        # ════════════════════ 分状态处理 ════════════════════
        st = event.current_state

        # ── 阶段 A: BOX 等待 SWEEP ──
        if st == EventState.BOX:
            # 7 天未 sweep 超时
            if close_time_ms - event.created_at_ms > self.config.sweep_max_hours * 3600 * 1000:
                self._terminate_event(
                    event, EventState.EXPIRED, close_time_ms,
                    "建档后7天内未发生扫低", {"elapsed_hours": (close_time_ms - event.created_at_ms) / 3600000}
                )
                return None
                
            # 失效检查: low < L - 0.5A 或 连续两根收盘 < L
            if low_p < (L - self.config.sweep_max_overshoot_atr * A) or self.consecutive_closes_below_l >= 2:
                self._terminate_event(
                    event, EventState.INVALIDATED, close_time_ms,
                    "跌破L-0.5A或连续2根收盘破L",
                    {"low": low_p, "limit": L - self.config.sweep_max_overshoot_atr * A, "consecutive": self.consecutive_closes_below_l}
                )
                return None
                
            # 扫低条件: L - 0.5A <= low < L 且 close >= L + 0.1A 且 close <= H
            is_sweep = (
                (L - self.config.sweep_max_overshoot_atr * A <= low_p < L) and
                (close_p >= L + self.config.sweep_min_recovery_atr * A) and
                (close_p <= H)
            )
            if is_sweep:
                event.sweep_time_ms = close_time_ms
                event.sweep_low_s = low_p
                event.sweep_volume_vs = volume_v
                
                # 记录痕迹
                trace = RawTrace(
                    trace_id=f"trc_sweep_{event.event_id}_{close_time_ms}",
                    symbol=self.symbol, venue=self.venue, timeframe="1h",
                    bar_open_ms=open_time_ms, bar_close_ms=close_time_ms, available_at_ms=close_time_ms,
                    trace_type="SWEEP_BAR",
                    price_low=low_p, price_high=high_p, price_close=close_p, volume=volume_v,
                    features={"L": L, "A": A, "overshoot": L - low_p}
                )
                event.traces.append(trace)
                self.traces.append(trace)
                
                self._make_transition(
                    event, EventState.SWEEP, close_time_ms,
                    "扫低回收确认(破L不超0.5A且同根收回L+0.1A)",
                    {"sweep_low": low_p, "sweep_volume": volume_v, "close": close_p}
                )
            return None

        # ── 阶段 B: SWEEP 等待 RETEST ──
        elif st == EventState.SWEEP:
            assert event.sweep_time_ms is not None
            assert event.sweep_low_s is not None
            assert event.sweep_volume_vs is not None
            
            # 超时检查: sweep 之后 72 小时
            if close_time_ms - event.sweep_time_ms > self.config.retest_max_hours * 3600 * 1000:
                self._terminate_event(
                    event, EventState.EXPIRED, close_time_ms,
                    "扫低后72小时内未发生缩量回踩", {"elapsed_hours": (close_time_ms - event.sweep_time_ms) / 3600000}
                )
                return None
                
            # 失效检查: low < S - 0.1A 或 连续两根收盘 < L
            if low_p < (event.sweep_low_s - self.config.retest_fail_buffer_atr * A) or self.consecutive_closes_below_l >= 2:
                self._terminate_event(
                    event, EventState.INVALIDATED, close_time_ms,
                    "跌破S-0.1A或连续2根收盘破L",
                    {"low": low_p, "s_limit": event.sweep_low_s - self.config.retest_fail_buffer_atr * A}
                )
                return None
                
            # 错过检查: 回踩确认前若已收盘突破 H + 0.1A
            if close_p > (H + self.config.breakout_buffer_atr * A):
                self._terminate_event(
                    event, EventState.MISSED_WITHOUT_RETEST, close_time_ms,
                    "回踩前价格已收盘突破H+0.1A(missed_without_retest)",
                    {"close": close_p, "h_limit": H + self.config.breakout_buffer_atr * A}
                )
                return None
                
            # 缩量回踩条件:
            # - L <= low <= L + 0.5A 且 low > S
            # - close >= L + 0.1A 且 close >= open (收阳)
            # - volume <= 0.8 * Vs
            is_retest = (
                (L <= low_p <= L + self.config.retest_max_zone_atr * A) and
                (low_p > event.sweep_low_s) and
                (close_p >= L + self.config.retest_recovery_atr * A) and
                (close_p >= open_p) and
                (volume_v <= self.config.retest_vol_multiplier * event.sweep_volume_vs)
            )
            if is_retest:
                event.retest_time_ms = close_time_ms
                event.retest_low_lr = low_p
                
                trace = RawTrace(
                    trace_id=f"trc_retest_{event.event_id}_{close_time_ms}",
                    symbol=self.symbol, venue=self.venue, timeframe="1h",
                    bar_open_ms=open_time_ms, bar_close_ms=close_time_ms, available_at_ms=close_time_ms,
                    trace_type="RETEST_BAR",
                    price_low=low_p, price_high=high_p, price_close=close_p, volume=volume_v,
                    features={"Lr": low_p, "Vs": event.sweep_volume_vs, "vol_ratio": volume_v / event.sweep_volume_vs}
                )
                event.traces.append(trace)
                self.traces.append(trace)
                
                self._make_transition(
                    event, EventState.RETEST, close_time_ms,
                    "缩量回踩确认(在L~L+0.5A高于S且收阳量<=0.8Vs)",
                    {"retest_low": low_p, "volume": volume_v, "vs": event.sweep_volume_vs}
                )
            return None

        # ── 阶段 C: RETEST 等待 BREAKOUT ──
        elif st == EventState.RETEST:
            assert event.retest_time_ms is not None
            assert event.retest_low_lr is not None
            
            # 超时检查: retest 之后 7 天
            if close_time_ms - event.retest_time_ms > self.config.breakout_max_hours_after_retest * 3600 * 1000:
                self._terminate_event(
                    event, EventState.EXPIRED, close_time_ms,
                    "回踩后7天内未发生突破", {"elapsed_hours": (close_time_ms - event.retest_time_ms) / 3600000}
                )
                return None
                
            # 失效检查: low < Lr - 0.1A 或 连续两根收盘 < L
            if low_p < (event.retest_low_lr - self.config.initial_stop_buffer_atr * A) or self.consecutive_closes_below_l >= 2:
                self._terminate_event(
                    event, EventState.INVALIDATED, close_time_ms,
                    "跌破Lr-0.1A或连续2根收盘破L",
                    {"low": low_p, "lr_limit": event.retest_low_lr - self.config.initial_stop_buffer_atr * A}
                )
                return None
                
            # 突破检查: 首次从下向上穿越 H + 0.1A
            # 前一根 close <= H + 0.1A, 当前 close > H + 0.1A
            prev_close = self.h1_closes[current_idx - 1]
            breakout_level = H + self.config.breakout_buffer_atr * A
            
            if prev_close <= breakout_level and close_p > breakout_level:
                # 触发首次穿越！仅评估这一次！
                vols_arr = np.array(self.h1_volumes, dtype=np.float64)
                v20 = compute_v20_median_volume(vols_arr, current_idx, self.config.breakout_vol_lookback)
                
                # 记录突破痕迹
                trace = RawTrace(
                    trace_id=f"trc_breakout_{event.event_id}_{close_time_ms}",
                    symbol=self.symbol, venue=self.venue, timeframe="1h",
                    bar_open_ms=open_time_ms, bar_close_ms=close_time_ms, available_at_ms=close_time_ms,
                    trace_type="BREAKOUT_BAR",
                    price_low=low_p, price_high=high_p, price_close=close_p, volume=volume_v,
                    features={"breakout_level": breakout_level, "v20": v20}
                )
                event.traces.append(trace)
                self.traces.append(trace)
                
                # 1. 量能检验: volume >= 1.5 * V20
                if v20 is None or volume_v < self.config.breakout_vol_multiplier * v20:
                    self._terminate_event(
                        event, EventState.VOLUME_REJECTED, close_time_ms,
                        "突破成交量不足1.5倍V20",
                        {"volume": volume_v, "v20": v20, "ratio": (volume_v / v20) if v20 else None}
                    )
                    return None
                    
                # 2. 真实 OI 检验
                oi_curr, oi_past, oi_growth, oi_status = extract_real_oi_growth(
                    oi_timestamps, oi_values, close_time_ms,
                    lookback_ms=self.config.oi_lookback_hours * 3600 * 1000,
                    max_distance_ms=self.config.oi_max_time_distance_sec * 1000
                )
                
                event.oi_at_breakout = oi_curr
                event.oi_6h_ago = oi_past
                event.oi_growth_6h = oi_growth
                
                if oi_status != "ok" or oi_growth is None:
                    self._terminate_event(
                        event, EventState.OI_UNAVAILABLE, close_time_ms,
                        f"OI数据缺失或不可用({oi_status})",
                        {"oi_status": oi_status, "oi_curr": oi_curr, "oi_past": oi_past}
                    )
                    return None
                    
                # 若开启 OI 过滤，检验增幅是否达到 2%
                if self.enable_oi_filter and oi_growth < self.config.oi_min_growth_pct:
                    self._terminate_event(
                        event, EventState.OI_REJECTED, close_time_ms,
                        f"6小时OI增长率不足2%(实际: {oi_growth * 100:.2f}%)",
                        {"oi_growth": oi_growth, "threshold": self.config.oi_min_growth_pct}
                    )
                    return None
                    
                # 3. 两项均通过 (或 B 组跳过 OI 过滤)，生成不可变候选快照！
                cand_id = f"cand_{event.event_id}_{close_time_ms}"
                initial_stop = event.retest_low_lr - self.config.initial_stop_buffer_atr * A
                initial_risk_r = close_p - initial_stop
                max_entry = close_p + self.config.max_gap_up_atr * A
                min_entry = H
                earliest_exec = close_time_ms + 15 * 60 * 1000  # 下一根更晚的 15m 开盘 (如 12:00 -> 12:15)
                
                snapshot = CandidateSnapshot.create(
                    candidate_id=cand_id,
                    event_id=event.event_id,
                    strategy_version=self.config.strategy_version,
                    symbol=self.symbol,
                    venue=self.venue,
                    snapshot_time_ms=close_time_ms,
                    available_at_ms=close_time_ms,
                    box_high_h=H,
                    box_low_l=L,
                    box_atr_a=A,
                    box_er=box.efficiency_ratio_er,
                    sweep_low_s=event.sweep_low_s,
                    retest_low_lr=event.retest_low_lr,
                    breakout_close=close_p,
                    breakout_volume=volume_v,
                    v20_median_volume=v20,
                    volume_ratio=volume_v / v20,
                    oi_breakout=oi_curr,
                    oi_6h_ago=oi_past,
                    oi_growth_6h=oi_growth,
                    initial_stop_price=initial_stop,
                    initial_risk_r=initial_risk_r,
                    max_entry_price=max_entry,
                    min_entry_price=min_entry,
                    execution_earliest_ms=earliest_exec,
                    pit_proven=False,
                    pit_assumption_note="Based on observation timestamp; exchange publication latency certificate unverified",
                    missing_fields=[]
                )
                
                event.candidate_id = cand_id
                event.breakout_time_ms = close_time_ms
                event.breakout_close_price = close_p
                event.breakout_volume = volume_v
                event.v20_median_volume = v20
                
                self._make_transition(
                    event, EventState.CANDIDATE, close_time_ms,
                    "放量突破与OI确认通过，生成候选快照",
                    {
                        "candidate_id": cand_id,
                        "breakout_close": close_p,
                        "volume_ratio": volume_v / v20,
                        "oi_growth_6h": oi_growth
                    }
                )
                self.candidates.append(snapshot)
                
                # 候选生成后，本事件完成使命，结束观察并转入冷却
                event.termination_reason = "候选生成成功"
                event.ended_at_ms = close_time_ms
                self.completed_events.append(event)
                self.active_event = None
                self.cooling_until_ms = close_time_ms + self.config.cooling_hours * 3600 * 1000
                
                return snapshot
                
            return None

        return None
