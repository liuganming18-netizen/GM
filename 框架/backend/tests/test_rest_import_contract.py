# -*- coding: utf-8 -*-
"""REST 补口入库器 `import_rest` 的**返回契约**回归测试（离线；零网络；临时库/临时目录）。

背景（2026-10-08 实测阻断）：
  `logs/_pool_restfill.py::import_rest` 在「待写入的行**已全部在库**」这条正常路径上写成
  `return 0, None`（**2 个值**），而调用方一律按 `added, sid, ev = import_rest(...)`
  解包 3 个值 ⇒ 抛 `ValueError: not enough values to unpack (expected 3, got 2)`，
  被 `batch_download.import_symbol` 记成 `import_failed`，
  使该周期出现 `failed_files=1`、阶段状态 `failed`、批量退出码 5 —— **并非数据缺失**。

本测试锁定契约：`import_rest` 在**所有**路径都必须返回 3 元组
`(added, source_id, evidence_status)`，其中 `evidence_status ∈
{"no_rows", "linked", "missing_body", "link_failed"}`。

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_rest_import_contract -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 后端实现/
PROJ = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "logs"))

_TMP = tempfile.TemporaryDirectory(prefix="rest_import_contract_")
TMP = Path(_TMP.name)
# 必须在 import _pool_restfill **之前**重定向，避免触碰正式库/断点/缓存目录
os.environ["MARKET_DB"] = str(TMP / "market.sqlite3")
os.environ["POOL_STATE_DIR"] = str(TMP / "state")
os.environ["POOL_CACHE_DIR"] = str(TMP / "cache")
os.environ["POOL_RAW_DIR"] = str(TMP / "raw")
os.environ["POOL_READONLY"] = "1"

import _pool_restfill as pr                                        # noqa: E402

TF = "1d"
IV = 86400000
SYM = "TESTUSDT"


def _schema(con):
    con.executescript((PROJ / "data_hub" / "schema.sql").read_text(encoding="utf-8"))


def _row(open_ms, close_ms):
    return [open_ms, "1.0", "2.0", "0.5", "1.5", "10.0", close_ms, "15.0", 3, "4.0", "6.0", "0"]


class RestImportReturnContract(unittest.TestCase):
    def setUp(self):
        self.db = TMP / f"{self._testMethodName}.sqlite3"
        if self.db.exists():
            self.db.unlink()
        self.con = sqlite3.connect(str(self.db))
        _schema(self.con)
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _seed_existing(self, open_ms, close_ms):
        self.con.execute(
            "INSERT INTO sources(exchange,market,symbol,timeframe,source_url,file_path,sha256,"
            "imported_at,downloaded_at,first_open,last_close,row_count) "
            "VALUES ('binance','um',?,?, 'seed','', 'seedsha', 1, 1, ?, ?, 1)",
            (SYM, TF, open_ms, close_ms))
        sid = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.con.execute(
            "INSERT INTO candle_versions VALUES ('binance','um',?,?,?,?, '1','2','0.5','1.5',"
            "'10','15',3,'4','6',?,NULL,1)", (SYM, TF, open_ms, close_ms, sid))
        self.con.execute(
            "INSERT INTO candle_heads VALUES ('binance','um',?,?,?,?)", (SYM, TF, open_ms, sid))
        self.con.commit()

    def test_all_rows_already_present_returns_3_tuple(self):
        """已全在库（正常幂等路径）也必须返回 3 元组，且 status='no_rows'。

        注意：必须带**完整正文证据**，否则会先被「缺证据」闸门拦下（返回 missing_body），
        就走不到「行已全在库」这条路径 —— 而这正是 2026-10-08 实际踩到的路径
        （阶段 A 已落盘正文 + 元数据，但该批根此前已由归档入库）。
        """
        o = 1567900800000
        self._seed_existing(o, o + IV - 1)
        body = TMP / "body_existing.json"
        body.write_bytes(b"[]")
        ev_meta = {"url": "https://example.invalid/klines", "host": "fapi.binance.com",
                   "http_code": 200, "body_path": str(body), "body_bytes": 2,
                   "body_sha256": "0" * 64, "sha_kind": "test", "fetched_at": "2026-10-08T00:00:00Z",
                   "phase": "fetch"}
        res = pr.import_rest(self.con, "binance", "um", SYM, TF,
                             [(o, _row(o, o + IV - 1))], "https://example.invalid/klines",
                             evidence=[ev_meta], link_path=TMP / "link_existing.json",
                             require_evidence=True)
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 3, f"必须返回 3 元组，实际 {res!r}")
        added, sid, ev = res
        self.assertEqual(added, 0)
        self.assertIsNone(sid)
        self.assertEqual(ev, "no_rows")

    def test_new_row_without_evidence_rejected(self):
        """新行 + require_evidence=True 且无正文证据 ⇒ 拒绝提交（missing_body），库内不留痕。"""
        o = 1567900800000
        res = pr.import_rest(self.con, "binance", "um", SYM, TF,
                             [(o, _row(o, o + IV - 1))], "https://example.invalid/klines",
                             evidence=[], require_evidence=True)
        self.assertEqual(len(res), 3)
        self.assertEqual(res, (0, None, "missing_body"))
        n = self.con.execute("SELECT COUNT(*) FROM candle_heads").fetchone()[0]
        self.assertEqual(n, 0, "拒绝提交时不得在库内留痕")

    def test_new_row_with_evidence_commits_and_links(self):
        """新行 + 完整正文证据 ⇒ 提交、返回 3 元组、sidecar 关联成功。"""
        o = 1567900800000
        body = TMP / "body.json"
        body.write_bytes(b"[]")
        ev_meta = {"url": "https://example.invalid/klines", "host": "fapi.binance.com",
                   "http_code": 200, "body_path": str(body), "body_bytes": 2,
                   "body_sha256": "0" * 64, "sha_kind": "test", "fetched_at": "2026-10-08T00:00:00Z",
                   "phase": "fetch"}
        link = TMP / "import_evidence.json"
        res = pr.import_rest(self.con, "binance", "um", SYM, TF,
                             [(o, _row(o, o + IV - 1))], "https://example.invalid/klines",
                             evidence=[ev_meta], link_path=link, require_evidence=True)
        self.assertEqual(len(res), 3)
        added, sid, ev = res
        self.assertEqual(added, 1)
        self.assertIsNotNone(sid)
        self.assertEqual(ev, "linked")
        n = self.con.execute("SELECT COUNT(*) FROM candle_heads").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertTrue(link.exists(), "sidecar 证据应已写入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
