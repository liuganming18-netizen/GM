# -*- coding: utf-8 -*-
"""资金费周期元数据 · **消费侧接线回归测试**（离线、零网络、只读伴生库）。

被测对象（**测谁就 import 谁**）
------------------------------
1. `strategies/funding_interval_meta_source.py` —— 消费侧适配层（本轮新增，非冻结）。
2. `周期元数据表修复与5m冷备_2026-10-13/_fim_query.py` —— 只读因果查询 API
   （**唯一事实源**，按文件路径加载，不复制一份查询逻辑）。
3. `strategies/wx_daily_375.py` 的消费契约 `_approx_interval_hours` /
   `build_norm4h_approx_events` —— **通过子进程调用真实模块**。

为什么要走子进程
----------------
`strategies/wx_daily_375.py` 在**模块级** `import numpy, pyarrow`，而门禁默认解释器
（推荐受管 3.12.14）**不含 pyarrow** ⇒ 进程内 import 必然失败。故契约测试用
`subprocess` 调用**具备 numpy/pyarrow 的解释器**（优先 `envs/freqtrade/bin/python`）执行
真实模块；该解释器缺失时相关用例 `skip`（不伪装通过）。适配层自身的用例则进程内运行。

覆盖（对应人类 2026-10-13 批复 HUMAN_RATIFIED_SETTLEMENT_CAUSAL_V1）
------------------------------------------------------------------
  A. 契约形态：`effective_from_ms == available_at_ms == calc_time_ms`；`available_at_ms` 显式非空
  B. **1h / 4h / 8h 真实周期切换**场景（真实契约函数判定）
  C. 折算与 kind：`rate_scaled = (4.0 / h) * rate`，`kind = norm4h_approx_dynamic_interval`
  D. **归档覆盖期外**：状态 `TAIL_BEYOND_COVERAGE` + 标记 `UNVERIFIABLE_NO_INTERVAL_META`
     + 哨兵阻断外推 ⇒ 契约抛 `FundingIntervalUnknownError`（**绝不静默按 8h 计费**）
  E. `max_staleness_ms` 陈旧度上限边界
  F. 查询 API 规则 1（精确结算点 + `epsilon_ms`，无静默取整）/ 规则 3（显式 UNVERIFIABLE 状态）
  G. 伴生库只读（`query_only` 拒写）+ 真实库 `meta.causal_conclusion_status` 已转 RATIFIED

运行：
  cd 后端实现 && sh run_tests.sh tests.test_funding_interval_consumer
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent            # 后端实现/
SITE = ROOT.parent                                       # 网站逆向研究/
W43 = SITE / "周期元数据表修复与5m冷备_2026-10-13"
MIGRATION_SQL = W43 / "12_生产库迁移_预备未执行.sql"
REAL_SIDECAR = W43 / "周期元数据表" / "funding_interval_meta.sqlite3"
FIM_MODULE = W43 / "_fim_query.py"
ADAPTER = ROOT / "strategies" / "funding_interval_meta_source.py"

# 具备 numpy/pyarrow 的解释器（契约子进程用）；缺失则相关用例 skip
CONTRACT_PY = Path("/Users/boos/.workbuddy-ai/binaries/python/envs/freqtrade/bin/python")

H = 3_600_000
BASE = 1_609_459_200_000          # 2021-01-01T00:00:00Z


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _M:
    src = None
    fim = None
    err = None

    @classmethod
    def ensure(cls):
        if cls.err:
            raise unittest.SkipTest(cls.err)
        if cls.src is not None:
            return
        try:
            cls.src = _load("fim_source_under_test", ADAPTER)
            cls.fim = _load("_fim_query_under_test_c", FIM_MODULE)
        except Exception as exc:                          # noqa: BLE001
            cls.err = f"被测模块不可用（{exc}）"
            raise unittest.SkipTest(cls.err) from exc


def migration_create_statements():
    text = MIGRATION_SQL.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    return [s.strip() for s in body.split(";")
            if s.strip() and s.strip().upper().startswith("CREATE")]


def build_temp_sidecar(path, settlements):
    """`settlements` = [(calc_ms, interval_hours)]，可含周期切换。"""
    con = sqlite3.connect(str(path))
    for stmt in migration_create_statements():
        con.execute(stmt)
    con.execute("INSERT INTO source_files(source_id, zip_name, symbol, period, bytes, sha256)"
                " VALUES (1, 'TSYM-fundingRate-2021-01.zip', 'TSYM', '2021-01', 1, 'aa')")
    for calc, h in settlements:
        con.execute(
            "INSERT INTO funding_interval_meta(exchange, market, symbol, calc_time_ms,"
            " funding_interval_hours, last_funding_rate, avail_settlement_ms,"
            " avail_archive_publish_ms, source_id) VALUES ('binance','um','TSYM',?,?,?,?,?,1)",
            (int(calc), int(h), "0.0001", int(calc), int(calc) + 30 * 24 * H))
    con.commit()
    con.close()
    return path


def segmented_settlements():
    """8h 段(20 笔) → 4h 段(40 笔) → 1h 段(60 笔)，返回 (settlements, seg_bounds)。"""
    out = []
    t = BASE
    a_start, a_end = t, None
    for _ in range(20):
        out.append((t, 8)); t += 8 * H
    a_end = t - 8 * H
    b_start = t
    for _ in range(40):
        out.append((t, 4)); t += 4 * H
    b_end = t - 4 * H
    c_start = t
    for _ in range(60):
        out.append((t, 1)); t += 1 * H
    c_end = t - 1 * H
    return out, {"A": (a_start, a_end), "B": (b_start, b_end), "C": (c_start, c_end)}


CONTRACT_SCRIPT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from strategies import wx_daily_375 as W
p = json.loads(sys.stdin.read())
res = {"ok": True}
try:
    if p["mode"] == "interval":
        h, anchor, nom = W._approx_interval_hours(p["records"], p["t"])
        res["h"] = h
    elif p["mode"] == "events":
        marks = {int(k): v for k, v in (p.get("marks") or {}).items()}
        raw = [(int(t), float(r)) for t, r in p["raw"]]
        ev = W.build_norm4h_approx_events(raw, marks, p["start"], p["end"], p["records"],
                                          max_drift_ms=p.get("max_drift_ms", 5000))
        res["events"] = [[int(t), r, m, s, k] for (t, r, m, s, k) in ev]
    else:
        res["ok"] = False; res["error"] = "bad_mode"
except Exception as e:
    res["ok"] = False
    res["error"] = type(e).__name__
    res["message"] = str(e)
print(json.dumps(res))
'''


@unittest.skipUnless(CONTRACT_PY.exists(), f"契约子进程解释器不存在：{CONTRACT_PY}")
class _ContractMixin:
    def run_contract(self, payload):
        proc = subprocess.run([str(CONTRACT_PY), "-c", CONTRACT_SCRIPT, str(ROOT)],
                              input=json.dumps(payload), capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(
                f"契约子进程失败 rc={proc.returncode}\n{proc.stderr[-1200:]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])


class AdapterShapeTest(unittest.TestCase):
    """A. 适配层产出的记录必须满足 `_approx_interval_hours` 第 41b 硬约束。"""

    @classmethod
    def setUpClass(cls):
        _M.ensure()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "side.sqlite3"
        self.settlements, self.bounds = segmented_settlements()
        build_temp_sidecar(self.db, self.settlements)
        self._old = os.environ.get("FUNDING_INTERVAL_META_DB")
        os.environ["FUNDING_INTERVAL_META_DB"] = str(self.db)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FUNDING_INTERVAL_META_DB", None)
        else:
            os.environ["FUNDING_INTERVAL_META_DB"] = self._old
        self.tmp.cleanup()

    def test_records_shape_and_ratified_calculation(self):
        recs, diag = _M.src.load_interval_meta("TSYM", BASE, BASE + 8 * H)
        self.assertGreater(len(recs), 0)
        for r in recs:
            self.assertIn("effective_from_ms", r)
            self.assertIn("interval_hours", r)
            self.assertIsNotNone(r.get("available_at_ms"),
                                 "第41b：缺 available_at_ms ⇒ 契约视为不可用")
        real = [r for r in recs if r["source_kind"] == _M.src.SOURCE_KIND]
        for r in real:
            self.assertEqual(r["effective_from_ms"], r["available_at_ms"],
                             "HUMAN_RATIFIED_SETTLEMENT_CAUSAL_V1：生效时刻 = 可得时刻 = calc_time")
            self.assertEqual(r["ratified_rule"], _M.src.RATIFIED_RULE)
        self.assertEqual(diag["ratified_status"], "RATIFIED_BY_HUMAN_2026_10_13")
        self.assertEqual(diag["status"], "OK")

    def test_diag_tail_beyond_coverage_marks_unverifiable(self):
        last = self.settlements[-1][0]
        recs, diag = _M.src.load_interval_meta("TSYM", BASE, last + 10 * H)
        self.assertEqual(diag["status"], "TAIL_BEYOND_COVERAGE")
        self.assertEqual(diag["tail_beyond_ms"], 10 * H)
        self.assertEqual(diag["unverifiable_marker"], _M.src.UNVERIFIABLE_NO_INTERVAL_META)
        self.assertIn("不得静默按 8h 计费", diag["reason"])
        self.assertIn("非费率本身错误", diag["reason"])

    def test_diag_no_coverage_symbol(self):
        recs, diag = _M.src.load_interval_meta("NOSUCHSYM", BASE, BASE + H)
        self.assertEqual(recs, [])
        self.assertEqual(diag["status"], "NO_COVERAGE")
        self.assertEqual(diag["unverifiable_marker"], _M.src.UNVERIFIABLE_NO_INTERVAL_META)

    def test_sentinel_injected_at_coverage_end(self):
        last = self.settlements[-1][0]
        recs, diag = _M.src.load_interval_meta("TSYM", BASE, last)
        self.assertEqual(diag["sentinel_at_ms"], last + 1)
        tail = recs[-1]
        self.assertEqual(tail["source_kind"], _M.src.SENTINEL_KIND)
        self.assertIsNone(tail["interval_hours"], "哨兵不得虚构任何周期数值")

    def test_stop_extrapolation_false_has_no_sentinel(self):
        recs, diag = _M.src.load_interval_meta("TSYM", BASE, BASE + 8 * H,
                                               stop_extrapolation=False)
        self.assertIsNone(diag["sentinel_at_ms"])
        self.assertTrue(all(r["source_kind"] == _M.src.SOURCE_KIND for r in recs))

    def test_assert_covered_or_mark(self):
        last = self.settlements[-1][0]
        _, ok_diag = _M.src.load_interval_meta("TSYM", BASE, BASE + 8 * H)
        self.assertIsNone(_M.src.assert_covered_or_mark(ok_diag))
        _, tail_diag = _M.src.load_interval_meta("TSYM", BASE, last + H)
        self.assertEqual(_M.src.assert_covered_or_mark(tail_diag, allow_tail=True),
                         _M.src.UNVERIFIABLE_NO_INTERVAL_META)
        with self.assertRaises(_M.src.UnverifiableIntervalMetaError):
            _M.src.assert_covered_or_mark(tail_diag)


class ContractSwitchTest(_ContractMixin, unittest.TestCase):
    """B/C/D/E. 真实契约函数（`wx_daily_375`）对适配层产出的判定。"""

    @classmethod
    def setUpClass(cls):
        _M.ensure()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "side.sqlite3"
        self.settlements, self.bounds = segmented_settlements()
        build_temp_sidecar(self.db, self.settlements)
        self._old = os.environ.get("FUNDING_INTERVAL_META_DB")
        os.environ["FUNDING_INTERVAL_META_DB"] = str(self.db)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FUNDING_INTERVAL_META_DB", None)
        else:
            os.environ["FUNDING_INTERVAL_META_DB"] = self._old
        self.tmp.cleanup()

    def _records(self, end_ms, **kw):
        recs, _ = _M.src.load_interval_meta("TSYM", BASE, end_ms, **kw)
        return recs

    def test_real_interval_switch_8h_4h_1h(self):
        """8h → 4h → 1h 三段，真实契约函数在各段内必须判出对应周期。"""
        a0, a1 = self.bounds["A"]; b0, b1 = self.bounds["B"]; c0, c1 = self.bounds["C"]
        recs = self._records(c1)
        for name, t, expect in (("A", a0 + 4 * H, 8.0),
                                ("B", b0 + 2 * H, 4.0),
                                ("C", c0 + 5 * H, 1.0)):
            with self.subTest(seg=name):
                res = self.run_contract({"mode": "interval", "records": recs, "t": t})
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["h"], expect)

    def test_scaling_factor_and_kind(self):
        """rate_scaled = (4.0 / h) * rate，kind = norm4h_approx_dynamic_interval。"""
        for seg, h in (("B", 4.0), ("C", 1.0)):
            b0, _ = self.bounds[seg]
            start = b0 + 4 * H
            end = start + 8 * H
            recs = self._records(end)
            raw = [[start - H, 0.0010]] + [[start + k * H, 0.0010] for k in range(10)]
            res = self.run_contract({"mode": "events", "records": recs, "raw": raw,
                                     "marks": {}, "start": start, "end": end})
            with self.subTest(seg=seg):
                self.assertTrue(res["ok"], res)
                self.assertTrue(res["events"], "应产生事件")
                for _t, r, _m, _s, k in res["events"]:
                    self.assertEqual(k, "norm4h_approx_dynamic_interval")
                    self.assertAlmostEqual(r, (4.0 / h) * 0.0010, places=12)

    def test_beyond_coverage_raises_not_defaults_to_8(self):
        """D. 覆盖期外 ⇒ 真实契约抛 FundingIntervalUnknownError（绝不按 8h 计费）。"""
        last = self.settlements[-1][0]
        end = last + 10 * H
        recs = self._records(end)
        start = last + 5 * H
        raw = [[start - H, 0.0010], [start, 0.0010], [start + H, 0.0010]]
        res = self.run_contract({"mode": "events", "records": recs, "raw": raw,
                                 "marks": {}, "start": start, "end": end})
        self.assertFalse(res["ok"], f"覆盖期外必须失败，却得到 {res}")
        self.assertEqual(res["error"], "FundingIntervalUnknownError")
        self.assertIn("不默认8h", res["message"])

    def test_before_coverage_also_blocks(self):
        """首笔结算之前同样不可判定（规则 3）。"""
        recs = self._records(self.bounds["C"][1])
        res = self.run_contract({"mode": "interval", "records": recs, "t": BASE - 10 * H})
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["h"])

    def test_max_staleness_ms_boundary(self):
        """E. 上限内可取，超出上限即不可判定（且不虚构数值）。"""
        last = self.settlements[-1][0]
        cap = 6 * H
        recs, diag = _M.src.load_interval_meta("TSYM", BASE, last + 10 * H,
                                               max_staleness_ms=cap)
        self.assertEqual(diag["sentinel_at_ms"], last + cap + 1)
        inside = self.run_contract({"mode": "interval", "records": recs, "t": last + cap})
        outside = self.run_contract({"mode": "interval", "records": recs, "t": last + cap + 1})
        self.assertEqual(inside["h"], 1.0, "上限内应沿用最后一条真实周期")
        self.assertIsNone(outside["h"], "超上限必须不可判定")

    def test_opt_out_extrapolation_does_not_invent_8(self):
        """显式关闭外推阻断时，沿用最后一条真实周期 —— 仍**不得**冒出 8。"""
        last = self.settlements[-1][0]
        recs = self._records(last, stop_extrapolation=False)
        res = self.run_contract({"mode": "interval", "records": recs, "t": last + 30 * 24 * H})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["h"], 1.0)


class QueryApiRulesTest(unittest.TestCase):
    """F. 查询 API 的规则 1（精确结算点）/ 规则 3（显式 UNVERIFIABLE 状态）。"""

    @classmethod
    def setUpClass(cls):
        _M.ensure()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "side.sqlite3"
        build_temp_sidecar(self.db, [(BASE, 8), (BASE + 8 * H, 4), (BASE + 12 * H, 4)])
        self.con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        self.con.execute("PRAGMA query_only=1")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_settlement_exact_match_requires_epsilon(self):
        f = _M.fim
        self.assertIsNotNone(f.settlement_at("TSYM", BASE, con=self.con))
        # 非结算点：epsilon=0 ⇒ None（不做静默取整）
        self.assertIsNone(f.settlement_at("TSYM", BASE + 1000, con=self.con))
        self.assertEqual(f.resolve_settlement("TSYM", BASE + 1000, con=self.con)["reason"],
                         "not_a_settlement_point_within_epsilon")
        # 显式容差才允许吸收抖动
        self.assertIsNotNone(f.settlement_at("TSYM", BASE + 1000, epsilon_ms=5000, con=self.con))
        with self.assertRaises(ValueError):
            f.settlement_at("TSYM", BASE, epsilon_ms=-1, con=self.con)

    def test_resolve_interval_reports_marker(self):
        f = _M.fim
        ok = f.resolve_interval("TSYM", BASE + 9 * H, con=self.con)
        self.assertEqual(ok["status"], "OK")
        self.assertIsNone(ok["marker"])
        self.assertEqual(ok["record"]["funding_interval_hours"], 4)
        self.assertEqual(ok["record"]["ratified_rule"], "HUMAN_RATIFIED_SETTLEMENT_CAUSAL_V1")
        none = f.resolve_interval("TSYM", BASE - H, con=self.con)
        self.assertEqual(none["status"], f.UNVERIFIABLE_NO_INTERVAL_META)
        self.assertEqual(none["reason"], f.REASON_NO_RECORD)
        stale = f.resolve_interval("TSYM", BASE + 12 * H + 10 * H,
                                   max_staleness_ms=H, con=self.con)
        self.assertEqual(stale["status"], f.UNVERIFIABLE_NO_INTERVAL_META)
        self.assertEqual(stale["reason"], f.REASON_STALE)

    def test_never_defaults_to_eight(self):
        f = _M.fim
        self.assertIsNone(f.interval_hours_at("NOSUCHSYM", BASE, con=self.con))
        self.assertIsNone(f.interval_hours_at("TSYM", BASE - H, con=self.con))


class ReadOnlyAndRatificationTest(unittest.TestCase):
    """G. 伴生库只读；真实库状态已批复。"""

    @classmethod
    def setUpClass(cls):
        _M.ensure()

    def test_sidecar_connection_refuses_writes(self):
        if not REAL_SIDECAR.exists():
            self.skipTest(f"伴生库不存在：{REAL_SIDECAR}")
        con = _M.src.open_sidecar()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("CREATE TABLE _should_fail(x)")
        finally:
            con.close()

    @unittest.skipUnless(REAL_SIDECAR.exists(), f"伴生库不存在：{REAL_SIDECAR}")
    def test_real_sidecar_meta_ratified(self):
        con = sqlite3.connect(f"file:{REAL_SIDECAR}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT k, v FROM meta"))
        finally:
            con.close()
        self.assertEqual(meta.get("causal_conclusion_status"), "RATIFIED_BY_HUMAN_2026_10_13")
        self.assertEqual(meta.get("ratified_rule"), "HUMAN_RATIFIED_SETTLEMENT_CAUSAL_V1")
        self.assertIn("严禁静默回退默认 8h", meta.get("fallback_policy", ""))

    @unittest.skipUnless(REAL_SIDECAR.exists(), f"伴生库不存在：{REAL_SIDECAR}")
    def test_real_sidecar_interval_switch_detected(self):
        """真实数据：0GUSDT 在 2025-09-22T13:00Z 由 4h 切到 1h（只读核验）。"""
        def ms(s):
            return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
        if not _M.src.coverage("0GUSDT")["n"]:
            self.skipTest("伴生库无 0GUSDT")
        before, _ = _M.src.load_interval_meta("0GUSDT", ms("2025-09-22T08:00:00Z"),
                                              ms("2025-09-22T12:00:00Z"))
        after, _ = _M.src.load_interval_meta("0GUSDT", ms("2025-09-22T08:00:00Z"),
                                             ms("2025-09-22T14:00:00Z"))
        h_before = _M.fim.interval_hours_at("0GUSDT", ms("2025-09-22T12:00:00Z"),
                                            db=REAL_SIDECAR)
        h_after = _M.fim.interval_hours_at("0GUSDT", ms("2025-09-22T14:00:00Z"),
                                           db=REAL_SIDECAR)
        self.assertEqual(h_before["funding_interval_hours"], 4)
        self.assertEqual(h_after["funding_interval_hours"], 1)
        self.assertTrue(before and after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
