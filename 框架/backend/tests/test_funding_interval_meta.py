# -*- coding: utf-8 -*-
"""周期元数据表（审计 Bug 4.1 · 独立表方案）与 5m 冻结策略的**常规回归测试**。

离线、零网络、零正式库写入。临时库用内存 SQLite；真实库只以 `mode=ro` 打开。

来源（由一次性验收脚本提升为常规测试集）
----------------------------------------
  · `周期元数据表修复与5m冷备_2026-10-13/11_周期元数据表验收.py`（9 项）
  · `周期元数据表修复与5m冷备_2026-10-13/21_5m策略校验.py`（7 项）
  · `审计报告对账_2026-10-13/C0_审计报告对账与处置建议.md` §8「真正待办清单」P2：
    「建资金费周期元数据历史表，按 `available_at_ms <= T` 严格因果查询」

覆盖
----
  A. `_fim_query.interval_hours_at` 的**因果语义**（临时库自造数据，与真实表无关）
  B. 真实周期元数据表的**不变量**（表存在时才跑，否则 skip）
  C. **迁移 DDL ≡ 已建成的独立表**（迁移件漂移的机械守门）
  D. `_tf_scope_policy` 的 5m 冻结 / 1m 拒绝判据（纯函数）

为什么要有 C 类
--------------
本目录迁移预备件初稿把源文件台账写成 `funding_interval_source` 且多一列 `created_at`，
而实际表与查询 API 用的是 `source_files` ⇒ 照初稿执行会让
`_fim_query.interval_hours_at()` 的 `JOIN source_files` 直接 `no such table`。
C 类把「迁移 DDL 建出的结构」与「独立库真实结构」逐列对齐，此类漂移不再依赖人工复核。

运行：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_funding_interval_meta -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 后端实现/
SITE = ROOT.parent                                     # 网站逆向研究/
W43 = SITE / "周期元数据表修复与5m冷备_2026-10-13"
MIGRATION_SQL = W43 / "12_生产库迁移_预备未执行.sql"
REAL_DB = W43 / "周期元数据表" / "funding_interval_meta.sqlite3"
FIM_MODULE = W43 / "_fim_query.py"
POLICY_MODULE = ROOT / "历史下载" / "_tf_scope_policy.py"


def _load(name: str, path: Path):
    """按文件路径加载模块（不污染 sys.path，避免与既有同名模块冲突）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Prereq:
    """一次性加载被测模块；缺件时相关用例 skip（而不是报错）。"""

    fim = None
    policy = None
    load_error = None

    @classmethod
    def ensure(cls):
        if cls.load_error is not None:
            raise unittest.SkipTest(cls.load_error)
        if cls.fim is not None:
            return
        try:
            cls.fim = _load("_fim_query_under_test", FIM_MODULE)
            cls.policy = _load("_tf_scope_policy_under_test", POLICY_MODULE)
        except Exception as exc:                        # noqa: BLE001
            cls.load_error = f"被测模块不可用（{exc}）"
            raise unittest.SkipTest(cls.load_error) from exc


# ── 迁移 DDL 解析 ────────────────────────────────────────────────────────────
def migration_create_statements():
    """从迁移件中抽出所有 `CREATE ...` 语句（去掉 `--` 注释行与 BEGIN/COMMIT）。"""
    text = MIGRATION_SQL.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    out = []
    for raw in body.split(";"):
        stmt = raw.strip()
        if stmt and stmt.upper().startswith("CREATE"):
            out.append(stmt)
    return out


def temp_db_from_migration():
    """在内存库中执行迁移件的 CREATE 语句，得到与迁移件等价的空结构。"""
    con = sqlite3.connect(":memory:")
    for stmt in migration_create_statements():
        con.execute(stmt)
    return con


