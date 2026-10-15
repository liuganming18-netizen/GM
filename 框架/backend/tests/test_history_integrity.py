# -*- coding: utf-8 -*-
"""历史下载**整改四项** + **二次退回两项**的故障测试（离线；临时库/临时目录；零网络）。

对应用户 2026-10-07 独立验收退回的四项：
  #1 范围缓存失效与身份/周期验证  → TestRangeCacheValidation
  #2 预演（--local-only）严格只读   → TestReadonlyDryRun
  #3 pipeline 状态协议 + 结构性无进展立即停 → TestPipelineGates / TestGwRetry
  #4 原响应总空间保护 + 失败区分 + 导入↔正文证据关联 → TestRawSpaceAndLinkage

对应用户 2026-10-07 **独立验收二次退回**（隔离复现）：
  #4b **总目录空间核算漏计**：cap=10B、正文=10B 实际落盘 598B（漏计 meta/关联文件）
      → TestRawBudgetAccounting（精确足迹：正文+元数据+sidecar；到量/连续/重启/低空间）
  #5  **无正文证据仍可导入**：空 evidence 仍新增 1 根 → TestEvidenceBlocking
      （空 evidence / 缺 body_sha256 / 关联写入失败 / 到量未留档 ⇒ 拒绝提交并阻塞验收）

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_history_integrity -v
"""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent          # 后端实现/
PROJECT = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
for _p in (str(HERE), PROJECT, str(HERE / "logs")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _pool_restfill as pr                              # noqa: E402
import _pool_pipeline as pp                              # noqa: E402

IV12 = 12 * 3600 * 1000

SCHEMA_SQL = """
CREATE TABLE symbol_pool(exchange TEXT, market TEXT, symbol TEXT, status TEXT);
CREATE TABLE candle_heads(exchange TEXT, market TEXT, symbol TEXT, timeframe TEXT,
    open_time INTEGER, source_id INTEGER,
    PRIMARY KEY(exchange,market,symbol,timeframe,open_time));
CREATE TABLE candle_versions(exchange TEXT, market TEXT, symbol TEXT, timeframe TEXT,
    open_time INTEGER, close_time INTEGER, open TEXT, high TEXT, low TEXT, close TEXT,
    volume TEXT, quote_volume TEXT, trade_count INTEGER, taker_buy_volume TEXT,
    taker_buy_quote_volume TEXT, source_id INTEGER, available_at INTEGER, is_closed INTEGER,
    PRIMARY KEY(exchange,market,symbol,timeframe,open_time,source_id));
CREATE TABLE sources(id INTEGER PRIMARY KEY, exchange TEXT, market TEXT, symbol TEXT,
    timeframe TEXT, source_url TEXT, file_path TEXT, sha256 TEXT, imported_at INTEGER,
    downloaded_at INTEGER, first_open INTEGER, last_close INTEGER, row_count INTEGER,
    UNIQUE(exchange,market,symbol,timeframe,sha256));
CREATE TABLE coverage_cache(exchange TEXT, market TEXT, symbol TEXT, timeframe TEXT,
    rows INTEGER, first_open INTEGER, last_close INTEGER, expected INTEGER, missing INTEGER,
    updated_at INTEGER, PRIMARY KEY(exchange,market,symbol,timeframe));
"""


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def make_temp_db(path, symbols, heads=None):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    for s in symbols:
        con.execute("INSERT INTO symbol_pool VALUES ('binance','um',?,'TRADING')", (s,))
    for (sym, tf, opens) in (heads or []):
        for o in opens:
            con.execute("INSERT INTO candle_heads VALUES ('binance','um',?,?,?,1)",
                        (sym, tf, o))
    con.commit()
    con.close()


class TestRangeCacheValidation(unittest.TestCase):
    """退回 #1：错误周期 / 过期缓存 / 错误身份必须被拒；有效缓存被接受。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_cache = pr.CACHE_DIR
        self._old_db = pr.DB
        pr.CACHE_DIR = Path(self.tmp.name)
        pr.DB = str(Path(self.tmp.name) / "market.sqlite3")
        self.addCleanup(self._restore)

    def _restore(self):
        pr.CACHE_DIR = self._old_cache
        pr.DB = self._old_db

    def _write(self, tf, obj):
        p = pr.CACHE_DIR / f"pool_range_cache_{tf}.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return p

    def _envelope(self, tf, identity=None, gen=None):
        return {"cache_format": pr.CACHE_FORMAT, "cache_version": pr.CACHE_VERSION,
                "tf": tf, "identity": identity or pr.range_cache_identity(tf),
                "generated_utc": gen or pr.iso(pr.now_ms()), "symbols": {}}

    def _entry(self, **kw):
        e = {"rest_first": "2020-01-01T00:00:00Z", "rest_last_raw": None,
             "probed_at": pr.iso(pr.now_ms()), "basis": "probe"}
        e.update(kw)
        return e

    def test_valid_cache_accepted(self):
        c = self._envelope("12h")
        c["symbols"]["binance:XUSDT"] = self._entry()
        self._write("12h", c)
        out, diag = pr.load_range_cache("12h")
        self.assertTrue(diag["valid"], diag)
        self.assertEqual(len(out["symbols"]), 1)

    def test_wrong_timeframe_rejected(self):
        c = self._envelope("1d")                     # 错误周期：写成 1d
        c["symbols"]["binance:XUSDT"] = self._entry()
        self._write("12h", c)                        # 放在 12h 文件名下
        out, diag = pr.load_range_cache("12h")
        self.assertFalse(diag["valid"])
        self.assertEqual(out["symbols"], {})
        self.assertIn("cache_timeframe_mismatch", diag["invalidated"])

    def test_stale_cache_rejected(self):
        c = self._envelope("12h", gen="2000-01-01T00:00:00Z")
        c["symbols"]["binance:XUSDT"] = self._entry()
        self._write("12h", c)
        out, diag = pr.load_range_cache("12h")
        self.assertFalse(diag["valid"])
        self.assertEqual(out["symbols"], {})
        self.assertIn("cache_expired", diag["invalidated"])

    def test_wrong_identity_rejected(self):
        c = self._envelope("12h", identity="wrong-generation")
        c["symbols"]["binance:XUSDT"] = self._entry()
        self._write("12h", c)
        out, diag = pr.load_range_cache("12h")
        self.assertFalse(diag["valid"])
        self.assertEqual(out["symbols"], {})
        self.assertIn("cache_identity_mismatch", diag["invalidated"])

    def test_format_version_mismatch_rejected(self):
        c = self._envelope("12h")
        c["cache_version"] = 1
        c["symbols"]["binance:XUSDT"] = self._entry()
        self._write("12h", c)
        _, diag = pr.load_range_cache("12h")
        self.assertFalse(diag["valid"])
        self.assertIn("cache_format_or_version_mismatch", diag["invalidated"])

    def test_entry_level_stale_dropped(self):
        c = self._envelope("12h")
        c["symbols"]["binance:XUSDT"] = self._entry(probed_at="2000-01-01T00:00:00Z")
        c["symbols"]["binance:YUSDT"] = self._entry()
        self._write("12h", c)
        out, diag = pr.load_range_cache("12h")
        self.assertTrue(diag["valid"])               # 信封有效
        self.assertEqual(list(out["symbols"]), ["binance:YUSDT"])
        self.assertIn("binance:XUSDT", diag["entries_dropped"])

    def test_entry_bad_order_dropped(self):
        c = self._envelope("12h")
        c["symbols"]["binance:XUSDT"] = self._entry(rest_first="2021-01-01T00:00:00Z",
                                                    rest_last_raw="2020-01-01T00:00:00Z")
        self._write("12h", c)
        out, diag = pr.load_range_cache("12h")
        self.assertEqual(out["symbols"], {})
        self.assertIn("binance:XUSDT", diag["entries_dropped"])

    def test_evidence_bound_entry_verified_then_tampered(self):
        ev = Path(self.tmp.name) / "evidence.json"
        ev.write_text(json.dumps({"generated_utc": pr.iso(pr.now_ms()),
                                  "symbols": {"binance:XUSDT": {}}}), encoding="utf-8")
        esha = sha256_bytes(ev.read_bytes())
        c = self._envelope("12h")
        c["symbols"]["binance:XUSDT"] = self._entry(
            basis="independent_acceptance_evidence", evidence_file=str(ev),
            evidence_sha256=esha)
        self._write("12h", c)
        out, diag = pr.load_range_cache("12h")
        self.assertEqual(list(out["symbols"]), ["binance:XUSDT"], diag)   # 证据一致 → 保留
        # 篡改证据文件 → 条目失效
        ev.write_text(json.dumps({"generated_utc": pr.iso(pr.now_ms()),
                                  "symbols": {"binance:XUSDT": {}}, "x": 1}), encoding="utf-8")
        out2, diag2 = pr.load_range_cache("12h")
        self.assertEqual(out2["symbols"], {})
        self.assertIn("binance:XUSDT", diag2["entries_dropped"])


class TestReadonlyDryRun(unittest.TestCase):
    """退回 #2：--local-only 预演不得改正式库/断点；无有效证据不得跳过未验前段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir()
        self.raw = self.base / "raw"
        self.raw.mkdir()
        self.db = self.base / "market.sqlite3"
        now = pr.now_ms()
        last_closed = (now // IV12) * IV12 - IV12
        opens = [last_closed - i * IV12 for i in range(5)][::-1]
        make_temp_db(self.db, ["AAAUSDT", "BBBUSDT"],
                     heads=[("AAAUSDT", "12h", opens), ("BBBUSDT", "12h", opens)])
        self.opens = opens
        self.last_closed = last_closed
        self._old = (pr.DB, pr.STATE_DIR, pr.CACHE_DIR, pr.RAW_DIR)
        pr.DB = str(self.db)
        pr.STATE_DIR = self.state
        pr.CACHE_DIR = self.cache
        pr.RAW_DIR = self.raw
        self.addCleanup(self._restore)
        # 夹具隔离（2026-10-09 修复）：**关闭活动池范围过滤**。
        # `_pool_scope.SCOPE_JSON` 在**导入期**由 env 固化（事后改 env 无效），默认指向真实的
        # `历史下载/标的池/scope_crypto.json`（币安 526 个加密币）⇒ 本类夹具的合成标的
        # `AAAUSDT`/`BBBUSDT` 会被 `_pool_restfill` 全部过滤掉 ⇒ 目标=0 ⇒ 门禁无阻塞项 ⇒
        # 假返回 0（旧契约「--local-only 恒 0」被退回的缺陷被掩盖）。
        # 指向**不存在的路径** = `logs/_pool_scope.py` 模块文档约定的「不过滤」隔离路径
        # （见其行 15：缺失/不可解析 ⇒ 返回 None，仅供隔离测试）。
        self._old_scope = pr._pool_scope.SCOPE_JSON
        pr._pool_scope.SCOPE_JSON = self.base / "no_such_scope.json"
        self.addCleanup(self._restore_scope)

    def _restore(self):
        pr.DB, pr.STATE_DIR, pr.CACHE_DIR, pr.RAW_DIR = self._old

    def _restore_scope(self):
        pr._pool_scope.SCOPE_JSON = self._old_scope

    def _run_local_only(self, extra=None):
        rep = self.base / "out.json"
        # 夹具自洽化（2026-10-11）：`--local-only` 为零网络预演，**不落盘**，
        # 与宿主真实剩余空间无关；但 `--min-free-gb` 的**默认值**会按宿主磁盘暂停，
        # 使退出码随环境变化（当时默认 30.0，本机 15m 下载使剩余 <30GB ⇒ 假返回 3）。
        # 注：该默认值已于 2026-10-11 由 30.0 降为 12.0
        # （见 logs/_pool_restfill.py:1016）；空间门禁本身由 `test_low_space_skipped` 覆盖。
        argv = ["_pool_restfill.py", "--tf", "12h", "--exchange", "binance", "--local-only",
                "--min-free-gb", "0", "--report", str(rep), "--use-range-cache", "1"]
        argv += (extra or [])
        with mock.patch.object(sys, "argv", argv):
            rc = pr.main()
        return rc, json.loads(rep.read_text(encoding="utf-8"))

    def test_readonly_no_db_or_state_change_and_no_skip_without_evidence(self):
        before_db = sha256_bytes(self.db.read_bytes())
        rc, rep = self._run_local_only()
        # 2026-10-08 独立验收退回 #1：预演退出码**不再恒 0**。
        # 全部标的处于「待范围证据」（未验官方前段）⇒ 存在未决阻塞项 ⇒ 返回 4（fail）。
        # 旧契约只按 deadline 返回非零，把 needs_range_evidence 当成功，属被退回的缺陷。
        self.assertEqual(rc, 4)
        self.assertEqual(rep["gate"]["status"], "fail")
        self.assertIn("needs_range_evidence", " ".join(rep["gate"]["blocking"]))
        self.assertTrue(rep["readonly"])
        self.assertEqual(rep["db_open_mode"], "ro+query_only")
        self.assertFalse(rep["state_written"])
        self.assertEqual(rep["totals"]["gateway"]["ok"], 0)          # 零网络
        self.assertEqual(rep["totals"]["rows_added"], 0)             # 零导入
        # 无有效范围证据 ⇒ 全部单列「待范围证据」，**不得**冒充本地已齐跳过
        self.assertEqual(rep["totals"]["skipped_local_complete"], 0)
        self.assertEqual(rep["totals"]["needs_range_evidence"], 2)
        for rec in rep["symbols"].values():
            self.assertEqual(rec["status"], "needs_range_evidence")
        # 正式库 / 断点：字节不变、无新文件
        self.assertEqual(before_db, sha256_bytes(self.db.read_bytes()))
        self.assertFalse((self.state / "12h.json").exists())
        self.assertEqual(list(self.state.iterdir()), [])

    def test_evidence_seed_allows_skip_and_keeps_db_unchanged(self):
        ev = self.base / "ev.json"
        first = pr.iso(self.opens[0])
        ev.write_text(json.dumps({
            "generated_utc": pr.iso(pr.now_ms()),
            "symbols": {"binance:AAAUSDT": {"rest_target_start": first},
                        "binance:BBBUSDT": {"rest_target_start": first}}}),
            encoding="utf-8")
        before_db = sha256_bytes(self.db.read_bytes())
        rc, rep = self._run_local_only(extra=["--evidence", str(ev)])
        self.assertEqual(rc, 0)
        self.assertTrue(rep["range_cache_diag"]["valid"], rep["range_cache_diag"])
        self.assertEqual(rep["totals"]["skipped_local_complete"], 2)
        self.assertEqual(rep["totals"]["needs_range_evidence"], 0)
        self.assertEqual(before_db, sha256_bytes(self.db.read_bytes()))
        self.assertFalse((self.state / "12h.json").exists())


class TestPipelineGates(unittest.TestCase):
    """退回 #3：状态协议一致（skipped_local_complete=ok，needs_fill/needs_range_evidence 阻塞）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _fill(self, symbols):
        p = self.dir / "fill.json"
        p.write_text(json.dumps({"targets_total": len(symbols), "symbols": symbols,
                                 "totals": {"failed": 0, "remaining": 0,
                                            "processed": len(symbols),
                                            "symbols_ok": sum(
                                                1 for v in symbols.values()
                                                if v["status"] in pp.FILL_OK_STATUS),
                                            "deadline_hit": False}}), encoding="utf-8")
        return p

    def test_skipped_local_complete_is_ok(self):
        pool = {"binance:AUSDT", "binance:BUSDT"}
        syms = {"binance:AUSDT": {"status": "skipped_local_complete"},
                "binance:BUSDT": {"status": "ok"}}
        with mock.patch.object(pp, "real_pool_keys", return_value=(pool, "")):
            ok, detail = pp.check_fill(self._fill(syms), 0)
        self.assertTrue(ok, detail)

    def test_needs_fill_blocks(self):
        pool = {"binance:AUSDT", "binance:BUSDT"}
        syms = {"binance:AUSDT": {"status": "skipped_local_complete"},
                "binance:BUSDT": {"status": "needs_fill"}}
        with mock.patch.object(pp, "real_pool_keys", return_value=(pool, "")):
            ok, detail = pp.check_fill(self._fill(syms), 0)
        self.assertFalse(ok)
        self.assertIn("needs_fill", detail)

    def test_needs_range_evidence_blocks(self):
        pool = {"binance:AUSDT"}
        syms = {"binance:AUSDT": {"status": "needs_range_evidence"}}
        with mock.patch.object(pp, "real_pool_keys", return_value=(pool, "")):
            ok, detail = pp.check_fill(self._fill(syms), 0)
        self.assertFalse(ok)
        self.assertIn("needs_range_evidence", detail)

    def test_no_progress_signature_identical_for_same_failure(self):
        self.assertEqual(pp.attempt_signature("fill", "存在 23 条非 ok 状态"),
                         pp.attempt_signature("fill", "存在 23 条非 ok 状态"))
        self.assertNotEqual(pp.attempt_signature("fill", "a"),
                            pp.attempt_signature("fill", "b"))
        self.assertNotEqual(pp.attempt_signature("fill", "x"),
                            pp.attempt_signature("verify", "x"))

    def test_needs_evidence_blocks(self):
        """二次退回 #5：逐币 needs_evidence（缺正文证据/关联失败）必须阻塞放行。"""
        self.assertIn("needs_evidence", pp.FILL_BLOCKING_STATUS)
        self.assertNotIn("needs_evidence", pp.FILL_OK_STATUS)
        pool = {"binance:AUSDT", "binance:BUSDT"}
        syms = {"binance:AUSDT": {"status": "ok"},
                "binance:BUSDT": {"status": "needs_evidence"}}
        with mock.patch.object(pp, "real_pool_keys", return_value=(pool, "")):
            ok, detail = pp.check_fill(self._fill(syms), 0)
        self.assertFalse(ok)
        self.assertIn("needs_evidence", detail)


class TestGwRetry(unittest.TestCase):
    """退回 #3：真实瞬时错误仍遵守网关重试；真封禁不重试。"""

    def test_transient_retried_then_ok(self):
        class FakeGw:
            def __init__(self):
                self.n = 0
            def get(self, name, params):
                self.n += 1
                if self.n < 3:
                    raise pr.RejectedRequest("网络请求未完成，错误类型：URLError")
                return {"数据": [1]}

            class store:
                @staticmethod
                def clock():
                    return pr.now_ms()

        fg = FakeGw()
        g = pr.Gw(fg)
        with mock.patch("time.sleep", lambda *_: None):
            r = g.get("binance.candles", {})
        self.assertEqual(r["数据"], [1])
        self.assertEqual(fg.n, 3)
        self.assertEqual(g.stats()["transient"], 2)
        self.assertEqual(g.stats()["ok"], 1)

    def test_block_not_retried(self):
        class FakeGw:
            def __init__(self):
                self.n = 0
            def get(self, name, params):
                self.n += 1
                raise pr.RejectedRequest("该来源已暂停：需要人工核查")

            class store:
                @staticmethod
                def clock():
                    return pr.now_ms()

        fg = FakeGw()
        g = pr.Gw(fg)
        with mock.patch("time.sleep", lambda *_: None):
            with self.assertRaises(pr.RejectedRequest):
                g.get("binance.candles", {})
        self.assertEqual(fg.n, 1)                    # 真封禁：立即停止，不重试
        self.assertEqual(g.stats()["rejected"], 1)


class TestRawSpaceAndLinkage(unittest.TestCase):
    """退回 #4：目录级总空间预算 + 失败区分 + 导入↔正文证据关联。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.raw = self.base / "raw"
        self.raw.mkdir()
        self._old_raw = pr.RAW_DIR
        pr.RAW_DIR = self.raw
        self.addCleanup(self._restore)
        self._old_state = dict(pr._RAW_STATE)
        pr._RAW_STATE.update({"enabled": False, "count": 0, "bytes": 0, "link_bytes": 0,
                              "skipped_over_cap": 0, "save_error": 0,
                              "skipped_low_space": 0, "cap_bytes": 0,
                              "dir_existing_bytes": 0, "min_free_gb": 0.0,
                              "last_error": None, "run_id": None})
        self.addCleanup(lambda: pr._RAW_STATE.update(self._old_state))

    def _restore(self):
        pr.RAW_DIR = self._old_raw

    def test_over_cap_is_directory_level(self):
        d = self.raw / "12h"
        d.mkdir(parents=True)
        (d / "existing.body").write_bytes(b"x" * 1000)      # 既有占用 1000B
        rec = pr.RawRecorder(lambda url, t: (200, {}, b"y" * 100), "12h", cap_bytes=1000)
        rec("https://fapi.binance.com/x", 10)
        self.assertEqual(pr._RAW_STATE["skipped_over_cap"], 1)
        self.assertEqual(pr._RAW_STATE["count"], 0)          # 未落盘
        self.assertEqual(pr._RAW_STATE["save_error"], 0)

    def test_save_error_distinct_from_over_cap(self):
        p = self.raw / "12h"
        p.write_bytes(b"iam-a-file-not-dir")                 # 目录位置是文件 → mkdir 失败
        # min_free_gb=0：本用例只验「落盘 IO 失败 ≠ 到量」；空间门禁由 test_low_space_skipped 覆盖。
        rec = pr.RawRecorder(lambda url, t: (200, {}, b"z" * 10), "12h", cap_bytes=10 ** 9,
                             min_free_gb=0)
        rec("https://fapi.binance.com/x", 10)
        self.assertEqual(pr._RAW_STATE["save_error"], 1)
        self.assertEqual(pr._RAW_STATE["skipped_over_cap"], 0)
        self.assertEqual(pr._RAW_STATE["count"], 0)
        self.assertIn("Error", pr._RAW_STATE["last_error"] or "")

    def test_low_space_skipped(self):
        (self.raw / "12h").mkdir(parents=True, exist_ok=True)
        rec = pr.RawRecorder(lambda url, t: (200, {}, b"z" * 10), "12h",
                             cap_bytes=10 ** 9, min_free_gb=1e12)   # 阈值远大于真实剩余
        rec("https://fapi.binance.com/x", 10)
        self.assertEqual(pr._RAW_STATE["skipped_low_space"], 1)
        self.assertEqual(pr._RAW_STATE["count"], 0)

    def test_import_evidence_linkage_closes_loop(self):
        db = self.base / "m.sqlite3"
        con = sqlite3.connect(db)
        con.executescript(SCHEMA_SQL)
        iv = IV12
        base_o = 1_700_000_000_000 // iv * iv
        opens = [base_o + i * iv for i in range(4)]
        body = json.dumps([[t, "1", "2", "0.5", "1.5", "10", t + iv - 1, "100", 3, "5", "6"]
                           for t in opens]).encode("utf-8")
        meta, footprint, reason = pr.write_raw_response(
            "12h", "https://fapi.binance.com/fapi/v1/klines?symbol=XUSDT&interval=12h",
            200, body, phase="fetch")
        self.assertEqual(reason, "ok")
        self.assertEqual(footprint, meta["_footprint_bytes"])
        self.assertGreater(footprint, len(body))     # 足迹 = 正文 + 元数据（> 正文）
        parsed = json.loads(body)
        rows = [(int(r[0]), r) for r in parsed]
        n, sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", rows,
                                    "https://fapi.binance.com/fapi/v1/klines",
                                    evidence=[meta], link_path=pr._evidence_link_path("12h"))
        self.assertEqual(n, 4)
        self.assertEqual(st, "linked")
        # 库内确有 4 根
        got = con.execute("SELECT COUNT(*) FROM candle_heads WHERE symbol='XUSDT'").fetchone()[0]
        self.assertEqual(got, 4)
        con.close()
        # sidecar：导入↔正文证据关联闭合、可离线复算
        link = json.loads(pr._evidence_link_path("12h").read_text(encoding="utf-8"))
        rec = link["links"][0]
        self.assertEqual(rec["source_id"], sid)
        self.assertEqual(rec["rows_added"], 4)
        self.assertEqual(rec["evidence"][0]["body_sha256"], meta["body_sha256"])
        self.assertEqual(rec["evidence"][0]["body_sha256"], sha256_bytes(body))
        body_on_disk = Path(rec["evidence"][0]["body_path"]).read_bytes()
        self.assertEqual(sha256_bytes(body_on_disk), meta["body_sha256"])   # 正文可复算
        # 导入时点 digest 与**正文解析出的时点**一致
        body_opens = [int(r[0]) for r in json.loads(body_on_disk)]
        self.assertEqual(rec["open_times_digest"], pr.opens_digest(body_opens))
        self.assertEqual(sorted(body_opens), sorted(opens))


class TestRawBudgetAccounting(unittest.TestCase):
    """二次退回 #4b：目录级总空间核算必须计入 **正文 + 元数据 + sidecar 增长**。

    独立验收隔离复现：cap=10B、正文=10B，实际目录占用 598B（`saved=1`）。
    根因：上一版只累加正文长度，`.meta.json` / `import_evidence.json` / 临时文件不计入。
    本类断言：① 精确足迹（正文+元数据）落盘**前**判定；② 目录实际占用**永不超过** cap；
    ③ 连续写入累计；④ 重启（既有占用）计入；⑤ sidecar 增长计入。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.raw = self.base / "raw"
        self.raw.mkdir()
        self._old_raw = pr.RAW_DIR
        pr.RAW_DIR = self.raw
        self.addCleanup(self._restore)
        self._old_state = dict(pr._RAW_STATE)
        pr._RAW_STATE.update({"enabled": False, "count": 0, "bytes": 0, "link_bytes": 0,
                              "skipped_over_cap": 0, "save_error": 0,
                              "skipped_low_space": 0, "cap_bytes": 0,
                              "dir_existing_bytes": 0, "min_free_gb": 0.0,
                              "last_error": None, "run_id": None})
        self.addCleanup(lambda: pr._RAW_STATE.update(self._old_state))

    def _restore(self):
        pr.RAW_DIR = self._old_raw

    def test_body_exactly_at_cap_is_blocked_by_meta(self):
        """cap=10B、正文恰好 10B：元数据使足迹超限 ⇒ **不得**落盘（复现验收 598B 漏算）。"""
        rec = pr.RawRecorder(lambda url, t: (200, {}, b"x" * 10), "12h", cap_bytes=10)
        rec("https://fapi.binance.com/x", 10)
        self.assertEqual(pr._RAW_STATE["skipped_over_cap"], 1)
        self.assertEqual(pr._RAW_STATE["count"], 0)
        self.assertEqual(pr._raw_dir_bytes("12h"), 0)          # 目录零占用（修复前为 598B）
        self.assertEqual(pr._RAW_STATE["bytes"], 0)

    def test_directory_total_never_exceeds_cap(self):
        """连续多次写入：目录**实际**占用（正文+元数据）始终 ≤ cap，到量即停。"""
        cap = 3000
        # min_free_gb=0：本用例只验「目录级总占用 ≤ cap、到量即停」；
        # 空间门禁由 test_low_space_skipped 单独覆盖，避免随宿主磁盘假失败。
        rec = pr.RawRecorder(lambda url, t: (200, {}, b"y" * 500), "12h", cap_bytes=cap,
                             min_free_gb=0)
        for _ in range(20):
            rec("https://fapi.binance.com/klines", 10)
        self.assertLessEqual(pr._raw_dir_bytes("12h"), cap)
        self.assertGreaterEqual(pr._RAW_STATE["skipped_over_cap"], 1)
        d = self.raw / "12h"
        # 每个成功落盘 = 1 个 .body + 1 个 .meta.json；计数与文件数一致
        self.assertEqual(pr._RAW_STATE["count"], len(list(d.glob("*.body"))))
        self.assertEqual(pr._RAW_STATE["count"], len(list(d.glob("*.meta.json"))))
        # 累计的 bytes 与目录实际占用一致（含元数据，非仅正文）
        self.assertEqual(pr._RAW_STATE["bytes"], pr._raw_dir_bytes("12h"))

    def test_restart_existing_bytes_counted(self):
        """重启后：既有目录占用计入预算，新 run 不得再越过 cap。"""
        d = self.raw / "12h"
        d.mkdir(parents=True)
        (d / "old.body").write_bytes(b"z" * 1000)              # 既有占用 1000B
        rec = pr.RawRecorder(lambda url, t: (200, {}, b"q" * 600), "12h", cap_bytes=1500)
        self.assertEqual(pr._RAW_STATE["dir_existing_bytes"], 1000)
        rec("https://fapi.binance.com/x", 10)                  # 1000+足迹 > 1500 ⇒ 阻塞
        self.assertEqual(pr._RAW_STATE["skipped_over_cap"], 1)
        self.assertEqual(pr._RAW_STATE["count"], 0)
        self.assertEqual(pr._raw_dir_bytes("12h"), 1000)       # 既有占用未被撑破

    def test_sidecar_growth_counted(self):
        """导入↔正文 sidecar 的增长量计入目录级总预算（link_bytes）。"""
        db = self.base / "m.sqlite3"
        con = sqlite3.connect(db)
        con.executescript(SCHEMA_SQL)
        iv = IV12
        base_o = 1_700_000_000_000 // iv * iv
        opens = [base_o + i * iv for i in range(4)]
        body = json.dumps([[t, "1", "2", "0.5", "1.5", "10", t + iv - 1, "100", 3, "5", "6"]
                           for t in opens]).encode("utf-8")
        meta, _fp, _r = pr.write_raw_response(
            "12h", "https://fapi.binance.com/fapi/v1/klines?symbol=XUSDT&interval=12h",
            200, body, phase="fetch")
        rows = [(int(r[0]), r) for r in json.loads(body)]
        before = pr._RAW_STATE["bytes"]
        n, _sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", rows,
                                     "https://fapi.binance.com/fapi/v1/klines",
                                     evidence=[meta], link_path=pr._evidence_link_path("12h"))
        con.close()
        self.assertEqual((n, st), (4, "linked"))
        self.assertGreater(pr._RAW_STATE["link_bytes"], 0)
        self.assertEqual(pr._RAW_STATE["bytes"], before + pr._RAW_STATE["link_bytes"])

    def test_write_raw_response_reports_over_cap_without_files(self):
        """write_raw_response 直接调用：over_cap 返回 None 且**不写任何文件**。"""
        meta, footprint, reason = pr.write_raw_response(
            "12h", "https://fapi.binance.com/x", 200, b"x" * 10,
            cap_bytes=10, used_bytes=0, dir_existing_bytes=0)
        self.assertIsNone(meta)
        self.assertEqual(reason, "over_cap")
        self.assertGreater(footprint, 10)                      # 足迹含元数据
        self.assertEqual(pr._raw_dir_bytes("12h"), 0)


class TestEvidenceBlocking(unittest.TestCase):
    """二次退回 #5：缺正文证据 / 关联写入失败 ⇒ **拒绝提交**并阻塞验收。

    独立验收隔离复现：空 evidence 仍新增 1 根（`evidence_n=0/save_error=0/last_error=null`）。
    本类断言：空 evidence、缺 body_sha256、关联写入失败、到量未留档 ⇒ `added=0` 且库内不留痕；
    已提交的旧数据不受影响；禁止用时点摘要冒充正文 SHA。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.raw = self.base / "raw"
        self.raw.mkdir()
        self._old_raw = pr.RAW_DIR
        pr.RAW_DIR = self.raw
        self.addCleanup(self._restore)
        self._old_state = dict(pr._RAW_STATE)
        pr._RAW_STATE.update({"enabled": False, "count": 0, "bytes": 0, "link_bytes": 0,
                              "skipped_over_cap": 0, "save_error": 0,
                              "skipped_low_space": 0, "cap_bytes": 0,
                              "dir_existing_bytes": 0, "min_free_gb": 0.0,
                              "last_error": None, "run_id": None})
        self.addCleanup(lambda: pr._RAW_STATE.update(self._old_state))
        self.iv = IV12
        self.base_o = 1_700_000_000_000 // self.iv * self.iv
        self.opens = [self.base_o + i * self.iv for i in range(3)]
        self.body = json.dumps(
            [[t, "1", "2", "0.5", "1.5", "10", t + self.iv - 1, "100", 3, "5", "6"]
             for t in self.opens]).encode("utf-8")
        self.rows = [(int(r[0]), r) for r in json.loads(self.body)]

    def _restore(self):
        pr.RAW_DIR = self._old_raw

    def _db(self):
        db = self.base / "m.sqlite3"
        con = sqlite3.connect(db)
        con.executescript(SCHEMA_SQL)
        return con

    def _heads(self, con):
        return con.execute("SELECT COUNT(*) FROM candle_heads").fetchone()[0]

    def _sources(self, con):
        return con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    def test_empty_evidence_refused(self):
        """空 evidence：拒绝导入（复现验收 `added=1` 的漏洞）。"""
        con = self._db()
        n, sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", self.rows,
                                    "https://fapi.binance.com/fapi/v1/klines",
                                    evidence=[], link_path=pr._evidence_link_path("12h"))
        self.assertEqual((n, sid, st), (0, None, "missing_body"))
        self.assertEqual(self._heads(con), 0)                  # 库内不留痕
        self.assertEqual(self._sources(con), 0)
        con.close()

    def test_evidence_without_body_sha_refused(self):
        """证据只有 URL、无 body_sha256/body_path ⇒ 不得冒充正文 SHA ⇒ 拒绝。"""
        con = self._db()
        n, sid, st = pr.import_rest(
            con, "binance", "um", "XUSDT", "12h", self.rows,
            "https://fapi.binance.com/fapi/v1/klines",
            evidence=[{"url": "https://fapi.binance.com/x", "http_code": 200}],
            link_path=pr._evidence_link_path("12h"))
        self.assertEqual((n, st), (0, "missing_body"))
        self.assertEqual(self._heads(con), 0)
        con.close()

    def test_link_failure_refused_and_rolled_back(self):
        """关联 sidecar 写入失败（父路径是文件）⇒ 拒绝提交并回滚。"""
        con = self._db()
        notdir = self.base / "notdir"
        notdir.write_bytes(b"iam-a-file")                      # 让 mkdir 失败
        link_path = notdir / "import_evidence.json"
        n, sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", self.rows,
                                    "https://fapi.binance.com/fapi/v1/klines",
                                    evidence=[{"body_sha256": "a" * 64, "body_path": "/x.body"}],
                                    link_path=link_path)
        self.assertEqual((n, sid, st), (0, None, "link_failed"))
        self.assertEqual(self._heads(con), 0)                  # 已回滚，库内不留痕
        self.assertEqual(self._sources(con), 0)
        con.close()

    def test_over_cap_fetch_yields_no_meta_then_import_blocked(self):
        """到量未留档 ⇒ `recorder.recent` 为空 ⇒ 该区间导入被拒（端到端闭环）。"""
        rec = pr.RawRecorder(lambda url, t: (200, {}, self.body), "12h", cap_bytes=1)
        rec("https://fapi.binance.com/fapi/v1/klines?symbol=XUSDT&interval=12h", 10)
        self.assertEqual(rec.recent, [])                       # 到量：无正文证据
        con = self._db()
        n, sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", self.rows,
                                    "https://fapi.binance.com/fapi/v1/klines",
                                    evidence=list(rec.recent),
                                    link_path=pr._evidence_link_path("12h"))
        self.assertEqual((n, st), (0, "missing_body"))
        self.assertEqual(self._heads(con), 0)
        con.close()

    def test_legacy_require_evidence_false_imports_without_body(self):
        """`require_evidence=False`（仅兼容旧路径，**生产不启用**）：允许无正文导入但需显式声明。"""
        con = self._db()
        n, sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", self.rows,
                                    "https://fapi.binance.com/fapi/v1/klines",
                                    evidence=[], link_path=None, require_evidence=False)
        self.assertEqual(n, 3)
        self.assertEqual(st, "missing_body")                   # 状态仍如实标注
        self.assertEqual(self._heads(con), 3)
        con.close()

    def test_existing_committed_rows_untouched_on_refusal(self):
        """拒绝提交**不影响**库内已提交的旧数据（保留已入库数据）。"""
        con = self._db()
        con.execute("INSERT INTO candle_heads VALUES ('binance','um','XUSDT','12h',?,1)",
                    (self.opens[0],))
        con.commit()
        n, _sid, st = pr.import_rest(con, "binance", "um", "XUSDT", "12h", self.rows,
                                     "https://fapi.binance.com/fapi/v1/klines",
                                     evidence=[], link_path=pr._evidence_link_path("12h"))
        self.assertEqual((n, st), (0, "missing_body"))
        self.assertEqual(self._heads(con), 1)                  # 旧数据原样保留
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
