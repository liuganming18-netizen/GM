# -*- coding: utf-8 -*-
"""OKX `volume` 单位缺陷修复工具的**隔离小库回归**（离线、零网络、有界夹具）。

覆盖合同要求的全部回归项：
  · 复现 CRO vol=2 / volCcy=20；ctVal=1 / 0.01 / 100；零量；重复原件；同根多源；
  · 修复后再运行**幂等**；**并发 head 变化不覆盖**；
  · **旧 source 冻结可读**；**纠错版本可读**；**回退 head 恢复**；
  · **摘要匹配**（新 source sha256 可独立复算）；**available_at 真实修复时点、不穿越**；
  · 价格 / quote / 其他列**不变**；`candles` 视图行数不变。

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_okx_volume_fix_isolation -v
"""
# ── W1-GUARD-BEGIN（2026-10-16）遗留组件守卫（显式 SKIP，非 PASS）──
# 本模块回归的是**旧工作区遗留件**（未随工程迁移，且属施工单禁止恢复项）。
# 组件缺失 ⇒ **显式 SKIP**：既不 PASS、也不静默 fallback（fail-closed 纪律）。
# 若遗留件将来被正式迁入，守卫自动失效，测试照常运行。
import importlib.util as _ilu_w1
from pathlib import Path as _PathW1

import pytest as _pytest_w1

MIG_W1 = _PathW1(__file__).resolve().parents[3].joinpath('数据', '研究输入', '外部迁移_2026-10-16')
_missing_w1 = [str(p) for p in (
    MIG_W1 / "OKX成交量离线修复_2026-10-11" / "04_okx_volume_fix.py",
) if not p.exists()]
_missing_w1 += [m for m in () if _ilu_w1.find_spec(m) is None]
if _missing_w1:
    _pytest_w1.skip(
        "W1: 旧工作区遗留组件未迁移且禁止恢复 ⇒ 显式 SKIP（非 PASS）: "
        + ", ".join(_missing_w1),
        allow_module_level=True,
    )
# ── W1-GUARD-END ──

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 框架/backend
# W1（2026-10-16）：原指向工程外路径（已删）⇒ 改按工程布局推导。
FRAMEWORK = HERE.parent                                # 框架/（含 data_hub 包）
PROJECT_ROOT = HERE.parent.parent                      # 工程根
PROJ = FRAMEWORK
# W1（2026-10-16）：原指向工程外工作区（已删）。该批修复脚本**未随工程迁移**
# ⇒ 这里给出工程内正式位置（可用 `OKX_FIX_DIR` 覆盖）；目录缺失时相关用例报错，
# **不静默跳过、不退回旧路径**。
FIXDIR = Path(os.environ.get("OKX_FIX_DIR") or (
    Path(__file__).resolve().parents[3] / "数据" / "研究输入" / "外部迁移_2026-10-16" /
    "OKX成交量离线修复_2026-10-11"))
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))

_TMP = tempfile.TemporaryDirectory(prefix="okx_vol_fix_iso_")
TMP = Path(_TMP.name)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, FIXDIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fix = _load("okxfix", "04_okx_volume_fix.py")
rollback = _load("okxrollback", "06_okx_volume_rollback.py")

