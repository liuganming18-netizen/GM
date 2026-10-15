# -*- coding: utf-8 -*-
"""六周期门禁**隔离回归测试**（离线、零网络、临时库/临时目录）。

目的（用户 2026-10-08 独立验收退回 #2/#3/#5）：
  证明「失败 / 限频待重试 / 缺证据 / 身份未决 / 残余缺口 / **错误周期豁免**」**不会假通过**，
  且只有「源真正不可得 + 准确周期 + 合约身份 + 区间 + 原始证据 + 实际 hash 校验」才豁免。

覆盖：
  A. `logs/_pool_gate.py` 判据（被 `_final_acceptance_6tf.py` 与 `_audit_pool_coverage.py` 共用）
  B. `历史下载/_final_acceptance_6tf.py --gate` 的**返回码**（存在未决 ⇒ 非零 4）
  C. `logs/_pool_restfill.py --local-only` 的**返回码**（待范围证据 ⇒ 非零 4；本地已齐 ⇒ 0）

运行：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_pool_gate -v
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent                # 后端实现/
# `data_hub`（统一网关/存储）位于唯一正式库所在项目；`_pool_restfill.py` 直接运行
# 时需其可被 import。子进程测试必须显式带上 PYTHONPATH，否则与生产运行环境不一致。
PROJ = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
sys.path.insert(0, str(ROOT / "logs"))
sys.path.insert(0, str(ROOT / "历史下载"))
sys.path.insert(0, str(PROJ))
import _pool_gate                                            # noqa: E402

PY = sys.executable
IV_1D = 86_400_000
IV_8H = 28_800_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(s):
    if len(s) == 10:
        s = s + "T00:00:00Z"
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "m.sqlite3"
        con = sqlite3.connect(self.db)
        con.executescript(
            "CREATE TABLE candle_heads(exchange TEXT, market TEXT, symbol TEXT, "
            "timeframe TEXT, open_time INTEGER, source_id INTEGER);"
            "CREATE TABLE coverage_cache(exchange TEXT, symbol TEXT, timeframe TEXT);"
            "CREATE TABLE symbol_pool(exchange TEXT, market TEXT, symbol TEXT, "
            "onboard_time INTEGER, status TEXT);"
            "CREATE TABLE candle_versions(exchange TEXT, market TEXT, symbol TEXT, "
            "timeframe TEXT, open_time INTEGER, source_id INTEGER, open REAL, high REAL, "
            "low REAL, close REAL, volume REAL);"
            "CREATE TABLE sources(id INTEGER PRIMARY KEY, exchange TEXT, market TEXT, "
            "symbol TEXT, timeframe TEXT, source_url TEXT, file_path TEXT, sha256 TEXT, "
            "imported_at INTEGER, downloaded_at INTEGER, first_open INTEGER, "
            "last_close INTEGER, row_count INTEGER);")
        con.commit()
        con.close()
        self.now = datetime(2026, 10, 8, 7, 0, 0, tzinfo=timezone.utc)
        self.last_closed = (int(self.now.timestamp() * 1000) // IV_1D) * IV_1D - IV_1D

    def tearDown(self):
        self.tmp.cleanup()

    # ── helpers ──
    def add_series(self, sym, first_iso, last_iso=None, ex="binance", mk="um", tf="1d",
                   iv=IV_1D, holes=(), extra=()):
        con = sqlite3.connect(self.db)
        a = iso_ms(first_iso)
        b = iso_ms(last_iso) if last_iso else self.last_closed
        hole = {iso_ms(h) for h in holes}
        t = a
        while t <= b:
            if t not in hole:
                con.execute("INSERT INTO candle_heads VALUES(?,?,?,?,?,1)",
                            (ex, mk, sym, tf, t))
            t += iv
        for e in extra:
            con.execute("INSERT INTO candle_heads VALUES(?,?,?,?,?,1)",
                        (ex, mk, sym, tf, iso_ms(e)))
        con.commit()
        con.close()

    def add_pool(self, sym, ex="binance", mk="um", status="TRADING"):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO symbol_pool VALUES(?,?,?,?,?)", (ex, mk, sym, 0, status))
        con.commit()
        con.close()

    def write_cache(self, tf, mapping, d=None):
        d = d or self.dir
        d.mkdir(parents=True, exist_ok=True)
        (d / f"pool_range_cache_{tf}.json").write_text(
            json.dumps({"symbols": mapping}), encoding="utf-8")

    def write_valid_cache(self, tf, sym, rest_first, rest_last, ex="binance", db=None):
        """写一份**通过 _pool_restfill 严格校验**的范围证据缓存（格式/版本/身份/TTL）。"""
        dbh = hashlib.sha256(str(db or self.db).encode("utf-8")).hexdigest()[:16]
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"pool_range_cache_{tf}.json").write_text(json.dumps({
            "cache_format": "pool_range_cache", "cache_version": 2, "tf": tf,
            "identity": f"pool_range_cache/v2|db={dbh}|tf={tf}|ex=binance.um+okx.swap",
            "generated_utc": now_iso,
            "symbols": {f"{ex}:{sym}": {"rest_first": rest_first, "rest_last_raw": rest_last,
                                        "probed_at": now_iso, "basis": "probe"}},
        }), encoding="utf-8")

    def write_identity(self, tf, mapping, d=None):
        d = d or self.dir
        (d / f"pool_identity_{tf}.json").write_text(
            json.dumps({"symbols": mapping}), encoding="utf-8")

    def write_canonical_identity(self, tf, sym, verdict, db_first_iso, rf_iso,
                                 ex="binance", mk="um", zero_volume=True):
        """写一份**能通过 canonical `_pool_verify._audit_identity` 实质核验**的身份记录。

        2026-10-08 收敛后：身份是否「已核」只由唯一验收器的 `_audit_identity` 判定，
        须绑定 `applies_to`（币种/周期/具体区间）+ `basis`（可核查依据）。
        本 helper 造 `basis.kind=official_archive_file`：真实 ZIP（`<SYM>-<tf>-<YYYY-MM>.zip`）
        + 官方 URL（含 `/futures/um/` 市场段）+ 真实 sha256。
        `zero_volume=True` ⇒ `archive_padding`（全 0 量占位行，与实测 pre-REST 段一致）；
        否则 `same_contract`（含真实成交行）。
        """
        import zipfile as _zip
        iv = {"1d": IV_1D, "8h": IV_8H}.get(tf, IV_1D)
        a, b = iso_ms(db_first_iso), iso_ms(rf_iso) - 1
        rows, t = [], a
        while t <= b:
            rows.append(f"{t},1,1,1,1,{0 if zero_volume else 123.0},0,0,0,0,0,0")
            t += iv
        per = datetime.fromtimestamp(a / 1000, timezone.utc).strftime("%Y-%m")
        zname = f"{sym}-{tf}-{per}.zip"
        zp = self.dir / zname
        with _zip.ZipFile(zp, "w") as z:
            z.writestr(zname[:-4] + ".csv", "\n".join(rows) + "\n")
        rec = {
            "verdict": verdict,
            "applies_to": {"exchange": ex, "symbol": sym, "timeframe": tf,
                           "range": [iso(a), iso(b)]},
            "basis": {"kind": "official_archive_file", "path": str(zp),
                      "url": (f"https://data.binance.vision/data/futures/{mk}/daily/"
                              f"klines/{sym}/{tf}/{zname}"),
                      "sha256": hashlib.sha256(zp.read_bytes()).hexdigest(),
                      "rows": len(rows), "first_open_utc": iso(a)},
        }
        self.write_identity(tf, {f"{ex}:{sym}": rec})
        return rec

    def write_evidence(self, name, content):
        p = self.dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def write_exemptions(self, entries, path=None):
        p = path or (self.dir / "exemptions.json")
        p.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        return p

    def evaluate(self, tf, targets, exemptions_path=None, identity_dir=None):
        exemptions = _pool_gate.load_exemptions(str(exemptions_path)) if exemptions_path else {}
        identity = _pool_gate.load_identity(
            (identity_dir or self.dir) / f"pool_identity_{tf}.json")
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return _pool_gate.evaluate_tf(con, tf, targets, self.now,
                                          range_cache_dir=self.dir,
                                          exemptions=exemptions, identity=identity)
        finally:
            con.close()


class TestGateCriteria(GateBase):
    def test_01_clean_complete_passes(self):
        self.add_series("AAA", "2024-01-01")
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "pass", r)

    def test_02_head_residual_blocks(self):
        self.add_series("AAA", "2024-06-01")          # 库内首根晚于官方最早
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail")
        self.assertGreater(r["aggregate"]["head_residual_bars"], 0)

    def test_03_no_range_evidence_blocks(self):
        self.add_series("AAA", "2024-01-01")
        self.write_cache("1d", {})                     # 无 rest_first
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["no_range_evidence"], 1)

    def test_04_pre_rest_identity_unverified_blocks(self):
        self.add_series("AAA", "2023-01-01")           # 库内首根早于 REST 最早
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["identity_unverified"], 1)

    def test_05_pre_rest_identity_resolved_passes(self):
        self.add_series("AAA", "2023-01-01")
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        # canonical 实质核验：applies_to + basis（官方归档文件 + sha256）
        self.write_canonical_identity("1d", "AAA", "archive_padding",
                                      "2023-01-01T00:00:00Z", "2024-01-01T00:00:00Z")
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "pass", r)

    def test_05b_pre_rest_identity_label_only_blocks(self):
        """2026-10-08 收敛：**仅写 verdict 标签**（无 applies_to/basis）不得放行。"""
        self.add_series("AAA", "2023-01-01")
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        self.write_identity("1d", {"binance:AAA": {"verdict": "different_generation"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail", r)
        self.assertEqual(r["aggregate"]["identity_unverified"], 1)

    def test_06_internal_gap_not_exempted_blocks(self):
        self.add_series("AAA", "2024-01-01", holes=["2024-03-01", "2024-03-02"])
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail")
        self.assertGreater(r["aggregate"]["internal_residual_bars"], 0)

    def test_07_internal_gap_exempted_with_valid_evidence_passes(self):
        self.add_series("AAA", "2024-01-01", holes=["2024-03-01", "2024-03-02"])
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        f = self.write_evidence("ev.json", '{"official":"no data"}')
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        p = self.write_exemptions([{
            "exchange": "binance", "symbol": "AAA", "timeframe": "1d",
            "kind": "official_not_published",
            "ranges": [["2024-03-01T00:00:00Z", "2024-03-02T00:00:00Z"]],
            "evidence": {"file": str(f), "sha256": sha}}])
        r = self.evaluate("1d", [("binance", "um", "AAA")], exemptions_path=p)
        self.assertEqual(r["status"], "pass", r)
        self.assertEqual(r["aggregate"]["internal_documented"], 2)

    def test_08_wrong_timeframe_exemption_does_not_cover(self):
        """**错误周期豁免**不得放行：豁免标 4h，实际缺的是 1d。"""
        self.add_series("AAA", "2024-01-01", holes=["2024-03-01"])
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        f = self.write_evidence("ev.json", "{}")
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        p = self.write_exemptions([{
            "exchange": "binance", "symbol": "AAA", "timeframe": "4h",
            "kind": "official_not_published",
            "ranges": [["2024-03-01T00:00:00Z", "2024-03-01T00:00:00Z"]],
            "evidence": {"file": str(f), "sha256": sha}}])
        r = self.evaluate("1d", [("binance", "um", "AAA")], exemptions_path=p)
        self.assertEqual(r["status"], "fail")
        self.assertGreater(r["aggregate"]["internal_residual_bars"], 0)

    def test_09_evidence_hash_mismatch_does_not_exempt(self):
        self.add_series("AAA", "2024-01-01", holes=["2024-03-01"])
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        f = self.write_evidence("ev.json", '{"v":1}')
        p = self.write_exemptions([{
            "exchange": "binance", "symbol": "AAA", "timeframe": "1d",
            "kind": "official_not_published",
            "ranges": [["2024-03-01T00:00:00Z", "2024-03-01T00:00:00Z"]],
            "evidence": {"file": str(f), "sha256": "deadbeef" * 8}}])
        r = self.evaluate("1d", [("binance", "um", "AAA")], exemptions_path=p)
        self.assertEqual(r["status"], "fail")

    def test_10_missing_evidence_file_does_not_exempt(self):
        self.add_series("AAA", "2024-01-01", holes=["2024-03-01"])
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        p = self.write_exemptions([{
            "exchange": "binance", "symbol": "AAA", "timeframe": "1d",
            "kind": "official_not_published",
            "ranges": [["2024-03-01T00:00:00Z", "2024-03-01T00:00:00Z"]],
            "evidence": {"file": str(self.dir / "nope.json"), "sha256": "x"}}])
        r = self.evaluate("1d", [("binance", "um", "AAA")], exemptions_path=p)
        self.assertEqual(r["status"], "fail")

    def test_11_tail_residual_blocks(self):
        last = iso(self.last_closed - 3 * IV_1D)
        self.add_series("AAA", "2024-01-01", last_iso=last)
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["tail_residual_bars"], 3)

    def test_12_empty_binance_blocks(self):
        self.write_cache("1d", {})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["empty"], 1)

    def test_13_okx_8h_structural_absent_passes(self):
        self.write_cache("8h", {})
        r = self.evaluate("8h", [("okx", "swap", "X-USDT-SWAP")])
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["aggregate"]["structural_absent"], 1)

    def test_14_off_boundary_reported_not_blocking(self):
        self.add_series("AAA", "2024-01-01", extra=["2026-09-28T16:00:00Z"])
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self.evaluate("1d", [("binance", "um", "AAA")])
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["boundary_contamination"]["rows"], 1)
        self.assertFalse(r["boundary_contamination"]["blocking"])


class TestAcceptanceExitCode(GateBase):
    def _run(self, tf):
        env = dict(os.environ)
        env.update({"MARKET_DB": str(self.db), "POOL_JSON": str(self.pool),
                    "POOL_CACHE_DIR": str(self.dir), "POOL_EXEMPTIONS": str(self.dir / "none.json"),
                    "POOL_IDENTITY_DIR": str(self.dir), "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": os.pathsep.join(
                        [p for p in [str(PROJ), os.environ.get("PYTHONPATH", "")] if p])})
        # 固定评估时点 = 夹具的 `self.now`（与 `add_series` 的 `last_closed` 同源）。
        # 若不传 `--now`，子进程按**运行时刻**取「最后闭合根」，夹具序列会随真实日历
        # 推进逐渐落后 ⇒ 尾残非零、本该通过的门禁用例随日历**假失败**（非确定性）。
        # `_final_acceptance_6tf.py --now` 正是为「可复现的固定截止复验」提供的入口。
        return subprocess.run([PY, str(ROOT / "历史下载" / "_final_acceptance_6tf.py"),
                               "--tf", tf, "--gate",
                               "--now", iso(int(self.now.timestamp() * 1000))], env=env,
                              capture_output=True, text=True)

    def setUp(self):
        super().setUp()
        self.pool = self.dir / "pool.json"
        self.pool.write_text(json.dumps({"items": [{"symbol": "AAA", "onboard_time": 0}]}),
                             encoding="utf-8")

    def test_gate_returns_nonzero_on_residual(self):
        self.add_series("AAA", "2024-06-01")
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self._run("1d")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

    def test_gate_returns_zero_on_pass(self):
        self.add_series("AAA", "2024-01-01")
        self.write_cache("1d", {"binance:AAA": {"rest_first": "2024-01-01T00:00:00Z"}})
        r = self._run("1d")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestRestfillExitCode(GateBase):
    def _run_local_only(self):
        env = dict(os.environ)
        # 夹具隔离（2026-10-09 修复）：**关闭活动池范围过滤**。
        # `logs/_pool_scope.py` 默认指向真实的 `历史下载/标的池/scope_crypto.json`
        # （币安 526 个加密币）；本类夹具用合成标的 `AAA`，会被全部过滤掉 ⇒ 目标=0 ⇒
        # 门禁无阻塞项 ⇒ `test_needs_range_evidence_returns_nonzero` 假返回 0。
        # 指向**不存在的路径** = `_pool_scope.py` 模块文档约定的「不过滤」隔离路径
        # （见其行 15「缺失/不可解析 ⇒ 返回 None，仅供隔离测试」）。
        env.update({"MARKET_DB": str(self.db), "PYTHONDONTWRITEBYTECODE": "1",
                    "POOL_SCOPE_JSON": str(self.dir / "no_such_scope.json"),
                    "POOL_STATE_DIR": str(self.dir / "state"),
                    "POOL_CACHE_DIR": str(self.dir), "POOL_RAW_DIR": str(self.dir / "raw"),
                    "PYTHONPATH": os.pathsep.join(
                        [p for p in [str(PROJ), os.environ.get("PYTHONPATH", "")] if p])})
        return subprocess.run([PY, str(ROOT / "logs" / "_pool_restfill.py"),
                               "--tf", "1d", "--exchange", "binance", "--local-only",
                               # 夹具自洽化（2026-10-11）：`--local-only` 零网络不落盘，
                               # 与宿主剩余空间无关；但 `--min-free-gb` 的**默认值**会按宿主
                               # 磁盘暂停 ⇒ 退出码随环境变化（当时默认 30.0，本机 15m 下载
                               # 使剩余 <30GB ⇒ 假返回 3）。
                               # 注：该默认值已于 2026-10-11 由 30.0 降为 12.0
                               # （见 logs/_pool_restfill.py:1016）；显式置 0 仍是为了
                               # 完全隔离环境依赖；空间门禁由 test_low_space_skipped 覆盖。
                               "--min-free-gb", "0"],
                              env=env, capture_output=True, text=True, cwd=str(ROOT))

    def test_needs_range_evidence_returns_nonzero(self):
        """无范围证据 ⇒ 待范围证据 ⇒ 非零（旧版会错误 return 0）。"""
        self.add_series("AAA", "2024-01-01")
        self.add_pool("AAA")
        r = self._run_local_only()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

    def test_local_complete_returns_zero(self):
        self.add_series("AAA", "2024-01-01")
        self.add_pool("AAA")
        self.write_valid_cache("1d", "AAA", "2024-01-01T00:00:00Z", iso(self.last_closed))
        r = self._run_local_only()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Test8hGenerationGap(GateBase):
    """8h **代际缝隙**（新合约上市前无合约窗口）端到端判据。

    复刻 2026-10-08 实测：库内旧代际段 [2025-04-01 .. 2025-04-29T16:00] 与新代际段
    [2025-04-30T08:00 ..] 之间缺 1 根（2025-04-30T00:00），官方 REST 最早=2025-04-30T08:00。
    · 有**周期绑定豁免（8h）+ 身份已核** ⇒ 通过（计入官方缺失）；
    · 无豁免 ⇒ 按内部残余阻塞；
    · 豁免错标周期（12h）⇒ 不覆盖 ⇒ 阻塞；
    · 身份未核 ⇒ 即使缺口已豁免，头段仍阻塞（两把锁各自独立）。
    """
    GAP = "2025-04-30T00:00:00Z"
    RF = "2025-04-30T08:00:00Z"

    def setUp(self):
        super().setUp()
        self.tf = "8h"
        self.lc8 = (int(self.now.timestamp() * 1000) // IV_8H) * IV_8H - IV_8H
        self.add_series("CTKUSDT", "2025-04-01T00:00:00Z", "2025-04-29T16:00:00Z",
                        tf="8h", iv=IV_8H)
        self.add_series("CTKUSDT", self.RF, iso(self.lc8), tf="8h", iv=IV_8H)
        self.write_cache("8h", {"binance:CTKUSDT": {"rest_first": self.RF,
                                                    "rest_last_raw": iso(self.lc8)}})
        # canonical 实质核验：applies_to + basis（官方归档文件：pre-REST 段为全 0 量占位行）。
        # 仅写 verdict 标签（无 applies_to/basis）不再放行 —— 见 test_05b。
        self.write_canonical_identity("8h", "CTKUSDT", "archive_padding",
                                      "2025-04-01T00:00:00Z", self.RF)

    def _exemptions(self, tf="8h"):
        f = self.write_evidence("p11_8h.json", '{"probe":"official_not_published"}')
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        return self.write_exemptions([{
            "exchange": "binance", "symbol": "CTKUSDT", "timeframe": tf,
            "kind": "official_not_published",
            "ranges": [[self.GAP, self.GAP]],
            "evidence": {"file": str(f), "sha256": sha}}])

    def test_20_8h_gap_period_bound_exemption_passes(self):
        r = self.evaluate("8h", [("binance", "um", "CTKUSDT")],
                          exemptions_path=self._exemptions())
        self.assertEqual(r["status"], "pass", r)
        self.assertEqual(r["aggregate"]["internal_documented"], 1)
        self.assertEqual(r["aggregate"]["internal_residual_bars"], 0)
        self.assertEqual(r["aggregate"]["identity_unverified"], 0)

    def test_21_8h_gap_without_exemption_blocks(self):
        r = self.evaluate("8h", [("binance", "um", "CTKUSDT")])
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["internal_residual_bars"], 1)

    def test_22_8h_gap_exemption_on_wrong_tf_does_not_cover(self):
        r = self.evaluate("8h", [("binance", "um", "CTKUSDT")],
                          exemptions_path=self._exemptions("12h"))
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["internal_residual_bars"], 1)

    def test_23_8h_gap_without_identity_blocks(self):
        self.write_identity("8h", {})                     # 清空身份 ⇒ 未核
        r = self.evaluate("8h", [("binance", "um", "CTKUSDT")],
                          exemptions_path=self._exemptions())
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["aggregate"]["identity_unverified"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
