# -*- coding: utf-8 -*-
"""策略 B：15 分钟持续异常主动买盘。

逐根判定（全部因果安全）：
  - 主动买入占比 buy_ratio = 主动买入计价额 / 计价成交额。
  - 历史分位基线**只取当前 bar 之前** lookback_bars 根（严格早于当前 bar）。
  - 异常 bar：主动买入占比 ≥ 基线分位( buy_ratio_quantile )
              且 计价成交额 ≥ 基线分位( volume_quantile )
              且**至少一维严格高于基线**（持平基线、无增强的常量序列不算异常）。
  - 持续性：连续 persist_bars 根异常 → 在第 persist_bars 根的**收盘**确认，
    **不回填**到首根。
  - 去重：同一异常段只产生一次事件；直到出现非异常 bar 才能开启新段。
  - 时间连续性：相邻 bar 必须首尾相接（open_time == 上一根 close_time + 1）；
    出现时间缺口即断开异常段，不把跨缺口的 bar 当作“连续”。
  - 字段缺失：主动买入字段缺失（buy_ratio 为 None）时不判异常，并断开异常段。

买卖差 = 2×主动买入量 − 总量（同一成交量单位）仅作展示，
**不**作为独立加分条件，避免把主动买入量重复计入两次证据。

参数必须显式配置；缺配置返回“待配置”，缺 15m 数据返回“数据不足”。
"""
from . import common
from .common import Condition, Event, quantile

KEY = "B"
NAME = "15分钟持续异常主动买盘"
TIMEFRAME = "15m"
DIRECTION = "long"
REQUIRED_CONFIG = [
    "lookback_bars",        # 历史分位基线回看根数（不含当前 bar）
    "min_history_bars",     # 开始判定所需的最小历史根数
    "buy_ratio_quantile",   # 主动买入占比分位（0–1）
    "volume_quantile",      # 计价成交额分位（0–1）
    "persist_bars",         # 连续异常根数
]

VALIDATION = {
    "lookback_bars": {"type": "int", "min": 1},
    "min_history_bars": {"type": "int", "min": 1},
    "buy_ratio_quantile": {"type": "float", "min": 0.0, "max": 1.0},
    "volume_quantile": {"type": "float", "min": 0.0, "max": 1.0},
    "persist_bars": {"type": "int", "min": 1},
}


def _rel_min_history_le_lookback(cfg):
    mh, lb = int(cfg["min_history_bars"]), int(cfg["lookback_bars"])
    if mh > lb:
        return [f"min_history_bars({mh}) 必须 ≤ lookback_bars({lb})："
                f"判定所需历史不得多于回看窗口"]
    return []


RELATIONS = [_rel_min_history_le_lookback]


def _need_bars(cfg):
    """产生事件所需的最小前缀根数（**必要下界**）。

    连续 pb 根异常中，首根至少需要 minh 根历史，故确认根下标 ≥ minh + pb - 1，
    最小前缀 = minh + pb。作为必要下界，用它做“数据不足”门槛不会漏掉可判定事件。
    """
    return int(cfg["min_history_bars"]) + int(cfg["persist_bars"])


def _is_anomaly(ratio, base_ratio, qv, base_vol):
    """异常 = 两维都不低于基线，且至少一维**严格增强**。

    - 常量序列（ratio == base 且 qv == base）→ 无增强 → 不判异常。
    - 占比或成交额任一维严格高于基线即可（不额外引入未确认的数值阈值）。
    """
    if ratio is None or base_ratio is None or base_vol is None or qv is None:
        return False
    if ratio < base_ratio or qv < base_vol:
        return False
    return (ratio > base_ratio) or (qv > base_vol)


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

    lb = int(cfg["lookback_bars"])
    minh = int(cfg["min_history_bars"])
    qr = float(cfg["buy_ratio_quantile"])
    qv = float(cfg["volume_quantile"])
    pb = int(cfg["persist_bars"])

    events = []
    streak = 0
    seg_start = None
    prev = None
    for idx, bar in enumerate(bars):
        # 时间连续性：与上一根必须首尾相接，否则视为不同异常段
        gap = prev is not None and bar.open_time != prev.close_time + 1
        prev = bar
        if gap:
            streak = 0
            seg_start = None

        prior = bars[max(0, idx - lb):idx]           # 严格早于当前 bar
        if len(prior) < minh:
            continue
        base_ratio = quantile([b.buy_ratio for b in prior], qr)
        base_vol = quantile([b.quote_volume for b in prior], qv)
        ratio = bar.buy_ratio
        if base_ratio is None or base_vol is None or ratio is None:
            streak = 0
            seg_start = None
            continue
        if _is_anomaly(ratio, base_ratio, bar.quote_volume, base_vol):
            if streak == 0:
                seg_start = idx
            streak += 1
            if streak == pb:
                ev = Event(
                    strategy_key=KEY, strategy_version=resolved["version"],
                    config_hash=resolved["config_hash"],
                    exchange="", market="", symbol="", timeframe=TIMEFRAME,
                    status="confirmed", direction=DIRECTION,
                    event_time=bar.close_time, confirmed_at=bar.close_time,
                    source_id=bar.source_id,
                    evidence={"segment_start_close_time": bars[seg_start].close_time,
                              "confirm_close_time": bar.close_time,
                              "consecutive_bars": pb,
                              "bars_from_segment_start": idx - seg_start},
                    conditions=[
                        Condition("主动买入占比 ≥ 历史分位", round(ratio, 8),
                                  round(base_ratio, 8), "ratio", True,
                                  f"基线根数 {len(prior)}，分位 {qr}"),
                        Condition("计价成交额 ≥ 历史分位", bar.quote_volume,
                                  round(base_vol, 6), "quote", True,
                                  f"基线根数 {len(prior)}，分位 {qv}"),
                        Condition("连续异常根数", streak, pb, "根", True,
                                  "确认时间取第 N 根收盘，不回填首根"),
                        Condition("买卖差(仅展示，不单独加分)", bar.buy_sell_diff, None,
                                  "base", None,
                                  "同一单位下 2×主动买入量−总量；不重复计入证据"),
                    ])
                events.append(ev)
        else:
            streak = 0
            seg_start = None

    return {"status": common.STATUS_OK, "events": events,
            "diagnostics": {"strategy": KEY, "timeframe": TIMEFRAME,
                            "bars": len(bars), "required_bars": need,
                            "note": "仅已收盘 15m；分位基线只用当前 bar 之前的 bars；"
                                    "同段去重；时间不连续处断开异常段"}}
