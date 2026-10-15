#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安聪明钱接入的**离线**验收（**零网络**：全部走注入的假网关 / 假传输）。

夹具 = **真实捕获的 ARXUSDT 响应**（2026-10-07T19:28:32Z，curl 直连
`www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/*`，无鉴权 200）。
夹具正文按**统一网关同口径**解析（`json.loads(raw, parse_float=str)`）——即数值为
**精确十进制字符串**，因此测试同时覆盖「字符串数值 → JSON number」的强制转换。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import smart_money as sm                                     # noqa: E402
from data_hub.gateway import (DeferredRequest, ENDPOINTS, Endpoint,  # noqa: E402
                              PublicGateway, RejectedRequest)

# ── 真实捕获正文（逐字，勿改；与 聪明钱/raw/*_ARXUSDT_20261007T192832Z.json 同源）──
OVERVIEW_RAW = ('{"code":"000000","message":null,"messageDetail":null,"data":'
                '{"symbol":"ARXUSDT","totalPositions":8937697.23200000,"totalTraders":267,'
                '"longShortRatio":10.1743,"longTraders":173,'
                '"longTradersQty":30297307.00000000,"longTradersAvgEntryPrice":0.23026264,'
                '"longWhales":107,"longWhalesQty":29961281.00000000,'
                '"longWhalesAvgEntryPrice":0.22988351,"shortTraders":94,'
                '"shortTradersQty":2977813.00000000,"shortTradersAvgEntryPrice":0.25697324,'
                '"shortWhales":61,"shortWhalesQty":2511523.00000000,'
                '"shortWhalesAvgEntryPrice":0.25843465,"longProfitTraders":53,'
                '"shortProfitTraders":62,"longProfitWhales":28,"shortProfitWhales":45,'
                '"updateTime":1791353989563},"success":true}')
STATS_RAW = ('{"code":"000000","message":null,"messageDetail":null,"data":'
             '{"longTraders":173,"longWhales":107,"longPositions":8137856.66020000,'
             '"longQty":30297307.00000000,"longWhalePositions":8047600.07660000,'
             '"longWhaleQty":29961281.00000000,"shortTraders":94,"shortWhales":61,'
             '"shortPositions":799840.57180000,"shortQty":2977813.00000000,'
             '"shortWhalePositions":674595.07780000,"shortWhaleQty":2511523.00000000},'
             '"success":true}')
DETAILS_RAW = ('{"code":"000000","message":null,"messageDetail":null,"data":['
               '{"timeRange":"ALL","traderName":null,"topTraderId":null,"side":"BUY",'
               '"netPositions":null,"lastTradeTime":1791401219648,"userType":2,"traders":172,'
               '"currentPositions":8137539.71220000,"currentQty":30296127.00000000,'
               '"avgEntryPrice":0.23026501},'
               '{"timeRange":"ALL","traderName":null,"topTraderId":null,"side":"SELL",'
               '"netPositions":null,"lastTradeTime":1791401219262,"userType":2,"traders":93,'
               '"currentPositions":799819.08380000,"currentQty":2977733.00000000,'
               '"avgEntryPrice":0.25697653},'
               '{"timeRange":"ALL","traderName":"爱归零","topTraderId":"5213286915681657088",'
               '"side":"SELL","netPositions":null,"lastTradeTime":1788613930000,"userType":0,'
               '"traders":1,"currentPositions":21.48800000,"currentQty":80.00000000,'
               '"avgEntryPrice":0.13440000},'
               '{"timeRange":"ALL","traderName":"我是真的抄底了",'
               '"topTraderId":"5236720111442798337","side":"BUY","netPositions":null,'
               '"lastTradeTime":1784133309000,"userType":0,"traders":1,'
               '"currentPositions":316.94800000,"currentQty":1180.00000000,'
               '"avgEntryPrice":0.16940678}],"total":4,"success":true}')


def _envelope(raw):
    """与统一网关同口径解析：浮点保留**精确十进制文本**（parse_float=str）。"""
    return json.loads(raw, parse_float=str)


class _FakeStore:
    def clock(self):
        return 1791401300000


