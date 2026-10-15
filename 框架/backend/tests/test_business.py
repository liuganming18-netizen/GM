# -*- coding: utf-8 -*-
"""离线测试：迁移 / 配置校验 / 因果时序 / 逐前缀一致 / 去重 / 只读契约。

不使用网络、不使用凭据；合成 bar 只存在于内存或临时库，不写共享行情库。
运行（必须用 Python 3.13；本包使用 PEP 604 类型语法）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  PYTHONDONTWRITEBYTECODE=1 $PY -m unittest discover -s tests -v
"""
import csv
import hashlib
import importlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PROJECT = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
for p in (str(HERE), PROJECT):
    if p not in sys.path:
        sys.path.insert(0, p)

# 关闭 adapter 的 TTL 缓存：测试会在同一进程内**改库后立刻再读**，
# 缓存（生产用于把 549 标的的重复计算摊薄）会让断言读到旧结果。
# 必须在任何 `importlib.reload(adapter)` 之前设置，reload 时才会读到。
os.environ["ADAPTER_CACHE"] = "0"

from data_hub.store import MarketStore                      # noqa: E402
from strategies import common, strategy_a, strategy_b       # noqa: E402
import migrate                                              # noqa: E402

IV = 14400000          # 4h
IV15 = 900000          # 15m
DAY_OPEN = 1704067200000
DAY_CLOSE = 1704153599999
T4H_FROZEN = 1704153600000
T4H_BREAKOUT = 1704168000000
T4H_RETEST = 1704182400000


def mk(k, o, h, l, c, qv, vol=None, tbv=None, tbqv=None, interval=IV):
    """构造一根已收盘 bar（时间戳连续：open_time == 上一根 close_time + 1）。"""
    ot = DAY_OPEN + k * interval
    vol = qv if vol is None else vol
    tbv = vol / 2 if tbv is None else tbv
    tbqv = qv / 2 if tbqv is None else tbqv
    return common.Bar(ot, ot + interval - 1, o, h, l, c, vol, qv, 10, tbv, tbqv, 1)


def mk_missing_taker(k, qv, interval=IV15):
    """主动买入字段缺失的 bar（用于验证缺失字段不被当作异常）。"""
    ot = DAY_OPEN + k * interval
    return common.Bar(ot, ot + interval - 1, 100, 101, 99, 100, qv, qv, 10, None, None, 1)


def cfg(**kw):
    base = {"range_lookback": 3, "breakout_volume_lookback": 2, "breakout_volume_mult": 1.5,
            "breakout_tolerance_pct": 0.001, "breakout_max_bars": 2, "retest_max_bars": 2,
            "retest_tolerance_pct": 0.002}
    base.update(kw)
    return {"version": "test", "config": base, "config_hash": common.config_hash(base),
            "frozen": False}


def cfg_b(**kw):
    base = {"lookback_bars": 5, "min_history_bars": 5, "buy_ratio_quantile": 0.8,
            "volume_quantile": 0.5, "persist_bars": 3}
    base.update(kw)
    return {"version": "test", "config": base, "config_hash": common.config_hash(base),
            "frozen": False}


def a_bars(retest=True):
    b = [mk(0, 101, 102, 99, 101, 1000), mk(1, 102, 103, 100, 102, 1000),
         mk(2, 101, 103, 100, 101, 1000), mk(3, 100, 102, 99, 100, 1000),
         mk(4, 101, 104, 101, 104, 2000)]
    if retest:
        b.append(mk(5, 103.5, 103.8, 103.0, 103.5, 1000))
    else:
        b.append(mk(5, 104, 104.5, 104.2, 104.4, 1000))
    b += [mk(6, 104, 104.5, 103.5, 104, 1000), mk(7, 104, 104.5, 103.5, 104, 1000)]
    return b


def b_bars(n_normal=5, n_anom=4, normal_ratio=0.5, anom_ratio=0.9):
    b, k = [], 0
    for _ in range(n_normal):
        b.append(mk(k, 100, 101, 99, 100, 1000, vol=1000, tbv=normal_ratio * 1000,
                    tbqv=normal_ratio * 1000, interval=IV15))
        k += 1
    for _ in range(n_anom):
        b.append(mk(k, 100, 101, 99, 100, 2000, vol=2000, tbv=anom_ratio * 2000,
                    tbqv=anom_ratio * 2000, interval=IV15))
        k += 1
    return b


def flat_bars(n=10, qv=1000, ratio=0.5):
    return [mk(k, 100, 101, 99, 100, qv, vol=qv, tbv=ratio * qv, tbqv=ratio * qv, interval=IV15)
            for k in range(n)]


def iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_series(path, exchange, market, symbol, timeframe, bars, source_id=1):
    """把合成 bar 写入临时库（candle_versions + candle_heads，使 candles 视图可见）。"""
    with closing(sqlite3.connect(path)) as con:
        for b in bars:
            con.execute(
                "INSERT INTO candle_versions(exchange,market,symbol,timeframe,open_time,"
                "close_time,open,high,low,close,volume,quote_volume,trade_count,"
                "taker_buy_volume,taker_buy_quote_volume,source_id,available_at,is_closed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (exchange, market, symbol, timeframe, b.open_time, b.close_time,
                 str(b.open), str(b.high), str(b.low), str(b.close), str(b.volume),
                 str(b.quote_volume), b.trades, str(b.taker_buy_volume),
                 str(b.taker_buy_quote_volume), source_id, None))
            con.execute("INSERT INTO candle_heads VALUES(?,?,?,?,?,?)",
                        (exchange, market, symbol, timeframe, b.open_time, source_id))
        con.commit()


def insert_signal(path, *, strategy_key="A", timeframe="4h", confirmed_at, evidence,
                  status="confirmed", direction="long", version="test-v1",
                  config_hash="cfgabc", data_version="dv:test"):
    with closing(sqlite3.connect(path)) as con:
        cur = con.execute(
            "INSERT INTO signals(strategy_key,strategy_version,config_hash,exchange,market,"
            "symbol,timeframe,status,direction,event_time,confirmed_at,data_version,source_id,"
            "evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (strategy_key, version, config_hash, "binance", "um", "1000PEPEUSDT", timeframe,
             status, direction, confirmed_at, confirmed_at, data_version, 1,
             json.dumps(evidence, ensure_ascii=False), confirmed_at))
        sid = cur.lastrowid
        con.execute("INSERT INTO signal_conditions(signal_id,name,actual,threshold,unit,"
                    "satisfied,note) VALUES(?,?,?,?,?,?,?)",
                    (sid, "突破收盘 ≥ 上沿×(1+容差)", "104.0", "103.103", "price", 1, "基线根数 2"))
        con.commit()
        return sid


def insert_wx_signal(path, *, arm="3a", timeframe="1h", confirmed_at, direction="long",
                     status="confirmed", evidence=None, version=None, config_hash=None):
    """插入一条**当前启用族（F_WX）**信号：版本/config_hash 默认取当前生效值。

    2026-10-11 方向门禁整改后，市场行方向只认「当前族 + 当前版本 + 当前配置 + confirmed」
    的信号；本助手用于构造这类信号（也可显式传 version/config_hash 造「非当前」反例）。
    """
    ver, arms = _wx_current_identity()
    return insert_signal(path, strategy_key=arm, timeframe=timeframe,
                         confirmed_at=confirmed_at, evidence=evidence or {},
                         status=status, direction=direction,
                         version=version or ver, config_hash=config_hash or arms.get(arm),
                         data_version="dv:test")


def _wx_current_identity():
    from strategies import wx_config as _wc
    from strategies.common import config_hash as _ch
    view, _ = _wc.load_view()
    if not view:
        return None, {}
    arms = {a: _ch(_wc.config_for_registered(view, a)) for a in ("3a", "3b", "3c", "3d")}
    return view.get("spec_version"), arms


