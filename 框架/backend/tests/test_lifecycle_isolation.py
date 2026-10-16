# -*- coding: utf-8 -*-
"""历史 K 线**读侧语义隔离**测试（离线；零网络；临时库）。

对应 2026-10-07 主助手 22:35 核查第 2/3 项：

  ① 官方归档**零量占位行**（volume==0 且 OHLC 四值相同）是归档证据，
     **不是有成交趋势样本** → 读侧 `tradeable=False`，趋势输入**剔除**。
  ② **同一符号两代合约**（ICPUSDT 旧合约 2022-06-10 09:00Z 交割退市、
     新合约 2022-09-27 02:30Z 上市）→ 按官方生命周期分段，
     **不拼成连续可交易历史**；趋势输入默认只保留最新一代。
  ③ 库内原始行**不删不改**（本模块只读、纯函数）。

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_lifecycle_isolation -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 后端实现/
# W1（2026-10-16）：原指向工程外路径（已删）⇒ 改按工程布局推导。
FRAMEWORK = str(HERE.parent)          # 框架/（含 data_hub 包）
PROJECT = str(HERE.parent.parent)     # 工程根（DATA_HUB_PROJECT 语义）
for _p in (str(HERE), FRAMEWORK, PROJECT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import history_lifecycle as hl                          # noqa: E402
import strategy_runner as sr                            # noqa: E402
from data_hub.store import MarketStore                  # noqa: E402

IV12 = 12 * 3600 * 1000
IV1D = 24 * 3600 * 1000


def ms(s):
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


class TestPaddingCriterion(unittest.TestCase):
    """① 占位判据：volume==0 且 OHLC 四值相同。"""

    def test_padding_true(self):
        self.assertTrue(hl.is_archive_padding_values("0", "6.44", "6.44", "6.44", "6.44"))
        self.assertTrue(hl.is_archive_padding_values(0.0, 1, 1, 1, 1))

    def test_real_trade_false(self):
        self.assertFalse(hl.is_archive_padding_values("5983639.04", "49.88", "50.80", "44.12", "47.90"))
        # 零量但 OHLC 不同 → 不是占位
        self.assertFalse(hl.is_archive_padding_values("0", "1", "2", "0.5", "1.5"))

    def test_missing_values_conservative_false(self):
        self.assertFalse(hl.is_archive_padding_values(None, None, None, None, None))
        self.assertFalse(hl.is_archive_padding_values("x", "1", "1", "1", "1"))


class TestLifecycleSegments(unittest.TestCase):
    """② ICPUSDT 官方生命周期分段与代际判定。"""

    def test_generation_boundaries(self):
        """边界按 12h 网格吸附（交割/上市时点落在 bar 内部）。"""
        # 交割前 1ms → g1
        self.assertEqual(hl.generation_of("binance", "ICPUSDT", "12h",
                                          ms("2022-06-10T12:00:00Z") - 1)["generation"], "g1")
        # 交割所在 bar（2022-06-10T00:00Z）仍属 g1（其内含真实成交至 09:00Z）
        self.assertEqual(hl.generation_of("binance", "ICPUSDT", "12h",
                                          ms("2022-06-10T00:00:00Z"))["generation"], "g1")
        # 吸附边界起 → gap（无合约窗口）
        self.assertEqual(hl.generation_of("binance", "ICPUSDT", "12h",
                                          ms("2022-06-10T12:00:00Z"))["generation"], "gap")
        # 新合约上市前 1ms（2022-09-27T00:00Z 前）→ 仍是 gap
        self.assertEqual(hl.generation_of("binance", "ICPUSDT", "12h",
                                          ms("2022-09-27T00:00:00Z") - 1)["generation"], "gap")
        # 上市所在 bar（2022-09-27T00:00Z）即首根 g2（其内含 02:30 起真实成交）
        self.assertEqual(hl.generation_of("binance", "ICPUSDT", "12h",
                                          ms("2022-09-27T00:00:00Z"))["generation"], "g2")
        self.assertEqual(hl.generation_of("binance", "ICPUSDT", "12h",
                                          ms("2022-09-27T02:30:00Z"))["generation"], "g2")

    def test_gap_segment_not_tradeable(self):
        seg = hl.generation_of("binance", "ICPUSDT", "12h", ms("2022-08-01T00:00:00Z"))
        self.assertFalse(seg["tradeable"])

    def test_latest_generation_is_g2(self):
        self.assertEqual(hl.latest_generation("binance", "ICPUSDT", "12h"), "g2")

    def test_scope_limited_to_12h(self):
        """范围限 12h：其他周期不注册、语义不变。"""
        self.assertIsNone(hl.lifecycle_segments("binance", "ICPUSDT", "1d"))
        self.assertIsNone(hl.generation_of("binance", "ICPUSDT", "1d", ms("2022-08-01T00:00:00Z")))

    def test_unregistered_symbol_untouched(self):
        a = hl.classify_values("binance", "1000PEPEUSDT", "12h", ms("2022-08-01T00:00:00Z"),
                               1, 1, 1, 1, 0.0)
        self.assertEqual(a["generation"], None)
        self.assertTrue(a["tradeable"])          # 未注册标的不做隔离


class TestClassifyValues(unittest.TestCase):
    """① + ② 组合：真实成交 / 占位 / 无合约窗口。"""

    def test_g1_real_row(self):
        a = hl.classify_values("binance", "ICPUSDT", "12h", ms("2021-07-01T00:00:00Z"),
                               "49.88", "50.80", "44.12", "47.90", "5983639.04")
        self.assertEqual(a["row_kind"], "traded")
        self.assertEqual(a["generation"], "g1")
        self.assertTrue(a["tradeable"])

    def test_gap_padding_row(self):
        a = hl.classify_values("binance", "ICPUSDT", "12h", ms("2022-09-01T00:00:00Z"),
                               "6.44", "6.44", "6.44", "6.44", "0.00")
        self.assertEqual(a["row_kind"], "archive_padding")
        self.assertEqual(a["generation"], "gap")
        self.assertFalse(a["tradeable"])

    def test_g2_real_row(self):
        a = hl.classify_values("binance", "ICPUSDT", "12h", ms("2022-09-27T00:00:00Z"),
                               10.0, 11.0, 9.0, 10.5, 1234.0)
        self.assertEqual(a["row_kind"], "traded")
        self.assertEqual(a["generation"], "g2")
        self.assertTrue(a["tradeable"])

    def test_other_padding_symbol(self):
        a = hl.classify_values("binance", "LITUSDT", "12h", ms("2025-12-01T00:00:00Z"),
                               1, 1, 1, 1, 0.0)
        self.assertEqual(a["row_kind"], "archive_padding")
        self.assertFalse(a["tradeable"])


class TestAnnotateRows(unittest.TestCase):
    def test_meta_counts_and_cross_generation(self):
        rows = [
            {"open_time": ms("2021-07-01T00:00:00Z"), "open": "10", "high": "11",
             "low": "9", "close": "10.5", "volume": "100"},
            {"open_time": ms("2022-06-11T00:00:00Z"), "open": "6.44", "high": "6.44",
             "low": "6.44", "close": "6.44", "volume": "0"},
            {"open_time": ms("2022-09-27T00:00:00Z"), "open": "10", "high": "11",
             "low": "9", "close": "10.5", "volume": "200"},
        ]
        anns, meta = hl.annotate_rows("binance", "ICPUSDT", "12h", rows)
        self.assertEqual(meta["archive_padding_rows"], 1)
        self.assertEqual(meta["non_tradeable_rows"], 1)
        self.assertTrue(meta["cross_generation"])
        self.assertEqual(sorted(meta["generations"]), ["g1", "g2", "gap"])

    def test_split_segments(self):
        rows = [
            {"open_time": ms("2021-07-01T00:00:00Z"), "open": "10", "high": "11",
             "low": "9", "close": "10.5", "volume": "100"},
            {"open_time": ms("2022-09-27T00:00:00Z"), "open": "10", "high": "11",
             "low": "9", "close": "10.5", "volume": "200"},
        ]
        segs = hl.split_segments("binance", "ICPUSDT", "12h", rows)
        keys = [s["generation"] if s else "(未注册)" for s, _r in segs]
        self.assertEqual(keys, ["g1", "g2"])


def _mk_rows(specs):
    return specs


class TestStrategyReadBars(unittest.TestCase):
    """③ 趋势输入侧：占位剔除 + 跨代际限用最新代；库内原行不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "market.sqlite3")
        MarketStore(self.path)
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("INSERT INTO sources(exchange,market,symbol,timeframe,source_url,"
                        "file_path,sha256,imported_at,downloaded_at,first_open,last_close,"
                        "row_count) VALUES('binance','um','ICPUSDT','12h','u','f','abc',1,1,0,0,1)")
            con.commit()
        self._insert("ICPUSDT", "12h", [
            # g1 真实成交
            ("2021-07-01T00:00:00Z", "10", "11", "9", "10.5", "100"),
            ("2021-07-01T12:00:00Z", "10.5", "12", "10", "11.5", "120"),
            # gap 官方零量占位
            ("2022-06-11T00:00:00Z", "6.44", "6.44", "6.44", "6.44", "0"),
            ("2022-09-01T00:00:00Z", "6.44", "6.44", "6.44", "6.44", "0"),
            # g2 真实成交
            ("2022-09-27T00:00:00Z", "10", "11", "9", "10.5", "200"),
            ("2022-09-27T12:00:00Z", "10.5", "12", "10", "11.5", "220"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, symbol, tf, rows):
        with closing(sqlite3.connect(self.path)) as con:
            for iso_t, o, h, low, c, v in rows:
                ot = ms(iso_t)
                ct = ot + (IV12 if tf == "12h" else IV1D) - 1
                con.execute(
                    "INSERT INTO candle_versions(exchange,market,symbol,timeframe,open_time,"
                    "close_time,open,high,low,close,volume,quote_volume,trade_count,"
                    "taker_buy_volume,taker_buy_quote_volume,source_id,available_at,is_closed) "
                    "VALUES('binance','um',?,?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,1)",
                    (symbol, tf, ot, ct, o, h, low, c, v, v, 1, "0", "0"))
                con.execute("INSERT INTO candle_heads VALUES('binance','um',?,?,?,1)",
                            (symbol, tf, ot))
            con.commit()

    def _conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def test_padding_excluded_and_latest_generation_only(self):
        with closing(self._conn()) as con:
            bars, total, life = sr.read_bars(con, "binance", "um", "ICPUSDT", "12h")
        # 库内 6 根；剔除 2 根占位 → 4 根成交；再限用最新代 g2 → 2 根
        self.assertEqual(total, 6)
        self.assertEqual(life["excluded_archive_padding"], 2)
        self.assertEqual(life["excluded_non_tradeable"], 2)
        self.assertTrue(life["cross_generation"])
        self.assertEqual(life["restricted_to_generation"], "g2")
        self.assertEqual(life["dropped_prior_generation_bars"], 2)
        self.assertEqual(len(bars), 2)
        self.assertTrue(all(b.volume > 0 for b in bars))

    def test_allow_cross_generation_keeps_both_but_drops_padding(self):
        with closing(self._conn()) as con:
            bars, _total, life = sr.read_bars(con, "binance", "um", "ICPUSDT", "12h",
                                              allow_cross_generation=True)
        self.assertIsNone(life["restricted_to_generation"])
        self.assertEqual(len(bars), 4)                 # g1 2 根 + g2 2 根
        self.assertEqual(life["excluded_archive_padding"], 2)

    def test_db_rows_untouched(self):
        """读侧隔离不改库：库内 6 行仍在（含占位）。"""
        with closing(self._conn()) as con:
            sr.read_bars(con, "binance", "um", "ICPUSDT", "12h")
        with closing(sqlite3.connect(self.path)) as con:
            n = con.execute("SELECT COUNT(*) FROM candles WHERE symbol='ICPUSDT'").fetchone()[0]
        self.assertEqual(n, 6)

    def test_non_registered_symbol_unaffected(self):
        self._insert("1000PEPEUSDT", "12h", [
            ("2024-01-01T00:00:00Z", "1", "2", "0.5", "1.5", "10"),
        ])
        with closing(self._conn()) as con:
            bars, _t, life = sr.read_bars(con, "binance", "um", "1000PEPEUSDT", "12h")
        self.assertEqual(len(bars), 1)
        self.assertEqual(life["excluded_archive_padding"], 0)
        self.assertFalse(life["cross_generation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
