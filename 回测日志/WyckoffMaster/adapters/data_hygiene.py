# -*- coding: utf-8 -*-
"""WyckoffMaster 回测数据加载层 · 3 条数据卫生规则（Data Hygiene Rules）。

本模块强制写死 3 条回测数据卫生铁律，所有回测数据加载必须通过本模块校验：

1. 同质币池锁定 (Homogeneous Pool Lock):
   - M0–M4 回测标的池严格锁定为 526 个拥有完整 K 线 + OI + Funding 的 Binance USDT 永续合约。
   - 物理排除无历史 OI 的 23 个 OKX 币种，严禁混入任何非 Binance 标的。

2. 基于 quote_volume 的准 PIT 动态过滤 (Quasi-PIT Dynamic Liquidity Filter):
   - 利用零空值的 K 线 quote_volume，在每个历史截面 t 进行动态准入判断。
   - 仅纳入“距首根 K 线已满 30 天且前 30 日滚动日均成交额达标”的币种。
   - 距首根 K 线不足 30 天或流动性未达标阶段，该币种在该截面不具备交易资格。

3. 长停牌切段保护 (Long Suspension Segmentation Guard - CVC / CVX / SLP / CTK / LIT 等):
   - 若某币种 K 线或 OI 出现连续 > 48 小时 (172,800,000 ms) 的停牌缺口，按合约停牌处理。
   - 停牌前最后一根闭合 15m bar 强平出场 (计双边费用与不利滑点)。
   - 复牌后重新累计 30 天指标预热，严禁跨停牌缺口连线计算 ATR 或结构锚点。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MIN15_MS = 900_000
SUSPENSION_GAP_THRESHOLD_MS = 48 * HOUR_MS  # 连续 > 48 小时视为长停牌
PREWARM_DAYS = 30
PREWARM_MS = PREWARM_DAYS * DAY_MS

POOL_FILE = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31/pool.json")


def load_canonical_526_pool() -> List[str]:
    """加载官方冻结的 526 个 Binance USDT 永续标的池。"""
    if not POOL_FILE.exists():
        raise FileNotFoundError(f"526 标的池文件不存在: {POOL_FILE}")
    with open(POOL_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    symbols = data.get("symbols", [])
    if len(symbols) != 526:
        raise ValueError(f"标的池数量异常: 期望 526，实际 {len(symbols)}")
    return symbols


CANONICAL_526_SET: Set[str] = set(load_canonical_526_pool())


# ════════════════════ 规则 1：同质币池锁定 ════════════════════

def assert_homogeneous_pool(symbols: Sequence[str]) -> List[str]:
    """规则 1：断言并锁定标的池为 526 个 Binance USDT 永续合约。

    - 排除所有 OKX 币种 (无历史 OI)；
    - 排除任何非 526 白名单币种。
    """
    cleaned: List[str] = []
    rejected_okx: List[str] = []
    rejected_other: List[str] = []

    for s in symbols:
        # 排除包含 OKX 特征或不在 526 白名单中的标的
        if "OKX" in s or "-SWAP" in s:
            rejected_okx.append(s)
            continue
        if s not in CANONICAL_526_SET:
            rejected_other.append(s)
            continue
        cleaned.append(s)

    if rejected_okx:
        raise ValueError(f"[数据卫生规则1] 检测到混入 {len(rejected_okx)} 个 OKX 币种，已严格拒绝: {rejected_okx[:5]}")
    if rejected_other:
        raise ValueError(f"[数据卫生规则1] 检测到混入 {len(rejected_other)} 个非 526 白名单币种，已严格拒绝: {rejected_other[:5]}")
    if not cleaned:
        raise ValueError("[数据卫生规则1] 筛选后有效标的池为空！")

    return cleaned


# ════════════════ 规则 2：准 PIT 动态成交额过滤 ════════════════

def is_admissible_quasi_pit(
    first_kline_ts_ms: int,
    current_ts_ms: int,
    rolling_30d_quote_volume_sum: float,
    min_rolling_daily_median_vol: float = 0.0,
) -> bool:
    """规则 2：准 PIT 动态过滤判定。

    在截面 current_ts_ms：
    1. 距该币首根 K 线必须已满 30 天 (current_ts - first_kline_ts >= 30 天)；
    2. 过去 30 日累计成交额 / 30 达到最低日均成交额门槛。
    """
    if current_ts_ms - first_kline_ts_ms < PREWARM_MS:
        return False
    daily_avg_quote_vol = rolling_30d_quote_volume_sum / float(PREWARM_DAYS)
    if daily_avg_quote_vol < min_rolling_daily_median_vol:
        return False
    return True


# ════════════════ 规则 3：长停牌切段与强平保护 ════════════════

def segment_on_suspension(
    bars: Sequence[Dict[str, Any]],
    max_gap_ms: int = SUSPENSION_GAP_THRESHOLD_MS,
) -> List[List[Dict[str, Any]]]:
    """规则 3：若某币种 K 线出现连续 > 48 小时的停牌缺口，按合约停牌切段处理。

    - 停牌前最后一根闭合 15m bar 标记为强平切点 (force_liquidate_exit)；
    - 复牌后独立切为新段，必须重新累计 30 天指标预热；
    - 严禁跨停牌缺口连线计算 ATR、Donchian 轨或结构锚点。
    """
    if not bars:
        return []

    segments: List[List[Dict[str, Any]]] = []
    current_seg: List[Dict[str, Any]] = []

    for i, bar in enumerate(bars):
        if not current_seg:
            current_seg.append(bar)
            continue

        prev_bar = current_seg[-1]
        prev_ts = prev_bar.get("close_time", prev_bar.get("open_time", 0))
        cur_ts = bar.get("open_time", 0)

        # 检查时间缺口是否超过 48 小时
        gap = cur_ts - prev_ts
        if gap > max_gap_ms:
            # 标记停牌前最后一根 bar 必须强平
            prev_bar["suspension_force_close"] = True
            prev_bar["suspension_gap_hours"] = gap / HOUR_MS
            segments.append(current_seg)
            # 复牌后开启新段，重新开始预热
            bar["post_suspension_resume"] = True
            current_seg = [bar]
        else:
            current_seg.append(bar)

    if current_seg:
        segments.append(current_seg)

    return segments


def print_data_hygiene_summary():
    """输出数据卫生规则摘要。"""
    print("=" * 60)
    print("WyckoffMaster 数据卫生规则 (Data Hygiene Rules) 就绪：")
    print(f"1. 同质币池锁定: 526 个 Binance USDT 永续标的 (已排除 23 个 OKX 标的)")
    print(f"2. 准 PIT 动态过滤: 截面距首根 K 线满 {PREWARM_DAYS} 天 + 前 30 日滚动成交额门槛")
    print(f"3. 长停牌切段保护: 缺口阈值 > {SUSPENSION_GAP_THRESHOLD_MS / HOUR_MS:.0f} 小时，停牌前末根 15m 强平出场，复牌后重新 30 天预热")
    print("=" * 60)