class TempDb:
    """临时库：真实 schema + 1 根真实形态日线（仅用于契约/迁移测试）。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "market.sqlite3")
        MarketStore(self.path)
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("INSERT INTO sources(exchange,market,symbol,timeframe,source_url,file_path,"
                        "sha256,imported_at,downloaded_at,first_open,last_close,row_count) "
                        "VALUES('binance','um','1000PEPEUSDT','1d','u','f','abc',1,1,"
                        "1704067200000,1704153599999,1)")
            con.execute("INSERT INTO candle_versions VALUES('binance','um','1000PEPEUSDT','1d',"
                        "1704067200000,1704153599999,'0.0012956','0.0014121','0.0012630',"
                        "'0.0014066','67542067435','90235763.9773825',471578,'33002121600',"
                        "'44095932.9676719',1,NULL,1)")
            con.execute("INSERT INTO candle_heads VALUES('binance','um','1000PEPEUSDT','1d',"
                        "1704067200000,1)")
            con.commit()

    def cleanup(self):
        self.tmp.cleanup()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = TempDb()
        self.addCleanup(self.db.cleanup)

    def _count(self):
        with closing(sqlite3.connect(self.db.path)) as con:
            return con.execute("SELECT COUNT(*) FROM candle_versions").fetchone()[0]

    def test_candle_probe_is_rowid_free(self):
        """K 线规模探针**不得依赖 rowid**：在 WITHOUT ROWID 表上必须仍可用。

        动机（2026-10-13 订正 `migrate.candle_count`）：`candle_versions` 的既定改造
        方向是 `WITHOUT ROWID`（见 库内冗余清理_2026-10-13/D4 施工单）。`WITHOUT ROWID`
        表**没有 rowid**，旧实现 `MAX(rowid)` 会抛 `OperationalError`，而旧代码把该异常
        一律翻译成「candle_versions 不存在」—— 把「表在、探针失效」**误报**成「表不存在」。
        本测试同时锁住三件事：① 探针在 WITHOUT ROWID 表上可用；② 口径是主键字典序末键；
        ③ 单调不减（调用方判据 `after >= before` 依赖它）。
        """
        with closing(sqlite3.connect(":memory:")) as con:
            con.execute(
                "CREATE TABLE candle_versions("
                "exchange TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,"
                "timeframe TEXT NOT NULL, open_time INTEGER NOT NULL,"
                "source_id INTEGER NOT NULL,"
                "PRIMARY KEY(exchange,market,symbol,timeframe,open_time,source_id)"
                ") WITHOUT ROWID")

            # 前置断言：该表确实没有 rowid，旧实现必然在此失败
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("SELECT MAX(rowid) FROM candle_versions").fetchone()

            # ① 空表：不报错，如实回报
            v, note = migrate.candle_count(con)
            self.assertIsNone(v)
            self.assertIn("空表", note)

            # ② 口径 = 主键字典序末键
            con.executemany(
                "INSERT INTO candle_versions VALUES(?,?,?,?,?,?)",
                [("binance", "um", "BTCUSDT", "1d", 1704067200000 + i * 86400000, 1)
                 for i in range(3)])
            v, note = migrate.candle_count(con)
            self.assertEqual(v, ("binance", "um", "BTCUSDT", "1d", 1704240000000, 1))
            self.assertIn("末键", note)

            # ③ 单调不减：新增一个字典序更大的键，探针只前进不回退
            con.execute("INSERT INTO candle_versions VALUES"
                        "('okx','swap','ETH-USDT-SWAP','1d',1704067200000,9)")
            v2, _ = migrate.candle_count(con)
            self.assertGreaterEqual(v2, v)
            self.assertEqual(v2, ("okx", "swap", "ETH-USDT-SWAP", "1d", 1704067200000, 9))

        # 表不存在：如实回报「不存在」，而不是抛异常
        with closing(sqlite3.connect(":memory:")) as empty:
            v3, note3 = migrate.candle_count(empty)
            self.assertIsNone(v3)
            self.assertIn("不存在", note3)

    def test_migration_idempotent_and_non_destructive(self):
        before = self._count()
        r1 = migrate.migrate(self.db.path)
        r2 = migrate.migrate(self.db.path)          # 重复执行
        self.assertTrue(r1["K线未丢失"])
        self.assertTrue(r2["K线未丢失"])
        self.assertEqual(self._count(), before)
        self.assertEqual(r1["本次应用"], [1, 2, 3, 4, 5, 6, 8, 9, 7])
        self.assertEqual(r2["本次应用"], [])        # 第二次无待应用
        with closing(sqlite3.connect(self.db.path)) as con:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("strategy_configs", "signals", "signal_conditions", "oi_history",
                  "funding_history", "schema_migrations",
                  "download_runs", "download_tasks", "symbol_pool", "coverage_cache",
                  "realtime_quotes", "symbol_classification", "collector_state",
                  "collector_rounds", "candle_field_notes",
                  # v8：未收盘 K 线临时层 + 每 (交易所,周期) 的实时 K 线链路状态
                  "realtime_klines", "kline_stream_state",
                  # v9：流通量（流动市值列的数据来源；币安 openInterestHist 同一行）
                  "symbol_supply"):
            self.assertIn(t, names)
        # v7：实时表须有**按字段独立时点/来源**列，否则 mark/funding 的局部更新
        # 会推进价格时点、伪造价格新鲜度。
        with closing(sqlite3.connect(self.db.path)) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info('realtime_quotes')")}
        self.assertLessEqual({"price_event_time", "price_received_at", "price_source",
                              "price_transport", "mark_event_time", "mark_source",
                              "funding_event_time", "funding_source", "volume_24h_base",
                              "volume_24h_contract", "volume_source", "open24h"}, cols)
        # v8：临时层主键 = (exchange,market,symbol,timeframe) ⇒ 每个合约每周期**只有一行**
        # （当前那根未收盘 bar），且不得有 is_closed=1 的历史混入。
        with closing(sqlite3.connect(self.db.path)) as con:
            kl_cols = {r[1] for r in con.execute("PRAGMA table_info('realtime_klines')")}
            kst_cols = {r[1] for r in con.execute("PRAGMA table_info('kline_stream_state')")}
        self.assertLessEqual({"exchange", "market", "symbol", "timeframe", "open_time",
                              "close_time", "open", "high", "low", "close", "volume",
                              "quote_volume", "trade_count", "is_closed", "event_time",
                              "received_at", "source", "transport", "updated_at"}, kl_cols)
        self.assertLessEqual({"last_msg_at", "last_forming_open_time", "last_closed_open_time",
                              "closed_written", "gapfill_calls", "gapfill_filled",
                              "gapfill_skipped", "reconnects", "backoff_s", "status"},
                             kst_cols)

    def test_v5_realtime_tables_separate_from_closed_candles(self):
        """v5 实时表与已收盘 K 线**分开**：实时只进 realtime_quotes，不写 candle_*。"""
        migrate.migrate(self.db.path)
        with closing(sqlite3.connect(self.db.path)) as con:
            con.row_factory = sqlite3.Row
            cols = {r["name"] for r in con.execute("PRAGMA table_info('realtime_quotes')")}
            sc = {r["name"] for r in con.execute("PRAGMA table_info('symbol_classification')")}
        # 单位与时间语义字段必须齐备（欧易张数、oiCcy/oiUsd、事件/接收/获取时间）
        self.assertLessEqual({"exchange", "market", "symbol", "last_price",
                              "price_change_pct_24h", "quote_volume_24h", "mark_price",
                              "funding_rate", "next_funding_time", "event_time",
                              "received_at", "fetched_at", "source", "transport",
                              "unit_note"}, cols)
        self.assertLessEqual({"instrument_category", "category_name", "category_field",
                              "in_active_range", "excluded_reason", "raw_json"}, sc)
        # 实时快照**不**得混入 candle_* 表：写入实时行后 K 线计数不变
        before = self._count()
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("INSERT INTO realtime_quotes(exchange,market,symbol,last_price,"
                        "event_time,received_at,source,transport,updated_at) "
                        "VALUES('binance','um','ZZZUSDT','1.0',1,2,'test','rest',3)")
            con.commit()
        self.assertEqual(self._count(), before)

    def test_v3_v4_tables_are_additive_and_idempotent(self):
        """v3/v4 只新增表，不改删 K 线；重复执行无副作用。"""
        migrate.migrate(self.db.path)
        with closing(sqlite3.connect(self.db.path)) as con:
            con.row_factory = sqlite3.Row
            dt_cols = {r["name"] for r in con.execute("PRAGMA table_info('download_tasks')")}
            sp_cols = {r["name"] for r in con.execute("PRAGMA table_info('symbol_pool')")}
            cc_cols = {r["name"] for r in con.execute("PRAGMA table_info('coverage_cache')")}
        self.assertLessEqual({"exchange", "market", "symbol", "timeframe", "status",
                              "planned_files", "imported_files", "missing_files", "failed_files",
                              "rows", "gaps"}, dt_cols)
        self.assertLessEqual({"exchange", "market", "symbol", "contract_type", "spot_status",
                              "priority_hint", "snapshot_utc"}, sp_cols)
        self.assertLessEqual({"exchange", "market", "symbol", "timeframe", "rows",
                              "first_open", "last_close", "missing"}, cc_cols)
        # 同一 (标的,周期) 幂等 upsert
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("INSERT INTO coverage_cache VALUES('binance','um','X','1d',5,1,2,5,0,1,1)")
            con.execute("INSERT INTO coverage_cache VALUES('binance','um','X','1d',6,1,2,6,0,1,2) "
                        "ON CONFLICT(exchange,market,symbol,timeframe) DO UPDATE SET rows=6")
            con.commit()
            n = con.execute("SELECT COUNT(*) FROM coverage_cache").fetchone()[0]
            rows = con.execute("SELECT rows FROM coverage_cache").fetchone()[0]
        self.assertEqual((n, rows), (1, 6))

    def test_signals_unique_key_includes_versions(self):
        migrate.migrate(self.db.path)
        with closing(sqlite3.connect(self.db.path)) as con:
            con.row_factory = sqlite3.Row
            uniq = migrate.signals_unique_columns(con)
        self.assertTrue(uniq, "signals 应有唯一索引")
        cols = set(uniq[0])
        self.assertIn("strategy_version", cols)
        self.assertIn("data_version", cols)

    def test_signals_unique_key_distinguishes_data_version(self):
        migrate.migrate(self.db.path)
        base = ("A", "v1", "cfg", "binance", "um", "1000PEPEUSDT", "4h", "confirmed",
                "long", T4H_RETEST, T4H_RETEST, None, 1, "{}", T4H_RETEST)
        with closing(sqlite3.connect(self.db.path)) as con:
            for dv in ("dv:a", "dv:b"):             # 同 confirmed_at、不同数据版本
                con.execute(
                    "INSERT INTO signals(strategy_key,strategy_version,config_hash,exchange,market,"
                    "symbol,timeframe,status,direction,event_time,confirmed_at,data_version,"
                    "source_id,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    base[:11] + (dv,) + base[12:])
            con.commit()
            n = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(n, 2)                      # 未被静默吞掉

    def test_dry_run_writes_nothing(self):
        migrate.migrate(self.db.path, dry_run=True)
        with closing(sqlite3.connect(self.db.path)) as con:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("strategy_configs", names)


class ConfigValidationTests(unittest.TestCase):
    """配置校验：类型/有限性/范围/窗口关系；非法 → 配置无效（不抛异常、不当 ok）。"""

    def test_quantile_above_range_is_invalid_not_indexerror(self):
        # 复现主助手离线场景：buy_ratio_quantile=2 曾抛 IndexError
        r = strategy_b.run(flat_bars(10), cfg_b(buy_ratio_quantile=2))
        self.assertEqual(r["status"], common.STATUS_INVALID)
        self.assertEqual(r["events"], [])
        self.assertTrue(any("buy_ratio_quantile" in e for e in r["diagnostics"]["config_errors"]))

    def test_quantile_negative_is_invalid(self):
        r = strategy_b.run(b_bars(), cfg_b(volume_quantile=-0.1))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_min_history_gt_lookback_is_invalid(self):
        r = strategy_b.run(b_bars(n_anom=6), cfg_b(min_history_bars=9, lookback_bars=5))
        self.assertEqual(r["status"], common.STATUS_INVALID)
        self.assertTrue(any("min_history_bars" in e for e in r["diagnostics"]["config_errors"]))

    def test_non_integer_window_is_invalid(self):
        r = strategy_a.run(a_bars(), cfg(range_lookback=3.5))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_negative_multiplier_is_invalid(self):
        r = strategy_a.run(a_bars(), cfg(breakout_volume_mult=-1.0))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_zero_multiplier_is_invalid(self):
        r = strategy_a.run(a_bars(), cfg(breakout_volume_mult=0))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_negative_tolerance_is_invalid(self):
        r = strategy_a.run(a_bars(), cfg(retest_tolerance_pct=-0.01))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_zero_persist_is_invalid(self):
        r = strategy_b.run(b_bars(), cfg_b(persist_bars=0))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_nan_is_invalid(self):
        r = strategy_a.run(a_bars(), cfg(breakout_volume_mult=float("nan")))
        self.assertEqual(r["status"], common.STATUS_INVALID)

    def test_none_value_is_pending_not_invalid(self):
        r = strategy_a.run(a_bars(), cfg(range_lookback=None))
        self.assertEqual(r["status"], common.STATUS_PENDING)
        self.assertIn("range_lookback", r["diagnostics"]["missing_config"])

    def test_valid_config_is_ok(self):
        self.assertEqual(strategy_a.run(a_bars(), cfg())["status"], common.STATUS_OK)
        self.assertEqual(strategy_b.run(b_bars(), cfg_b())["status"], common.STATUS_OK)

    def test_quantile_defensive_no_exception(self):
        self.assertIsNone(common.quantile([], 2))
        self.assertIsNone(common.quantile([1, 2, 3], None))
        self.assertIsNone(common.quantile([1, 2, 3], float("nan")))
        self.assertEqual(common.quantile([1, 2, 3], 2), 3)      # 越界截断到 1.0
        self.assertEqual(common.quantile([1, 2, 3], -1), 1)


class StrategyATests(unittest.TestCase):
    def test_pending_config_when_unset(self):
        r = strategy_a.run(a_bars(), None)
        self.assertEqual(r["status"], common.STATUS_PENDING)
        self.assertEqual(r["events"], [])
        self.assertIn("range_lookback", r["diagnostics"]["missing_config"])

    def test_insufficient_data(self):
        r = strategy_a.run(a_bars()[:4], cfg())
        self.assertEqual(r["status"], common.STATUS_INSUFFICIENT)
        self.assertEqual(r["events"], [])

    def test_confirm_time_is_retest_bar_not_breakout(self):
        bars = a_bars()
        r = strategy_a.run(bars, cfg())
        self.assertEqual(r["status"], common.STATUS_OK)
        self.assertEqual(len(r["events"]), 1)
        ev = r["events"][0]
        self.assertEqual(ev.confirmed_at, bars[5].close_time)          # 回踩 bar
        self.assertNotEqual(ev.confirmed_at, bars[4].close_time)       # 不回填突破 bar
        self.assertEqual(ev.event_time, bars[5].close_time)

    def test_retest_must_be_independent_later_bar(self):
        bars = [mk(0, 101, 102, 99, 101, 1000), mk(1, 102, 103, 100, 102, 1000),
                mk(2, 101, 103, 100, 101, 1000), mk(3, 100, 102, 99, 100, 1000),
                mk(4, 101, 104, 103.0, 104, 2000),      # 突破 bar 自身也触及上沿
                mk(5, 104, 104.5, 104.2, 104.4, 1000), mk(6, 104, 104.5, 104.2, 104.4, 1000)]
        r = strategy_a.run(bars, cfg())
        self.assertEqual(r["events"], [])

    def test_no_lookahead_future_bar_does_not_change_event(self):
        bars = a_bars()
        r1 = strategy_a.run(bars, cfg())
        mutated = list(bars)
        mutated[7] = mk(7, 999, 9999, 1, 9999, 9_000_000)   # 篡改最后一根
        r2 = strategy_a.run(mutated, cfg())
        self.assertEqual([e.confirmed_at for e in r1["events"]],
                         [e.confirmed_at for e in r2["events"]])
        self.assertEqual(len(r2["events"]), 1)


class StrategyAPrefixTests(unittest.TestCase):
    """逐前缀一致性：不允许“事后补出当时未能判定的确认”。"""

    def test_prefix_consistency_no_backfill(self):
        bars = a_bars()
        full = sorted(e.confirmed_at for e in strategy_a.run(bars, cfg())["events"])
        self.assertEqual(full, [bars[5].close_time])
        for k in range(len(bars) + 1):
            got = sorted(e.confirmed_at
                         for e in strategy_a.run(bars[:k], cfg())["events"])
            last = bars[k - 1].close_time if k else None
            want = sorted(t for t in full if last is not None and t <= last)
            self.assertEqual(got, want, f"A 前缀 k={k} 不一致：{got} != {want}")

    def test_event_is_visible_at_the_bar_that_confirms_it(self):
        bars = a_bars()
        # 只有 6 根（末根即回踩 bar）时就必须已能判定，不得等到第 7 根才补出
        r6 = strategy_a.run(bars[:6], cfg())
        self.assertEqual(r6["status"], common.STATUS_OK)
        self.assertEqual([e.confirmed_at for e in r6["events"]], [bars[5].close_time])
        # 只有 5 根（末根是突破 bar）时不得有事件
        self.assertEqual(strategy_a.run(bars[:5], cfg())["events"], [])


class StrategyBTests(unittest.TestCase):
    def test_pending_config_when_unset(self):
        r = strategy_b.run(b_bars(), None)
        self.assertEqual(r["status"], common.STATUS_PENDING)
        self.assertIn("persist_bars", r["diagnostics"]["missing_config"])

    def test_insufficient_data(self):
        r = strategy_b.run(b_bars()[:5], cfg_b())
        self.assertEqual(r["status"], common.STATUS_INSUFFICIENT)

    def test_dedupe_single_event_per_segment(self):
        bars = b_bars(n_anom=4)          # 连续 4 根异常，persist=3
        r = strategy_b.run(bars, cfg_b())
        self.assertEqual(r["status"], common.STATUS_OK)
        self.assertEqual(len(r["events"]), 1)                      # 同段只 1 次
        ev = r["events"][0]
        self.assertEqual(ev.confirmed_at, bars[7].close_time)      # 第 3 根异常收盘
        self.assertNotEqual(ev.confirmed_at, bars[5].close_time)   # 不回填首根
        self.assertEqual(ev.evidence["consecutive_bars"], 3)

    def test_baseline_uses_only_prior_bars(self):
        bars = b_bars(n_anom=4)
        r1 = strategy_b.run(bars, cfg_b())
        mutated = list(bars)
        mutated[-1] = mk(8, 100, 101, 99, 100, 100, vol=100, tbv=50, tbqv=50, interval=IV15)
        r2 = strategy_b.run(mutated, cfg_b())
        self.assertEqual([e.confirmed_at for e in r1["events"]],
                         [e.confirmed_at for e in r2["events"]])

    def test_buy_sell_diff_not_a_gate(self):
        r = strategy_b.run(b_bars(n_anom=4), cfg_b())
        cond = [c for c in r["events"][0].conditions if "买卖差" in c.name][0]
        self.assertIsNone(cond.satisfied)

    def test_flat_series_is_not_anomaly(self):
        # 常量成交量 + 恒定主动买入占比：无增强 → 不得判为异常
        r = strategy_b.run(flat_bars(10), cfg_b())
        self.assertEqual(r["status"], common.STATUS_OK)
        self.assertEqual(r["events"], [])

    def test_time_gap_breaks_segment(self):
        b = flat_bars(5)                                  # 5 根正常
        for k in (5, 6):                                  # 2 根异常（连续）
            b.append(mk(k, 100, 101, 99, 100, 2000, vol=2000, tbv=1800, tbqv=1800,
                        interval=IV15))
        for k in (10, 11):                                # 时间跳空后的 2 根异常
            b.append(mk(k, 100, 101, 99, 100, 2000, vol=2000, tbv=1800, tbqv=1800,
                        interval=IV15))
        r = strategy_b.run(b, cfg_b())                    # persist=3
        self.assertEqual(r["events"], [])                 # 跨缺口不构成“连续”

    def test_missing_taker_fields_break_segment(self):
        b = flat_bars(5)
        for k in (5, 6):
            b.append(mk(k, 100, 101, 99, 100, 2000, vol=2000, tbv=1800, tbqv=1800,
                        interval=IV15))
        b.append(mk_missing_taker(7, 2000))               # 主动买入字段缺失
        b.append(mk(8, 100, 101, 99, 100, 2000, vol=2000, tbv=1800, tbqv=1800, interval=IV15))
        r = strategy_b.run(b, cfg_b())
        self.assertEqual(r["status"], common.STATUS_OK)
        self.assertEqual(r["events"], [])                 # 缺失处断开，未凑满 3 根

    def test_prefix_consistency(self):
        bars = b_bars(n_normal=5, n_anom=5)
        full = sorted(e.confirmed_at for e in strategy_b.run(bars, cfg_b())["events"])
        for k in range(len(bars) + 1):
            got = sorted(e.confirmed_at
                         for e in strategy_b.run(bars[:k], cfg_b())["events"])
            last = bars[k - 1].close_time if k else None
            want = sorted(t for t in full if last is not None and t <= last)
            self.assertEqual(got, want, f"B 前缀 k={k} 不一致：{got} != {want}")


class _AdapterCase(unittest.TestCase):
    """按需把 adapter 指向临时库。"""

    @classmethod
    def _bind(cls, with_signals=False, invalid_b=False):
        cls.db = TempDb()
        migrate.migrate(cls.db.path)
        cls.path = cls.db.path
        if with_signals:
            cls.sid_4h = insert_signal(
                cls.db.path, timeframe="4h", confirmed_at=T4H_RETEST,
                evidence={"frozen_at": T4H_FROZEN, "range_upper": 103.0, "range_lower": 99.0,
                          "breakout_close_time": T4H_BREAKOUT, "retest_close_time": T4H_RETEST})
            cls.sid_1d = insert_signal(
                cls.db.path, timeframe="1d", confirmed_at=DAY_CLOSE,
                evidence={"frozen_at": DAY_OPEN, "range_upper": 0.0014, "range_lower": 0.0012,
                          "breakout_close_time": DAY_OPEN, "retest_close_time": DAY_CLOSE})
        if invalid_b:
            cfgj = {"lookback_bars": 5, "min_history_bars": 5, "buy_ratio_quantile": 2,
                    "volume_quantile": 0.5, "persist_bars": 3}
            with closing(sqlite3.connect(cls.db.path)) as con:
                con.execute("INSERT INTO strategy_configs(strategy_key,version,config_json,"
                            "config_hash,frozen,note,created_at) VALUES('B','t1',?,?,0,NULL,1)",
                            (json.dumps(cfgj), common.config_hash(cfgj)))
                con.commit()
        os.environ["MARKET_DB"] = cls.db.path
        os.environ["DATA_HUB_PROJECT"] = PROJECT
        # 工作台活动池来自标的池分类产物；测试用**空目录** ⇒ 分类缺失 ⇒ 降级为
        # 「K线覆盖 ∪ 实时快照 ∪ 数据总池」，保证夹具隔离（不读到真实 549 只）。
        cls._pooldir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._pooldir.cleanup)
        os.environ["MARKET_POOL_DIR"] = cls._pooldir.name
        # 夹具测试**不得触发真实外部采集**：关闭 1m 近期尾段的按需拉取
        os.environ["MARKET_TAIL_FETCH"] = "0"
        # 夹具隔离（2026-10-09 修复）：聪明钱快照目录必须指向**空临时目录**。
        # 真实快照目录（后端实现/快照/smart_money/）由采集器持续写入 500+ 个真实快照，
        # 而夹具标的 `1000PEPEUSDT` 恰在真实覆盖内 ⇒ 不隔离时
        # `ContractEmptyTests.test_smart_money_missing_not_faked`（断言「无快照 ⇒
        # available:false 且 reason 含『不』」）会读到真实数据而**假失败**。
        # 与 tests/test_smart_money.py 同法：`SNAP_DIR` 在导入期由 env 固化，
        # 改 env 无效，必须直接改写模块级 `SNAP_DIR`。
        _sm = importlib.import_module("smart_money")
        cls._smdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._smdir.cleanup)
        cls._sm_orig_dir = _sm.SNAP_DIR
        _sm.SNAP_DIR = Path(cls._smdir.name)
        cls.addClassCleanup(lambda: setattr(_sm, "SNAP_DIR", cls._sm_orig_dir))
        cls.adapter = importlib.reload(importlib.import_module("adapter"))

    @classmethod
    def tearDownClass(cls):
        cls.db.cleanup()


class WorkbenchScopeTests(_AdapterCase):
    """工作台活动池口径：币安 COIN ∪ 欧易补充；INDEX 另列；flat 实时字段为数值。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=False)
        pool = Path(cls._pooldir.name)
        (pool / "binance_classified.json").write_text(json.dumps({
            "coins": [{"symbol": "1000PEPEUSDT", "base_asset": "1000PEPE",
                       "quote_asset": "USDT", "contract_type": "PERPETUAL",
                       "underlying_type": "COIN"}],
            "crypto_index": [{"symbol": "BTCDOMUSDT", "base_asset": "BTCDOM",
                              "underlying_type": "INDEX", "contract_type": "PERPETUAL"}],
        }, ensure_ascii=False), encoding="utf-8")
        (pool / "cross_exchange_supplement.json").write_text(json.dumps({
            "okx_crypto_live_usdt_linear": 286,
            "supplement": [{"inst_id": "CRO-USDT-SWAP", "base": "CRO",
                            "naming_risk": "待人工确认：币安存在命名相近 base"}],
        }, ensure_ascii=False), encoding="utf-8")
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(cls.db.path)) as con:
            con.execute(
                "INSERT OR REPLACE INTO realtime_quotes(exchange,market,symbol,last_price,"
                "price_change_pct_24h,quote_volume_24h,price_event_time,price_received_at,"
                "price_source,price_transport,mark_price,mark_event_time,mark_source,updated_at)"
                " VALUES('binance','um','1000PEPEUSDT','0.0014066','0.0123','90235763.9',"
                "?,?,'binance.fapi','rest','0.0014',?,'binance.fapi',?)",
                (now, now, now, now))
            con.commit()
        cls.adapter = importlib.reload(importlib.import_module("adapter"))

    def test_active_scope_and_index_separate(self):
        m = self.adapter.v1_markets()
        self.assertEqual(m["total"], 2)                 # 币安 1 + 欧易补充 1
        self.assertEqual(m["active_scope"]["binance_coins"], 1)
        self.assertEqual(len(m["crypto_index"]), 1)     # INDEX 另列，不计入币池
        self.assertEqual(m["crypto_index"][0]["symbol"], "BTCDOMUSDT")
        self.assertTrue(m["active_scope"]["mapping_unverified"])   # 映射不明保留待核

    def test_flat_realtime_fields_are_numbers(self):
        it = next(x for x in self.adapter.v1_markets()["items"]
                  if x["market_id"] == "1000PEPEUSDT")
        self.assertIsInstance(it["last_price"], float)              # 非字符串
        self.assertAlmostEqual(it["last_price"], 0.0014066)
        self.assertIsInstance(it["price_change_ratio_24h"], float)
        self.assertIsInstance(it["mark_price"], float)
        self.assertEqual(it["realtime_status"], "ok")
        self.assertIn("binance.fapi", it["realtime_source"])
        self.assertIsNotNone(it["realtime_time"])