class FakeGateway:
    """假网关：返回**真实捕获正文**（已按网关口径解析），并记录调用。"""

    def __init__(self, mapping=None, origin="公共REST"):
        self.mapping = mapping if mapping is not None else {
            "binance.smart_money_overview": _envelope(OVERVIEW_RAW),
            "binance.smart_money_stats": _envelope(STATS_RAW),
            "binance.smart_money_details": _envelope(DETAILS_RAW),
        }
        self.origin = origin
        self.calls = []
        self.store = _FakeStore()

    def get(self, name, params=None):
        self.calls.append((name, dict(params or {})))
        if name not in self.mapping:
            raise RejectedRequest("接口未登记")
        return {"数据": self.mapping[name], "来源": self.origin,
                "请求编号": len(self.calls), "获取时间UTC毫秒": 1791401300000}


class EnvelopeTest(unittest.TestCase):
    def test_success_envelope(self):
        data, err = sm._envelope_data(_envelope(OVERVIEW_RAW))
        self.assertIsNone(err)
        self.assertEqual(data["symbol"], "ARXUSDT")

    def test_data_null_is_not_error(self):
        data, err = sm._envelope_data({"code": "000000", "data": None, "success": True})
        self.assertIsNone(data)
        self.assertIsNone(err)

    def test_business_error_code_is_error(self):
        data, err = sm._envelope_data({"code": "000002", "message": "参数非法", "data": None})
        self.assertIsNone(data)
        self.assertIn("000002", err)

    def test_non_dict_body(self):
        _, err = sm._envelope_data(None)
        self.assertIn("未返回 JSON", err)