TF = "15m"
IV = 900_000
MARKET = "swap"


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.name = self._testMethodName
        self.db = TMP / f"{self.name}.sqlite3"
        if self.db.exists():
            self.db.unlink()
        self.raw = TMP / f"raw_{self.name}"
        self.con = sqlite3.connect(str(self.db))
        self.con.row_factory = sqlite3.Row
        self.con.executescript((PROJ / "data_hub" / "schema.sql").read_text(encoding="utf-8"))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    # ── 夹具 ───────────────────────────────────────────────────────────
    def _add_source(self, sid_want, symbol, rows, tf=TF, url=None, sha=None):
        """rows = [(open_time, vol, volCcy, volCcyQuote)] ⇒ 按**缺陷映射**写 volume=r5 + head。"""
        url = url or "https://www.okx.com/api/v5/market/history-candles"
        sha = sha or _sha(f"src|{symbol}|{tf}|{sid_want}")
        opens = [r[0] for r in rows]
        self.con.execute(
            "INSERT INTO sources(id,exchange,market,symbol,timeframe,source_url,file_path,sha256,"
            "imported_at,downloaded_at,first_open,last_close,row_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid_want, "okx", MARKET, symbol, tf, url, "", sha, 1000, 1000,
             min(opens), max(opens) + IV - 1, len(rows)))
        for ot, v5, v6, v7 in rows:
            self.con.execute(
                "INSERT INTO candle_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("okx", MARKET, symbol, tf, ot, ot + IV - 1, "0.08062", "0.08063", "0.08042",
                 "0.08045", str(v5), str(v7), 0, "", "", sid_want, None, 1))   # ← 缺陷：r5
            self.con.execute(
                "INSERT INTO candle_heads VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(exchange,market,symbol,timeframe,open_time) DO NOTHING",
                ("okx", MARKET, symbol, tf, ot, sid_want))
        self.con.commit()

    def _add_body(self, tf, url, data_rows, tag=""):
        """写一份 OKX 原始正文（真格式 {"code","data"}），返回 evidence 记录。"""
        d = self.raw / tf
        d.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"code": "0", "msg": "", "data": data_rows}).encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        p = d / f"{sha[:12]}{tag}.body"
        p.write_bytes(payload)
        return {"url": url, "host": "www.okx.com", "http_code": 200, "body_path": str(p),
                "body_bytes": len(payload), "body_sha256": sha, "sha_kind": "HTTP原始字节SHA256",
                "fetched_at": "2026-10-11T00:00:00Z", "phase": "fetch"}

    def _add_sidecar(self, tf, links):
        d = self.raw / tf
        d.mkdir(parents=True, exist_ok=True)
        p = d / "import_evidence.json"
        cur = {"links": []}
        if p.exists():
            cur = json.loads(p.read_text(encoding="utf-8"))
        cur["links"].extend(links)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")

    def _link(self, sid, symbol, evidence, tf=TF):
        return {"source_id": sid, "exchange": "okx", "symbol": symbol, "tf": tf,
                "evidence_n": len(evidence), "evidence": evidence}

    def _snapshot_versions(self):
        return [tuple(r) for r in self.con.execute(
            "SELECT exchange,market,symbol,timeframe,open_time,close_time,open,high,low,close,"
            "volume,quote_volume,trade_count,taker_buy_volume,taker_buy_quote_volume,source_id,"
            "available_at,is_closed FROM candle_versions ORDER BY open_time,source_id")]

    def _heads(self):
        return {int(r[0]): int(r[1]) for r in self.con.execute(
            "SELECT open_time, source_id FROM candle_heads")}

    def _view_count(self):
        return self.con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]

    def _plan(self, only=None):
        return fix.build_plan(self.db, self.raw, tfs=(TF, "12h", "1h", "30m", "4h"),
                              only_sources=only, include_unchanged=True)

    def _apply(self, plan, **kw):
        return fix.apply_plan(self.db, plan, batch_size=kw.pop("batch_size", 1000),
                              checkpoint=kw.pop("checkpoint", None),
                              available_at_mode=kw.pop("available_at_mode", "repair"),
                              min_free_gb=kw.pop("min_free_gb", 0.0), log=lambda *_: None)

    def _row(self, sid, ot):
        return self.con.execute(
            "SELECT * FROM candle_versions WHERE source_id=? AND open_time=?",
            (sid, ot)).fetchone()


