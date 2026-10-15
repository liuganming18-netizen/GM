#!/usr/bin/env python3
"""MarketDataRepository —— SQLite 热层 + DuckDB/Parquet 冷层 的独立读路由组件。

设计（对应用户 P1 规格）：
- 热层 = market_v2.sqlite3（默认近 30 天；但**每周期保证 latest200 可返回 200 根**）。
- 冷层 = data/archive/parquet/{metric}/... 下的 zstd Parquet 分片。
- 边界来自**已对账归档的固定水位**（构造时传入 `watermark_ms`），**不用 now-30天**。
- 点查（最近 N 根 / 指定时间前 N 根）**优先 SQLite**；全池横截面/回测走 **DuckDB**。
- 交叉查询**显式 ORDER BY 排序 + 去重**；保持原查询 `limit`、时间**左闭右开**边界、
  以及 `candles` 视图的**头版本语义**（只返回 head 指向的那一条版本）。

本模块只读，不改任何库/文件；不参与生产流量（由上层决定何时切换）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

INTERVAL_MS = {"5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000,
               "4h": 14400000, "8h": 28800000, "12h": 43200000, "1d": 86400000}

CANDLE_COLS = ("exchange", "market", "symbol", "timeframe", "open_time", "close_time",
               "open", "high", "low", "close", "volume", "quote_volume", "trade_count",
               "taker_buy_volume", "taker_buy_quote_volume", "source_id", "available_at",
               "is_closed")
HEAD_COLS = ("exchange", "market", "symbol", "timeframe", "open_time", "source_id")
OI_COLS = ("exchange", "market", "symbol", "period", "ts", "open_interest",
           "open_interest_value", "oi_ccy", "oi_usd", "source", "fetched_at")
FUNDING_COLS = ("exchange", "market", "symbol", "funding_time", "funding_rate",
                "mark_price", "rate_type", "source", "fetched_at")
SOURCE_COLS = ("id", "exchange", "market", "symbol", "timeframe", "source_url", "file_path",
               "sha256", "imported_at", "downloaded_at", "first_open", "last_close", "row_count")


class MarketDataRepository:
    """只读路由。所有查询返回 **时间升序** 的 dict 列表（与 `MarketStore.candles` 一致）。"""

    def __init__(self, hot_db, archive_root, watermark_ms, retention_days=30, min_bars=200,
                 latest_ms=None):
        self.hot_db = str(hot_db)
        self.archive_root = Path(archive_root)
        self.watermark_ms = int(watermark_ms)
        self.retention_days = int(retention_days)
        self.min_bars = int(min_bars)
        # 默认查询右边界 = **最新可得**，绝不使用冻结水位当默认 end。
        # 冻结水位只用于推导「热层下界 / 冷层上界」；若拿它当默认 end，会把水位之后
        # 的所有新根（切流后每天仍在增长）静默截掉。
        self._latest_ms = None if latest_ms is None else int(latest_ms)

    # ---------- 单一事实源：归档水位 ----------
    # 水位由**归档器**（`p3_archive_hot.py`）在校验通过后推进并写入
    # `archive_root/_state.json`。读侧必须读**同一份**，否则会出现
    # 「归档器已推进水位、读侧仍用 env 里的旧水位」⇒ 冷热边界两套语义并存，
    # 中间那段既不在热层下界之上、也不在冷档上界之下的数据会被静默漏读。
    @staticmethod
    def read_state(archive_root):
        """读取归档状态文件 `_state.json`（只读）。缺失/损坏返回 {}。"""
        try:
            return json.loads((Path(archive_root) / "_state.json").read_text())
        except Exception:                                    # noqa: BLE001
            return {}

    @classmethod
    def from_archive(cls, hot_db, archive_root, retention_days=None, min_bars=200,
                     watermark_ms=None, latest_ms=None):
        """以 `_state.json` 为准构造（**生产读侧唯一入口**）。

        `watermark_ms` 仅在显式传入时覆盖状态文件（供测试/复现）；`retention_days`
        同理。状态文件缺失时退化为 `reconcile_watermark_ms`，再退化为 0
        （= 热层下界不设天数约束，只靠 latest200，保守但绝不漏读冷档）。
        """
        st = cls.read_state(archive_root)
        wm = watermark_ms or st.get("watermark_ms") or st.get("reconcile_watermark_ms") or 0
        rd = retention_days if retention_days is not None else st.get("retention_days", 30)
        return cls(hot_db, archive_root, watermark_ms=int(wm), retention_days=int(rd),
                   min_bars=min_bars, latest_ms=latest_ms)

    def latest_ms(self):
        """最新可得的右边界（毫秒）。可注入以便复现；默认取当前墙钟。"""
        if self._latest_ms is not None:
            return self._latest_ms
        import time
        return int(time.time() * 1000)

    # ---------- 热层下界：保证 latest200 ----------
    def hot_floor_ms(self, timeframe, latest_closed_ms=None):
        """热层必须保留的最早 open_time（含）。

        = min( 水位 - retention_days , latest_closed - min_bars*interval )
        取更早者 ⇒ 同时满足「近30天」与「每周期 latest200 可返回200根」。
        """
        iv = INTERVAL_MS[timeframe]
        by_days = self.watermark_ms - self.retention_days * 86400000
        if latest_closed_ms is None:
            return by_days
        by_bars = int(latest_closed_ms) - self.min_bars * iv
        return min(by_days, by_bars)

    # ---------- SQLite 热层 ----------
    def _hot(self):
        con = sqlite3.connect(f"file:{self.hot_db}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con

    def _hot_candles(self, exchange, market, symbol, timeframe, start, end, limit, source_id):
        table = "candles" if source_id is None else "candle_versions"
        sql = (f"SELECT * FROM {table} WHERE exchange=? AND market=? AND symbol=? AND timeframe=? "
               "AND open_time>=? AND open_time<? ")
        params = [exchange, market, symbol, timeframe, start, end]
        if source_id is not None:
            sql += "AND source_id=? "
            params.append(source_id)
        sql += "ORDER BY open_time DESC LIMIT ?"
        params.append(limit)
        with closing(self._hot()) as con:
            return [dict(r) for r in con.execute(sql, params)]

    # ---------- 冷层（Parquet）路径 ----------
    def _candle_shards(self, exchange, market, symbol, timeframe, metric="candles"):
        d = self.archive_root / metric / exchange / market / symbol / timeframe
        return sorted(d.glob("*.parquet")) if d.is_dir() else []

    def _flat_shards(self, exchange, market, symbol, metric):
        d = self.archive_root / metric / exchange / market / symbol
        return sorted(d.glob("*.parquet")) if d.is_dir() else []

    def _cold_candles(self, exchange, market, symbol, timeframe, start, end, limit, source_id):
        import duckdb
        # 冷档 metric 目录名恒为 `candles`（该目录存的就是 `candle_versions` 的**全版本**行），
        # 不是 `candle_versions`。此前按 source_id 分支写 `candle_versions` ⇒ 目录不存在 ⇒
        # 带 source_id 的冷查询恒返回空。已实核订正。
        shards = self._candle_shards(exchange, market, symbol, timeframe, "candles")
        if not shards:
            return []
        # 冷层是「全版本」；无 source_id 时需按 head 过滤 ⇒ 走 candle_heads 分片取 head。
        cols = ", ".join(CANDLE_COLS)
        with duckdb.connect() as d:
            d.execute("PRAGMA threads=2")
            files = [str(p) for p in shards]
            if source_id is None:
                heads = self._candle_shards(exchange, market, symbol, timeframe, "candle_heads")
                hfiles = [str(p) for p in heads]
                if not hfiles:
                    return []
                sql = (f"SELECT {', '.join('v.'+c for c in CANDLE_COLS)} FROM read_parquet(?) v "
                       "JOIN read_parquet(?) h USING(exchange,market,symbol,timeframe,open_time,source_id) "
                       "WHERE v.exchange=? AND v.market=? AND v.symbol=? AND v.timeframe=? "
                       "AND v.open_time>=? AND v.open_time<? "
                       "ORDER BY v.open_time DESC LIMIT ?")
                rows = d.execute(sql, [files, hfiles, exchange, market, symbol, timeframe,
                                       start, end, limit]).fetchall()
            else:
                sql = (f"SELECT {cols} FROM read_parquet(?) "
                       "WHERE exchange=? AND market=? AND symbol=? AND timeframe=? "
                       "AND open_time>=? AND open_time<? AND source_id=? "
                       "ORDER BY open_time DESC LIMIT ?")
                rows = d.execute(sql, [files, exchange, market, symbol, timeframe,
                                       start, end, source_id, limit]).fetchall()
            names = [c[0] for c in d.description]
        return [dict(zip(names, r)) for r in rows]

    # ---------- 公开：candles ----------
    def candles(self, exchange, market, symbol, timeframe, start=0, end=None, limit=500,
                source_id=None, prefer="hot"):
        """点查 K 线。返回升序。

        路由：默认先热层；若热层命中不足 `limit` 且请求区间早于热层下界 ⇒ 用冷层补齐。
        头版本语义：无 source_id 时读 `candles` 视图口径（= candle_versions JOIN candle_heads）。
        """
        if not 1 <= limit <= 2000:
            raise ValueError("每次读取限制为1至2000根")
        if end is None:
            end = self.latest_ms()   # 默认右边界 = 最新可得，**不是**冻结水位
        hot = []
        if prefer != "cold":
            hot = self._hot_candles(exchange, market, symbol, timeframe, start, end, limit, source_id)
        # 路由条件（本窗修复，关键）：
        #   原写法 `len(hot) < limit and start < hot_floor` 对**完全不在热层的组合**
        #   （例如 263 个非池 OKX 组合：冷档有分片、热层一行没有）会短路——热层返回 0，
        #   而 start 落在热层区间内 ⇒ 冷档**永不查询** ⇒ 静默返回空。这与
        #   `oi()`/`funding()` 的既有条件不一致，属语义分叉。
        #   现统一为「热层没凑够 limit 就问冷档」；冷档与热层在 [hot_floor, 水位] 有重叠，
        #   合并时同 key 以热层为准（_merge_dedup），不会引入重复或倒退。
        if prefer == "cold" or len(hot) < limit:
            cold = self._cold_candles(exchange, market, symbol, timeframe, start, end, limit, source_id)
            merged = self._merge_dedup(hot, cold, key=("open_time",), limit=limit)
            return list(reversed(merged))
        return list(reversed(hot))

    @staticmethod
    def _merge_dedup(a, b, key, limit):
        """显式按 key 去重（同 key 保留先出现的 a）→ 排序 → 截断。a、b 均为 open_time DESC。"""
        seen, out = set(), []
        for row in list(a) + list(b):
            k = tuple(row[c] for c in key)
            if k in seen:
                continue
            seen.add(k)
            out.append(row)
        out.sort(key=lambda r: tuple(r[c] for c in key), reverse=True)
        return out[:limit]

    # ---------- 公开：OI ----------
    def oi(self, exchange, market, symbol, period="snapshot", start=None, end=None, limit=500,
           prefer="hot"):
        """OI 点查。period ∈ snapshot|5m；保持 period 与原始金额语义（open_interest / _value / oi_ccy / oi_usd）。"""
        where = "exchange=? AND market=? AND symbol=? AND period=?"
        params = [exchange, market, symbol, period]
        if start is not None:
            where += " AND ts>=?"; params.append(start)
        if end is not None:
            where += " AND ts<=?"; params.append(end)
        hot = []
        if prefer != "cold":
            with closing(self._hot()) as con:
                hot = [dict(r) for r in con.execute(
                    f"SELECT * FROM oi_history WHERE {where} ORDER BY ts DESC LIMIT ?",
                    params + [limit])]
        if prefer == "cold" or len(hot) < limit:
            cold = self._cold_flat(exchange, market, symbol, "oi", where, params, limit,
                                   order_by="ts DESC")
            hot = self._merge_dedup(hot, cold, key=("period", "ts"), limit=limit)
        return list(reversed(hot))

    # ---------- 公开：资金费 ----------
    def funding(self, exchange, market, symbol, start=None, end=None, limit=500, prefer="hot"):
        """真实结算事件。保留真实事件时间与费率正负号；不重采样、不补 0。"""
        where = "exchange=? AND market=? AND symbol=?"
        params = [exchange, market, symbol]
        if start is not None:
            where += " AND funding_time>=?"; params.append(start)
        if end is not None:
            where += " AND funding_time<=?"; params.append(end)
        hot = []
        if prefer != "cold":
            with closing(self._hot()) as con:
                hot = [dict(r) for r in con.execute(
                    f"SELECT * FROM funding_history WHERE {where} ORDER BY funding_time DESC LIMIT ?",
                    params + [limit])]
        if prefer == "cold" or len(hot) < limit:
            cold = self._cold_flat(exchange, market, symbol, "funding", where, params, limit,
                                   order_by="funding_time DESC")
            hot = self._merge_dedup(hot, cold, key=("funding_time",), limit=limit)
        return list(reversed(hot))

    def _cold_flat(self, exchange, market, symbol, metric, where, params, limit, order_by):
        import duckdb
        shards = self._flat_shards(exchange, market, symbol, metric)
        if not shards:
            return []
        with duckdb.connect() as d:
            rows = d.execute(
                f"SELECT * FROM read_parquet(?) WHERE {where} ORDER BY {order_by} LIMIT ?",
                [ [str(p) for p in shards] ] + params + [limit]).fetchall()
            names = [c[0] for c in d.description]
        return [dict(zip(names, r)) for r in rows]

    # ---------- 公开：全池横截面（DuckDB over Parquet） ----------
    def cross_section(self, timeframe, at_ms, lookback=200, symbols=None, metric="candles"):
        """固定时刻的全池横截面（每币「过去 lookback 根」），走 DuckDB over 冷层 Parquet。

        返回 {symbol: [rows...]}，每行升序；不足 lookback 根的币按实际返回（不补）。
        """
        import duckdb
        base = self.archive_root / metric
        heads = self.archive_root / "candle_heads"
        glob = str(base / "*" / "*" / "*" / timeframe / "*.parquet")
        hglob = str(heads / "*" / "*" / "*" / timeframe / "*.parquet")
        # 廉价存在性判断（避免对 1.2 万+ 分片 rglob 全树）。
        if not next(base.glob(f"*/*/*/{timeframe}/*.parquet"), None):
            return {}
        with duckdb.connect() as d:
            d.execute("PRAGMA threads=2")
            # 横截面必须走**头版本语义**：`candles` 目录存全版本，直接 ROW_NUMBER 会把
            # 同一 open_time 的旧版本也计入 ⇒ 用 candle_heads 过滤到 head 指向的那一条。
            sql = (
                "WITH v AS ("
                f"  SELECT exchange, market, symbol, open_time, close, source_id "
                f"  FROM read_parquet('{glob}') WHERE timeframe=? AND open_time<?), "
                "h AS ("
                f"  SELECT exchange, market, symbol, open_time, source_id "
                f"  FROM read_parquet('{hglob}') WHERE timeframe=?), "
                "j AS (SELECT v.symbol, v.open_time, v.close FROM v "
                "      JOIN h USING(exchange, market, symbol, open_time, source_id)), "
                "ranked AS ("
                "  SELECT symbol, open_time, close, "
                "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY open_time DESC) rn "
                "  FROM j) "
                "SELECT symbol, open_time, close FROM ranked WHERE rn<=? ORDER BY symbol, open_time")
            rows = d.execute(sql, [timeframe, at_ms, timeframe, lookback]).fetchall()
        out = {}
        for sym, ot, cl in rows:
            if symbols and sym not in symbols:
                continue
            out.setdefault(sym, []).append({"open_time": ot, "close": cl})
        return out
