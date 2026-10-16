# -*- coding: utf-8 -*-
"""v3.1 研究输入加载（数据/研究输入/WyckoffMaster_v31/{is,val}）。

物理隔离：只读 `is/`（IS 段 + 90 日预热）与 `val/`（Val 段 + 90 日预热）；
**没有** holdout 目录，加载后仍逐段断言无时间戳落入 Holdout 区间。
不引用 v3.0 全时段目录 `数据/研究输入/WyckoffMaster/`（含 Holdout）。
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from . import paths as P

SEG_DIR = {"IS": "is", "Val": "val"}


class Instrument:
    __slots__ = ("symbol", "tick_size", "step_size", "price_precision",
                 "quantity_precision", "onboard_time")

    def __init__(self, row):
        self.symbol = row["symbol"]
        self.tick_size = float(row["tick_size"]) if row["tick_size"] else 0.0
        self.step_size = float(row["step_size"]) if row["step_size"] else 0.0
        self.price_precision = int(row["price_precision"])
        self.quantity_precision = int(row["quantity_precision"])
        self.onboard_time = int(row["onboard_time"])


@functools.lru_cache(maxsize=1)
def load_instruments() -> dict:
    df = pq.read_table(P.V31_ROOT / "instruments.parquet").to_pandas()
    return {r["symbol"]: Instrument(r) for _, r in df.iterrows()}


@functools.lru_cache(maxsize=1)
def load_pool() -> list:
    d = json.loads((P.V31_ROOT / "pool.json").read_text(encoding="utf-8"))
    return list(d["symbols"])


def _arr(df, col, dtype=float):
    return np.asarray(df[col].to_numpy(), dtype=dtype)


class SegmentData:
    """单个段（IS/Val）的只读研究输入。"""

    def __init__(self, seg: str):
        P.assert_segment_not_holdout(seg)
        self.seg = seg
        self.root = P.V31_ROOT / SEG_DIR[seg]
        if not self.root.exists():
            raise FileNotFoundError(f"研究输入段目录不存在：{self.root}")
        self.instruments = load_instruments()
        self._cache = {}

    # ── 可用标的 ──────────────────────────────────────────────────────────
    def symbols(self, tf: str = "1h") -> list:
        d = self.root / "ohlcv" / tf
        got = sorted(p.stem for p in d.glob("*.parquet"))
        pool = set(load_pool())
        return [s for s in got if s in pool]

    def _read(self, key, path: Path):
        if key in self._cache:
            return self._cache[key]
        df = pq.read_table(path).to_pandas()
        self._cache[key] = df
        return df

    # ── OHLCV（信号周期 / 执行周期共用）────────────────────────────────────
    def ohlcv(self, sym: str, tf: str) -> dict:
        p = self.root / "ohlcv" / tf / f"{sym}.parquet"
        if not p.exists():
            return None
        df = self._read(("ohlcv", sym, tf), p)
        if len(df) == 0:
            return None
        ot = _arr(df, "open_time", np.int64)
        P.assert_no_holdout_ts(ot)
        return {
            "open_time": ot,
            "close_time": _arr(df, "close_time", np.int64),
            "open": _arr(df, "open"),
            "high": _arr(df, "high"),
            "low": _arr(df, "low"),
            "close": _arr(df, "close"),
            "volume": _arr(df, "volume"),
            "quote_volume": _arr(df, "quote_volume"),
        }

    # ── 资金费结算事件（真实 rate + 真实结算 mark）────────────────────────
    def funding(self, sym: str) -> dict:
        p = self.root / "funding" / f"{sym}.parquet"
        if not p.exists():
            return None
        df = self._read(("funding", sym), p)
        if len(df) == 0:
            return None
        return {
            "funding_time": _arr(df, "funding_time", np.int64),
            "funding_rate": _arr(df, "funding_rate"),
            "mark_price": _arr(df, "mark_price"),
        }

    # ── OI（5m 桶起点）────────────────────────────────────────────────────
    def oi(self, sym: str) -> dict:
        p = self.root / "oi_metrics" / f"{sym}.parquet"
        if not p.exists():
            return None
        df = self._read(("oi", sym), p)
        if len(df) == 0:
            return None
        return {"ts": _arr(df, "ts", np.int64), "open_interest": _arr(df, "open_interest")}

    def instrument(self, sym: str) -> Instrument:
        return self.instruments.get(sym)


def grid15(start_ms: int, end_ms: int):
    """全局 15m 网格（半开 [start, end)），起点向下对齐到 15m 边界。"""
    t0 = (start_ms // P.DETAIL_MS) * P.DETAIL_MS
    return range(t0, end_ms, P.DETAIL_MS)


if __name__ == "__main__":
    for seg in ("IS", "Val"):
        d = SegmentData(seg)
        syms = d.symbols("1h")
        print(seg, "symbols(1h)=", len(syms), "first=", syms[:3])
        b = d.ohlcv(syms[0], "1h")
        print("  ", syms[0], "1h rows", len(b["open_time"]), "inst", d.instrument(syms[0]).tick_size)