class FieldMappingTest(unittest.TestCase):
    """逐字段核验：持仓金额及币种 / 内部占比及分母 / 均价及口径 / 身份 / 时间与新鲜度。"""

    def setUp(self):
        self.p = sm.fetch_smart_money("binance", "ARXUSDT", gw=FakeGateway())

    def test_available_and_raw_passthrough(self):
        self.assertTrue(self.p["available"])
        self.assertEqual(self.p["overview"]["symbol"], "ARXUSDT")
        self.assertEqual(self.p["stats"]["longTraders"], 173)

    def test_positions_amount_ccy_and_source(self):
        # ① 持仓金额及币种：来自 details/stats.*Positions，单位 USDT，**不是**数量
        self.assertAlmostEqual(self.p["long"]["positions_usd"], 8137856.6602)
        self.assertEqual(self.p["long"]["positions_ccy"], "USDT")
        self.assertEqual(self.p["long"]["positions_source"], "details/stats.longPositions")
        self.assertAlmostEqual(self.p["short"]["positions_usd"], 799840.5718)
        self.assertEqual(self.p["short"]["positions_ccy"], "USDT")

    def test_qty_never_written_into_amount(self):
        self.assertNotEqual(self.p["long"]["positions_usd"], self.p["long"]["qty"])
        self.assertNotEqual(self.p["long"]["positions_usd"],
                            self.p["overview"]["longTradersQty"])

    def test_qty_unit_is_base_coin(self):
        self.assertEqual(self.p["long"]["qty_ccy"], "ARX")
        self.assertAlmostEqual(self.p["long"]["qty"], 30297307.0)

    def test_internal_ratio_has_explicit_denominator(self):
        # ② 内部占比及分母：分子=该侧鲸鱼仓位，分母=该侧总持仓额（显式给出）
        self.assertAlmostEqual(self.p["long"]["whale_share_percent"], 98.9)
        den = self.p["long"]["whale_share_denominator"]
        self.assertEqual(den["field"], "details/stats.longPositions")
        self.assertAlmostEqual(den["value"], 8137856.6602)
        self.assertEqual(den["ccy"], "USDT")
        self.assertAlmostEqual(self.p["short"]["whale_share_percent"], 84.3)

    def test_whale_traders_share_denominator_is_count(self):
        self.assertAlmostEqual(self.p["long"]["whale_traders_share_percent"], 61.8)
        self.assertEqual(self.p["long"]["whale_traders_share_denominator"]["unit"], "人")

    def test_avg_entry_price_and_basis(self):
        # ③ 持仓均价及口径：来自 overview.*AvgEntryPrice；加权方式币安未公开 ⇒ 如实标注
        self.assertAlmostEqual(self.p["long"]["avg_entry_price"], 0.23026264)
        self.assertAlmostEqual(self.p["short"]["avg_entry_price"], 0.25697324)
        self.assertIn("未公开", self.p["long"]["avg_entry_price_basis"])
        self.assertEqual(self.p["long"]["avg_entry_price_ccy"], "USDT/ARX")

    def test_long_short_ratio_is_qty_based(self):
        # ④ 币安 longShortRatio 实测 = 数量比（非人数比），并给显式分子分母
        r = self.p["ratios"]["long_short_ratio"]
        self.assertEqual(r["basis"], "qty")
        self.assertAlmostEqual(r["value"], 10.1743)
        self.assertAlmostEqual(r["recomputed_from_qty"], 10.1743, places=3)
        self.assertEqual(r["numerator"]["field"], "overview.longTradersQty")
        self.assertEqual(r["denominator"]["field"], "overview.shortTradersQty")
        self.assertNotAlmostEqual(r["value"], self.p["ratios"]["traders_long_short_ratio"]["value"])

    def test_identity_and_not_substitute(self):
        # ⑤ 标的身份 + 队列身份边界
        self.assertEqual(self.p["symbol"], "ARXUSDT")
        self.assertEqual(self.p["market_id"], "ARXUSDT")
        self.assertIn("undisclosed", self.p["cohort_definition"])
        self.assertIn("全市场持仓", self.p["not_a_substitute_for"])

    def test_three_time_concepts_are_separate(self):
        # ⑥ 三种时间严格分开：交易活动时间 / 上游快照时点(未公开) / 本机取回时点
        self.assertEqual(self.p["update_time_ms"], 1791353989563)
        self.assertEqual(self.p["update_time"], "2026-10-07T06:19:49Z")
        # 交易活动时间（源 lastTradeTime），与 last_trade_time_* 同义
        self.assertEqual(self.p["trade_event_time_ms"], 1791401219648)
        self.assertEqual(self.p["last_trade_time_ms"], self.p["trade_event_time_ms"])
        self.assertEqual(self.p["trade_event_time"], "2026-10-07T19:26:59Z")
        # 上游数据快照时点：币安未公开 ⇒ null（不推断）
        self.assertIsNone(self.p["upstream_snapshot_time"])
        self.assertIsNone(self.p["upstream_snapshot_time_ms"])
        # 本机取回时点独立存在
        self.assertIsNotNone(self.p["local_fetched_at"])
        self.assertEqual(self.p["local_fetched_at"], self.p["fetched_at"])
        # 陈旧判据基于**本机取回时点**，不是 lastTradeTime / updateTime
        self.assertIn("本机取回", self.p["quality"]["basis"])
        self.assertEqual(self.p["quality"]["trade_event_time"], self.p["trade_event_time"])
        self.assertIsNone(self.p["quality"]["upstream_snapshot_time"])
        self.assertNotEqual(self.p["quality"]["event_time"], self.p["update_time"])

    def test_details_split_aggregate_vs_named(self):
        self.assertEqual(self.p["details_split"]["aggregate_rows"], 2)
        self.assertEqual(self.p["details_split"]["named_trader_rows"], 2)
        self.assertEqual(self.p["details_total"], 4)

    def test_numbers_are_json_numbers_and_ids_stay_strings(self):
        self.assertIsInstance(self.p["overview"]["totalPositions"], float)
        self.assertIsInstance(self.p["overview"]["longTraders"], int)
        self.assertIsInstance(self.p["stats"]["longPositions"], float)
        named = [r for r in self.p["details"] if r.get("userType") == 0]
        self.assertEqual(len(named), 2)
        self.assertIsInstance(named[0]["topTraderId"], str)
        self.assertIsInstance(named[0]["currentPositions"], float)

    def test_no_null_reasons_when_full(self):
        self.assertEqual(self.p["null_reasons"], {})