# ══════════════════════════ 1) 核心映射与列不变 ══════════════════════════
class Correctness(Base):
    def test_reproduce_cro_and_fix(self):
        """复现 CRO：库内 volume=2（张数），原始 volCcy=20 ⇒ 修复后 volume='20'。"""
        o = 1617871500000
        self._add_source(900001, "CRO-USDT-SWAP", [(o, "2", "20", "3.942")])
        ev = self._add_body(TF, "https://www.okx.com/api/v5/market/history-candles?instId=CRO",
                            [[str(o), "0.08062", "0.08063", "0.08042", "0.08045", "2", "20",
                              "3.942", "1"]])
        self._add_sidecar(TF, [self._link(900001, "CRO-USDT-SWAP", [ev])])

        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 1)
        self.assertEqual(plan["totals"]["rows_changed"], 1)
        res = self._apply(plan)
        rec = res["results"][0]
        self.assertEqual(rec["rows_written"], 1)
        self.assertEqual(rec["heads_switched"], 1)
        self.assertEqual(rec["rows_value_changed"], 1)

        new = self._row(rec["new_source_id"], o)
        self.assertEqual(new["volume"], "20", "修复后必须是 r[6]=volCcy")
        self.assertEqual(new["quote_volume"], "3.942")
        self.assertEqual(new["open"], "0.08062")
        self.assertEqual(new["close"], "0.08045")
        self.assertEqual(new["close_time"], o + IV - 1)
        self.assertEqual(new["is_closed"], 1)

    def test_other_columns_bit_identical_to_old(self):
        """除 volume / source_id / available_at 外，其余列必须与旧行**逐字段相同**。"""
        o = 1617871500000
        self._add_source(900002, "CRO-USDT-SWAP", [(o, "2", "20", "3.942")])
        ev = self._add_body(TF, "u", [[str(o), "0.08062", "0.08063", "0.08042", "0.08045",
                                       "2", "20", "3.942", "1"]])
        self._add_sidecar(TF, [self._link(900002, "CRO-USDT-SWAP", [ev])])
        old = self._row(900002, o)
        res = self._apply(self._plan())
        new = self._row(res["results"][0]["new_source_id"], o)
        for k in ("exchange", "market", "symbol", "timeframe", "open_time", "close_time", "open",
                  "high", "low", "close", "quote_volume", "trade_count", "taker_buy_volume",
                  "taker_buy_quote_volume", "is_closed"):
            self.assertEqual(new[k], old[k], f"列 {k} 不得变化")

    def test_ctval_one_source_untouched(self):
        """ctVal=1（vol==volCcy）⇒ 该来源**不动**（不扩大生产修复范围）。"""
        o = 1726574400000
        self._add_source(900003, "LUNA-USDT-SWAP", [(o, "1700", "1700", "15300")])
        ev = self._add_body(TF, "u", [[str(o), "1", "1", "1", "1", "1700", "1700", "15300", "1"]])
        self._add_sidecar(TF, [self._link(900003, "LUNA-USDT-SWAP", [ev])])
        before = self._snapshot_versions()
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "no_value_change")
        self._apply(plan)
        self.assertEqual(self._snapshot_versions(), before, "ctVal=1 来源不得被改写")

    def test_ctval_001_and_100(self):
        """ctVal=0.01 / 100 的量值方向正确（volCcy 可小于 / 大于 vol）。"""
        o1, o2 = 1756944000000, 1761696000000
        self._add_source(900004, "OKB-USDT-SWAP", [(o1, "12345", "123.45", "7890.1")])
        self._add_source(900005, "OL-USDT-SWAP", [(o2, "7", "700", "900")])
        e1 = self._add_body(TF, "u1", [[str(o1), "1", "1", "1", "1", "12345", "123.45",
                                        "7890.1", "1"]])
        e2 = self._add_body(TF, "u2", [[str(o2), "1", "1", "1", "1", "7", "700", "900", "1"]])
        self._add_sidecar(TF, [self._link(900004, "OKB-USDT-SWAP", [e1]),
                               self._link(900005, "OL-USDT-SWAP", [e2])])
        res = self._apply(self._plan())
        got = {r["symbol"]: self._row(r["new_source_id"], o1 if r["symbol"].startswith("OKB") else o2)
               for r in res["results"]}
        self.assertEqual(got["OKB-USDT-SWAP"]["volume"], "123.45")
        self.assertEqual(got["OL-USDT-SWAP"]["volume"], "700")

    def test_zero_volume_row_kept_as_zero(self):
        """零量行（vol==volCcy=='0'）在同一受影响来源内一并迁移，值仍为 '0'。"""
        o_bad, o_zero = 1617871500000, 1617872400000
        self._add_source(900006, "CRO-USDT-SWAP",
                         [(o_bad, "2", "20", "3.942"), (o_zero, "0", "0", "0")])
        ev = self._add_body(TF, "u", [[str(o_bad), "1", "1", "1", "1", "2", "20", "3.942", "1"],
                                      [str(o_zero), "1", "1", "1", "1", "0", "0", "0", "1"]])
        self._add_sidecar(TF, [self._link(900006, "CRO-USDT-SWAP", [ev])])
        plan = self._plan()
        self.assertEqual(plan["totals"]["rows_changed"], 1)
        self.assertEqual(plan["totals"]["rows_to_write"], 2, "整源替换：零量行一并迁移")
        res = self._apply(plan)
        nsid = res["results"][0]["new_source_id"]
        self.assertEqual(self._row(nsid, o_zero)["volume"], "0")
        self.assertEqual(self._row(nsid, o_bad)["volume"], "20")

    def test_duplicate_body_same_bar_written_once(self):
        """重复原件（同根出现在多份正文）⇒ 只写 1 行。"""
        o = 1617871500000
        self._add_source(900007, "CRO-USDT-SWAP", [(o, "2", "20", "3.942")])
        row = [str(o), "1", "1", "1", "1", "2", "20", "3.942", "1"]
        e1 = self._add_body(TF, "u1", [row], tag="-a")
        e2 = self._add_body(TF, "u2", [row], tag="-b")
        self._add_sidecar(TF, [self._link(900007, "CRO-USDT-SWAP", [e1, e2])])
        res = self._apply(self._plan())
        self.assertEqual(res["results"][0]["rows_written"], 1)

    def test_same_bar_multi_source_head_conditional(self):
        """同根多源：head 只属于其中一个来源；另一来源的修复**不得**抢走 head。"""
        o = 1617871500000
        self._add_source(900008, "CRO-USDT-SWAP", [(o, "2", "20", "3.942")])
        self._add_source(900009, "CRO-USDT-SWAP", [(o, "2", "20", "3.942")])   # head 归 900008
        row = [str(o), "1", "1", "1", "1", "2", "20", "3.942", "1"]
        self._add_sidecar(TF, [self._link(900008, "CRO-USDT-SWAP", [self._add_body(TF, "u1", [row])]),
                               self._link(900009, "CRO-USDT-SWAP", [self._add_body(TF, "u2", [row])])])
        self.assertEqual(self._heads()[o], 900008)
        res = self._apply(self._plan())
        by_old = {r["old_source_id"]: r for r in res["results"]}
        self.assertEqual(by_old[900008]["heads_switched"], 1)
        self.assertEqual(by_old[900009]["heads_switched"], 0)
        self.assertEqual(self._heads()[o], by_old[900008]["new_source_id"])
        # 两个修订来源的版本行都可读
        self.assertIsNotNone(self._row(by_old[900008]["new_source_id"], o))
        self.assertIsNotNone(self._row(by_old[900009]["new_source_id"], o))

    def test_source_without_original_body_skipped(self):
        """无原件（sidecar 无链接）⇒ 标 no_original_body，不动库。"""
        o = 1791158400000
        self._add_source(900010, "CRO-USDT-SWAP", [(o, "5", "50", "3.9")])
        before = self._snapshot_versions()
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "no_original_body")
        self._apply(plan)
        self.assertEqual(self._snapshot_versions(), before)