def columns_of(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


# ── A. 因果语义（临时库自造数据）────────────────────────────────────────────
class FimQueryCausalSemanticsTest(unittest.TestCase):
    """`interval_hours_at` 必须严格因果：只认 `available_at_ms <= T` 的最近一笔。"""

    SYM = "TESTSYM"
    # (calc_time_ms, interval_hours, archive_publish_ms)
    ROWS = [
        (1_000_000, 8, 1_000_000 + 86_400_000 * 30),
        (2_000_000, 4, 2_000_000 + 86_400_000 * 30),
        (3_000_000, 1, 3_000_000 + 86_400_000 * 30),
    ]

    def setUp(self):
        _Prereq.ensure()
        self.con = temp_db_from_migration()
        self.con.execute(
            "INSERT INTO source_files(source_id, zip_name, symbol, period, bytes, sha256)"
            " VALUES (1, 'TESTSYM-fundingRate-2020-01.zip', ?, '2020-01', 1, 'deadbeef')",
            (self.SYM,))
        for calc, iv, publish in self.ROWS:
            self.con.execute(
                "INSERT INTO funding_interval_meta(exchange, market, symbol, calc_time_ms,"
                " funding_interval_hours, last_funding_rate, avail_settlement_ms,"
                " avail_archive_publish_ms, source_id) VALUES ('binance','um',?,?,?,?,?,?,1)",
                (self.SYM, calc, iv, "0.0001", calc, publish))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _at(self, T, basis="settlement"):
        return _Prereq.fim.interval_hours_at(self.SYM, T, basis=basis, con=self.con)

    def test_returns_none_before_first_event(self):
        self.assertIsNone(self._at(self.ROWS[0][0] - 1))

    def test_returns_row_at_exact_event_time(self):
        r = self._at(self.ROWS[0][0])
        self.assertIsNotNone(r)
        self.assertEqual(r["funding_interval_hours"], 8)

    def test_last_known_causal_between_events(self):
        r = self._at(2_500_000)
        self.assertEqual(r["funding_interval_hours"], 4)
        self.assertEqual(r["calc_time_ms"], 2_000_000)

    def test_unknown_symbol_returns_none(self):
        self.assertIsNone(_Prereq.fim.interval_hours_at("NOSUCHSYM", 9_999_999, con=self.con))
        self.assertFalse(_Prereq.fim.has_symbol("NOSUCHSYM", con=self.con))
        self.assertTrue(_Prereq.fim.has_symbol(self.SYM, con=self.con))

    def test_never_defaults_to_eight(self):
        """无任何满足行 ⇒ None；绝不用 8 兜底。"""
        for T in (0, 999_999):
            self.assertIsNone(self._at(T), f"T={T} 应返回 None 而非默认 8")

    def test_archive_publish_basis_is_stricter(self):
        """archive_publish 口径下，归档发布前取不到值（而 settlement 能取到）。"""
        T = 1_500_000                       # 落在第 1、2 笔之间
        self.assertIsNotNone(self._at(T, basis="settlement"))
        self.assertIsNone(self._at(T, basis="archive_publish"))

    def test_invalid_basis_raises(self):
        with self.assertRaises(ValueError):
            self._at(2_500_000, basis="bogus")

    def test_returned_row_available_at_never_exceeds_T(self):
        for T in range(0, 4_000_000, 137_000):
            for basis in ("settlement", "archive_publish"):
                r = _Prereq.fim.interval_hours_at(self.SYM, T, basis=basis, con=self.con)
                if r is not None:
                    self.assertLessEqual(r["available_at_ms"], T,
                                         f"basis={basis} T={T} 返回了未来可得行")


# ── B. 真实表不变量（只读；缺表则 skip）─────────────────────────────────────
@unittest.skipUnless(REAL_DB.exists(), f"独立周期元数据表不存在：{REAL_DB}")
class RealTableInvariantsTest(unittest.TestCase):
    """真实独立表的结构与取值不变量（只读打开，绝不写）。"""

    @classmethod
    def setUpClass(cls):
        _Prereq.ensure()
        cls.con = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
        cls.con.execute("PRAGMA query_only=1")

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_row_count_positive(self):
        n = self.con.execute("SELECT COUNT(*) FROM funding_interval_meta").fetchone()[0]
        self.assertGreater(n, 0)
        # 源文件台账是另一量级（文件数 << 事件数），只断言非空，不作相等断言
        self.assertGreater(
            self.con.execute("SELECT COUNT(*) FROM source_files").fetchone()[0], 0)

    def test_primary_key_is_unique(self):
        total = self.con.execute("SELECT COUNT(*) FROM funding_interval_meta").fetchone()[0]
        distinct = self.con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT exchange, market, symbol, calc_time_ms"
            " FROM funding_interval_meta)").fetchone()[0]
        self.assertEqual(total, distinct, "PK(exchange,market,symbol,calc_time_ms) 有重复")

    def test_avail_settlement_equals_calc_time(self):
        bad = self.con.execute(
            "SELECT COUNT(*) FROM funding_interval_meta"
            " WHERE avail_settlement_ms <> calc_time_ms").fetchone()[0]
        self.assertEqual(bad, 0, "settlement 口径假设被破坏（应恒等于 calc_time_ms）")

    def test_interval_value_domain(self):
        vals = [r[0] for r in self.con.execute(
            "SELECT DISTINCT funding_interval_hours FROM funding_interval_meta")]
        self.assertTrue(set(vals) <= {1, 2, 4, 8}, f"越域取值：{sorted(set(vals) - {1,2,4,8})}")

    def test_no_negative_or_zero_interval(self):
        bad = self.con.execute(
            "SELECT COUNT(*) FROM funding_interval_meta"
            " WHERE funding_interval_hours IS NOT NULL AND funding_interval_hours <= 0"
        ).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_uniform_eight_hours_is_falsified(self):
        """「统一按 8h」若成立则本表无存在必要；用真实分布守住该结论。"""
        total, non8 = self.con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN funding_interval_hours <> 8 THEN 1 ELSE 0 END)"
            " FROM funding_interval_meta").fetchone()
        self.assertGreater(total, 0)
        self.assertGreater(non8 / total, 0.5,
                           f"non-8h 占比 {non8}/{total} 过低，与「统一 8h 被证伪」结论冲突")

    def test_source_files_referential_integrity(self):
        orphan = self.con.execute(
            "SELECT COUNT(*) FROM funding_interval_meta m"
            " LEFT JOIN source_files s ON s.source_id = m.source_id WHERE s.source_id IS NULL"
        ).fetchone()[0]
        self.assertEqual(orphan, 0, "存在悬空 source_id")

    def test_zip_name_is_unique(self):
        total = self.con.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
        distinct = self.con.execute(
            "SELECT COUNT(DISTINCT zip_name) FROM source_files").fetchone()[0]
        self.assertEqual(total, distinct)