class MissingAndBoundaryTest(unittest.TestCase):
    def test_non_binance_returns_missing_not_substitute(self):
        p = sm.fetch_smart_money("okx", "BTC-USDT", gw=FakeGateway())
        self.assertFalse(p["available"])
        self.assertIn("只覆盖", p["reason"])
        self.assertIsNone(p["overview"])

    def test_invalid_symbol(self):
        p = sm.fetch_smart_money("binance", "../../etc/passwd", gw=FakeGateway())
        self.assertFalse(p["available"])
        self.assertIn("不合法", p["reason"])

    def test_chinese_named_contract_is_valid(self):
        # 币安**存在中文名合约**（实测该 bapi 返回真实数据）⇒ 不得按 ASCII 拒绝
        for s in ("哈基米USDT", "币安人生USDT", "牛来USDT", "龙虾USDT", "我踏马来了USDT"):
            self.assertTrue(sm.valid_symbol(s), s)
        for s in ("../../etc/passwd", "A/B", "A B", "", "x" * 33, "A?b", "A#b", "A.b"):
            self.assertFalse(sm.valid_symbol(s), s)
        p = sm.fetch_smart_money("binance", "哈基米USDT", gw=FakeGateway())
        self.assertTrue(p["available"])
        self.assertEqual(p["symbol"], "哈基米USDT")
        self.assertEqual(p["long"]["positions_ccy"], "USDT")

    def test_invalid_time_range(self):
        p = sm.fetch_smart_money("binance", "ARXUSDT", time_range="1H", gw=FakeGateway())
        self.assertFalse(p["available"])
        self.assertIn("timeRange", p["reason"])

    def test_symbol_without_coverage_returns_null_reason(self):
        gw = FakeGateway(mapping={
            "binance.smart_money_overview": {"code": "000000", "data": None, "success": True},
            "binance.smart_money_stats": {"code": "000000", "data": None, "success": True},
            "binance.smart_money_details": {"code": "000000", "data": None, "success": True},
        })
        p = sm.fetch_smart_money("binance", "NOTCOVERED", gw=gw)
        self.assertFalse(p["available"])
        self.assertIn("data:null", p["reason"])

    def test_upstream_rejection_is_reported(self):
        class Boom(FakeGateway):
            def get(self, name, params=None):
                raise RejectedRequest("该来源已暂停：模拟封禁")
        p = sm.fetch_smart_money("binance", "ARXUSDT", gw=Boom())
        self.assertFalse(p["available"])
        self.assertIn("RejectedRequest", p["reason"])

    def test_zero_or_missing_denominator_gives_null_not_zero(self):
        self.assertIsNone(sm._pct(100, 0))
        self.assertIsNone(sm._pct(100, None))
        self.assertIsNone(sm._pct(None, 100))
        self.assertEqual(sm._pct(50, 200), 25.0)

    def test_only_qty_no_positions_amount_is_null(self):
        gw = FakeGateway(mapping={
            "binance.smart_money_overview": _envelope(
                '{"code":"000000","data":{"symbol":"XUSDT","longTradersQty":999,'
                '"longTradersAvgEntryPrice":1.5},"success":true}'),
            "binance.smart_money_stats": _envelope(
                '{"code":"000000","data":{"longQty":999},"success":true}'),
            "binance.smart_money_details": _envelope('{"code":"000000","data":[],"total":0}'),
        })
        p = sm.fetch_smart_money("binance", "XUSDT", gw=gw)
        self.assertTrue(p["available"])
        self.assertIsNone(p["long"]["positions_usd"])
        self.assertIn("long.positions_usd", p["null_reasons"])
        self.assertAlmostEqual(p["long"]["avg_entry_price"], 1.5)


