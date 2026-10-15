# -*- coding: utf-8 -*-
"""历史 K 线**读侧语义隔离**：官方归档零量占位行 + 多代合约生命周期分段。

背景（2026-10-07 12h 任务，主助手 22:35 核查要求第 2/3 项）：
  1. 币安官方归档在**上市前 / 无合约窗口**会产出**零量平价占位行**
     （`volume==0` 且 `O=H=L=C`）。这些行是**官方归档证据**，
     但**不是有成交的趋势样本**，不得计入趋势输入。
  2. 个别标的为**同一符号、两代合约**（旧合约交割退市 → 新合约上市）。
     库内两代行**不得拼成一条连续可交易历史**，须按官方生命周期分段。

本模块纪律（只读）：
  - **不改库、不删行、不发请求**；只在**读取 / 趋势输入**侧打标签或隔离。
  - 库内原始记录与唯一现有库（`market.sqlite3`）保持不变。
  - 隔离/占位根数如实上报，不静默丢弃。
  - **范围限 12h**（本任务边界）；其他周期不做本模块的隔离，语义不变。
    如需扩展到其他周期，只须向下方注册表增项。

官方来源（可核查）：
  - ICP 旧合约下架公告：https://www.binance.com/en/support/announcement/detail/af469aeeab074738bb4a276070a9d11b
    （ICP USDT-M 合约 2022-06-10 09:00 UTC 自动交割，随后下架；旧符号改 ICPUSDT_SETTLED）
  - ICP 新合约上市公告：https://www.binance.com/en/support/announcement/detail/adabdfbc53344094808a7bea464f101b
    （USDⓈ-M ICP 永续 2022-09-27 02:30 UTC 上市，最高 25x）
  证据包：`验收证据/P04_继续补齐_日线剩余_官方公告原文与ICP生命周期证据_2026-10-07.json`
"""
from __future__ import annotations

from datetime import datetime, timezone

# ── 作用域：本模块只处理 12h（本任务边界）────────────────────────────────────
SCOPE_TF = "12h"

# ── 归档占位标的（12h）：由 pool_identity_12h.json 官方证据判定 ────────────────
# 判据：volume==0 且 OHLC 四值相同 ⇒ 官方归档占位行。
PADDING_SYMBOLS_12H = frozenset({
    ("binance", "AIAUSDT"), ("binance", "CTKUSDT"), ("binance", "CVCUSDT"),
    ("binance", "CVXUSDT"), ("binance", "LITUSDT"), ("binance", "MAVIAUSDT"),
    ("binance", "PUMPUSDT"), ("binance", "SLPUSDT"), ("binance", "ICPUSDT"),
})


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


# ── 官方合约生命周期分段（按标的 × 周期注册；当前仅 ICPUSDT 12h）──────────────
# 段语义：`tradeable=True` 表示该段是**可交易合约**的真实成交历史；
#         `tradeable=False` 表示官方**无合约窗口**（归档零量占位，非成交样本）。
#
# **12h 网格吸附（重要）**：官方交割/上市时点落在 12h bar 内部，bar 的开盘时间
# 不等于时点本身。为避免把「含真实成交的首根新合约 bar」误判为无合约窗口，
# 分段边界按 bar 开盘时间吸附：
#   · 旧合约交割 2022-06-10 09:00 UTC 落在 2022-06-10T00:00Z 那根内
#     → 该根仍属 g1（其 [00:00,12:00) 含真实成交至 09:00），g1 至 2022-06-10T12:00Z 止；
#   · 新合约上市 2022-09-27 02:30 UTC 落在 2022-09-27T00:00Z 那根内
#     → 该根即首根 g2（其 [00:00,12:00) 含 02:30 起的真实成交），g2 自 2022-09-27T00:00Z 起。
# 与库内实测一致：真实行 2021-07-01T00:00Z..2022-06-10T00:00Z（g1）、
#                占位行 2022-06-10T12:00Z..2022-09-21T12:00Z（gap）、
#                真实行 2022-09-27T00:00Z.. （g2）。
ICP_G1_END = _ms("2022-06-10T12:00:00Z")
ICP_G2_START = _ms("2022-09-27T00:00:00Z")

LIFECYCLE_SEGMENTS_12H = {
    ("binance", "ICPUSDT"): [
        {"generation": "g1", "label": "第一代（旧）ICPUSDT 永续",
         "start": None, "end": ICP_G1_END, "tradeable": True,
         "official_event_utc": "2022-06-10T09:00:00Z",
         "source": "af469aeeab074738bb4a276070a9d11b（2022-06-10 09:00 UTC 交割退市）"},
        {"generation": "gap", "label": "官方无合约窗口（归档零量占位）",
         "start": ICP_G1_END, "end": ICP_G2_START, "tradeable": False,
         "official_event_utc": "2022-06-10T09:00:00Z → 2022-09-27T02:30:00Z",
         "source": "af469aee…（退市）→ adabdfbc…（2022-09-27 02:30 UTC 新合约上市）"},
        {"generation": "g2", "label": "第二代（现行）ICPUSDT 永续",
         "start": ICP_G2_START, "end": None, "tradeable": True,
         "official_event_utc": "2022-09-27T02:30:00Z",
         "source": "adabdfbc53344094808a7bea464f101b（2022-09-27 02:30 UTC 上市）"},
    ],
}


