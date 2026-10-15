# -*- coding: utf-8 -*-
"""策略 A：4 小时固定区间 → 放量突破收盘确认 → 后续独立回踩守住。

事件顺序（全部只用已收盘 bar，全部因果安全）：
  1) 冻结区间：用 bars[i-range_lookback : i] 形成上沿 U / 下沿 L，在 bars[i-1] 收盘时**冻结**。
  2) 突破确认：在 i 之后的 breakout_max_bars 根内，出现收盘 ≥ U×(1+tol) 且
     计价成交额 ≥ 前 breakout_volume_lookback 根均值 × mult 的 bar → 在该 bar 收盘确认。
  3) 独立回踩：在突破 bar **之后**（严格更晚的 bar）的 retest_max_bars 根内，
     出现 low ≤ U×(1+retest_tol) 且 close ≥ U×(1-retest_tol) 的 bar → 在该 bar 收盘确认。
  信号在回踩确认后产生，**不回填**到突破时点。

参数必须显式配置；缺配置返回“待配置”，缺 4h 数据返回“数据不足”。
"""
from . import common
from .common import Bar, Condition, Event, quantile  # noqa: F401

KEY = "A"
NAME = "4小时固定区间→放量突破收盘确认→独立回踩守住"
TIMEFRAME = "4h"
DIRECTION = "long"
REQUIRED_CONFIG = [
    "range_lookback",              # 区间形成根数
    "breakout_volume_lookback",    # 突破放量基线根数（严格取突破 bar 之前）
    "breakout_volume_mult",        # 放量倍数
    "breakout_tolerance_pct",      # 突破容差（占价格比例）
    "breakout_max_bars",           # 冻结后允许出现突破的最大根数
    "retest_max_bars",             # 突破后允许回踩的最大根数
    "retest_tolerance_pct",        # 回踩触及上沿的容差（占价格比例）
]

# 类型 / 有限性 / 范围校验（未冻结参数不得用默认值兜底）
VALIDATION = {
    "range_lookback": {"type": "int", "min": 1},
    "breakout_volume_lookback": {"type": "int", "min": 1},
    "breakout_volume_mult": {"type": "float", "min": 0.0, "exclusive_min": True},
    "breakout_tolerance_pct": {"type": "float", "min": 0.0},
    "breakout_max_bars": {"type": "int", "min": 1},
    "retest_max_bars": {"type": "int", "min": 1},
    "retest_tolerance_pct": {"type": "float", "min": 0.0},
}
RELATIONS = []


def _need_bars(cfg):
    """产生事件所需的最小前缀根数（**必要下界**，不是估计值）。

    事件至少需要：区间窗口 rl 根 + 放量基线 vl 根 + 突破 bar 1 根 + 独立回踩 bar 1 根。
    最早可确认的回踩下标 = max(rl, vl) + 1，故最小前缀 = max(rl, vl) + 2。

    这是必要下界：任何事件都不可能在该前缀之前确定，因此用它做“数据不足”门槛
    不会漏掉本可判定的事件，从而保证逐前缀一致（见 tests 的前缀一致性测试）。
    """
    return max(int(cfg["range_lookback"]), int(cfg["breakout_volume_lookback"])) + 2


def run(bars, resolved):
    status, missing, errors = common.validate_config(
        resolved, REQUIRED_CONFIG, VALIDATION, RELATIONS)
    if status == common.STATUS_PENDING:
        return common.pending_result(KEY, missing, TIMEFRAME)
    if status == common.STATUS_INVALID:
        return common.invalid_result(KEY, errors, TIMEFRAME)
    cfg = resolved["config"]
    need = _need_bars(cfg)
    if len(bars) < need:
        return common.insufficient_result(KEY, TIMEFRAME, len(bars), need)

    rl = int(cfg["range_lookback"])
    vl = int(cfg["breakout_volume_lookback"])
    vmult = float(cfg["breakout_volume_mult"])
    btol = float(cfg["breakout_tolerance_pct"])
    bmax = int(cfg["breakout_max_bars"])
    rmax = int(cfg["retest_max_bars"])
    rtol = float(cfg["retest_tolerance_pct"])

    events = []
    i = rl
    n = len(bars)
    while i < n:
        window = bars[i - rl:i]                       # 冻结用窗口，全部早于 bar i
        upper = max(b.high for b in window)
        lower = min(b.low for b in window)
        frozen_close = bars[i - 1].close_time

        breakout = None
        for j in range(i, min(i + bmax, n)):
            base = bars[max(0, j - vl):j]             # 严格早于 j
            if len(base) < vl:
                continue
            avg_vol = common.mean(b.quote_volume for b in base)
            if avg_vol is None or avg_vol <= 0:
                continue
            if bars[j].close >= upper * (1 + btol) and bars[j].quote_volume >= avg_vol * vmult:
                breakout = (j, avg_vol)
                break
        if breakout is None:
            i += 1
            continue

        j, avg_vol = breakout
        retest = None
        for r in range(j + 1, min(j + 1 + rmax, n)):  # 严格晚于突破 bar
            if (bars[r].low <= upper * (1 + rtol)) and (bars[r].close >= upper * (1 - rtol)):
                retest = r
                break

        if retest is not None:
            r = retest
            ev = Event(
                strategy_key=KEY, strategy_version=resolved["version"],
                config_hash=resolved["config_hash"],
                exchange="", market="", symbol="", timeframe=TIMEFRAME,
                status="confirmed", direction=DIRECTION,
                event_time=bars[r].close_time, confirmed_at=bars[r].close_time,
                source_id=bars[r].source_id,
                evidence={"frozen_at": frozen_close, "range_upper": upper, "range_lower": lower,
                          "breakout_close_time": bars[j].close_time,
                          "retest_close_time": bars[r].close_time,
                          "bars_breakout_to_retest": r - j},
                conditions=[
                    Condition("区间已预先冻结", frozen_close, None, "ms", True,
                              f"冻结于 bar[{i-1}] 收盘，区间根数 {rl}"),
                    Condition("突破收盘 ≥ 上沿×(1+容差)", round(bars[j].close, 10),
                              round(upper * (1 + btol), 10), "price", True, None),
                    Condition("突破计价成交额 ≥ 前均值×倍数", bars[j].quote_volume,
                              round(avg_vol * vmult, 6), "quote", True,
                              f"基线根数 {vl}"),
                    Condition("回踩 low 触及上沿容差内", bars[r].low,
                              round(upper * (1 + rtol), 10), "price", True, None),
                    Condition("回踩收盘 ≥ 上沿容差内", bars[r].close,
                              round(upper * (1 - rtol), 10), "price", True, None),
                ])
            events.append(ev)
            i = r + 1                                  # 该区间已消费
        else:
            i = j + 1

    return {"status": common.STATUS_OK, "events": events,
            "diagnostics": {"strategy": KEY, "timeframe": TIMEFRAME,
                            "bars": len(bars), "required_bars": need,
                            "note": "仅已收盘 4h；区间先冻结，回踩必须来自更晚的独立 bar"}}