class OkxImportTests(unittest.TestCase):
    """欧易补币入库：sha256 取真实 rows 内容；raw 保留张数；缺失字段有说明。"""

    def setUp(self):
        self.db = TempDb()
        self.addCleanup(self.db.cleanup)
        migrate.migrate(self.db.path)
        self.raw = tempfile.TemporaryDirectory()
        self.addCleanup(self.raw.cleanup)
        os.environ["MARKET_DB"] = self.db.path
        os.environ["OKX_RAW_DIR"] = self.raw.name
        sys.path.insert(0, str(HERE / "历史下载"))
        self.mod = importlib.reload(importlib.import_module("okx_download"))

    def test_sha256_real_rows_and_raw_and_notes(self):
        # 欧易原始 9 元组：[ts,o,h,l,c,vol(张数),volCcy(基础币),volCcyQuote(计价),confirm]
        rows = [["1704067200000", "1", "2", "0.5", "1.5", "100", "10", "15", "1"],
                ["1704153600000", "1.5", "2.5", "1.0", "2.0", "200", "20", "40", "1"]]
        with closing(sqlite3.connect(self.db.path)) as con:
            n, sid = self.mod.import_rows(con, "CRO-USDT-SWAP", "1d", rows, "u", call_ids=[7, 8])
            con.commit()
            self.assertEqual(n, 2)
            got = con.execute("SELECT sha256,file_path,row_count FROM sources WHERE id=?",
                              (sid,)).fetchone()
            notes = {r[0] for r in con.execute(
                "SELECT field FROM candle_field_notes WHERE symbol='CRO-USDT-SWAP'")}
            vol = con.execute("SELECT volume,quote_volume,trade_count,taker_buy_volume "
                              "FROM candle_versions WHERE symbol='CRO-USDT-SWAP' "
                              "ORDER BY open_time LIMIT 1").fetchone()
        # sha256 = 真实原始 rows 内容拼接的哈希（不是边界/根数摘要）
        want = hashlib.sha256(
            "\n".join("|".join(str(x) for x in c) for c in rows).encode()).hexdigest()
        self.assertEqual(got[0], want)
        self.assertEqual(got[2], 2)
        # raw 附表存在且保留 c[5] 张数与 api_call 关联
        raw = Path(got[1])
        self.assertTrue(raw.exists())
        obj = json.loads(raw.read_text(encoding="utf-8"))
        self.assertEqual(obj["rows_raw"][0][5], "100")          # 张数保留
        self.assertEqual(obj["api_call_ids"], [7, 8])
        self.assertEqual(obj["sha256_rows"], want)
        # 落库：volume=volCcy(基础币)、quote=volCcyQuote(计价)、taker 空串、trade_count 占位
        self.assertEqual(vol[0], "10")
        self.assertEqual(vol[1], "15")
        self.assertEqual(vol[2], 0)
        self.assertEqual(vol[3], "")
        for f in ("taker_buy_volume", "taker_buy_quote_volume", "trade_count", "volume"):
            self.assertIn(f, notes)

    def test_sha256_changes_with_content(self):
        r1 = [["1704067200000", "1", "2", "0.5", "1.5", "100", "10", "15", "1"]]
        r2 = [["1704067200000", "1", "2", "0.5", "1.6", "100", "10", "15", "1"]]   # 仅收盘价不同
        with closing(sqlite3.connect(self.db.path)) as con:
            _n1, s1 = self.mod.import_rows(con, "A-USDT-SWAP", "1d", r1, "u")
            _n2, s2 = self.mod.import_rows(con, "B-USDT-SWAP", "1d", r2, "u")
            con.commit()
            h1 = con.execute("SELECT sha256 FROM sources WHERE id=?", (s1,)).fetchone()[0]
            h2 = con.execute("SELECT sha256 FROM sources WHERE id=?", (s2,)).fetchone()[0]
        self.assertNotEqual(h1, h2)      # 内容不同 ⇒ 哈希不同（旧实现只哈希边界/根数会相同）


