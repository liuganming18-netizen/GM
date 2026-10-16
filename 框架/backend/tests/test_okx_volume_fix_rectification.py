# -*- coding: utf-8 -*-
"""OKX `volume` 修复工具（**整改 rev2**）的故障回归。

**加载新目录**：`outputs/网站逆向研究/OKX成交量离线修复整改_2026-10-11/`
（`04_okx_volume_fix_v2.py` / `06_okx_volume_rollback_v2.py` / `volccy_common.py`）。

覆盖 75 合同 §1–§5 的**独立反例**：
  §1 混合新建/既有修订 source（无 checkpoint 重跑）不再 FK 失败 / 错挂（lastrowid 禁用）；
     半途中断可恢复；多币与同币多源。
  §2 错误库 checkpoint 被拒；apply→head-revert→同 checkpoint 再 apply **真实重切**（不虚报）；
     各提交阶段崩溃可恢复；部分 head 切换；available_at 保留首次真实修复时点。
  §3 同根 volCcy 20/200 冲突 fail closed；SHA 失效 / 缺行 / 清单变化 fail closed；OHLC 不一致 fail closed。
  §4 num_eq 相邻大整数不等、拒绝 NaN/Inf/空；数值等价。
  §5 回退逐根短批 + 持久断点；缺 journal 拒绝；生产 purge 硬拒绝；第三方 head 不抢。

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest \
      tests.test_okx_volume_fix_rectification -v
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
    MIG_W1 / "OKX成交量离线修复整改_2026-10-11" / "04_okx_volume_fix_v2.py",
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

HERE = Path(__file__).resolve().parent.parent                       # 框架/backend
# W1（2026-10-16）：原指向工程外路径（已删）⇒ 改按工程布局推导。
FRAMEWORK = HERE.parent                                             # 框架/（含 data_hub 包）
PROJECT_ROOT = HERE.parent.parent                                   # 工程根
PROJ = FRAMEWORK
# 该批旧修复脚本**未随工程迁移** ⇒ 给出工程内正式位置（可用 `OKX_RECT_FIX_DIR` 覆盖）。
FIXDIR = Path(os.environ.get("OKX_RECT_FIX_DIR") or (
    PROJECT_ROOT / "数据" / "研究输入" / "外部迁移_2026-10-16" /
    "OKX成交量离线修复整改_2026-10-11"))                             # ← 未迁移
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))

_TMP = tempfile.TemporaryDirectory(prefix="okx_vol_rect_")
TMP = Path(_TMP.name)

# 封闭范围文件（避免依赖真实 scope；crypto-only 过滤可测）
_SCOPE = TMP / "scope_crypto.json"
_SCOPE_SYMS = ["CRO-USDT-SWAP", "LUNA-USDT-SWAP", "OKB-USDT-SWAP", "OL-USDT-SWAP",
               "RAY-USDT-SWAP", "PI-USDT-SWAP"]
_SCOPE.write_text(json.dumps({"scope_version": "test", "okx_symbols": _SCOPE_SYMS}),
                  encoding="utf-8")
os.environ["POOL_SCOPE_JSON"] = str(_SCOPE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, FIXDIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fix = _load("okxfixv2", "04_okx_volume_fix_v2.py")
rollback = _load("okxrollbackv2", "06_okx_volume_rollback_v2.py")
C = sys.modules["volccy_common"]

TF = "15m"
IV = 900_000
MARKET = "swap"


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


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
    def _add_source(self, sid, symbol, rows, tf=TF):
        """rows = [(open_time, vol, volCcy, volCcyQuote)] ⇒ 按**缺陷映射**写 volume=r5 + head。"""
        url = "https://www.okx.com/api/v5/market/history-candles"
        opens = [r[0] for r in rows]
        self.con.execute(
            "INSERT INTO sources(id,exchange,market,symbol,timeframe,source_url,file_path,sha256,"
            "imported_at,downloaded_at,first_open,last_close,row_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "okx", MARKET, symbol, tf, url, "", _sha(f"src|{symbol}|{tf}|{sid}"), 1000, 1000,
             min(opens), max(opens) + IV - 1, len(rows)))
        for ot, v5, v6, v7 in rows:
            self.con.execute(
                "INSERT INTO candle_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("okx", MARKET, symbol, tf, ot, ot + IV - 1, "0.08062", "0.08063", "0.08042",
                 "0.08045", str(v5), str(v7), 0, "", "", sid, None, 1))
            self.con.execute(
                "INSERT INTO candle_heads VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(exchange,market,symbol,timeframe,open_time) DO NOTHING",
                ("okx", MARKET, symbol, tf, ot, sid))
        self.con.commit()

    def _add_body(self, tf, data_rows, tag=""):
        d = self.raw / tf
        d.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"code": "0", "msg": "", "data": data_rows}).encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        p = d / f"{sha[:12]}{tag}.body"
        p.write_bytes(payload)
        return {"url": "https://www.okx.com/api/v5/market/history-candles?instId=x&bar=15m",
                "host": "www.okx.com", "http_code": 200, "body_path": str(p),
                "body_bytes": len(payload), "body_sha256": sha, "sha_kind": "HTTP原始字节SHA256",
                "fetched_at": "2026-10-11T00:00:00Z", "phase": "fetch"}

    def _link(self, sid, symbol, evidence, open_times, tf=TF):
        return {"source_id": sid, "exchange": "okx", "symbol": symbol, "tf": tf,
                "first_open": _iso(min(open_times)), "last_open": _iso(max(open_times)),
                "rows_added": len(open_times), "open_times_n": len(open_times),
                "open_times_digest": C.sha256_text(",".join(str(o) for o in sorted(open_times))),
                "evidence_n": len(evidence), "evidence": evidence}

    def _add_sidecar(self, tf, links):
        d = self.raw / tf
        d.mkdir(parents=True, exist_ok=True)
        p = d / "import_evidence.json"
        cur = {"links": []}
        if p.exists():
            cur = json.loads(p.read_text(encoding="utf-8"))
        cur["links"].extend(links)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")

    def _okx_row(self, ot, vol, volCcy, volCcyQuote="3.942"):
        return [str(ot), "0.08062", "0.08063", "0.08042", "0.08045", str(vol), str(volCcy),
                str(volCcyQuote), "1"]

    def _plan(self, only=None, tfs=(TF,), include_superseded=False):
        return fix.build_plan(self.db, self.raw, tfs=tfs, only_sources=only,
                              include_unchanged=True, include_superseded=include_superseded)

    def _apply(self, plan, **kw):
        return fix.apply_plan(self.db, plan, raw_dir=str(self.raw),
                              batch_size=kw.pop("batch_size", 1000),
                              journal=kw.pop("journal", None),
                              available_at_mode=kw.pop("available_at_mode", "repair"),
                              min_free_gb=kw.pop("min_free_gb", 0.0),
                              wal_budget_gb=kw.pop("wal_budget_gb", 0.0),
                              health_url="", log=lambda *_: None)

    def _heads(self):
        return {int(r[0]): int(r[1]) for r in self.con.execute(
            "SELECT open_time,source_id FROM candle_heads")}

    def _vol(self, sid, ot):
        r = self.con.execute("SELECT volume FROM candle_versions WHERE source_id=? AND open_time=?",
                             (sid, ot)).fetchone()
        return None if r is None else r[0]

    def _new_sid(self, plan):
        return self.con.execute("SELECT id FROM sources WHERE sha256=?",
                                (plan["plans"][0]["new_sha256"],)).fetchone()[0]


# ══════════════════════════ §1 source 身份解析 ══════════════════════════
class SourceIdentity(Base):
    def _seed_two(self):
        o1, o2 = 1617871500000, 1617872400000
        self._add_source(900001, "CRO-USDT-SWAP", [(o1, "2", "20", "3.9")])
        self._add_source(900002, "CRO-USDT-SWAP", [(o2, "3", "30", "4.0")])
        e1 = self._add_body(TF, [self._okx_row(o1, "2", "20", "3.9")])
        e2 = self._add_body(TF, [self._okx_row(o2, "3", "30", "4.0")])
        self._add_sidecar(TF, [self._link(900001, "CRO-USDT-SWAP", [e1], [o1]),
                               self._link(900002, "CRO-USDT-SWAP", [e2], [o2])])
        return o1, o2

    def test_mixed_new_and_existing_revision_no_fk_and_correct_attach(self):
        """先只修第2源（建修订），再整计划修2源：**不得** FK 失败 / 错挂（旧 lastrowid 缺陷）。"""
        o1, o2 = self._seed_two()
        r2 = self._apply(self._plan(only={900002}))
        rid2 = r2["results"][0]["new_source_id"]
        # 完整计划（两源，强制保留已修订来源以复现「混合新旧」路径，无 checkpoint）
        r = self._apply(self._plan(include_superseded=True))
        by_old = {x["old_source_id"]: x for x in r["results"]}
        self.assertEqual(by_old[900002]["new_source_id"], rid2, "既有修订必须被复用（非新建）")
        self.assertNotEqual(by_old[900001]["new_source_id"], rid2, "第1源不得错挂到第2源的修订")
        # 各自版本行归属正确
        self.assertEqual(self._vol(by_old[900001]["new_source_id"], o1), "20")
        self.assertIsNone(self._vol(by_old[900001]["new_source_id"], o2))
        self.assertEqual(self._vol(rid2, o2), "30")
        self.assertIsNone(self._vol(rid2, o1))
        # head 归属正确
        self.assertEqual(self._heads()[o1], by_old[900001]["new_source_id"])
        self.assertEqual(self._heads()[o2], rid2)

    def test_multi_symbol_and_same_symbol_multi_source(self):
        """多币 + 同币多源混合：逐源独立、互不污染。"""
        o1, o2, o3 = 1617871500000, 1617872400000, 1617873300000
        self._add_source(910001, "CRO-USDT-SWAP", [(o1, "2", "20", "3.9")])
        self._add_source(910002, "OKB-USDT-SWAP", [(o2, "12345", "123.45", "7.1")])
        self._add_source(910003, "CRO-USDT-SWAP", [(o3, "7", "70", "5.0")])
        e1 = self._add_body(TF, [self._okx_row(o1, "2", "20", "3.9")])
        e2 = self._add_body(TF, [self._okx_row(o2, "12345", "123.45", "7.1")])
        e3 = self._add_body(TF, [self._okx_row(o3, "7", "70", "5.0")])
        self._add_sidecar(TF, [self._link(910001, "CRO-USDT-SWAP", [e1], [o1]),
                               self._link(910002, "OKB-USDT-SWAP", [e2], [o2]),
                               self._link(910003, "CRO-USDT-SWAP", [e3], [o3])])
        r = self._apply(self._plan())
        self.assertEqual(len(r["results"]), 3)
        got = {(x["symbol"], x["old_source_id"]): x for x in r["results"]}
        self.assertEqual(self._vol(got[("CRO-USDT-SWAP", 910001)]["new_source_id"], o1), "20")
        self.assertEqual(self._vol(got[("OKB-USDT-SWAP", 910002)]["new_source_id"], o2), "123.45")
        self.assertEqual(self._vol(got[("CRO-USDT-SWAP", 910003)]["new_source_id"], o3), "70")

    def test_midway_interrupt_then_full_run(self):
        """半途中断（只完成 1 源）后再整计划：补齐剩余源、幂等、不 FK 失败。"""
        o1, o2 = self._seed_two()
        self._apply(self._plan(only={900001}))
        plan2 = self._plan()
        self.assertEqual(plan2["totals"]["sources_planned"], 1, "已完成的源应被排除")
        r = self._apply(plan2)
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["old_source_id"], 900002)
        self.assertEqual(self._vol(r["results"][0]["new_source_id"], o2), "30")
        self.assertEqual(self._plan()["totals"]["sources_planned"], 0, "全部完成后幂等为 0")


# ══════════════════════════ §2 checkpoint / 恢复 ══════════════════════════
class CheckpointResume(Base):
    def _seed(self, sid=920001, n=3):
        rows = [(1617871500000 + i * IV, "2", "20", "3.9") for i in range(n)]
        self._add_source(sid, "CRO-USDT-SWAP", rows)
        ev = self._add_body(TF, [self._okx_row(o, v, c, q) for o, v, c, q in rows])
        self._add_sidecar(TF, [self._link(sid, "CRO-USDT-SWAP", [ev], [r[0] for r in rows])])
        return [r[0] for r in rows]

    def test_wrong_db_journal_refused(self):
        """错误库 checkpoint ⇒ 拒绝（绑定库身份 realpath）。"""
        self._seed()
        j1 = TMP / "ck_wrong.json"
        self._apply(self._plan(), journal=str(j1))
        other = TMP / "other.sqlite3"
        if other.exists():
            other.unlink()
        c2 = sqlite3.connect(str(other))
        c2.executescript((PROJ / "data_hub" / "schema.sql").read_text(encoding="utf-8"))
        c2.commit()
        c2.close()
        with self.assertRaises(fix.JournalBindError):
            fix.load_journal(str(j1), other, {"plan_global_digest": "x"}, "repair")

    def test_head_revert_then_same_checkpoint_reapply_truthful(self):
        """apply→head-revert→同 checkpoint 再 apply：**真实重切**（不虚报），最终值正确。"""
        ots = self._seed(n=3)
        j = TMP / "ck_revert.json"
        r1 = self._apply(self._plan(), journal=str(j))
        nsid = r1["results"][0]["new_source_id"]
        rollback.head_revert(self.db, [(920001, nsid, "CRO-USDT-SWAP", TF)],
                             batch_size=1000, log=lambda *_: None)
        self.assertEqual(self._heads()[ots[0]], 920001, "已回退到旧来源")
        r2 = self._apply(self._plan(), journal=str(j))
        rec = r2["results"][0]
        self.assertEqual(rec["heads_switched"], 3, "必须真实重切 3 个 head（不虚报 1）")
        self.assertEqual(self._heads()[ots[0]], nsid)
        self.assertEqual(self._vol(nsid, ots[0]), "20", "最终值必须是修正值")

    def test_resume_after_partial_head_switch(self):
        """部分 head 切换：只补切剩余，不重复已切。"""
        ots = self._seed(n=3)
        j = TMP / "ck_partial.json"
        r1 = self._apply(self._plan(), journal=str(j))
        nsid = r1["results"][0]["new_source_id"]
        # 手工把 2 个 head 切回旧来源（模拟部分切换）
        for ot in ots[:2]:
            self.con.execute("UPDATE candle_heads SET source_id=920001 WHERE open_time=?", (ot,))
        self.con.commit()
        r2 = self._apply(self._plan(), journal=str(j))
        self.assertEqual(r2["results"][0]["heads_switched"], 2)
        for ot in ots:
            self.assertEqual(self._heads()[ot], nsid)

    def test_crash_stages_recoverable(self):
        """各提交阶段崩溃（回退 journal stage + 删部分版本行）后可恢复。"""
        ots = self._seed(n=3)
        j = TMP / "ck_crash.json"
        r1 = self._apply(self._plan(), journal=str(j))
        nsid = r1["results"][0]["new_source_id"]
        # 模拟：崩溃在 versions_written（head 仍指旧来源 + 缺 1 行版本 + journal 回退阶段）
        self.con.execute("UPDATE candle_heads SET source_id=920001 WHERE open_time=?", (ots[1],))
        self.con.execute("DELETE FROM candle_versions WHERE source_id=? AND open_time=?",
                         (nsid, ots[1]))
        self.con.commit()
        doc = json.loads(j.read_text(encoding="utf-8"))
        doc["sources"]["920001"]["stage"] = "source_created"
        doc["sources"]["920001"]["rows_written"] = 1
        j.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self._apply(self._plan(), journal=str(j))
        for ot in ots:
            self.assertEqual(self._vol(nsid, ot), "20")
            self.assertEqual(self._heads()[ot], nsid)

    def test_available_at_keeps_first_repair_time(self):
        """§2 修订已存在：补写行的 available_at 保留**首次**修复时点；来源 imported_at 不变。"""
        ots = self._seed(n=3)
        j = TMP / "ck_avail.json"
        r1 = self._apply(self._plan(), journal=str(j))
        nsid = r1["results"][0]["new_source_id"]
        first_ms = json.loads(j.read_text(encoding="utf-8"))["sources"]["920001"]["repair_ms"]
        imported = self.con.execute("SELECT imported_at FROM sources WHERE id=?",
                                    (nsid,)).fetchone()[0]
        # head 仍指旧来源 + 删除一行版本 + 推进时间，再 apply
        self.con.execute("UPDATE candle_heads SET source_id=920001 WHERE open_time=?", (ots[2],))
        self.con.execute("DELETE FROM candle_versions WHERE source_id=? AND open_time=?",
                         (nsid, ots[2]))
        self.con.commit()
        time.sleep(0.02)
        self._apply(self._plan(), journal=str(j))
        avail = self.con.execute("SELECT available_at FROM candle_versions WHERE source_id=? AND "
                                 "open_time=?", (nsid, ots[2])).fetchone()[0]
        self.assertEqual(avail, first_ms, "补写行必须沿用首次修复时点，不得取新时间")
        self.assertEqual(self.con.execute("SELECT imported_at FROM sources WHERE id=?",
                                          (nsid,)).fetchone()[0], imported)


# ══════════════════════════ §3 原件冲突 / 绑定 ══════════════════════════
class OriginalIntegrity(Base):
    def test_same_root_conflict_vol20_200_fail_closed(self):
        """同根两份原件 volCcy 20/200 ⇒ fail closed（不得先到先得写 20）。"""
        o = 1617871500000
        self._add_source(930001, "CRO-USDT-SWAP", [(o, "2", "20", "3.9")])
        e1 = self._add_body(TF, [self._okx_row(o, "2", "20", "3.9")], tag="-a")
        e2 = self._add_body(TF, [self._okx_row(o, "2", "200", "39.0")], tag="-b")
        self._add_sidecar(TF, [self._link(930001, "CRO-USDT-SWAP", [e1, e2], [o])])
        before = [tuple(r) for r in self.con.execute("SELECT * FROM candle_versions")]
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "fail_closed_original_conflict")
        self._apply(plan)
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT * FROM candle_versions")],
                         before, "冲突来源不得被写")

    def test_duplicate_identical_body_deduped(self):
        """同根两份**全字段一致**原件 ⇒ 去重，正常修复。"""
        o = 1617871500000
        self._add_source(930002, "CRO-USDT-SWAP", [(o, "2", "20", "3.9")])
        e1 = self._add_body(TF, [self._okx_row(o, "2", "20", "3.9")], tag="-a")
        e2 = self._add_body(TF, [self._okx_row(o, "2", "20", "3.9")], tag="-b")
        self._add_sidecar(TF, [self._link(930002, "CRO-USDT-SWAP", [e1, e2], [o])])
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 1)
        self.assertEqual(plan["plans"][0]["dup_identical_bodies"], 1)
        r = self._apply(plan)
        self.assertEqual(self._vol(r["results"][0]["new_source_id"], o), "20")

    def test_body_sha_mismatch_fail_closed(self):
        """原件 SHA 失效 ⇒ 不写该来源（binding_mismatch）。"""
        o = 1617871500000
        self._add_source(930003, "CRO-USDT-SWAP", [(o, "2", "20", "3.9")])
        ev = self._add_body(TF, [self._okx_row(o, "2", "20", "3.9")])
        Path(ev["body_path"]).write_bytes(b'{"code":"0","data":[["tampered"]]}')
        self._add_sidecar(TF, [self._link(930003, "CRO-USDT-SWAP", [ev], [o])])
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "binding_mismatch")

    def test_row_ohlc_inconsistent_fail_closed(self):
        """正文 OHLC 与库内旧行不一致 ⇒ fail closed。"""
        o = 1617871500000
        self._add_source(930004, "CRO-USDT-SWAP", [(o, "2", "20", "3.9")])
        bad = [str(o), "9.99", "9.99", "9.99", "9.99", "2", "20", "3.9", "1"]   # OHLC 不符
        ev = self._add_body(TF, [bad])
        self._add_sidecar(TF, [self._link(930004, "CRO-USDT-SWAP", [ev], [o])])
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "fail_closed_original_conflict")

    def test_body_manifest_change_between_plan_and_apply_fail_closed(self):
        """计划后原件清单变化（正文丢失）⇒ apply fail closed（不得沿用旧 row_count/sha）。"""
        o1, o2 = 1617871500000, 1617872400000
        self._add_source(930005, "CRO-USDT-SWAP",
                         [(o1, "2", "20", "3.9"), (o2, "3", "30", "4.0")])
        e1 = self._add_body(TF, [self._okx_row(o1, "2", "20", "3.9")], tag="-a")
        e2 = self._add_body(TF, [self._okx_row(o2, "3", "30", "4.0")], tag="-b")
        self._add_sidecar(TF, [self._link(930005, "CRO-USDT-SWAP", [e1, e2], [o1, o2])])
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 1)
        Path(e2["body_path"]).unlink()                       # 计划后正文丢失
        with self.assertRaises(RuntimeError):
            self._apply(plan)


# ══════════════════════════ §4 num_eq ══════════════════════════
class NumEq(unittest.TestCase):
    def test_large_adjacent_integers_not_equal(self):
        self.assertFalse(C.num_eq("9007199254740992", "9007199254740993"),
                         "float 会吞精度；有限 Decimal 必须判不等")

    def test_rejects_nan_inf_empty(self):
        for bad in ("nan", "NaN", "inf", "Infinity", "", None, "abc", "-inf"):
            self.assertFalse(C.num_eq(bad, "1"), f"{bad!r} 必须判不等")
            self.assertFalse(C.num_eq("1", bad), f"{bad!r} 必须判不等")

    def test_numeric_equivalence(self):
        self.assertTrue(C.num_eq("20", "20.0"))
        self.assertTrue(C.num_eq("0", "0.0"))
        self.assertFalse(C.num_eq("2", "20"))
        self.assertTrue(C.num_eq("123456789.123456789012345678901234567890",
                                 "123456789.123456789012345678901234567890"))

    def test_huge_precision_distinction(self):
        self.assertFalse(C.num_eq("123456789.000000000000000001", "123456789.000000000000000002"))


# ══════════════════════════ §5 回退 ══════════════════════════
class Rollback(Base):
    def _seed_apply(self, sid=940001, n=4):
        rows = [(1617871500000 + i * IV, "2", "20", "3.9") for i in range(n)]
        self._add_source(sid, "CRO-USDT-SWAP", rows)
        ev = self._add_body(TF, [self._okx_row(o, v, c, q) for o, v, c, q in rows])
        self._add_sidecar(TF, [self._link(sid, "CRO-USDT-SWAP", [ev], [r[0] for r in rows])])
        j = TMP / f"{self.name}.journal.json"
        r = self._apply(self._plan(), journal=str(j))
        return [r[0] for r in rows], r["results"][0]["new_source_id"], j

    def test_head_revert_short_batch_and_checkpoint(self):
        ots, nsid, _j = self._seed_apply(n=4)
        ck = TMP / f"{self.name}.ck.json"
        out = rollback.head_revert(self.db, [(940001, nsid, "CRO-USDT-SWAP", TF)],
                                   batch_size=2, checkpoint=str(ck), log=lambda *_: None)
        self.assertEqual(out[0]["heads_reverted"], 4)
        for ot in ots:
            self.assertEqual(self._heads()[ot], 940001)
        self.assertTrue(ck.exists(), "短批回退必须落持久断点")
        ckdoc = json.loads(ck.read_text(encoding="utf-8"))
        self.assertIn(f"940001->{nsid}", ckdoc["reverted"])

    def test_third_party_head_not_reverted(self):
        ots, nsid, _j = self._seed_apply(n=3)
        self.con.execute("INSERT INTO sources(id,exchange,market,symbol,timeframe,source_url,"
                         "file_path,sha256,imported_at,downloaded_at,first_open,last_close,row_count)"
                         " VALUES (940099,'okx','swap','CRO-USDT-SWAP','15m','other','','x',1,1,?,?,1)",
                         (ots[0], ots[0] + IV - 1))
        self.con.execute("INSERT INTO candle_versions VALUES ('okx','swap','CRO-USDT-SWAP','15m',"
                         "?,?,'1','1','1','1','9','9',0,'','',940099,NULL,1)",
                         (ots[0], ots[0] + IV - 1))
        self.con.execute("UPDATE candle_heads SET source_id=940099 WHERE open_time=?", (ots[0],))
        self.con.commit()
        rollback.head_revert(self.db, [(940001, nsid, "CRO-USDT-SWAP", TF)],
                             batch_size=1000, log=lambda *_: None)
        self.assertEqual(self._heads()[ots[0]], 940099, "第三方 head 不得被回退抢走")
        self.assertEqual(self._heads()[ots[1]], 940001)

    def test_rollback_requires_journal_gate(self):
        with self.assertRaises(SystemExit):
            rollback.main(["--db", str(self.db), "--journal", str(TMP / "nope.json")])

    def test_purge_refused_on_production(self):
        with self.assertRaises(SystemExit):
            rollback.purge_revision(C.PROD_DB, [])

    def test_purge_allowed_on_isolated_after_revert(self):
        ots, nsid, _j = self._seed_apply(n=2)
        rollback.head_revert(self.db, [(940001, nsid, "CRO-USDT-SWAP", TF)],
                             batch_size=1000, log=lambda *_: None)
        out = rollback.purge_revision(self.db, [(940001, nsid, "CRO-USDT-SWAP", TF)],
                                      log=lambda *_: None)
        self.assertEqual(out[0]["deleted_versions"], 2)
        self.assertIsNone(self.con.execute("SELECT id FROM sources WHERE id=?", (nsid,)).fetchone())


# ══════════════════════════ §7 范围 / 参数 ══════════════════════════
class ScopeAndParams(Base):
    def test_forbidden_timeframe_rejected(self):
        with self.assertRaises(ValueError):
            fix.build_plan(self.db, self.raw, tfs=("1m",))

    def test_crypto_only_excludes_out_of_scope(self):
        o1, o2 = 1617871500000, 1617872400000
        self._add_source(950001, "CRO-USDT-SWAP", [(o1, "2", "20", "3.9")])
        self._add_source(950002, "ZZZ-USDT-SWAP", [(o2, "5", "50", "3.9")])   # 不在 scope
        e1 = self._add_body(TF, [self._okx_row(o1, "2", "20", "3.9")])
        e2 = self._add_body(TF, [self._okx_row(o2, "5", "50", "3.9")])
        self._add_sidecar(TF, [self._link(950001, "CRO-USDT-SWAP", [e1], [o1]),
                               self._link(950002, "ZZZ-USDT-SWAP", [e2], [o2])])
        plan = self._plan()
        self.assertEqual(plan["totals"]["sources_planned"], 1)
        self.assertEqual(plan["plans"][0]["symbol"], "CRO-USDT-SWAP")

    def test_batch_size_bounds(self):
        self.assertEqual(C.validate_batch_size(500), 500)
        with self.assertRaises(ValueError):
            C.validate_batch_size(0)
        with self.assertRaises(ValueError):
            C.validate_batch_size(C.BATCH_MAX + 1)

    def test_already_revised_source_excluded(self):
        """已修订来源（volrev=1）不再进入计划（防重复修订）。"""
        o = 1617871500000
        self._add_source(950003, "CRO-USDT-SWAP", [(o, "2", "20", "3.9")])
        ev = self._add_body(TF, [self._okx_row(o, "2", "20", "3.9")])
        self._add_sidecar(TF, [self._link(950003, "CRO-USDT-SWAP", [ev], [o])])
        self._apply(self._plan())
        self.assertEqual(self._plan()["totals"]["sources_planned"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