# ══════════════════════════ 2) 保护旧版本 / 血缘 / 摘要 ══════════════════════════
class Protection(Base):
    def _seed(self, sid=910001, sym="CRO-USDT-SWAP", o=1617871500000):
        self._add_source(sid, sym, [(o, "2", "20", "3.942")])
        ev = self._add_body(TF, "u", [[str(o), "1", "1", "1", "1", "2", "20", "3.942", "1"]])
        self._add_sidecar(TF, [self._link(sid, sym, [ev])])
        return o, sid

    def test_old_source_frozen_and_readable(self):
        """旧 source 行与旧版本行**原样冻结可读**（不 UPDATE、不 DELETE）。"""
        o, sid = self._seed()
        old_before = self._row(sid, o)
        res = self._apply(self._plan())
        old_after = self._row(sid, o)
        self.assertEqual(tuple(old_before), tuple(old_after), "旧版本行不得被改写")
        self.assertEqual(old_after["volume"], "2", "旧值（坏值）保持原样可读")
        self.assertIsNotNone(self.con.execute(
            "SELECT id FROM sources WHERE id=?", (sid,)).fetchone())
        self.assertEqual(res["results"][0]["old_source_id"], sid)

    def test_new_source_digest_and_lineage_discoverable(self):
        """新 source 摘要可独立复算；`source_url` 带 volrev/supersedes 标记可发现。"""
        o, sid = self._seed()
        plan = self._plan()
        p = plan["plans"][0]
        expect = _sha(f"{fix.REV_SCHEME}|{p['old_sha256']}|{p['rowset_digest']}|"
                      f"{p['body_manifest_digest']}")
        self.assertEqual(p["new_sha256"], expect)
        self._apply(plan)
        s = self.con.execute("SELECT * FROM sources WHERE sha256=?", (expect,)).fetchone()
        self.assertIsNotNone(s)
        self.assertIn("volrev=1", s["source_url"])
        self.assertIn(f"supersedes={sid}", s["source_url"])
        self.assertEqual(s["file_path"], "", "file_path 必须保持 ''（restfill 范围判据不变）")
        self.assertEqual(s["row_count"], 1)
        self.assertEqual(s["first_open"], o)

    def test_available_at_is_repair_time_no_time_travel(self):
        """新版本行 available_at = 真实修复时点（>= 计划生成时间），不得穿越回更早。"""
        o, sid = self._seed()
        t_before = int(time.time() * 1000)
        res = self._apply(self._plan())
        new = self._row(res["results"][0]["new_source_id"], o)
        self.assertIsNotNone(new["available_at"])
        self.assertGreaterEqual(new["available_at"], t_before)
        self.assertLessEqual(new["available_at"], int(time.time() * 1000) + 1000)
        self.assertIsNone(self._row(sid, o)["available_at"], "旧行保持 NULL（未被改写）")

    def test_available_at_inherit_mode(self):
        """--available-at inherit ⇒ 沿用旧值（NULL）。"""
        o, sid = self._seed()
        res = self._apply(self._plan(), available_at_mode="inherit")
        self.assertIsNone(self._row(res["results"][0]["new_source_id"], o)["available_at"])

    def test_candles_view_row_count_unchanged(self):
        """`candles` 视图行数不变（head 一一对应，不新增可见根）。"""
        o, _ = self._seed()
        before = self._view_count()
        self._apply(self._plan())
        self.assertEqual(self._view_count(), before)

    def test_old_rowset_digest_detects_tampering(self):
        """旧行集摘要**负例**：一旦旧行被改写，不变式必须报警（证明校验有牙）。"""
        o, sid = self._seed()
        plan = self._plan()
        self.assertTrue(fix.verify_old_frozen(self.db, plan)[0]["unchanged"])
        self.con.execute("UPDATE candle_versions SET volume='999' WHERE source_id=? AND open_time=?",
                         (sid, o))
        self.con.commit()
        self.assertFalse(fix.verify_old_frozen(self.db, plan)[0]["unchanged"],
                         "旧行被改写后摘要必须变化（否则不变式无意义）")

    def test_apply_then_old_frozen_invariant_holds(self):
        """修复后旧行集摘要仍与计划一致（端到端不变式）。"""
        o, sid = self._seed()
        plan = self._plan()
        self._apply(plan)
        v = fix.verify_old_frozen(self.db, plan)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0]["unchanged"], "修复不得改写旧版本行")