# ── C. 迁移 DDL ≡ 已建成的独立表 ────────────────────────────────────────────
class MigrationDdlParityTest(unittest.TestCase):
    """迁移预备件建出的结构必须与已验收的独立表逐列一致（防「等价 DDL」漂移）。"""

    def setUp(self):
        _Prereq.ensure()

    def test_migration_uses_source_files_not_funding_interval_source(self):
        stmts = "\n".join(migration_create_statements())
        self.assertIn("source_files", stmts)
        self.assertNotIn("funding_interval_source", stmts,
                         "迁移件又用了与查询 API 不一致的表名")
        self.assertIn("funding_interval_meta", stmts)

    def test_migration_ddl_is_parseable_and_creates_expected_tables(self):
        con = temp_db_from_migration()
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertTrue({"source_files", "funding_interval_meta"} <= names, f"缺表：{names}")

    def test_migration_schema_matches_built_table(self):
        if not REAL_DB.exists():
            self.skipTest(f"独立库不存在：{REAL_DB}")
        mig = temp_db_from_migration()
        real = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
        try:
            for table in ("source_files", "funding_interval_meta"):
                self.assertEqual(
                    columns_of(mig, table), columns_of(real, table),
                    f"迁移件与独立库的 {table} 列集不一致")
        finally:
            mig.close()
            real.close()

    def test_fim_query_works_against_migration_schema(self):
        """回归：迁移件建出的结构必须能被查询 API 直接使用（初稿在此必然失败）。"""
        con = temp_db_from_migration()
        try:
            con.execute(
                "INSERT INTO source_files(source_id, zip_name, symbol, period, bytes, sha256)"
                " VALUES (7, 'X-fundingRate-2020-01.zip', 'XUSDT', '2020-01', 1, 'aa')")
            con.execute(
                "INSERT INTO funding_interval_meta(exchange, market, symbol, calc_time_ms,"
                " funding_interval_hours, last_funding_rate, avail_settlement_ms,"
                " avail_archive_publish_ms, source_id)"
                " VALUES ('binance','um','XUSDT', 500, 4, '0.0001', 500, 900, 7)")
            con.commit()
            r = _Prereq.fim.interval_hours_at("XUSDT", 500, con=con)
            self.assertIsNotNone(r, "迁移结构下查询 API 取不到值（表名/列名不匹配？）")
            self.assertEqual(r["funding_interval_hours"], 4)
            self.assertEqual(r["source_file"], "X-fundingRate-2020-01.zip")
        finally:
            con.close()

    def test_negative_control_old_schema_breaks_query(self):
        """负例控制：初稿的 `funding_interval_source` 表名必须让查询 API 失败。

        若本用例也「通过」，说明上面的对齐测试缺乏判别力（恒真），必须修正测试本身。
        """
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(
                "CREATE TABLE funding_interval_source(source_id INTEGER PRIMARY KEY,"
                " zip_name TEXT, symbol TEXT, period TEXT, bytes INTEGER, sha256 TEXT,"
                " created_at INTEGER);"
                "CREATE TABLE funding_interval_meta(exchange TEXT, market TEXT, symbol TEXT,"
                " calc_time_ms INTEGER, funding_interval_hours INTEGER,"
                " last_funding_rate TEXT, avail_settlement_ms INTEGER,"
                " avail_archive_publish_ms INTEGER, source_id INTEGER);")
            con.execute("INSERT INTO funding_interval_source VALUES"
                        " (1,'z.zip','XUSDT','2020-01',1,'aa',0)")
            con.execute("INSERT INTO funding_interval_meta VALUES"
                        " ('binance','um','XUSDT',500,4,'0.0001',500,900,1)")
            con.commit()
            with self.assertRaises(sqlite3.OperationalError):
                _Prereq.fim.interval_hours_at("XUSDT", 500, con=con)
        finally:
            con.close()