class FreshnessTest(unittest.TestCase):
    """陈旧判据 = **本机取回时点**；lastTradeTime / updateTime 均不参与。"""

    def test_ok_stale_unknown_by_local_fetch(self):
        now = 1791401300000
        fresh = {"snapshot_refreshed_at_ms": now - 30_000}
        self.assertEqual(sm._freshness(fresh, now=now, stale_after_s=600)["status"], "ok")
        old = {"snapshot_refreshed_at_ms": now - 900_000}
        self.assertEqual(sm._freshness(old, now=now, stale_after_s=600)["status"], "stale")
        self.assertEqual(sm._freshness({}, now=now)["status"], "unknown")

    def test_low_activity_but_freshly_fetched_is_not_stale(self):
        # 关键：交易活动很少（lastTradeTime 很旧）但**本机刚成功取回** ⇒ 不得误判陈旧
        now = 1791401300000
        fr = sm._freshness({"snapshot_refreshed_at_ms": now - 5_000,
                            "trade_event_time_ms": now - 90 * 24 * 3600 * 1000,
                            "update_time_ms": now - 9 * 3600 * 1000}, now=now)
        self.assertEqual(fr["status"], "ok")
        self.assertLessEqual(fr["age_seconds"], 600)
        # 交易活动时间仍如实随附展示
        self.assertIsNotNone(fr["trade_event_time"])

    def test_falls_back_to_local_fetch_time_and_says_so(self):
        now = 1791401300000
        fr = sm._freshness({"local_fetched_at_ms": now - 1000}, now=now)
        self.assertEqual(fr["status"], "ok")
        self.assertIn("本机取回", fr["basis"])

    def test_update_time_and_last_trade_alone_do_not_make_it_fresh(self):
        now = 1791401300000
        self.assertEqual(sm._freshness({"update_time_ms": now}, now=now)["status"], "unknown")
        self.assertEqual(sm._freshness({"last_trade_time_ms": now}, now=now)["status"], "unknown")

    def test_upstream_snapshot_time_is_always_null(self):
        now = 1791401300000
        fr = sm._freshness({"snapshot_refreshed_at_ms": now - 1000}, now=now)
        self.assertIsNone(fr["upstream_snapshot_time"])
        self.assertIn("未公开", fr["upstream_snapshot_time_note"])


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = sm.SNAP_DIR
        sm.SNAP_DIR = Path(self.tmp.name)

    def tearDown(self):
        sm.SNAP_DIR = self._orig
        self.tmp.cleanup()

    def test_write_read_roundtrip_and_no_temp_left(self):
        payload = {"available": True, "symbol": "ARXUSDT", "long": {"positions_usd": 1.0}}
        sm.write("binance", "ARXUSDT", payload)
        got = sm.read("binance", "ARXUSDT")
        self.assertEqual(got["symbol"], "ARXUSDT")
        self.assertIn("snapshot_refreshed_at_ms", got)
        leftovers = [n for n in __import__("os").listdir(sm.SNAP_DIR) if n.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_read_missing_returns_none_not_error(self):
        self.assertIsNone(sm.read("binance", "NOPE"))

    def test_list_and_coverage(self):
        sm.write("binance", "AUSDT", {"available": True})
        sm.write("binance", "BUSDT", {"available": True, "last_trade_time_ms": 1})
        self.assertEqual(sorted(sm.list_symbols()),
                         [("binance", "AUSDT"), ("binance", "BUSDT")])
        cov = sm.coverage()
        self.assertEqual(cov["symbols_on_disk"], 2)
        self.assertEqual(cov["freshness"]["ok"] + cov["freshness"]["stale"]
                         + cov["freshness"]["unknown"], 2)
        # 持续刷新集合与落盘集合**分开**报告
        self.assertIn("continuous_set", cov)
        self.assertIn("collector", cov)

    def test_refresh_snapshot_counts_and_failure_keeps_previous(self):
        gw = FakeGateway()
        stats = sm.refresh_snapshot(gw, ["ARXUSDT"], extra={"mode_run": "test"})
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["mode_run"], "test")
        self.assertTrue(sm.read("binance", "ARXUSDT")["available"])
        # 第二次：上游 data:null ⇒ skipped，**不覆盖**上一份好快照
        gw2 = FakeGateway(mapping={
            "binance.smart_money_overview": {"code": "000000", "data": None, "success": True},
            "binance.smart_money_stats": {"code": "000000", "data": None, "success": True},
            "binance.smart_money_details": {"code": "000000", "data": None, "success": True},
        })
        stats2 = sm.refresh_snapshot(gw2, ["ARXUSDT"])
        self.assertEqual(stats2["skipped"], 1)
        self.assertTrue(sm.read("binance", "ARXUSDT")["available"])