# ══════════════════════════ 3) 幂等 / 并发 / 回退 ══════════════════════════
class IdempotencyAndRollback(Base):
    def _seed(self, sid=920001, sym="CRO-USDT-SWAP", o=1617871500000):
        self._add_source(sid, sym, [(o, "2", "20", "3.942")])
        ev = self._add_body(TF, "u", [[str(o), "1", "1", "1", "1", "2", "20", "3.942", "1"]])
        self._add_sidecar(TF, [self._link(sid, sym, [ev])])
        return o, sid

    def test_second_apply_is_idempotent(self):
        """修复后**再跑一次**：不新增版本行、不改 head、source 复用（断点续跑安全）。"""
        o, sid = self._seed()
        r1 = self._apply(self._plan())
        n_ver1 = self.con.execute("SELECT COUNT(*) FROM candle_versions").fetchone()[0]
        n_src1 = self.con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        nsid = r1["results"][0]["new_source_id"]
        r2 = self._apply(self._plan())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM candle_versions").fetchone()[0],
                         n_ver1, "第二次运行不得新增版本行")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                         n_src1, "第二次运行不得新增来源行")
        self.assertEqual(self._heads()[o], nsid)
        self.assertEqual(r2["results"][0]["new_source_id"], nsid, "复用同一修订来源")

    def test_concurrent_head_change_not_overwritten(self):
        """并发 head 变化：head 已被第三方来源占用 ⇒ 修复**不覆盖**。"""
        o, sid = self._seed()
        # 造一个"第三方"来源并抢走 head
        self.con.execute(
            "INSERT INTO sources(id,exchange,market,symbol,timeframe,source_url,file_path,sha256,"
            "imported_at,downloaded_at,first_open,last_close,row_count) "
            "VALUES (920099,'okx',?,?,?,?,'','x',1,1,?,?,1)", (MARKET, "CRO-USDT-SWAP", TF,
                                                              "other", o, o + IV - 1))
        self.con.execute(
            "INSERT INTO candle_versions VALUES ('okx',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (MARKET, "CRO-USDT-SWAP", TF, o, o + IV - 1, "1", "1", "1", "1", "999", "999",
             0, "", "", 920099, None, 1))
        self.con.execute("UPDATE candle_heads SET source_id=920099 WHERE open_time=?", (o,))
        self.con.commit()
        self.assertEqual(self._heads()[o], 920099)

        res = self._apply(self._plan())
        rec = res["results"][0]
        self.assertEqual(rec["heads_switched"], 0, "不得抢走第三方 head")
        self.assertEqual(rec["heads_not_on_old"], 1)
        self.assertEqual(self._heads()[o], 920099, "head 仍归第三方来源")
        # 修订来源的版本行仍已写入（可读、可审计），只是没有成为 head
        self.assertIsNotNone(self._row(rec["new_source_id"], o))

    def test_rollback_head_revert_and_reapply(self):
        """回退：head 切回旧来源（新修订行仍可读）⇒ 再次修复可再前进。"""
        o, sid = self._seed()
        plan = self._plan()
        applied = self._apply(plan)
        nsid = applied["results"][0]["new_source_id"]
        self.assertEqual(self._heads()[o], nsid)

        out = TMP / f"{self.name}_applied.json"
        out.write_text(json.dumps({"apply": applied}, ensure_ascii=False), encoding="utf-8")
        rev = rollback.head_revert(self.db, [(sid, nsid, "CRO-USDT-SWAP", TF)],
                                   log=lambda *_: None)
        self.assertEqual(rev[0]["heads_reverted"], 1)
        self.assertEqual(self._heads()[o], sid, "head 已还原到旧来源")
        self.assertIsNotNone(self._row(nsid, o), "修订版本行仍保留可读")

        r2 = self._apply(plan)                                   # 再前进
        self.assertEqual(self._heads()[o], nsid)

    def test_rollback_purge_refuses_while_head_references(self):
        """purge 保护：修订来源仍被 head 引用时**拒绝**删除。"""
        o, sid = self._seed()
        applied = self._apply(self._plan())
        nsid = applied["results"][0]["new_source_id"]
        with self.assertRaises(SystemExit):
            rollback.purge_revision(self.db, [(sid, nsid, "CRO-USDT-SWAP", TF)],
                                    log=lambda *_: None)

    def test_rollback_purge_after_revert(self):
        """先回退 head 再 purge ⇒ 修订来源可安全清除（隔离测试清理路径）。"""
        o, sid = self._seed()
        applied = self._apply(self._plan())
        nsid = applied["results"][0]["new_source_id"]
        rollback.head_revert(self.db, [(sid, nsid, "CRO-USDT-SWAP", TF)], log=lambda *_: None)
        out = rollback.purge_revision(self.db, [(sid, nsid, "CRO-USDT-SWAP", TF)],
                                      log=lambda *_: None)
        self.assertEqual(out[0]["deleted_versions"], 1)
        self.assertEqual(out[0]["deleted_sources"], 1)
        self.assertIsNone(self.con.execute("SELECT id FROM sources WHERE id=?",
                                           (nsid,)).fetchone())
        self.assertEqual(self._heads()[o], sid)

    def test_checkpoint_resume(self):
        """断点续跑：checkpoint 里已完成的来源被跳过，不重复写。"""
        o1, o2 = 1617871500000, 1617872400000
        self._add_source(930001, "CRO-USDT-SWAP", [(o1, "2", "20", "3.9")])
        self._add_source(930002, "OKB-USDT-SWAP", [(o2, "100", "1", "3.9")])
        e1 = self._add_body(TF, "u1", [[str(o1), "1", "1", "1", "1", "2", "20", "3.9", "1"]])
        e2 = self._add_body(TF, "u2", [[str(o2), "1", "1", "1", "1", "100", "1", "3.9", "1"]])
        self._add_sidecar(TF, [self._link(930001, "CRO-USDT-SWAP", [e1]),
                               self._link(930002, "OKB-USDT-SWAP", [e2])])
        ck = TMP / f"{self.name}_ck.json"
        plan = self._plan()
        self._apply(plan, checkpoint=str(ck))
        n = self.con.execute("SELECT COUNT(*) FROM candle_versions").fetchone()[0]
        self._apply(plan, checkpoint=str(ck))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM candle_versions").fetchone()[0], n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