class RealtimeHealthTests(_AdapterCase):
    """realtime_health：**新鲜价格时点 且 有效心跳** 才 ok；缺一即 stale。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=False)

    def _set(self, px_age_ms=None, hb_age_ms=None):
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("DELETE FROM realtime_quotes")
            con.execute("DELETE FROM collector_state")
            if px_age_ms is not None:
                con.execute(
                    "INSERT INTO realtime_quotes(exchange,market,symbol,last_price,"
                    "price_event_time,price_received_at,price_source,price_transport,updated_at)"
                    " VALUES('binance','um','1000PEPEUSDT','1',?,?,'binance.fstream','ws',?)",
                    (now - px_age_ms, now - px_age_ms, now))
            if hb_age_ms is not None:
                con.execute("INSERT INTO collector_state(name,pid,started_at,heartbeat_at,status,"
                            "updated_at) VALUES('realtime',1,?,?,'running',?)",
                            (now, now - hb_age_ms, now))
            con.commit()

    def test_ok_only_when_both_fresh(self):
        self._set(px_age_ms=5_000, hb_age_ms=5_000)
        rh = self.adapter.realtime_health()
        self.assertEqual(rh["status"], "ok")
        self.assertTrue(rh["price_fresh"])
        self.assertTrue(rh["heartbeat_valid"])

    def test_stale_when_price_old_but_heartbeat_alive(self):
        """无价格消息但心跳活跃 → 仍 stale（不能仅凭心跳判 ok）。"""
        self._set(px_age_ms=700_000, hb_age_ms=5_000)
        rh = self.adapter.realtime_health()
        self.assertEqual(rh["status"], "stale")
        self.assertFalse(rh["price_fresh"])
        self.assertTrue(rh["heartbeat_valid"])

    def test_stale_when_heartbeat_missing(self):
        """有新鲜价格但缺心跳 → 不 ok。"""
        self._set(px_age_ms=5_000, hb_age_ms=None)
        rh = self.adapter.realtime_health()
        self.assertEqual(rh["status"], "stale")
        self.assertTrue(rh["price_fresh"])
        self.assertFalse(rh["heartbeat_valid"])

    def test_stale_when_heartbeat_expired(self):
        self._set(px_age_ms=5_000, hb_age_ms=300_000)
        self.assertEqual(self.adapter.realtime_health()["status"], "stale")

    def test_not_connected_when_nothing(self):
        self._set(px_age_ms=None, hb_age_ms=None)
        self.assertEqual(self.adapter.realtime_health()["status"], "not_connected")


class ContractEmptyTests(_AdapterCase):
    """只读契约（无信号）：缺失返回 null/显式状态；聪明钱不得伪造。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=False)

    def test_health_and_markets_real_only(self):
        h = self.adapter.v1_health()
        self.assertTrue(any(i["name"] == "统一行情数据库" for i in h["items"]))
        m = self.adapter.v1_markets()
        self.assertEqual(m["total"], 1)
        self.assertEqual(m["items"][0]["market_id"], "1000PEPEUSDT")

    def test_smart_money_missing_not_faked(self):
        it = self.adapter.v1_markets()["items"][0]
        self.assertFalse(it["smart_money"]["available"])
        self.assertIn("不", it["smart_money"]["reason"])

    def test_one_minute_empty_not_fabricated(self):
        r = self.adapter.v1_candles("binance", "1000PEPEUSDT", {"timeframe": ["1m"], "limit": ["5"]})
        self.assertEqual(r["bars"], [])
        self.assertEqual(r["quality"]["status"], "missing")
        self.assertEqual(r["evidence_level"], "缺失")

    def test_forming_bar_field_separate_from_closed_bars(self):
        """`forming_bar` 独立可空字段；夹具下不触发外呼，故为 null，且 bars 恒为已收盘。"""
        r = self.adapter.v1_candles("binance", "1000PEPEUSDT", {"timeframe": ["1m"], "limit": ["5"]})
        self.assertIn("forming_bar", r)
        self.assertIsNone(r["forming_bar"])
        for b in r["bars"]:
            self.assertTrue(b["is_closed"])
        # 1d 也带该字段（可空），保持契约一致
        d = self.adapter.v1_candles("binance", "1000PEPEUSDT", {"timeframe": ["1d"], "limit": ["2"]})
        self.assertIn("forming_bar", d)
        self.assertTrue(all(b["is_closed"] for b in d["bars"]))

    def test_candles_annotations_null_when_no_signals(self):
        r = self.adapter.v1_candles("binance", "1000PEPEUSDT", {"timeframe": ["1d"], "limit": ["5"]})
        self.assertIsNone(r["annotations"])
        self.assertIn("annotations", r["quality"]["note"])

    def test_oi_and_funding_missing(self):
        self.assertEqual(self.adapter.v1_oi("binance", "1000PEPEUSDT", {})["quality"]["status"],
                         "missing")
        self.assertEqual(self.adapter.v1_funding("binance", "1000PEPEUSDT", {})["quality"]["status"],
                         "missing")

    def test_strategies_pending_config_and_no_third(self):
        """2026-10-11 契约更新：注册表 = 当前启用族 F_WX(3a–3d) + 历史 A/B。

        旧断言只认 ['A','B']。现按派工 06 第 2 条：正式注册接口只展示当前 WX 四组，
        旧 A/B 单列 legacy、enabled_for_current_run=false。
        """
        s = self.adapter.v1_strategies()
        keys = sorted(i["strategy_key"] for i in s["items"])
        self.assertEqual(keys, ["3a", "3b", "3c", "3d", "A", "B"])
        self.assertEqual(s["current_run_keys"], ["3a", "3b", "3c", "3d"])
        self.assertEqual(s["legacy_keys"], ["A", "B"])
        cur = {i["strategy_key"]: i for i in s["current"]}
        self.assertEqual(sorted(cur), ["3a", "3b", "3c", "3d"])
        for it in s["current"]:
            self.assertTrue(it["enabled_for_current_run"])
            self.assertEqual(it["implementation_status"], "implemented")
            self.assertEqual(it["run_status"], "pending")          # run_frozen=false
            self.assertEqual(it["config_status"], "已配置")          # 由生效视图绑定
        for it in s["legacy"]:
            self.assertFalse(it["enabled_for_current_run"])
            self.assertEqual(it["implementation_status"], "legacy_readable_only")
            self.assertEqual(it["config_status"], "待配置")
            self.assertFalse(it["frozen"])

    def test_strategy_singular_backward_compat(self):
        """旧单数端点保持兼容，但顶层代表**当前族首组**（3a），不再代表旧策略 A。"""
        r = self.adapter.v1_strategy()
        self.assertEqual(r["strategy_key"], "3a")
        self.assertIn("唐奇安", r["name"])
        self.assertEqual(r["status"], "research_candidate")
        self.assertEqual(sorted(x["strategy_key"] for x in r["registry"]),
                         ["3a", "3b", "3c", "3d", "A", "B"])
        self.assertEqual(r["current_run_keys"], ["3a", "3b", "3c", "3d"])

    def test_signals_empty_ok_not_fake_success(self):
        r = self.adapter.v1_signals({})
        self.assertEqual(r["items"], [])
        self.assertEqual(r["quality"]["status"], "ok")
        self.assertEqual(r["total"], 0)

    def test_unknown_market_is_missing_not_zero(self):
        r = self.adapter.v1_market("binance", "NOPE")
        self.assertEqual(r["quality"]["status"], "missing")

    def test_rule_snapshot_no_signal_returns_missing(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["retest"],
                                           "marker_time": [iso(T4H_RETEST)]})
        self.assertEqual(r["quality"]["status"], "missing")


class ContractSignalTests(_AdapterCase):
    """只读契约（有信号）：结构对齐 types.ts；规则快照精确匹配。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=True)

    def test_signals_list_is_signal_shape(self):
        """2026-10-11 契约更新：signal brief 增加事件身份与可执行门禁字段（SIG03/SIG04/UI05）。

        新增字段：confirmed_at/event_time/strategy_key/strategy_version/config_hash/
        data_version/source_id/source_masks/execution_label/actionable/actionable_reason/
        current_direction。断言改为**必含**这些键（允许后续再加，不锁死全集）。
        """
        r = self.adapter.v1_signals({})
        self.assertEqual(r["total"], 2)
        required = {"signal_id", "exchange", "market_id", "symbol", "status",
                    "direction", "strength", "created_at", "expires_at", "timeframe",
                    "confirmed_at", "event_time", "strategy_key", "strategy_version",
                    "config_hash", "data_version", "source_id", "source_masks",
                    "execution_label", "actionable", "actionable_reason", "current_direction"}
        for it in r["items"]:
            self.assertTrue(required.issubset(set(it.keys())),
                            f"缺少契约字段：{sorted(required - set(it.keys()))}")
            self.assertIn(it["status"], {"candidate", "waiting_retest", "confirmed",
                                         "invalidated", "expired", "undecided"})
            # 历史事实方向与当前可执行方向分开：actionable=false ⇒ current_direction=null
            if not it["actionable"]:
                self.assertIsNone(it["current_direction"])
                self.assertIsNotNone(it["actionable_reason"])

    def test_signal_detail_shape(self):
        r = self.adapter.v1_signal(str(self.sid_4h))
        self.assertEqual(r["signal"]["signal_id"], str(self.sid_4h))
        self.assertEqual(set(r["conditions"][0].keys()), {"name", "satisfied", "detail"})
        self.assertEqual(set(r["frozen_range"].keys()),
                         {"start_time", "end_time", "upper", "lower", "bar_count", "note"})
        self.assertIsNone(r["risk"])
        self.assertIsNone(r["invalidation"])
        self.assertEqual(r["orders"], [])

    def test_rule_snapshot_exact_match_retest(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["retest"],
                                           "marker_time": [iso(T4H_RETEST)]})
        self.assertEqual(r["quality"]["status"], "ok")
        self.assertEqual(r["marker_kind"], "retest")
        self.assertEqual(r["signal_id"], str(self.sid_4h))

    def test_rule_snapshot_exact_match_breakout_and_frozen(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["breakout"],
                                           "marker_time": [iso(T4H_BREAKOUT)]})
        self.assertEqual(r["quality"]["status"], "ok")
        self.assertEqual(r["marker_kind"], "breakout")
        r2 = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                           {"marker_kind": ["frozen_range"],
                                            "marker_time": [iso(T4H_FROZEN)]})
        self.assertEqual(r2["quality"]["status"], "ok")

    def test_rule_snapshot_no_fallback_to_latest(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["retest"],
                                           "marker_time": [iso(1704000000000)]})
        self.assertEqual(r["quality"]["status"], "missing")
        self.assertIsNone(r.get("signal_id"))

    def test_rule_snapshot_b_not_labeled_retest(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["retest"],
                                           "marker_time": [iso(T4H_RETEST)],
                                           "strategy": ["B"]})
        self.assertEqual(r["quality"]["status"], "missing")
        self.assertIsNone(r.get("signal_id"))

    def test_rule_snapshot_requires_marker_time(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["retest"]})
        self.assertEqual(r["quality"]["status"], "undecided")

    def test_rule_snapshot_unsupported_kind(self):
        r = self.adapter.v1_rule_snapshot("binance", "1000PEPEUSDT",
                                          {"marker_kind": ["stop"],
                                           "marker_time": [iso(T4H_RETEST)]})
        self.assertEqual(r["quality"]["status"], "not_connected")

    def test_annotations_wired_from_real_signals(self):
        r = self.adapter.v1_candles("binance", "1000PEPEUSDT",
                                    {"timeframe": ["1d"], "limit": ["5"]})
        self.assertEqual(r["quality"]["status"], "ok")
        self.assertIsNotNone(r["annotations"])
        self.assertEqual(len(r["annotations"]["frozen_ranges"]), 1)
        self.assertEqual(len(r["annotations"]["breakouts"]), 1)
        self.assertEqual(len(r["annotations"]["retests"]), 1)


class MarketDirectionTests(_AdapterCase):
    """`/api/v1/markets` 的方向字段：**只来自当前启用族 F_WX 的真实信号**；
    无信号 / 非当前族 / 非当前版本配置 / 非 confirmed ⇒ null，不造方向。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=True)
        # 当前启用族（F_WX 3a）的**当前版本/当前配置**已确认信号 → 才可作为当前方向。
        cls.sid_wx = insert_wx_signal(cls.db.path, arm="3a", confirmed_at=T4H_RETEST + 10_000)

    def test_direction_mapped_from_latest_real_signal(self):
        m = self.adapter.v1_market("binance", "1000PEPEUSDT")   # 端到端：走 /markets/{ex}/{mid}
        self.assertEqual(m["signal_direction"], "long")
        self.assertEqual(m["signal_status"], "confirmed")
        ref = m["signal_ref"]
        # 方向来源 = 当前启用族 3a（历史族 A/B 的信号不参与当前方向）
        self.assertEqual(ref["strategy_key"], "3a")
        self.assertEqual(ref["timeframe"], "1h")
        self.assertEqual(ref["direction"], "long")
        self.assertTrue(str(ref["confirmed_at"]).endswith("Z"))
        self.assertTrue(str(ref["event_time"]).endswith("Z"))
        # 运行未放行 ⇒ 不作「当前可执行」提示，但历史事实可读
        self.assertFalse(m["signal_actionable"])
        self.assertIsNone(m["signal_current_direction"])

    def test_no_signal_means_null_direction(self):
        """有真实历史数据但**没有信号**的标的 → 方向必须为 null，不得用派生量冒充。"""
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("INSERT INTO candle_versions VALUES('binance','um','BTCUSDT','1d',"
                        "1704067200000,1704153599999,'100','110','90','105','1000','105000',"
                        "10,'500','52500',1,NULL,1)")
            con.execute("INSERT INTO candle_heads VALUES('binance','um','BTCUSDT','1d',"
                        "1704067200000,1)")
            con.commit()
        r = self.adapter.v1_markets()
        row = next(i for i in r["items"] if i["symbol"] == "BTCUSDT")
        self.assertIsNone(row["signal_direction"])
        self.assertIsNone(row["signal_status"])
        self.assertIsNone(row["signal_ref"])

    def test_markets_endpoint_carries_direction(self):
        r = self.adapter.v1_markets()
        row = next(i for i in r["items"] if i["symbol"] == "1000PEPEUSDT")
        self.assertEqual(row["signal_direction"], "long")
        self.assertEqual(row["signal_status"], "confirmed")
        self.assertEqual(row["signal_ref"]["strategy_key"], "3a")
        # 同一响应里没有信号的标的必须是 null，不能被邻行「带上」方向
        others = [i for i in r["items"] if i["symbol"] != "1000PEPEUSDT"]
        self.assertTrue(all(i["signal_direction"] is None for i in others))
        self.assertTrue(all(i["signal_status"] is None for i in others))


class MarketDirectionGuardTests(_AdapterCase):
    """方向只认契约枚举 + **当前版本/配置/状态**；脏值不得透传，非当前不冒充当前。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=True)
        cls.sid_wx = insert_wx_signal(cls.db.path, arm="3a", confirmed_at=T4H_RETEST + 10_000)

    def test_direction_guards_and_latest_wins(self):
        # ① 更晚的**历史族 A** 脏值信号：不参与当前方向（当前族过滤）⇒ 仍取 3a
        insert_signal(self.db.path, strategy_key="A", timeframe="4h",
                      confirmed_at=T4H_RETEST + 60_000, direction="bullish", status="weird",
                      evidence={})
        m = self.adapter.v1_market("binance", "1000PEPEUSDT")
        self.assertEqual(m["signal_direction"], "long")
        self.assertEqual(m["signal_ref"]["strategy_key"], "3a")
        # ② 当前族但**非当前版本**（更晚）：不冒充当前 ⇒ 仍取当前版本的 3a
        insert_wx_signal(self.db.path, arm="3b", confirmed_at=T4H_RETEST + 120_000,
                         version="F_WX_other_version")
        m1 = self.adapter.v1_market("binance", "1000PEPEUSDT")
        self.assertEqual(m1["signal_direction"], "long")
        self.assertEqual(m1["signal_ref"]["strategy_key"], "3a")
        # ③ 当前族 + 当前版本 + 当前配置，但**脏值方向/状态** ⇒ 方向/状态回 null
        insert_wx_signal(self.db.path, arm="3c", confirmed_at=T4H_RETEST + 180_000,
                         direction="bullish", status="weird")
        m2 = self.adapter.v1_market("binance", "1000PEPEUSDT")
        self.assertIsNone(m2["signal_direction"])
        self.assertIsNone(m2["signal_status"])
        # ④ 当前族非 confirmed（失效）：状态可读，但**方向回 null**（按状态过滤方向）
        insert_wx_signal(self.db.path, arm="3c", confirmed_at=T4H_RETEST + 240_000,
                         direction="long", status="invalidated")
        m3 = self.adapter.v1_market("binance", "1000PEPEUSDT")
        self.assertIsNone(m3["signal_direction"])
        self.assertEqual(m3["signal_status"], "invalidated")
        # ⑤ 再插更晚的当前族已确认合法信号 ⇒ 取最近确认的那条
        insert_wx_signal(self.db.path, arm="3d", confirmed_at=T4H_RETEST + 300_000,
                         direction="long", status="confirmed")
        m4 = self.adapter.v1_market("binance", "1000PEPEUSDT")
        self.assertEqual(m4["signal_direction"], "long")
        self.assertEqual(m4["signal_status"], "confirmed")
        self.assertEqual(m4["signal_ref"]["strategy_key"], "3d")
        self.assertEqual(m4["signal_ref"]["timeframe"], "1h")


class RegistryInvalidConfigTests(_AdapterCase):
    """注册表：非法配置 → 配置无效。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(invalid_b=True)

    def test_invalid_config_reported(self):
        r = self.adapter.v1_strategy_one("B")
        self.assertEqual(r["strategy"]["config_status"], "配置无效")
        self.assertTrue(r["strategy"]["config_errors"])

    def test_other_strategy_still_pending(self):
        r = self.adapter.v1_strategy_one("A")
        self.assertEqual(r["strategy"]["config_status"], "待配置")