# ── D. 5m 冻结 / 1m 拒绝策略（纯函数）───────────────────────────────────────
class TfScopePolicyTest(unittest.TestCase):
    """`_tf_scope_policy` 是周期范围的**唯一事实源**；这些判据不得被静默放宽。"""

    def setUp(self):
        _Prereq.ensure()
        self.p = _Prereq.policy

    def test_rejected_tfs_is_1m_only(self):
        self.assertEqual(tuple(self.p.REJECTED_TFS), ("1m",))

    def test_frozen_history_tfs_is_5m(self):
        self.assertEqual(tuple(self.p.FROZEN_HISTORY_TFS), ("5m",),
                         "5m 历史导入应保持冻结（用户 2026-10-13 指令：仅 ZIP 冷备）")

    def test_allowed_excludes_1m_and_5m(self):
        allowed = set(self.p.allowed_history_tfs())
        self.assertNotIn("1m", allowed)
        self.assertNotIn("5m", allowed)
        self.assertTrue({"1d", "12h", "8h", "4h", "1h", "30m", "15m"} <= allowed)

    def test_check_refuses_5m(self):
        ok, problems = self.p.check_history_tfs(["5m"], entry="校验:5m导入")
        self.assertFalse(ok)
        self.assertEqual([q["reason"] for q in problems], ["frozen_5m_history"])

    def test_check_refuses_1m(self):
        ok, problems = self.p.check_history_tfs(["1m"], entry="校验:1m导入")
        self.assertFalse(ok)
        self.assertEqual([q["reason"] for q in problems], ["rejected_1m_deleted"])

    def test_check_accepts_allowed_set(self):
        ok, problems = self.p.check_history_tfs(["1h", "15m"], entry="校验:正常")
        self.assertTrue(ok, f"不应拒绝：{problems}")

    def test_check_flags_unknown_tf(self):
        ok, problems = self.p.check_history_tfs(
            ["7m"], entry="校验:未知", known=self.p.FULL_HISTORY_ORDER)
        self.assertFalse(ok)
        self.assertEqual([q["reason"] for q in problems], ["unknown_tf"])

    def test_policy_snapshot_sha_matches_file(self):
        snap = self.p.policy_snapshot()
        actual = hashlib.sha256(POLICY_MODULE.read_bytes()).hexdigest()
        self.assertEqual(snap["module_sha256"], actual)
        self.assertEqual(snap["frozen_history_tfs"], ["5m"])
        self.assertEqual(snap["rejected_tfs"], ["1m"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
