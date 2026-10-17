# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 冻结策略规则与参数配置。

严格对应 evidence/WX_EVENT_TREND_V0_1_策略与回测冻结规则.md
SHA256: 1f1167de35412cb7d1d3608d8069c4108324f7641de7c369bf94882f1679a8ab
严禁随意调整任何参数！本轮不寻优。
"""
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass(frozen=True)
class EventTrendConfig:
    strategy_name: str = "WX_EVENT_TREND_V0_1"
    strategy_version: str = "v0.1.0"
    venue: str = "binance"
    market: str = "um"
    
    # 周期架构
    timeframe_env: str = "4h"          # 4h 环境箱体
    timeframe_event: str = "1h"        # 1h 事件推动
    timeframe_exec: str = "15m"        # 15m 执行撮合
    
    # 箱体定义 (BOX)
    box_bars_4h: int = 42              # 42 根 4h K 线 (7 天)
    atr_period_4h: int = 14            # 4h Wilder ATR(14)
    atr_warmup_bars: int = 200         # 至少预热 200 根完整 4h K 线
    er_threshold: float = 0.30         # 方向效率 ER <= 0.30
    box_min_width_atr: float = 2.0     # 最小箱体宽度 2.0 * ATR
    box_max_width_atr: float = 8.0     # 最大箱体宽度 8.0 * ATR
    
    # 扫低回收 (SWEEP)
    sweep_max_hours: int = 168         # 建档后 7 天 (168 小时) 内
    sweep_max_overshoot_atr: float = 0.5   # 最深不能破 L - 0.5A
    sweep_min_recovery_atr: float = 0.1    # 收盘回到 L + 0.1A 以上
    
    # 缩量回踩 (RETEST)
    retest_max_hours: int = 72         # sweep 后 72 小时内
    retest_min_zone_atr: float = 0.0   # 回踩区域低点 >= L
    retest_max_zone_atr: float = 0.5   # 回踩区域低点 <= L + 0.5A
    retest_recovery_atr: float = 0.1   # 收盘回到 L + 0.1A 以上
    retest_vol_multiplier: float = 0.8 # 回踩成交量 <= 0.8 * Vs
    retest_fail_buffer_atr: float = 0.1# 回踩期间跌破 S - 0.1A 则失效
    
    # 突破确认与 OI 决策 (BREAKOUT)
    breakout_max_hours_after_retest: int = 168   # retest 后 7 天内
    breakout_max_total_hours: int = 336          # 整个事件观察不超过 14 天 (336 小时)
    breakout_buffer_atr: float = 0.1             # 向上穿越 H + 0.1A
    breakout_vol_lookback: int = 20              # 前 20 根 1h 成交量中位数 (V20)
    breakout_vol_multiplier: float = 1.5         # 成交量 >= 1.5 * V20
    
    # 真实 OI 决策门槛
    oi_lookback_hours: int = 6                   # 6 小时持仓对比
    oi_min_growth_pct: float = 0.02              # 持仓数量增长 >= 2% (0.02)
    oi_max_time_distance_sec: int = 900          # 观测距收盘不超过 15 分钟
    
    # 冷却期
    cooling_hours: int = 24                      # 任何事件结束后同币冷却 24 小时
    
    # 入场执行与限制
    entry_delay_15m_bars: int = 1                # 严格更晚的下一根 15m 开盘 (如 12:00 确认，12:15 开盘买入)
    max_gap_up_atr: float = 0.5                  # 开盘价 > 突破收盘 + 0.5A 则取消
    initial_stop_buffer_atr: float = 0.1         # P0 = Lr - 0.1A
    
    # 资金与仓位
    # 初始本金是本包**唯一事实源**（runner / adapter 不得另立默认值）。
    # 注意：冻结规则只固定「每笔风险 = 提交时权益的 0.5%」这个**比例**，
    # 并未固定绝对本金；10000 USDT 是本轮研究口径，已写入每次 Run 的 inputs.json / summary。
    initial_equity: float = 2000.0
    risk_budget_per_trade: float = 0.005         # 每笔预算亏损 0.5% 账户权益
    max_active_positions: int = 3                # 最多 3 仓
    max_symbol_positions: int = 1                # 同币最多 1 仓
    max_total_risk_budget: float = 0.015         # 总风险预算上限 1.5%
    max_notional_per_position: float = 0.30      # 单仓名义金额上限 30%
    max_total_notional: float = 0.90             # 总名义金额上限 90%
    
    # 退出规则
    trailing_trigger_r: float = 2.0              # 4h 收盘浮盈达到 2R 启用跟踪止损
    trailing_lookback_4h_bars: int = 10          # 最近 10 根 4h 最低价作为新止损
    max_holding_days: int = 10                   # 最长持仓 10 个自然日 (240 小时)

    # 撮合约束 (冻结规则 §5)
    max_participation_of_prev_15m_quote: float = 0.001   # 订单名义 ≤ 已完成前一根 15m 成交额 0.1%
    
    # 费率假设 (基准方案 A/B)
    taker_fee_bps: float = 5.0                   # 5 bps (0.0005)
    slippage_bps: float = 3.0                    # 3 bps (0.0003)
    
    # 成本压力测试 (方案 C)
    stress_taker_fee_bps: float = 10.0           # 10 bps (0.0010)
    stress_slippage_bps: float = 6.0             # 6 bps (0.0006)
    
    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)