class RunnerCliTests(unittest.TestCase):
    """离线运行器 CLI：未冻结须显式试运行、拒绝截断、数据版本绑定实际消费来源。"""

    RUNNER = str(HERE / "strategy_runner.py")

    def _db(self, *, frozen, persist=3):
        db = TempDb()
        self.addCleanup(db.cleanup)
        migrate.migrate(db.path)
        add_series(db.path, "binance", "um", "1000PEPEUSDT", "15m",
                   b_bars(n_normal=5, n_anom=5))
        cfgj = {"lookback_bars": 5, "min_history_bars": 5, "buy_ratio_quantile": 0.8,
                "volume_quantile": 0.5, "persist_bars": persist}
        with closing(sqlite3.connect(db.path)) as con:
            con.execute("INSERT INTO strategy_configs(strategy_key,version,config_json,"
                        "config_hash,frozen,note,created_at) VALUES('B','t1',?,?,?,NULL,1)",
                        (json.dumps(cfgj), common.config_hash(cfgj), 1 if frozen else 0))
            con.commit()
        return db.path

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, self.RUNNER, *args], capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_unfrozen_requires_research_trial(self):
        db = self._db(frozen=False)
        r = self._run("--db", db, "--strategy", "B")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未冻结", r.stderr)

    def test_research_trial_runs_readonly(self):
        db = self._db(frozen=False)
        r = self._run("--db", db, "--strategy", "B", "--research-trial")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["mode"], "research_trial")
        self.assertEqual(out["bars_used"], 10)
        self.assertEqual(out["status"], common.STATUS_OK)
        self.assertNotIn("write", out)

    def test_write_refused_when_unfrozen(self):
        db = self._db(frozen=False)
        r = self._run("--db", db, "--strategy", "B", "--write")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("拒绝写入", r.stderr)

    def test_write_ok_when_frozen(self):
        db = self._db(frozen=True)
        r = self._run("--db", db, "--strategy", "B", "--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["mode"], "confirmed")
        self.assertEqual(out["write"]["written"], 1)
        self.assertEqual(out["write"]["skipped"], 0)
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)

    def test_write_twice_reports_skipped_not_silent(self):
        db = self._db(frozen=True)
        self._run("--db", db, "--strategy", "B", "--write")
        r = self._run("--db", db, "--strategy", "B", "--write")
        out = json.loads(r.stdout)
        self.assertEqual(out["write"]["written"], 0)
        self.assertEqual(out["write"]["skipped"], 1)      # 显式报告，不静默吞掉

    def test_max_bars_refuses_truncation(self):
        db = self._db(frozen=False)
        r = self._run("--db", db, "--strategy", "B", "--research-trial", "--max-bars", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("拒绝静默截断", r.stderr)

    def test_data_version_covers_consumed_sources(self):
        db = self._db(frozen=False)
        r = self._run("--db", db, "--strategy", "B", "--research-trial")
        out = json.loads(r.stdout)
        self.assertTrue(str(out["data_version"]).startswith("dv:"))
        self.assertEqual([s["source_id"] for s in out["data_sources"]], [1])
        self.assertFalse(out["data_sources_unknown"])
        self.assertEqual(out["events"][0]["evidence"]["data_sources"][0]["source_id"], 1)

    def test_invalid_config_reported_by_cli(self):
        db = self._db(frozen=True, persist=0)
        r = self._run("--db", db, "--strategy", "B", "--write")
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], common.STATUS_INVALID)
        self.assertTrue(out["config_errors"])