class GatewayBapiCodeTest(unittest.TestCase):
    """共享网关改动的回归：币安 bapi 成功码 `000000` 必须被接受（且仅新增该码）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from data_hub.store import MarketStore
        self.store = MarketStore(Path(self.tmp.name) / "t.sqlite3")
        ENDPOINTS.setdefault("test.bapi", Endpoint(
            "example.invalid", "/bapi/x", frozenset(), frozenset(), 60_000))

    def tearDown(self):
        self.tmp.cleanup()

    def _gw(self, body):
        return PublicGateway(self.store, transport=lambda url, timeout: (
            200, {"content-type": "application/json"}, body), budget_units=1200,
            min_interval_ms=0)

    def test_000000_accepted_and_cached(self):
        gw = self._gw(b'{"code":"000000","data":{"ok":1},"success":true}')
        res = gw.get("test.bapi")
        self.assertEqual(res["来源"], "公共REST")
        self.assertEqual(res["数据"]["code"], "000000")
        self.assertEqual(gw.get("test.bapi")["来源"], "共享缓存")

    def test_real_failure_code_still_rejected(self):
        gw = self._gw(b'{"code":"000002","message":"x","data":null,"success":false}')
        with self.assertRaises(RejectedRequest):
            gw.get("test.bapi")


class EndpointRegistrationTest(unittest.TestCase):
    def test_endpoints_registered_on_binance_bh_host(self):
        for name in ("binance.smart_money_overview", "binance.smart_money_stats",
                     "binance.smart_money_details", "binance.smart_money_list"):
            self.assertIn(name, ENDPOINTS)
            self.assertEqual(ENDPOINTS[name].host, "www.binance.bh")
        self.assertIn("timeRange", ENDPOINTS["binance.smart_money_stats"].required)
        self.assertNotIn("timeRange", ENDPOINTS["binance.smart_money_overview"].required)


class CollectorWiringTest(unittest.TestCase):
    def test_loop_and_symbol_selection_exist(self):
        import realtime_collector as rc
        self.assertTrue(callable(rc.smart_money_loop))
        uni = {"binance_coins": ["BTCUSDT", "ETHUSDT", "AAVEUSDT"], "binance_index": []}
        head = rc.smart_money_symbols(uni, n=2)
        self.assertEqual(head[:2], ["AAVEUSDT", "BTCUSDT"])   # ASCII 升序前 2
        self.assertIn("ETHUSDT", head)                        # 主流清单并入
        orig = rc.SMART_MONEY_FULL_POOL
        try:
            rc.SMART_MONEY_FULL_POOL = True
            self.assertEqual(rc.smart_money_symbols(uni), ["AAVEUSDT", "BTCUSDT", "ETHUSDT"])
        finally:
            rc.SMART_MONEY_FULL_POOL = orig


class ApiRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = sm.SNAP_DIR
        sm.SNAP_DIR = Path(self.tmp.name)

    def tearDown(self):
        sm.SNAP_DIR = self._orig
        self.tmp.cleanup()

    def test_route_returns_missing_reason_without_snapshot(self):
        import adapter
        body, status = adapter.route_v1("/api/v1/smart-money/binance/ARXUSDT", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertIn("不触发任何交易所请求", body["reason"])

    def test_route_reads_snapshot_and_exposes_frontend_contract(self):
        import adapter
        sm.write("binance", "ARXUSDT", sm.fetch_smart_money("binance", "ARXUSDT",
                                                            gw=FakeGateway()))
        body, status = adapter.route_v1("/api/v1/smart-money/binance/ARXUSDT", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        # 前端既有契约：顶层 overview / stats 可直接消费
        self.assertEqual(body["overview"]["symbol"], "ARXUSDT")
        self.assertIsInstance(body["overview"]["totalPositions"], float)
        self.assertIsInstance(body["stats"]["longPositions"], float)
        self.assertIn("snapshot", body)
        self.assertEqual(body["snapshot"]["written_by"].split(" → ")[0],
                         "realtime_collector.smart_money_loop")

    def test_stale_threshold_is_never_relaxed_by_full_pool(self):
        """陈旧阈值**固定** 600s（=采集周期×2）：full_pool 索引也**不放宽**（不掩盖停摆）。"""
        import adapter
        now = sm.now_ms()
        sm.write("binance", "ARXUSDT", {"available": True, "symbol": "ARXUSDT"})
        # 落盘时点 = 现在 ⇒ ok，阈值恒为 600
        for fp in (True, False):
            sm.write_index({"full_pool": fp, "targets": 526 if fp else 60})
            body, _ = adapter.route_v1("/api/v1/smart-money/binance/ARXUSDT", {})
            self.assertEqual(body["quality"]["stale_after_s"], 600)
            self.assertEqual(body["quality"]["status"], "ok")
        # 本机取回时点很旧 ⇒ 无论 full_pool 与否都判 stale
        sm.write("binance", "ARXUSDT", {"available": True, "symbol": "ARXUSDT"},
                 refreshed_at_ms=now - 1_200_000)
        for fp in (True, False):
            sm.write_index({"full_pool": fp, "targets": 526 if fp else 60})
            body, _ = adapter.route_v1("/api/v1/smart-money/binance/ARXUSDT", {})
            self.assertEqual(body["quality"]["stale_after_s"], 600)
            self.assertEqual(body["quality"]["status"], "stale")

    def test_route_exposes_collector_liveness_separately(self):
        """逐标的新鲜度与「采集器是否在推进」分开报告。"""
        import adapter
        sm.write("binance", "ARXUSDT", {"available": True, "symbol": "ARXUSDT"})
        sm.write_index({"full_pool": False, "targets": 60, "ok": 60})
        body, _ = adapter.route_v1("/api/v1/smart-money/binance/ARXUSDT", {})
        self.assertEqual(body["snapshot"]["collector"]["status"], "ok")
        self.assertIn("last_round_at", body["snapshot"]["collector"])

    def test_missing_snapshot_reports_data_null_evidence_when_queried(self):
        """本轮已查询但上游 data:null 的标的 ⇒ 明确写「当次无覆盖、非永久结论」。"""
        import adapter
        sm.write_index({"full_pool": True, "targets": 526, "skipped": 3,
                        "skipped_symbols": ["binance:PROMPTUSDT"],
                        "failures": ["binance:PROMPTUSDT → 币安聪明钱接口对该合约返回 `data:null`"]})
        body, _ = adapter.route_v1("/api/v1/smart-money/binance/PROMPTUSDT", {})
        self.assertFalse(body["available"])
        self.assertIn("该次无覆盖", body["reason"])
        self.assertIn("不是永久结论", body["reason"])
        # 未被本轮集合覆盖的标的 ⇒ 说法不同（不得冒充「无覆盖」）
        body2, _ = adapter.route_v1("/api/v1/smart-money/binance/NOTINROUND", {})
        self.assertFalse(body2["available"])
        self.assertIn("未覆盖", body2["reason"])
        self.assertNotIn("该次无覆盖", body2["reason"])

    def test_skipped_symbols_recorded_in_index(self):
        """`refresh_snapshot` 把本轮 data:null 的标的记进索引（当次无覆盖证据）。"""
        tmp = tempfile.TemporaryDirectory()
        orig = sm.SNAP_DIR
        sm.SNAP_DIR = Path(tmp.name)
        try:
            gw = FakeGateway(mapping={
                "binance.smart_money_overview": {"code": "000000", "data": None, "success": True},
                "binance.smart_money_stats": {"code": "000000", "data": None, "success": True},
                "binance.smart_money_details": {"code": "000000", "data": None, "success": True},
            })
            stats = sm.refresh_snapshot(gw, ["PROMPTUSDT"])
            self.assertEqual(stats["skipped_symbols"], ["binance:PROMPTUSDT"])
            self.assertEqual(sm.read_index()["skipped_symbols"], ["binance:PROMPTUSDT"])
        finally:
            sm.SNAP_DIR = orig
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