def is_archive_padding_values(volume, o, h, low, c) -> bool:
    """按**数值**判归档占位：`volume==0` 且 OHLC 四值相同。取不到数值保守返回 False。"""
    try:
        vol = float(volume)
        o, h, low, c = float(o), float(h), float(low), float(c)
    except (TypeError, ValueError):
        return False
    return vol == 0.0 and o == h == low == c


def is_archive_padding_row(row) -> bool:
    """官方归档占位行判据：`volume==0` 且 OHLC 四值相同。

    纯函数，不依赖标的；返回 True 表示该行是归档占位（非有成交趋势样本）。
    取不到数值时保守返回 False（不误判为占位）。
    """
    try:
        return is_archive_padding_values(row["volume"], row["open"],
                                         row["high"], row["low"], row["close"])
    except (TypeError, KeyError, IndexError):
        return False


def lifecycle_segments(exchange: str, symbol: str, tf: str):
    """返回该 (交易所, 标的, 周期) 的官方生命周期分段；未注册返回 None。"""
    if tf != SCOPE_TF:
        return None
    return LIFECYCLE_SEGMENTS_12H.get((exchange, symbol))


def generation_of(exchange: str, symbol: str, tf: str, open_time: int):
    """返回 open_time 所属生命周期段 dict；未注册该标的则返回 None。"""
    segs = lifecycle_segments(exchange, symbol, tf)
    if not segs:
        return None
    for s in segs:
        lo, hi = s["start"], s["end"]
        if (lo is None or open_time >= lo) and (hi is None or open_time < hi):
            return s
    return None


def classify_row(exchange: str, symbol: str, tf: str, row) -> dict:
    """给单行打语义标签（不改行、不改库）。

    返回：
      row_kind   : "archive_padding" | "traded"
      generation : 生命周期段代号（如 "g1"/"gap"/"g2"）或 None
      tradeable  : 是否可计入**有成交趋势样本**
    """
    return classify_values(exchange, symbol, tf, int(row["open_time"]),
                           row["open"], row["high"], row["low"], row["close"], row["volume"])


def classify_values(exchange: str, symbol: str, tf: str, open_time: int,
                    o, h, low, c, volume) -> dict:
    """按**数值**给一行打语义标签（供非映射对象如策略 Bar 复用）。"""
    seg = generation_of(exchange, symbol, tf, int(open_time))
    padding = (tf == SCOPE_TF
               and (exchange, symbol) in PADDING_SYMBOLS_12H
               and is_archive_padding_values(volume, o, h, low, c))
    seg_tradeable = seg["tradeable"] if seg else True
    return {
        "row_kind": "archive_padding" if padding else "traded",
        "generation": seg["generation"] if seg else None,
        "tradeable": (not padding) and seg_tradeable,
    }


def annotate_rows(exchange: str, symbol: str, tf: str, rows) -> tuple[list, dict]:
    """批量打标签。返回 `(annotations, meta)`，**不修改 rows 本身**。

    annotations[i] 对应 rows[i] 的 classify_row 结果。
    meta 汇总：占位根数、按代际的根数、涉及代际列表、是否跨代。
    """
    anns = [classify_row(exchange, symbol, tf, r) for r in rows]
    padding_n = sum(1 for a in anns if a["row_kind"] == "archive_padding")
    non_tradeable_n = sum(1 for a in anns if not a["tradeable"])
    gens = []
    for a in anns:
        g = a["generation"]
        if g is not None and g not in gens:
            gens.append(g)
    per_gen = {}
    for a in anns:
        g = a["generation"] or "(未注册)"
        per_gen[g] = per_gen.get(g, 0) + 1
    meta = {
        "scope_tf": SCOPE_TF,
        "registered": lifecycle_segments(exchange, symbol, tf) is not None
                      or (tf == SCOPE_TF and (exchange, symbol) in PADDING_SYMBOLS_12H),
        "archive_padding_rows": padding_n,
        "non_tradeable_rows": non_tradeable_n,
        "generations": gens,
        "rows_per_generation": per_gen,
        "cross_generation": len([g for g in gens if g != "gap"]) > 1,
    }
    return anns, meta


def latest_generation(exchange: str, symbol: str, tf: str):
    """返回该标的最新一代（可交易）代号；未注册返回 None。"""
    segs = lifecycle_segments(exchange, symbol, tf)
    if not segs:
        return None
    tradeable = [s["generation"] for s in segs if s["tradeable"]]
    return tradeable[-1] if tradeable else None


def split_segments(exchange: str, symbol: str, tf: str, rows):
    """把行按生命周期段切成 `[(segment_dict, rows), ...]`（保持原顺序）。"""
    segs = lifecycle_segments(exchange, symbol, tf)
    if not segs:
        return [(None, list(rows))]
    out, order = {}, []
    for r in rows:
        s = generation_of(exchange, symbol, tf, int(r["open_time"]))
        key = s["generation"] if s else "(未注册)"
        if key not in out:
            out[key] = []
            order.append((key, s))
        out[key].append(r)
    return [(s, out[k]) for k, s in order]