class PoolAndCoverageTests(_AdapterCase):
    """数据总池 / 库存摘要缓存：真来源才判，池 ≠ 交易资格。"""

    def setUp(self):
        # 每个用例独立临时库：池是否为空必须从零开始，避免用例间互相污染
        db = TempDb()
        self.addCleanup(db.cleanup)
        migrate.migrate(db.path)
        self.db = db
        os.environ["MARKET_DB"] = db.path
        os.environ["DATA_HUB_PROJECT"] = PROJECT
        self.adapter = importlib.reload(importlib.import_module("adapter"))

    @classmethod
    def tearDownClass(cls):
        pass

    def test_pool_missing_when_not_synced(self):
        r = self.adapter.v1_pool()
        self.assertEqual(r["quality"]["status"], "missing")
        self.assertIn("symbol_pool", r["missing_reason"])

    def test_coverage_cache_preferred_over_full_scan(self):
        """缓存非空时 covered() 必须读缓存（证明未做全表 GROUP BY）。"""
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("INSERT INTO coverage_cache VALUES('binance','um','1000PEPEUSDT','1d',"
                        "999,1704067200000,1704153599999,999,0,7,1)")
            con.commit()
        cov = self.adapter.covered()
        self.assertEqual(len(cov), 1)
        self.assertEqual(cov[0]["rows"], 999)
        self.assertEqual(cov[0]["missing_within_observed_range"], 0)
        self.assertEqual(self.adapter.v1_markets()["total"], 1)

    def test_markets_separate_pool_from_eligibility(self):
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute(
                "INSERT INTO symbol_pool(exchange,market,symbol,contract_type,quote_asset,"
                "margin_asset,status,onboard_time,base_asset,spot_symbol,spot_status,"
                "spot_match_method,priority_hint,snapshot_utc,source_call_id,updated_at) "
                "VALUES('binance','um','1000PEPEUSDT','PERPETUAL','USDT','USDT','TRADING',"
                "1683244800000,'1000PEPE','PEPEUSDT','TRADING','multiplier_stripped',"
                "'现货TRADING（优先）',1683244800000,NULL,1)")
            con.commit()
        it = self.adapter.v1_markets()["items"][0]
        self.assertEqual(it["contract_type"], "PERPETUAL")
        self.assertTrue(it["is_usdt_perp"])
        self.assertEqual(it["spot_status"], "listed")   # 契约值：TRADING → listed
        self.assertIsNone(it["eligibility"])              # 资格未冻结 → 不下结论
        self.assertIn("未定", it["eligibility_reason"])

    def test_market_item_maps_stored_close_gaps_and_spot_status(self):
        """折叠行摘要 / 数据时点 / 缺口 / 现货状态必须来自真实已存数据。

        同时守住三条禁止：历史 close 不得冒充实时价、日线 quote_volume 不得冒充
        24h 或 5m 成交量、无原生短周期的字段必须留空。
        """
        from bulk_import import import_zip_streaming
        iv = 86400000
        base = DAY_OPEN
        # 真实缺口：只放第 1、3 根（中间第 2 根不写入）
        rows = []
        for k in (0, 2):
            ot = base + k * iv
            close = 100.0 + k
            rows.append([ot, str(close - 0.5), str(close + 1.5), str(close - 1.0),
                         str(close), "10", ot + iv - 1, str(1000 + k), 5, "6", "600", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            w = csv.writer(buf)
            for r in rows:
                w.writerow(r)
            zpath = Path(tmp) / "TESTSYM-1d-2024-01.zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("TESTSYM-1d-2024-01.csv", buf.getvalue().encode())
            digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
            cpath = Path(str(zpath) + ".CHECKSUM")
            cpath.write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
            import_zip_streaming(MarketStore(self.db.path), zpath, cpath, "TESTSYM", "1d",
                                 "https://data.binance.vision/example.zip")
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute(
                "INSERT INTO symbol_pool(exchange,market,symbol,contract_type,quote_asset,"
                "margin_asset,status,onboard_time,base_asset,spot_symbol,spot_status,"
                "spot_match_method,priority_hint,snapshot_utc,source_call_id,updated_at) "
                "VALUES('binance','um','TESTSYM','PERPETUAL','USDT','USDT','TRADING',"
                "1683244800000,'TEST','TESTSYM','TRADING','exact','现货TRADING（优先）',"
                "1683244800000,NULL,1)")
            # 已存首末范围内应有 3 根，实际 2 根 → 缺 1 根（真实计数，来自下载器维护的缓存）
            con.execute("INSERT INTO coverage_cache VALUES('binance','um','TESTSYM','1d',"
                        "2,?,?,3,1,1,1)", (base, base + 2 * iv + iv - 1))
            con.commit()
        it = self.adapter.v1_markets()["items"][0]
        s = it["summary"]
        self.assertEqual(s["timeframe"], "1d")                 # 明示周期，不暗示实时
        self.assertEqual(s["latest_close"], 102.0)             # 最后已存收盘（历史归档）
        self.assertEqual(s["change_ratio"], 0.02)              # 相邻两根收盘变化比例（后端算）
        self.assertEqual(s["quote_volume"], 1002.0)            # **该 bar 的**成交额
        self.assertEqual(s["event_time"], self.adapter.iso(base + 2 * iv + iv - 1))
        self.assertEqual(it["data_time"], s["event_time"])
        self.assertIn("缺 1 根", it["data_missing_reason"])
        # 禁止冒充：无真实滚动口径 / 无原生短周期 → 一律留空
        self.assertIsNone(it["volume_24h_quote"])
        # `volume_by_timeframe`（2026-10-11 接通）：**只取该周期最近一根已收盘 bar** 的
        # `quote_volume`。本夹具的 1d 归档确有已收盘 bar（open_time = last_close - 1d + 1），
        # ⇒ 必须给出**真实值**（不是留空、不是别的周期顶替、不是 0），且
        # close/close_time 与 `/markets` 的 summary 同源同一根（102.0 / base+3*iv-1）。
        self.assertEqual(it["volume_by_timeframe"], {"1d": {
            "quote_volume": 1002.0, "base_volume": 10.0, "close": 102.0,
            "close_time": self.adapter.iso(base + 3 * iv - 1)}})
        # 市值：本夹具无 `symbol_supply`、无实时价 ⇒ 必须留空（不得用别的币顶替）。
        self.assertIsNone(it["market_cap_usd"])
        self.assertIsNone(it["market_cap_note"])
        self.assertIsNone(it["circulating_supply"])
        self.assertIsNone(it["open_interest_total"])
        self.assertIsNone(it["funding_rate"])
        self.assertEqual(it["spot_status"], "listed")
        self.assertEqual(it["available_timeframes"], ["1d"])
        cell = it["coverage"][0]
        self.assertEqual(cell["missing_within_observed_range"], 1)
        self.assertEqual(cell["expected"], 3)
        self.assertEqual(cell["gaps"], [])                     # 区间未入库 → 不猜区间
        self.assertIn("仅计数", cell["gaps_note"])
        self.assertIsNotNone(cell["source"]["source_url"])

    def test_spot_status_unknown_is_not_not_listed(self):
        """现货快照未命中 = 未知，不得判为未挂牌。"""
        self.assertEqual(self.adapter.spot_status_of(None), "unknown")
        self.assertEqual(self.adapter.spot_status_of({}), "unknown")
        self.assertEqual(self.adapter.spot_status_of({"spot_status": None}), "unknown")
        self.assertEqual(self.adapter.spot_status_of({"spot_status": "TRADING"}), "listed")
        self.assertEqual(self.adapter.spot_status_of({"spot_status": "BREAK"}), "not_listed")

    def test_market_summary_does_not_depend_on_incidental_market_key(self):
        """市场标识必须显式取值：cell.market → 传入映射 → 都没有则返回空摘要，不抛 KeyError。"""
        ad = self.adapter
        # ① cell 自带 market（来自 covered() 原始记录）→ 无该标的K线，返回空摘要但不抛异常
        s, c = ad.market_summary("binance", "NOSUCHSYM", [{"timeframe": "1d", "market": "um"}])
        self.assertIsNone(s)
        self.assertEqual(c["timeframe"], "1d")
        # ② cell 无 market，调用方给明确映射（symbol_pool.market）
        s, _ = ad.market_summary("binance", "NOSUCHSYM", [{"timeframe": "1d"}], market="um")
        self.assertIsNone(s)
        # ③ 两者都取不到 → 不猜、不抛
        s, c = ad.market_summary("binance", "NOSUCHSYM", [{"timeframe": "1d"}])
        self.assertIsNone(s)
        self.assertEqual(c["timeframe"], "1d")
        # ④ 无 cell
        self.assertEqual(ad.market_summary("binance", "X", []), (None, None))

    def test_data_missing_reason_reports_zero_and_stays_empty_when_unknown(self):
        """计数已知（含 0）要写明根数；计数未知必须留空，不猜、不断言成因。"""
        ad = self.adapter
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("INSERT INTO coverage_cache VALUES('binance','um','ZEROSYM','1d',"
                        "5,1,5,5,0,1,1)")                     # 缺 0 根
            con.execute("INSERT INTO coverage_cache VALUES('binance','um','UNKSYM','1d',"
                        "5,1,5,NULL,NULL,1,1)")               # 计数未知
            con.commit()
        zero = ad.v1_market("binance", "ZEROSYM")
        self.assertEqual(zero["data_missing_reason"], "1d 该已存范围缺 0 根")
        self.assertEqual(zero["coverage"][0]["missing_within_observed_range"], 0)
        unk = ad.v1_market("binance", "UNKSYM")
        self.assertIsNone(unk["data_missing_reason"])          # 未知保持空
        self.assertIsNone(unk["coverage"][0]["missing_within_observed_range"])
        # 不得仅凭计数断言成因
        self.assertNotIn("非本地丢弃", unk["coverage"][0]["gaps_note"])

    def test_health_counts_only_successful_public_rest_calls(self):
        """公共 REST 只按**成功**请求留证；失败请求单独计数，不得判为已连通。"""
        ad = self.adapter
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("INSERT INTO api_calls(id,request_key,endpoint,scope,params_json,"
                        "started_at,status,http_status) VALUES(1,'k1','binance.instruments',"
                        "'fapi.binance.com','{}',1,'成功',200)")
            con.execute("INSERT INTO api_calls(id,request_key,endpoint,scope,params_json,"
                        "started_at,status,http_status) VALUES(2,'k2','okx.instruments',"
                        "'www.okx.com','{}',2,'失败',NULL)")
            con.commit()
        st = ad.api_stats()
        self.assertEqual((st["ok_calls"], st["failed_calls"]), (1, 1))
        item = next(i for i in ad.health_items() if "REST" in i["name"])
        self.assertEqual(item["status"], "ok")
        self.assertIn("成功公共 REST 请求 1 次", item["detail"])
        self.assertIn("失败 1 次（失败不计作连通）", item["detail"])
        # 归档下载必须与 REST 网关分开陈述
        arch = next(i for i in ad.health_items() if "归档" in i["name"])
        self.assertIn("不经 REST 网关", arch["detail"])
        self.assertIn("不计入 api_calls", arch["detail"])
        # 只剩失败请求时，绝不能判为 ok
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute("DELETE FROM api_calls WHERE status='成功'")
            con.commit()
        item2 = next(i for i in ad.health_items() if "REST" in i["name"])
        self.assertNotEqual(item2["status"], "ok")
        self.assertEqual(item2["status"], "missing")

    def test_pool_endpoint_lists_snapshot_without_eligibility(self):
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute(
                "INSERT INTO symbol_pool(exchange,market,symbol,contract_type,quote_asset,"
                "margin_asset,status,onboard_time,base_asset,spot_symbol,spot_status,"
                "spot_match_method,priority_hint,snapshot_utc,source_call_id,updated_at) "
                "VALUES('binance','um','NEWUSDT','PERPETUAL','USDT','USDT','TRADING',"
                "1704067200000,'NEW',NULL,NULL,'not_in_spot_trading_snapshot','未知',1,NULL,1)")
            con.commit()
        r = self.adapter.v1_pool()
        self.assertEqual(r["total"], 1)
        self.assertIsNone(r["items"][0]["eligibility"])
        self.assertIsNone(r["items"][0]["spot_status"])   # 无来源即未知，不判“未上现货”
        st = self.adapter.v1_pool_status()
        self.assertEqual(st["pool_total"], 1)
        self.assertEqual(st["pool_with_candles"], 0)      # 池里有、库里没有
        self.assertEqual(st["pool_without_candles"], 1)

    def test_okx_swap_pool_row_yields_usdt_perp_identity(self):
        """欧易 SWAP 行必须同样判为 USDT 永续，且身份字段齐全。

        回归点（549 全接入的前置）：`is_usdt_perp` 原先只认币安的
        `contractType='PERPETUAL'`，欧易 `instType='SWAP'` 会被判成「非永续」，
        导致工作台 549 里那 23 只欧易标的 base/quote/contract_type 全空，
        前端无法与币安统一处理。
        """
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute(
                "INSERT INTO symbol_pool(exchange,market,symbol,contract_type,quote_asset,"
                "margin_asset,status,onboard_time,base_asset,spot_symbol,spot_status,"
                "spot_match_method,priority_hint,snapshot_utc,source_call_id,updated_at) "
                "VALUES('okx','swap','AEON-USDT-SWAP','SWAP','USDT','USDT','live',"
                "1753615800000,'AEON',NULL,NULL,'no_spot_source','未知',1,NULL,1)")
            con.commit()
        p = self.adapter.pool_index()[("okx", "AEON-USDT-SWAP")]
        m = self.adapter.market_item("okx", "AEON-USDT-SWAP", pool=p)
        self.assertEqual(m["base"], "AEON")
        self.assertEqual(m["quote"], "USDT")
        self.assertEqual(m["contract_type"], "SWAP")
        self.assertIs(m["is_usdt_perp"], True)          # 不因不是 'PERPETUAL' 就判否
        self.assertEqual(m["spot_status"], "unknown")   # 欧易无现货来源 → 未知，非「未上现货」
        row = self.adapter.v1_pool()["items"][0]
        self.assertEqual((row["base"], row["quote"], row["contract_type"]),
                         ("AEON", "USDT", "SWAP"))


class BulkImportTests(unittest.TestCase):
    """流式入库适配器：与主项目导入器逐行一致，且保留 taker / available_at 语义。"""

    def setUp(self):
        self.db = TempDb()
        self.addCleanup(self.db.cleanup)
        self.dl = HERE / "历史下载"
        if str(self.dl) not in sys.path:
            sys.path.insert(0, str(self.dl))

    @staticmethod
    def _rows(n=4, symbol="TESTSYM", tf="1d"):
        iv = 86400000
        out = []
        for k in range(n):
            ot = DAY_OPEN + k * iv
            out.append([ot, "100.5", "101.5", "99.5", "101.0", "1234.5", ot + iv - 1,
                        "125000.25", 42, "600.5", "61000.5", "0"])
        return out

    def _make_zip(self, tmp, rows, symbol="TESTSYM", tf="1d", corrupt=False):
        import csv as _csv
        import hashlib as _hashlib
        import io as _io
        import zipfile as _zip
        buf = _io.StringIO()
        w = _csv.writer(buf)
        for r in rows:
            w.writerow(r)
        zpath = Path(tmp) / f"{symbol}-{tf}-2024-01.zip"
        with _zip.ZipFile(zpath, "w", _zip.ZIP_DEFLATED) as z:
            z.writestr(f"{symbol}-{tf}-2024-01.csv", buf.getvalue().encode())
        digest = _hashlib.sha256(zpath.read_bytes()).hexdigest()
        cpath = Path(str(zpath) + ".CHECKSUM")
        cpath.write_text(f"{'0' * 64 if corrupt else digest}  {zpath.name}\n", encoding="utf-8")
        return zpath, cpath

    def test_streaming_matches_official_importer_row_by_row(self):
        from bulk_import import import_zip_streaming
        with tempfile.TemporaryDirectory() as tmp:
            z, c = self._make_zip(tmp, self._rows())
            db_a = TempDb()
            self.addCleanup(db_a.cleanup)
            store_a, store_b = MarketStore(self.db.path), MarketStore(db_a.path)
            ra = store_a.import_binance_zip(z, c, "TESTSYM", "1d", "u")
            rb = import_zip_streaming(store_b, z, c, "TESTSYM", "1d", "u")
            self.assertEqual(ra["新增版本数"], rb["新增版本数"])
            cols = ("open_time,close_time,open,high,low,close,volume,quote_volume,trade_count,"
                    "taker_buy_volume,taker_buy_quote_volume,available_at,is_closed")
            with closing(sqlite3.connect(self.db.path)) as ca, \
                    closing(sqlite3.connect(db_a.path)) as cb:
                qa = ca.execute(f"SELECT {cols} FROM candle_versions WHERE symbol='TESTSYM' "
                                f"ORDER BY open_time").fetchall()
                qb = cb.execute(f"SELECT {cols} FROM candle_versions WHERE symbol='TESTSYM' "
                                f"ORDER BY open_time").fetchall()
                ha = ca.execute("SELECT COUNT(*) FROM candle_heads WHERE symbol='TESTSYM'").fetchone()[0]
                hb = cb.execute("SELECT COUNT(*) FROM candle_heads WHERE symbol='TESTSYM'").fetchone()[0]
            self.assertEqual(qa, qb)
            self.assertEqual(ha, hb)
            self.assertEqual(ha, len(self._rows()))
            self.assertTrue(all(r[9] is not None and r[9] != "" for r in qb))   # taker 保留
            self.assertTrue(all(r[11] is None for r in qb))                     # available_at 不编造
            self.assertEqual(import_zip_streaming(store_b, z, c, "TESTSYM", "1d", "u")["新增版本数"], 0)

    def test_archive_importers_preserve_existing_live_head(self):
        """历史 ZIP 可存成新 version，但不能把已有 REST/WS head 改回历史 NULL 时点。"""
        from bulk_import import import_zip_streaming
        from data_hub.store import now_ms
        with tempfile.TemporaryDirectory() as tmp:
            z, c = self._make_zip(tmp, self._rows(n=1))
            db_b = TempDb(); self.addCleanup(db_b.cleanup)
            cases = ((MarketStore(self.db.path), "store.import_binance_zip"),
                     (MarketStore(db_b.path), "bulk_import.import_zip_streaming"))
            for store, importer in cases:
                now = now_ms()
                live_values = ("binance", "um", "TESTSYM", "1d", DAY_OPEN, DAY_CLOSE,
                               "100.5", "103.0", "99.0", "102.0", "1234.5", "125000.25",
                               42, "600.5", "61000.5")
                with store.transaction() as con:
                    cur = con.execute(
                        "INSERT INTO sources(exchange,market,symbol,timeframe,source_url,file_path,"
                        "sha256,imported_at,downloaded_at,first_open,last_close,row_count) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("binance", "um", "TESTSYM", "1d", "https://fapi.binance.com/live",
                         "binance:rest:kline", "live-version", now, now, DAY_OPEN, DAY_CLOSE, 1))
                    live_id = cur.lastrowid
                    con.execute("INSERT INTO candle_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                                (*live_values, live_id, now))
                    con.execute("INSERT INTO candle_heads VALUES(?,?,?,?,?,?)",
                                ("binance", "um", "TESTSYM", "1d", DAY_OPEN, live_id))
                if importer == "store.import_binance_zip":
                    result = store.import_binance_zip(z, c, "TESTSYM", "1d",
                                                      "https://data.binance.vision/archive")
                else:
                    result = import_zip_streaming(store, z, c, "TESTSYM", "1d",
                                                  "https://data.binance.vision/archive")
                self.assertEqual(result["新增版本数"], 1)
                with closing(store.connect()) as con:
                    current = con.execute(
                        "SELECT v.source_id,v.close,v.available_at FROM candles v WHERE "
                        "exchange='binance' AND market='um' AND symbol='TESTSYM' AND timeframe='1d' "
                        "AND open_time=?", (DAY_OPEN,)).fetchone()
                    versions = con.execute(
                        "SELECT COUNT(*) FROM candle_versions WHERE exchange='binance' AND market='um' "
                        "AND symbol='TESTSYM' AND timeframe='1d' AND open_time=?", (DAY_OPEN,)).fetchone()[0]
                self.assertEqual(current["source_id"], live_id)
                self.assertEqual(current["close"], "102.0")
                self.assertEqual(current["available_at"], now)
                self.assertEqual(versions, 2)

    def test_streaming_rejects_checksum_mismatch(self):
        from bulk_import import import_zip_streaming
        with tempfile.TemporaryDirectory() as tmp:
            z, c = self._make_zip(tmp, self._rows(), corrupt=True)
            store = MarketStore(self.db.path)
            with self.assertRaises(ValueError):
                import_zip_streaming(store, z, c, "TESTSYM", "1d", "u")
            with closing(sqlite3.connect(self.db.path)) as con:
                n = con.execute("SELECT COUNT(*) FROM candle_versions WHERE symbol='TESTSYM'"
                                ).fetchone()[0]
            self.assertEqual(n, 0)

    def test_streaming_rejects_unclosed_and_unknown_timeframe(self):
        from bulk_import import import_zip_streaming
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._rows()
            future = DAY_OPEN + 10 ** 13                      # 远未收盘
            rows.append([future - future % 86400000, "1", "1", "1", "1", "1",
                         future - future % 86400000 + 86399999, "1", 1, "1", "1", "0"])
            z, c = self._make_zip(tmp, rows)
            store = MarketStore(self.db.path)
            with self.assertRaises(ValueError):
                import_zip_streaming(store, z, c, "TESTSYM", "1d", "u")
            with self.assertRaises(ValueError):
                import_zip_streaming(store, z, c, "TESTSYM", "3d", "u")   # 未配置周期


class RealtimeKlineTests(_AdapterCase):
    """实时 K 线：全周期可读、未收盘走**独立临时层**、欧易无 8h 不合成。

    纪律：夹具下 `MARKET_TAIL_FETCH=0` ⇒ 不得触发任何真实外呼；
    临时层有行时才能给出 forming_bar，且 bars 恒为已收盘。
    """

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=False)

    def _put_forming(self, exchange, market, symbol, tf, open_time, close_time, px):
        with closing(sqlite3.connect(self.db.path)) as con:
            con.execute(
                "INSERT INTO realtime_klines(exchange,market,symbol,timeframe,open_time,close_time,"
                "open,high,low,close,volume,quote_volume,trade_count,taker_buy_volume,"
                "taker_buy_quote_volume,is_closed,event_time,received_at,source,transport,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (exchange, market, symbol, tf, open_time, close_time, px, px, px, px,
                 "1", "1", 1, "0", "0", open_time + 1000, self.adapter._now_ms(),
                 "binance.fstream", "ws", self.adapter._now_ms()))
            con.commit()

    def test_all_contract_timeframes_readable(self):
        """3000 原有全部周期按钮（含 12h/8h）都必须能读到：不报错、不造数，bars 恒已收盘。"""
        for tf in ("1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"):
            r = self.adapter.v1_candles("binance", "1000PEPEUSDT",
                                        {"timeframe": [tf], "limit": ["3"]})
            self.assertEqual(r["timeframe"], tf)
            self.assertIsInstance(r["bars"], list)
            self.assertTrue(all(b["is_closed"] for b in r["bars"]))
            self.assertIsNone(r["forming_bar"])       # 夹具无临时层行且不外呼
            self.assertIn("realtime", r)

    def test_okx_8h_marked_not_native_no_synthesis(self):
        """欧易无 8h 原生周期：如实 is_native=false 并说明，不合成、不跨所拼接。"""
        r = self.adapter.v1_candles("okx", "CRO-USDT-SWAP",
                                    {"timeframe": ["8h"], "limit": ["3"]})
        self.assertFalse(r["is_native"])
        self.assertEqual(r["bars"], [])
        self.assertIn("不合成", r["quality"]["note"])

    def test_okx_candle_source_is_limited_to_binance_missing_supplement(self):
        """币安已存在的 token 不得因 Binance 缺历史而转读 OKX；只放行补充差集。"""
        r = self.adapter.v1_candles("okx", "BTC-USDT-SWAP",
                                    {"timeframe": ["1h"], "limit": ["3"]})
        self.assertEqual(r["bars"], [])
        self.assertEqual(r["quality"]["status"], "out_of_scope")
        self.assertIn("币安缺失的补充标的", r["quality"]["note"])
        self.assertIsNone(r["forming_bar"])

        supplement = Path(self._pooldir.name) / "cross_exchange_supplement.json"
        supplement.write_text(json.dumps({"supplement": [{"inst_id": "CRO-USDT-SWAP"}]}),
                               encoding="utf-8")
        r_ok = self.adapter.v1_candles("okx", "CRO-USDT-SWAP",
                                       {"timeframe": ["1h"], "limit": ["3"]})
        self.assertTrue(r_ok["is_native"])
        self.assertEqual(r_ok["quality"]["status"], "missing")  # 夹具无库存，但合约范围获准

    def test_forming_bar_read_from_temp_layer_only(self):
        """临时层有行 ⇒ forming_bar 带真实来源/新鲜度；**bars 仍恒为已收盘**。"""
        self._put_forming("binance", "um", "1000PEPEUSDT", "5m",
                          1_790_000_000_000, 1_790_000_299_999, "0.00123")
        r = self.adapter.v1_candles("binance", "1000PEPEUSDT",
                                    {"timeframe": ["5m"], "limit": ["3"]})
        fb = r["forming_bar"]
        self.assertIsNotNone(fb)
        self.assertFalse(fb["is_closed"])
        self.assertEqual(fb["close"], 0.00123)
        self.assertEqual(fb["source"], "binance.fstream/ws")
        self.assertIn("realtime_klines", fb["note"])
        self.assertTrue(all(b["is_closed"] for b in r["bars"]))
        self.assertEqual(r["realtime"]["transport"], "ws")
        self.assertIn("realtime_klines", r["realtime"]["origin"])

    def test_temp_layer_closed_row_not_used_as_forming(self):
        """临时层**不得**出现 is_closed=1 的行（已收盘一律进 candle_*）。"""
        with closing(sqlite3.connect(self.db.path)) as con:
            n = con.execute("SELECT COUNT(*) FROM realtime_klines WHERE is_closed<>0").fetchone()[0]
        self.assertEqual(n, 0)

    def test_import_plan_reports_live_state_not_stale_text(self):
        """IMPORT_PLAN 不得再写死「未启动下载采集」这类过时文案；须现读真实状态。"""
        r = self.adapter.v1_import_plan()
        blob = json.dumps(r, ensure_ascii=False)
        for stale in ("未启动全池下载", "本轮未启动", "未启动下载采集"):
            self.assertNotIn(stale, blob)
        self.assertIn("realtime_klines", r)
        self.assertIn("history", r)
        self.assertIn("8h", r["plan"]["native_skip"]["okx"])
        self.assertIn("order", r["plan"])
        # 2026-10-11：1m 已彻底删除，order 末位改为最细的 5m
        self.assertEqual(r["plan"]["order"][-1], "5m")
        self.assertNotIn("1m", r["plan"]["order"])

    def test_rest_tail_does_not_replace_existing_closed_head(self):
        """按需 REST 尾段只补缺；已有归档/实时版本及 source/available_at 不得被改指。"""
        self.adapter._TAIL_LAST.pop(("binance", "1000PEPEUSDT", "1d"), None)
        with closing(sqlite3.connect(self.db.path)) as con:
            before = con.execute(
                "SELECT c.source_id,c.close,c.available_at,(SELECT COUNT(*) FROM candle_versions v "
                "WHERE v.exchange=c.exchange AND v.market=c.market AND v.symbol=c.symbol "
                "AND v.timeframe=c.timeframe AND v.open_time=c.open_time) "
                "FROM candles c WHERE exchange='binance' AND market='um' "
                "AND symbol='1000PEPEUSDT' AND timeframe='1d' AND open_time=?", (DAY_OPEN,)).fetchone()
        self.assertIsNotNone(before)
        bar = {"open_time": DAY_OPEN, "close_time": DAY_CLOSE, "open": "0.00130",
               "high": "0.00145", "low": "0.00125", "close": "0.00140",
               "volume": "123", "quote_volume": "456", "trade_count": 10,
               "taker_buy_volume": "60", "taker_buy_quote_volume": "240", "is_closed": True}
        added = self.adapter._tail_store("binance", "1000PEPEUSDT", "1d", [bar],
                                         "https://fapi.binance.com/fapi/v1/klines")
        with closing(sqlite3.connect(self.db.path)) as con:
            after = con.execute(
                "SELECT c.source_id,c.close,c.available_at,(SELECT COUNT(*) FROM candle_versions v "
                "WHERE v.exchange=c.exchange AND v.market=c.market AND v.symbol=c.symbol "
                "AND v.timeframe=c.timeframe AND v.open_time=c.open_time) "
                "FROM candles c WHERE exchange='binance' AND market='um' "
                "AND symbol='1000PEPEUSDT' AND timeframe='1d' AND open_time=?", (DAY_OPEN,)).fetchone()
        self.assertEqual(added, 0)
        self.assertEqual(after, before)


