# -*- coding: utf-8 -*-
"""12h **UTC 边界一致性**测试（离线；零网络；临时库/临时目录）。

对应 2026-10-07 用户独占授权的最小改动（**仅 12h，不动其他周期**）：

  根因：OKX 12h 曾用 `candle12H`（UTC+8 开盘，UTC 锚点 04:00/16:00），与币安 12h
        以及历史补齐脚本用的 `12Hutc`（UTC 00:00/12:00）边界不一致，且
        `data_hub/gateway.py` 的 `okx.candles` 白名单只接受 `12Hutc` —— 传 `12H`
        会被 RejectedRequest 拒绝，导致 OKX 12h 尾段补缺静默失败、旧混边界行持续产生。

  修正：
    ① realtime_collector.OKX_CANDLE_CHANNEL["12h"]  = "candle12Hutc"
    ② realtime_collector.OKX_REST_BAR["12h"]        = "12Hutc"
    ③ realtime_collector._expected_closed_open(okx,12h) 不再加 4h 偏移（= UTC 00/12）
    ④ adapter._OKX_BAR["12h"]                       = "12Hutc"
    ⑤ 读侧隔离 adapter._utc_boundary_filter：OKX 12h 非 UTC 对齐旧行不进 UTC 序列
       （**库内原行保留、不删除**）

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_12h_utc_boundary -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 后端实现/
PROJECT = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
for _p in (str(HERE), PROJECT, str(HERE / "logs")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 导入 adapter 前把库指到临时路径，避免任何模块级连接碰到正式行情库
_TMP = tempfile.mkdtemp(prefix="t12h_")
os.environ.setdefault("MARKET_DB", os.path.join(_TMP, "market.sqlite3"))
os.environ["MARKET_TAIL_FETCH"] = "0"                  # 本测试绝不触发外部采集

import realtime_collector as rc                        # noqa: E402
import adapter as ad                                   # noqa: E402
from data_hub.gateway import PublicGateway, RejectedRequest   # noqa: E402

IV12 = 12 * 3600 * 1000


class TestOkx12hMapping(unittest.TestCase):
    """① ② ④：12h 频道/bar 必须落在官方 UTC+0 口径。"""

    def test_ws_channel_is_candle12hutc(self):
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["12h"], "candle12Hutc")

    def test_rest_bar_is_12hutc(self):
        self.assertEqual(rc.OKX_REST_BAR["12h"], "12Hutc")

    def test_adapter_okx_bar_is_12hutc(self):
        self.assertEqual(ad._OKX_BAR["12h"], "12Hutc")

    def test_other_periods_unchanged(self):
        """回归守卫：只改 12h，其他周期一字不动。"""
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["1d"], "candle1Dutc")
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["4h"], "candle4H")
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["1h"], "candle1H")
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["30m"], "candle30m")
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["15m"], "candle15m")
        self.assertEqual(rc.OKX_CANDLE_CHANNEL["5m"], "candle5m")
        self.assertNotIn("1m", rc.OKX_CANDLE_CHANNEL)      # 1m 已于 2026-10-11 彻底删除
        self.assertNotIn("8h", rc.OKX_CANDLE_CHANNEL)      # OKX 无原生 8h：不合成
        self.assertEqual(ad._OKX_BAR["1d"], "1Dutc")
        self.assertEqual(ad._OKX_BAR["4h"], "4H")
        self.assertEqual(ad._OKX_BAR["1h"], "1H")
        self.assertNotIn("8h", ad._OKX_BAR)


class TestExpectedClosedOpen(unittest.TestCase):
    """③：OKX 12h 闭合锚点 = UTC 00:00/12:00，与币安 12h 完全一致。"""

    # 2026-10-07T12:35:46Z（本机实测时刻）
    NOW = 1791376546000
    WANT_12H = 1791331200000       # 2026-10-07T00:00:00Z

    def test_okx_12h_equals_binance_12h(self):
        okx = rc._expected_closed_open(self.NOW, "12h", "okx")
        bnc = rc._expected_closed_open(self.NOW, "12h", "binance")
        self.assertEqual(okx, bnc)
        self.assertEqual(okx, self.WANT_12H)

    def test_okx_12h_is_utc_aligned(self):
        okx = rc._expected_closed_open(self.NOW, "12h", "okx")
        self.assertEqual(okx % IV12, 0)
        # 旧锚点 04:00Z 必须**不再**出现
        self.assertNotEqual(okx % IV12, 4 * 3600 * 1000)

    def test_other_tf_unchanged_for_both_exchanges(self):
        for tf in ("1d", "4h", "1h", "30m", "15m", "5m"):
            self.assertEqual(rc._expected_closed_open(self.NOW, tf, "okx"),
                             rc._expected_closed_open(self.NOW, tf, "binance"), tf)


class TestGatewayWhitelist(unittest.TestCase):
    """网关白名单：`okx.candles` 接受 12Hutc、拒绝 12H（根因证据）。"""

    def setUp(self):
        # _request 只做登记/参数校验，不触网；store 不参与
        self.gw = PublicGateway(None, budget_units=1200, min_interval_ms=250)

    def test_12hutc_accepted(self):
        spec, canonical, _enc, _key = self.gw._request(
            "okx.candles", {"instId": "CRO-USDT-SWAP", "bar": "12Hutc"})
        self.assertEqual(canonical["bar"], "12Hutc")
        self.assertEqual(spec.path, "/api/v5/market/history-candles")

    def test_plain_12h_rejected(self):
        with self.assertRaises(RejectedRequest):
            self.gw._request("okx.candles",
                             {"instId": "CRO-USDT-SWAP", "bar": "12H"})

    def test_candle_prefixed_bar_rejected(self):
        """REST 的 bar 不带 candle 前缀：传 candle12Hutc 应被拒绝。"""
        with self.assertRaises(RejectedRequest):
            self.gw._request("okx.candles",
                             {"instId": "CRO-USDT-SWAP", "bar": "candle12Hutc"})


class TestReadSideIsolation(unittest.TestCase):
    """⑤：读侧隔离 OKX 12h 非 UTC 对齐旧行，但**不删除**（返回 kept 原行）。"""

    def _rows(self, opens):
        return [{"open_time": o, "close_time": o + IV12 - 1} for o in opens]

    def test_okx12h_offboundary_isolated(self):
        base = 1791331200000                       # 2026-10-07T00:00Z（对齐）
        off = base + 4 * 3600 * 1000               # 04:00Z（旧 UTC+8 锚点）
        rows = self._rows([base - IV12, off, base])
        kept, iso = ad._utc_boundary_filter(rows, "okx", "12h")
        self.assertEqual(iso, 1)
        self.assertEqual([r["open_time"] for r in kept], [base - IV12, base])
        # 原行仍在（过滤只影响返回值，不 mutate 输入）
        self.assertEqual(len(rows), 3)

    def test_non_okx_or_other_tf_passthrough(self):
        base = 1791331200000
        rows = self._rows([base + 4 * 3600 * 1000])
        kept, iso = ad._utc_boundary_filter(rows, "binance", "12h")
        self.assertEqual((len(kept), iso), (1, 0))
        kept, iso = ad._utc_boundary_filter(rows, "okx", "4h")
        self.assertEqual((len(kept), iso), (1, 0))

    def test_all_aligned_untouched(self):
        base = 1791331200000
        rows = self._rows([base - IV12, base])
        kept, iso = ad._utc_boundary_filter(rows, "okx", "12h")
        self.assertEqual((len(kept), iso), (2, 0))


class TestTrendReadSide(unittest.TestCase):
    """趋势读取（strategy_runner.read_bars）与 bar 缺值语义（离线临时库）。

    对应 2026-10-07 独立验收退回第 2 项：
      - `strategies/common.from_row` 把**空字符串**视为缺值（None），
        不再 `float('')` 抛 ValueError（OKX 12h 的 taker_buy_volume 常为空串）。
      - `strategy_runner.read_bars` 读 OKX 12h 时**隔离非 UTC 对齐旧行**（保留库内原行）。
    """

    def _mkdb(self):
        from strategies import common
        con = sqlite3.connect(":memory:")
        # 测试夹具自清理：本连接为 :memory: 一次性库，用后即关，
        # 避免 ResourceWarning: unclosed database（仅测试噪声，无生产影响）。
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE candles(exchange TEXT, market TEXT, symbol TEXT, timeframe TEXT, "
            "open_time INTEGER, close_time INTEGER, is_closed INTEGER, open TEXT, high TEXT, "
            "low TEXT, close TEXT, volume TEXT, quote_volume TEXT, trade_count INTEGER, "
            "taker_buy_volume TEXT, taker_buy_quote_volume TEXT, available_at INTEGER, "
            "source_id INTEGER)")
        return con, common

    def _ins(self, con, ex, mk, sym, tf, ot, taker):
        con.execute(
            "INSERT INTO candles(exchange,market,symbol,timeframe,open_time,close_time,is_closed,"
            "open,high,low,close,volume,quote_volume,trade_count,taker_buy_volume,"
            "taker_buy_quote_volume,available_at,source_id) VALUES "
            "(?,?,?,?,?,?,1,'1','2','0.5','1.5','10','15',3,?,?,'15',NULL)",
            (ex, mk, sym, tf, ot, ot + IV12 - 1, taker, "7"))
        con.commit()

    def test_from_row_empty_string_is_none(self):
        """空字符串缺值 → None，绝不 float('') 抛错、也不用 0 冒充。"""
        _con, common = self._mkdb()
        row = {"open_time": 1, "close_time": 2, "open": "1", "high": "2", "low": "0.5",
               "close": "1.5", "volume": "10", "quote_volume": "15", "trade_count": 3,
               "taker_buy_volume": "", "taker_buy_quote_volume": "", "source_id": None}
        bar = common.from_row(row)
        self.assertIsNone(bar.taker_buy_volume)
        self.assertIsNone(bar.taker_buy_quote_volume)
        self.assertEqual(bar.close, 1.5)

    def test_read_bars_okx12h_isolates_non_utc(self):
        """OKX 12h 非 UTC 对齐旧行被隔离，库内原行不动；币安 12h 不受影响。"""
        import strategy_runner as sr
        con, _ = self._mkdb()
        base = 1791331200000                      # 2026-10-07T00:00Z（对齐）
        off = base + 4 * 3600 * 1000              # 04:00Z（旧 UTC+8 锚点）
        for o in (base - IV12, off, base):
            self._ins(con, "okx", "swap", "CRO-USDT-SWAP", "12h", o, "")
        bars, total, life = sr.read_bars(con, "okx", "swap", "CRO-USDT-SWAP", "12h")
        self.assertEqual(total, 3)                          # 范围内原始 3 根
        self.assertEqual(len(bars), 2)                      # 隔离后 2 根
        self.assertEqual(life["excluded_non_utc_12h"], 1)   # 如实计数
        self.assertTrue(all(b.open_time % IV12 == 0 for b in bars))
        # 库内原行仍在（读侧只影响返回，不删行）
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM candles WHERE exchange='okx' AND timeframe='12h'"
        ).fetchone()[0], 3)

    def test_read_bars_binance_untouched(self):
        import strategy_runner as sr
        con, _ = self._mkdb()
        base = 1791331200000
        off = base + 4 * 3600 * 1000
        for o in (base - IV12, off, base):
            self._ins(con, "binance", "um", "1000PEPEUSDT", "12h", o, "")
        bars, total, life = sr.read_bars(con, "binance", "um", "1000PEPEUSDT", "12h")
        self.assertEqual(len(bars), 3)                      # 币安不隔离
        self.assertEqual(life["excluded_non_utc_12h"], 0)

    def test_read_bars_okx_other_tf_untouched(self):
        import strategy_runner as sr
        con, _ = self._mkdb()
        base = 1791331200000
        off = base + 4 * 3600 * 1000
        for o in (base - IV12, off, base):
            self._ins(con, "okx", "swap", "CRO-USDT-SWAP", "4h", o, "")
        bars, total, life = sr.read_bars(con, "okx", "swap", "CRO-USDT-SWAP", "4h")
        self.assertEqual(len(bars), 3)                      # 仅 12h 生效，其他周期原样
        self.assertEqual(life["excluded_non_utc_12h"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