class GapfillBridgeTests(_AdapterCase):
    """断线桥接必须发现 archive→实时之间的内部洞，不可只看最新末根。"""

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=False)
        import realtime_collector as rc
        cls.rc = rc
        cls._old_db = rc.DB
        rc.DB = cls.db.path
        cls.addClassCleanup(lambda: setattr(rc, "DB", cls._old_db))

    def test_binance_bridge_uses_database_gap_and_preserves_newer_live_head(self):
        rc = self.rc
        tf, iv, sym = "4h", rc.TF_MS["4h"], "BTCUSDT"
        want = rc._expected_closed_open(rc.now_ms(), tf, "binance")
        archived = want - 2 * iv
        missing = want - iv
        archive_url = ("https://data.binance.vision/data/futures/um/daily/klines/"
                       "BTCUSDT/4h/BTCUSDT-4h-archive.zip")

        def bar(open_time, px, available_at=None):
            return {"open_time": open_time, "close_time": open_time + iv - 1,
                    "open": str(px), "high": str(px + 2), "low": str(px - 2),
                    "close": str(px + 1), "volume": "10", "quote_volume": "100",
                    "trade_count": 5, "taker_buy_volume": "6",
                    "taker_buy_quote_volume": "60", "is_closed": True,
                    "available_at": available_at}

        with closing(rc.db()) as con:
            rc._store_closed_bars(con, "binance", "um", sym, tf, [bar(archived, 100)],
                                  archive_url, "archive")
            rc._store_closed_bars(con, "binance", "um", sym, tf,
                                  [bar(want, 200, rc.now_ms())],
                                  "https://fapi.binance.com/fapi/v1/klines", "ws")
            con.execute(
                "INSERT INTO coverage_cache(exchange,market,symbol,timeframe,rows,first_open,"
                "last_close,expected,missing,source_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("binance", "um", sym, tf, 2, archived, want + iv - 1, 3, 1, 2, rc.now_ms()))
            con.commit()

        # 模拟进程刚启动：无任何内存 WS 游标，也必须从 DB archive anchor 找到中间缺口。
        rc._KLAST_OPEN.clear()
        rc._KLAST_CLOSED.clear()
        rc._GAPFILL_LAST.pop(("binance", "um", tf), None)

        class FakeGateway:
            def __init__(self): self.params = None
            def get(self, name, params):
                self.params = (name, dict(params))
                rows = []
                for ot, px in ((missing, 110), (want, 999)):
                    rows.append([ot, str(px), str(px + 2), str(px - 2), str(px + 1),
                                 "10", ot + iv - 1, "100", 5, "6", "60"])
                return {"数据": rows}

        fake = FakeGateway()
        calls, filled, skipped = rc.kline_gap_fill(
            "binance", "um", tf, fake, max_calls=1, symbols=(sym,), src_label="unit-test")
        self.assertEqual((calls, filled, skipped), (1, 1, 0))
        self.assertEqual(fake.params[0], "binance.candles")
        self.assertEqual(fake.params[1]["startTime"], missing)
        with closing(rc.db()) as con:
            rows = con.execute(
                "SELECT c.open_time,c.close,c.available_at,s.file_path FROM candles c "
                "JOIN sources s ON s.id=c.source_id WHERE c.exchange='binance' AND c.market='um' "
                "AND c.symbol=? AND c.timeframe=? AND c.open_time BETWEEN ? AND ? ORDER BY c.open_time",
                (sym, tf, archived, want)).fetchall()
            coverage = con.execute(
                "SELECT rows,expected,missing,source_count FROM coverage_cache WHERE exchange=? "
                "AND market=? AND symbol=? AND timeframe=?", ("binance", "um", sym, tf)).fetchone()
        self.assertEqual([r[0] for r in rows], [archived, missing, want])
        self.assertEqual(tuple(coverage), (3, 3, 0, 3))
        self.assertEqual(rows[1][3], "binance:rest:kline")
        self.assertIsNotNone(rows[1][2])
        # REST 回包里故意给 want 一个不同的 close；已有 WS head 必须原样保持。
        self.assertEqual(rows[2][1], "201")
        self.assertEqual(rows[2][3], "binance:ws:kline")


class TransportResilienceTests(unittest.TestCase):
    """传输层**请求时**解析代理：环境代理端点消失后必须自愈；代理仍存活时绝不改走直连。

    背景（2026-10-06 实测）：沙箱会话轮换后，长驻采集进程持有的
    `HTTP_PROXY=http://127.0.0.1:62185` 端口已无人监听，全部 REST `URLError`；
    而 `urllib` 默认只在 opener 构造时读一次环境变量，于是永久打向死端口。
    """

    @classmethod
    def setUpClass(cls):
        import gateway_ext
        cls.ge = gateway_ext

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "ALL_PROXY", "all_proxy")}
        self.ge._proxy_state["at"] = 0.0
        self.ge._proxy_state["value"] = None
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.ge._proxy_state["at"] = 0.0
        self.ge._proxy_state["value"] = None

    def _only(self, value):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            os.environ.pop(k, None)
        os.environ["HTTP_PROXY"] = value
        os.environ["HTTPS_PROXY"] = value

    @staticmethod
    def _proxy_handler(opener):
        from urllib.request import ProxyHandler
        for h in opener.handlers:
            if isinstance(h, ProxyHandler):
                return h
        return None

    def test_dead_env_proxy_resolves_to_none(self):
        """环境代理端口无人监听 → 解析为 None（普通环境默认行为），不再打向死端口。"""
        self._only("http://127.0.0.1:9")
        self.assertIsNone(self.ge.resolve_proxy(force=True))

    def test_live_env_proxy_is_used(self):
        """环境代理端口可连 → 继续走它（不绕过仍然生效的沙箱代理）。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        addr = f"http://127.0.0.1:{srv.getsockname()[1]}"
        self._only(addr)
        self.assertEqual(self.ge.resolve_proxy(force=True), addr)

    def test_direct_and_proxy_use_distinct_openers(self):
        direct = self.ge._opener_for(None)
        via = self.ge._opener_for("http://127.0.0.1:9")
        self.assertIsNot(direct, via)
        self.assertIs(self.ge._opener_for(None), direct)          # 复用，不每次新建
        # 直连 opener **不带任何代理钩子**（ProxyHandler({}) 不注册 http_open/https_open，
        # 因而不会出现在 handlers 里）；代理 opener 的 http/https 都指向该代理。
        self.assertIsNone(self._proxy_handler(direct))
        handler = self._proxy_handler(via)
        self.assertIsNotNone(handler)
        self.assertEqual(handler.proxies,
                         {"http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"})


class RestChannelHealthTests(unittest.TestCase):
    """健康判据必须覆盖 **REST 出网通道**，不能只看「进程存活 + 心跳新鲜」。"""

    @classmethod
    def setUpClass(cls):
        import realtime_collector as rc
        cls.rc = rc

    def test_unknown_when_no_calls_yet(self):
        ok, detail = self.rc._rest_health(None)
        self.assertTrue(ok)
        self.assertIn("尚无调用记录", detail)

    def test_unhealthy_when_every_attempt_failed(self):
        rest = {"attempts": 528, "ok": 0, "fail": 528,
                "endpoints": {"binance.oi": {"ok": 0, "fail": 528}},
                "last_ok_age_s": None,
                "last_error": "binance.oi → RejectedRequest: 网络请求未完成"}
        ok, detail = self.rc._rest_health(rest)
        self.assertFalse(ok)
        self.assertIn("全部失败", detail)

    def test_unhealthy_when_last_success_too_old(self):
        rest = {"attempts": 10, "ok": 1, "fail": 9, "endpoints": {},
                "last_ok_age_s": self.rc.REST_STALE_S + 1, "last_error": "x"}
        self.assertFalse(self.rc._rest_health(rest)[0])

    def test_healthy_when_recent_success(self):
        rest = {"attempts": 10, "ok": 9, "fail": 1,
                "endpoints": {"binance.oi": {"ok": 9, "fail": 1}},
                "last_ok_age_s": 3.0, "last_error": "x"}
        ok, detail = self.rc._rest_health(rest)
        self.assertTrue(ok)
        self.assertIn("REST 通道正常", detail)
        self.assertIn("binance.oi ok=9/fail=1", detail)

    def test_rest_gateway_records_per_endpoint(self):
        rc = self.rc
        with rc.REST_STATS_LOCK:
            rc.REST_STATS.update({"attempts": 0, "ok": 0, "fail": 0, "deferred": 0,
                                  "endpoints": {},
                                  "last_ok_at": None, "last_ok_endpoint": None,
                                  "last_error": None, "last_error_at": None,
                                  "last_error_endpoint": None})

        class Fake:
            def get(self, name, params=None):
                if name == "boom":
                    raise RuntimeError("dead proxy")
                return {"数据": {}}

        gw = rc.RestGateway(Fake())
        gw.get("binance.oi", {"symbol": "BTCUSDT"})
        with self.assertRaises(RuntimeError):
            gw.get("boom")
        with rc.REST_STATS_LOCK:
            snap = json.loads(json.dumps(rc.REST_STATS))
        self.assertEqual((snap["ok"], snap["fail"]), (1, 1))
        self.assertEqual(snap["endpoints"]["binance.oi"]["ok"], 1)
        self.assertEqual(snap["endpoints"]["boom"]["fail"], 1)
        self.assertIn("dead proxy", snap["last_error"])
        self.assertIsNotNone(snap["last_ok_at"])

    def test_local_pacing_is_not_counted_as_failure(self):
        """`DeferredRequest`（共享预算/最小间隔/冷却）是**本地节流**，不算通道失败。"""
        rc = self.rc
        from data_hub.gateway import DeferredRequest
        with rc.REST_STATS_LOCK:
            rc.REST_STATS.update({"attempts": 0, "ok": 0, "fail": 0, "deferred": 0,
                                  "endpoints": {}, "last_ok_at": None,
                                  "last_ok_endpoint": None, "last_error": None,
                                  "last_error_at": None, "last_error_endpoint": None})

        class Fake:
            def get(self, name, params=None):
                raise DeferredRequest(rc.now_ms() + 1000, "请求间隔保护")

        gw = rc.RestGateway(Fake())
        with self.assertRaises(DeferredRequest):
            gw.get("binance.premium_index")
        with rc.REST_STATS_LOCK:
            snap = json.loads(json.dumps(rc.REST_STATS))
        self.assertEqual(snap["deferred"], 1)
        self.assertEqual(snap["fail"], 0)
        self.assertIsNone(snap["last_error"])
        self.assertEqual(snap["endpoints"]["binance.premium_index"]["fail"], 0)

    def test_snapshot_reports_transport_label(self):
        snap = self.rc.rest_stats_snapshot()
        self.assertIn("proxy", snap)
        self.assertIn("last_ok_age_s", snap)


class OiHistoryQueryTests(_AdapterCase):
    """`/oi` 的加性查询能力 —— 「OI 总持仓量就几天」修复的回归（2026-10-11）。

    背景：用户反馈「OI 总持仓量就几天」。根因**不在数据**，而在取数口径：
      · `/oi` 不带 `period` ⇒ 只读 `period='snapshot'`（本机快照仅 2026-10-05 起约 6 天）；
      · `?period=5m` ⇒ 归档其实有 6 年（生产库 `oi_history` 5m 分区 1.271 亿行 / 526 标的），
        但旧实现把 `LIMIT 2000` 写死，5m 粒度下只够 6.9 天。
    本轮加性新增 `period=auto`（5m 自适应金字塔 + 该末点**之后**的 snapshot 近端）与
    `limit` / `from` / `to` / `bucket_ms` / `spread`。**默认（无 period）行为逐字不变**。

    本测试全部在 `TempDb` 临时库上跑，不读生产库、不联网。
    """

    HOUR = 3_600_000
    FIVE_M = 300_000
    N_5M = 12 * 288                  # 12 天 × 288 根
    N_TAIL = 24                      # snapshot 近端条数

    @classmethod
    def setUpClass(cls):
        cls._bind(with_signals=False)
        # 基准对齐到整点 ⇒ 小时分桶断言可复现；整体贴近「现在」⇒ 自适应金字塔落到 5m/1h 档。
        # ⚠ 归档跨度必须**大于近端 5m 档的 6 天**（`OI_SPREAD_STEPS`），否则全部落在 5m 档、
        #   金字塔不会降采样，`test_auto_pyramid_downsamples_but_keeps_real_values` 就失去意义。
        #   2026-10-11 近端档由 2 天放宽到 6 天后，夹具跨度同步由 3 天改为 12 天。
        base = int(time.time() * 1000) - 12 * 24 * cls.HOUR
        cls.base = base - (base % cls.HOUR)
        rows = []
        for i in range(cls.N_5M):
            ts = cls.base + i * cls.FIVE_M
            rows.append(("binance", "um", "1000PEPEUSDT", "5m", ts,
                         1000.0 + i, 2000.0 + i, 1000.0 + i, 2000.0 + i, "archive", ts))
        cls.last5 = cls.base + (cls.N_5M - 1) * cls.FIVE_M
        for i in range(1, cls.N_TAIL + 1):
            ts = cls.last5 + i * 20 * 60 * 1000
            rows.append(("binance", "um", "1000PEPEUSDT", "snapshot", ts,
                         5000.0 + i, 6000.0 + i, 5000.0 + i, 6000.0 + i, "collector", ts))
        # OKX：只有 snapshot，没有任何 5m 归档 ⇒ `auto` 必须如实退化为纯 snapshot
        for i in range(3):
            ts = cls.base + i * cls.HOUR
            rows.append(("okx", "swap", "BTC-USDT-SWAP", "snapshot", ts,
                         7.0 + i, 8.0 + i, 7.0 + i, 8.0 + i, "collector", ts))
        with closing(sqlite3.connect(cls.db.path)) as con:
            con.executemany(
                "INSERT OR REPLACE INTO oi_history(exchange,market,symbol,period,ts,"
                "open_interest,open_interest_value,oi_ccy,oi_usd,source,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        cls.seeded = {1000.0 + i for i in range(cls.N_5M)} | {5000.0 + i for i in range(1, 25)}

    def test_default_is_still_snapshot_only(self):
        """零回归：不带 `period` 时**只读 snapshot**，与修复前逐字一致。"""
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT", {})
        self.assertEqual(r["total"], self.N_TAIL)
        self.assertEqual({i["period"] for i in r["items"]}, {"snapshot"})

    def test_period_5m_is_archive_only(self):
        # 默认 `limit=2000` 在 5m 粒度只够 ~6.9 天 ⇒ 先断言**默认档确实截断**，
        # 再显式放大到上限，断言**归档全量**可取（两条一起才说明 limit 真的在起作用）。
        d = self.adapter.v1_oi("binance", "1000PEPEUSDT", {"period": ["5m"]})
        self.assertEqual(d["total"], self.adapter.HISTORY_LIMIT_DEFAULT)
        self.assertEqual({i["period"] for i in d["items"]}, {"5m"})
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                               {"period": ["5m"], "limit": ["20000"]})
        self.assertEqual(r["total"], self.N_5M)
        self.assertEqual({i["period"] for i in r["items"]}, {"5m"})

    def test_unknown_period_is_empty_not_fabricated(self):
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT", {"period": ["7m"]})
        self.assertEqual(r["quality"]["status"], "missing")
        self.assertNotIn("items", r)

    def test_auto_merges_tail_without_overlap(self):
        """`auto` = 5m 归档 + 该末点之后的 snapshot；两段**零重叠**、整体时间升序。"""
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                               {"period": ["auto"], "spread": ["0"], "limit": ["20000"]})
        its = r["items"]
        self.assertEqual(r["total"], self.N_5M + self.N_TAIL)
        self.assertEqual([i["event_time"] for i in its],
                         sorted(i["event_time"] for i in its))
        p5 = [i for i in its if i["period"] == "5m"]
        ps = [i for i in its if i["period"] == "snapshot"]
        self.assertEqual(len(p5), self.N_5M)
        self.assertEqual(len(ps), self.N_TAIL)
        boundary = self.adapter.iso(self.last5)
        self.assertLessEqual(max(i["event_time"] for i in p5), boundary)
        self.assertGreater(min(i["event_time"] for i in ps), boundary)

    def test_auto_pyramid_downsamples_but_keeps_real_values(self):
        """自适应金字塔：点数下降，但每一点都必须是**库内真实观测**（不插值、不造值）。"""
        raw = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                                 {"period": ["auto"], "spread": ["0"]})
        pyr = self.adapter.v1_oi("binance", "1000PEPEUSDT", {"period": ["auto"]})
        # 归档 12 天：近端 6 天走 5m 档（≈1728 点）、更早 6 天走 1h 档（≈144 点）⇒ 总数
        # 明显少于 `spread=0` 的原生 5m 档（受默认 limit=2000 约束）。
        self.assertLess(pyr["total"], raw["total"])
        self.assertGreater(pyr["total"], 400)
        for it in pyr["items"]:
            # `oi_history.open_interest` 是 TEXT 列 ⇒ 接口按字符串透传，比较前转 float
            self.assertIn(float(it["open_interest"]), self.seeded)
        self.assertEqual(pyr["items"][-1]["event_time"],
                         self.adapter.iso(self.last5 + self.N_TAIL * 20 * 60 * 1000))

    def test_auto_degrades_to_snapshot_when_no_archive(self):
        """OKX 无 5m 归档 ⇒ `auto` 如实只给 snapshot，不用别的交易所/周期顶替。"""
        r = self.adapter.v1_oi("okx", "BTC-USDT-SWAP", {"period": ["auto"]})
        self.assertEqual(r["total"], 3)
        self.assertEqual({i["period"] for i in r["items"]}, {"snapshot"})

    def test_limit_clamped_both_ends(self):
        lo = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                                {"period": ["5m"], "limit": ["0"]})
        self.assertEqual(lo["total"], 1)
        hi = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                                {"period": ["5m"], "limit": ["99999999"]})
        self.assertEqual(hi["total"], self.N_5M)
        self.assertLessEqual(hi["total"], self.adapter.HISTORY_LIMIT_MAX)

    def test_bad_limit_falls_back_to_default(self):
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                               {"period": ["auto"], "spread": ["0"], "limit": ["abc"]})
        # `limit` 非法 ⇒ 回落默认；且返回总量**受 `limit` 约束**（近端 snapshot 段
        # 优先占满预算，归档金字塔用剩余预算），不再出现「5m 全量 + 近端另加」的 2×limit。
        self.assertEqual(r["total"], self.adapter.HISTORY_LIMIT_DEFAULT)

    def test_from_to_window(self):
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                               {"period": ["5m"],
                                "from": [str(self.base)],
                                "to": [str(self.base + 9 * self.FIVE_M)]})
        self.assertEqual(r["total"], 10)
        self.assertEqual(r["items"][0]["event_time"], self.adapter.iso(self.base))

    def test_bucket_ms_returns_latest_real_observation_per_bucket(self):
        """分桶只做**抽样选择**：每桶 1 条，取值 = 该桶内时间最晚的真实观测（非均值）。"""
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                               {"period": ["5m"], "bucket_ms": [str(self.HOUR)]})
        its = r["items"]
        self.assertEqual(r["total"], self.N_5M * self.FIVE_M // self.HOUR)   # 72 桶
        # 第 0 桶 = [base, base+1h) 内 i=0..11，最晚 i=11
        self.assertEqual(its[0]["event_time"], self.adapter.iso(self.base + 11 * self.FIVE_M))
        self.assertEqual(float(its[0]["open_interest"]), 1011.0)

    def test_spread_zero_is_raw_5m_not_bucketed(self):
        """`spread=0` ⇒ 不分桶、直接取最近 N 条原生 5m；近端 snapshot 段优先占满预算。

        预算顺序是本轮的关键修复：**先**取「归档末点之后的 snapshot 近端」并优先占满
        `limit`，**再**把剩余预算给归档金字塔。反过来会让近端被截断，在归档末点处留洞。
        """
        r = self.adapter.v1_oi("binance", "1000PEPEUSDT",
                               {"period": ["auto"], "spread": ["0"], "limit": ["100"]})
        self.assertEqual(r["total"], 100)
        ps = [i for i in r["items"] if i["period"] == "snapshot"]
        p5 = [i for i in r["items"] if i["period"] == "5m"]
        self.assertEqual(len(ps), self.N_TAIL)              # 近端全给（24 条）
        self.assertEqual(len(p5), 100 - self.N_TAIL)        # 剩余预算 76 条给 5m
        boundary = self.adapter.iso(self.last5)
        self.assertGreater(min(i["event_time"] for i in ps), boundary)   # 零重叠


class OiBucketRowsTests(unittest.TestCase):
    """`oi_bucket_rows`：已收盘 5m 桶落 `'5m'`、最新一根另落 `'snapshot'`。

    背景（2026-10-11 用户反馈「OI 柱形图有缺失」的根因 A1）：此前 `oi_sweep` 每标的
    每轮只落 **1** 行 `'snapshot'`，一轮 528 标的约 9~10 分钟 + `interval=600s`
    ⇒ 相邻两行中位间隔 **15 分钟**，而 K 线轴是 **5 分钟** ⇒ 最近 181 个 5m 槽位里
    只有约 50 根柱（**72% 空槽**）。

    这三条回归锁死切桶的**边界**：`桶起点 + 5m <= now` 才算已收盘（`<=` 不是 `<`）。
    """

    STEP = 300_000
    NOW = 1_791_600_123_456          # 故意**不落在 5m 边界**上，贴近真实调用

    @classmethod
    def setUpClass(cls):
        import realtime_collector as rc
        cls.rc = rc

    def _arr(self, n=12):
        """12 根 5m 桶，最后一根仍在形成中（起点 + 5m > now）。"""
        latest = self.NOW - 60_000
        latest -= latest % self.STEP
        opens = [latest - i * self.STEP for i in range(n)][::-1]
        return [{"symbol": "XUSDT", "timestamp": ts,
                 "sumOpenInterest": str(1000 + i), "sumOpenInterestValue": str(2000 + i),
                 "CMCCirculatingSupply": "12345.6"} for i, ts in enumerate(opens)]

    def test_closed_buckets_go_5m_and_latest_goes_snapshot(self):
        arr = self._arr()
        rows, newest, oldest = self.rc.oi_bucket_rows("XUSDT", arr, self.NOW, 111)
        p5 = [r for r in rows if r["period"] == "5m"]
        ps = [r for r in rows if r["period"] == "snapshot"]
        self.assertEqual(len(p5), len(arr) - 1)          # 12 根里 11 根已收盘
        self.assertEqual(len(ps), 1)                     # 最新那一根
        self.assertEqual(ps[0]["ts"], arr[-1]["timestamp"])
        # **最近一根已收盘桶**必须在 5m 里（曾被 off-by-one 的判据排掉）
        self.assertIn(arr[-2]["timestamp"], [r["ts"] for r in p5])
        # 未收盘的那一根**不得**出现在 5m 里
        self.assertNotIn(arr[-1]["timestamp"], [r["ts"] for r in p5])
        self.assertEqual((newest, oldest), (arr[-1]["timestamp"], arr[0]["timestamp"]))
        self.assertEqual({r["source"] for r in rows}, {"binance.fapi.openInterestHist"})
        self.assertEqual({r["fetched_at"] for r in rows}, {111})
        # 数量与金额取自**同一行、同一时点**
        self.assertEqual(p5[0]["oi_ccy"], p5[0]["open_interest"])
        self.assertEqual(p5[0]["oi_usd"], p5[0]["open_interest_value"])
        self.assertEqual(p5[0]["exchange"], "binance")
        self.assertEqual(p5[0]["market"], "um")

    def test_last_bucket_exactly_closed_is_written_as_5m(self):
        """边界：`桶起点 + 5m == now` 视为**已收盘**（`<=`，不是 `<`）。"""
        ts = self.NOW - self.STEP
        arr = [{"symbol": "XUSDT", "timestamp": ts, "sumOpenInterest": "1",
                "sumOpenInterestValue": "2", "CMCCirculatingSupply": "3"}]
        rows, _, _ = self.rc.oi_bucket_rows("XUSDT", arr, self.NOW)
        self.assertEqual([r["period"] for r in rows], ["5m", "snapshot"])

    def test_forming_bucket_is_not_written_as_5m(self):
        ts = self.NOW - self.STEP + 1                    # 桶起点 + 5m > now
        arr = [{"symbol": "XUSDT", "timestamp": ts, "sumOpenInterest": "1",
                "sumOpenInterestValue": "2", "CMCCirculatingSupply": "3"}]
        rows, _, _ = self.rc.oi_bucket_rows("XUSDT", arr, self.NOW)
        self.assertEqual([r["period"] for r in rows], ["snapshot"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
