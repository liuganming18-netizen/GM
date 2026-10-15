# -*- coding: utf-8 -*-
"""OKX `volume` 修复工具（**执行入口 / 验收门禁整改 rev3**）的**真实 CLI 子进程**故障回归。

**加载新目录**：`outputs/网站逆向研究/OKX成交量执行验收整改_2026-10-11/`
（`04_okx_volume_fix_v3.py` / `06_okx_volume_rollback_v3.py` / `09_全字段不变式验收_v3.py` /
`volccy_common.py`）。

对照 78 合同的**实际 CLI / 验收缺陷**（V2-01..V2-04）：
  §1 真实 CLI 续跑：默认复跑 / 部分完成+重建 / 容量暂停 / 结束后复跑 / 回退后再前进
     —— 全部用**真实子进程**端到端验证（不靠单元测试直接传旧 plan）。
  §2 恢复验收不再只看 volume：close 坏成 999 ⇒ 不 skip_complete、fail closed 非零退出。
  §3 验收 expected keys 来自**冻结计划**：缺行 / 额外行 / 缺 head / 坏 metadata ⇒ FAIL 非零退出；
     缺 fixed_rows 的 dry-run 计划被拒绝。
  §4 source 提交但 journal 未落盘：恢复统一**首次真实修复时点**；库身份 inode 变化被拒。

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest \
      tests.test_okx_volume_exec_v3 -v
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent                       # 后端实现/
PROJ = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
FIXDIR = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
              "OKX成交量执行验收整改_2026-10-11")                    # ← 新目录
PY = "/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3"
FIX = FIXDIR / "04_okx_volume_fix_v3.py"
ROLL = FIXDIR / "06_okx_volume_rollback_v3.py"
ACC = FIXDIR / "09_全字段不变式验收_v3.py"
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))

_TMP = tempfile.TemporaryDirectory(prefix="okx_vol_exec_v3_")
TMP = Path(_TMP.name)

_SCOPE = TMP / "scope_crypto.json"
_SCOPE_SYMS = ["CRO-USDT-SWAP", "LUNA-USDT-SWAP", "OKB-USDT-SWAP", "OL-USDT-SWAP",
               "RAY-USDT-SWAP", "PI-USDT-SWAP"]
_SCOPE.write_text(json.dumps({"scope_version": "test", "okx_symbols": _SCOPE_SYMS}),
                  encoding="utf-8")

TF = "15m"
IV = 900_000
MARKET = "swap"


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def run_cli(script, *args):
    """真实子进程调用；返回 CompletedProcess（不 assert 退出码，交由用例判定）。"""
    env = dict(os.environ)
    env["POOL_SCOPE_JSON"] = str(_SCOPE)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([PY, str(script), *args], capture_output=True, text=True,
                          env=env, cwd=str(FIXDIR), timeout=300)


class Base(unittest.TestCase):
    def setUp(self):
        self.name = self._testMethodName
        self.dir = TMP / self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = self.dir / "iso.sqlite3"
        self.raw = self.dir / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.journal = self.dir / "ck.json"
        self.plan_file = self.dir / "frozen_plan.json"
        self.con = sqlite3.connect(str(self.db))
        self.con.row_factory = sqlite3.Row
        self.con.executescript((PROJ / "data_hub" / "schema.sql").read_text(encoding="utf-8"))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    # ── 夹具 ───────────────────────────────────────────────────────────
    def _add_source(self, sid, symbol, rows, tf=TF):
        """rows = [(open_time, vol, volCcy, volCcyQuote)] ⇒ 缺陷映射 volume=r5 + head。"""
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
                "open_times_digest": _sha(",".join(str(o) for o in sorted(open_times))),
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

    def _seed(self, sid=920001, symbol="CRO-USDT-SWAP", n=3):
        rows = [(1617871500000 + i * IV, "2", "20", "3.9") for i in range(n)]
        self._add_source(sid, symbol, rows)
        ev = self._add_body(TF, [self._okx_row(o, v, c, q) for o, v, c, q in rows])
        self._add_sidecar(TF, [self._link(sid, symbol, [ev], [r[0] for r in rows])])
        return [r[0] for r in rows]

    # ── CLI 封装 ───────────────────────────────────────────────────────
    def _fix_args(self, mode="apply", out=None, extra=()):
        return ["--mode", mode, "--db", str(self.db), "--raw-dir", str(self.raw),
                "--tfs", TF, "--journal", str(self.journal), "--plan-file", str(self.plan_file),
                "--out", str(out or (self.dir / f"{mode}_out.json")), "--batch-size", "1000",
                "--min-free-gb", "0", "--wal-budget-gb", "0", *extra]

    def _apply(self, extra=()):
        return run_cli(FIX, *self._fix_args("apply", extra=extra))

    def _heads(self):
        return {int(r[0]): int(r[1]) for r in self.con.execute(
            "SELECT open_time,source_id FROM candle_heads")}

    def _fk_off(self):
        """模拟故障态（删版本行但 head 仍在）时临时关 FK（schema 默认 ON）。"""
        self.con.execute("PRAGMA foreign_keys=OFF")

    def _vol(self, sid, ot):
        r = self.con.execute("SELECT volume FROM candle_versions WHERE source_id=? AND open_time=?",
                             (sid, ot)).fetchone()
        return None if r is None else r[0]

    def _new_sid(self):
        r = self.con.execute("SELECT id FROM sources WHERE source_url LIKE '%volrev=1%'").fetchall()
        return [int(x[0]) for x in r]


# ══════════════════════════ §1 真实 CLI 续跑 ══════════════════════════
class CliResume(Base):
    def test_dry_run_writes_frozen_plan_with_fixed_rows(self):
        self._seed(n=3)
        r = run_cli(FIX, "--mode", "dry-run", "--db", str(self.db), "--raw-dir", str(self.raw),
                    "--tfs", TF, "--out", str(self.dir / "dry.json"),
                    "--plan-file", str(self.plan_file))
        self.assertEqual(r.returncode, 0, r.stderr)
        frozen = json.loads(self.plan_file.read_text(encoding="utf-8"))
        self.assertEqual(frozen["tool_version"], "okx-volfix-rev3")
        self.assertEqual(len(frozen["plans"]), 1)
        self.assertIn("fixed_rows", frozen["plans"][0], "冻结计划必须含精确 expected keys")
        self.assertEqual(len(frozen["plans"][0]["fixed_rows"]), 3)
        self.assertIn("db_identity", frozen)
        self.assertIn("inode", frozen["db_identity"])
        self.assertIn("source_identity_digest", frozen)

    def test_first_apply_then_default_rerun_resumes(self):
        """V2-01：首次 apply 后**默认复跑**（同 journal，不重建计划）必须成功且幂等。"""
        ots = self._seed(n=3)
        r1 = self._apply()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        j1 = json.loads(self.journal.read_text(encoding="utf-8"))
        nsid = int(j1["sources"]["920001"]["new_source_id"])
        r2 = self._apply()                                # 默认复跑
        self.assertEqual(r2.returncode, 0, f"默认复跑必须成功：{r2.stderr}\n{r2.stdout}")
        self.assertIn("复用冻结计划", r2.stdout, "默认复跑应复用冻结计划，而非重跑 build_plan")
        d2 = json.loads((self.dir / "apply_out.json").read_text(encoding="utf-8"))
        self.assertTrue(d2["reused_frozen_plan"])
        self.assertEqual(d2["apply"]["results"][0]["resumed"], "skip_complete")
        for ot in ots:
            self.assertEqual(self._heads()[ot], nsid)
            self.assertEqual(self._vol(nsid, ot), "20")

    def test_partial_completion_then_resume_and_rebuild_requires_new_journal(self):
        """部分完成 + 重建：默认复跑补齐；--rebuild-plan 同 journal 被拒、新 journal 可用。"""
        o1, o2 = 1617871500000, 1617872400000
        self._add_source(930001, "CRO-USDT-SWAP", [(o1, "2", "20", "3.9")])
        self._add_source(930002, "CRO-USDT-SWAP", [(o2, "3", "30", "4.0")])
        e1 = self._add_body(TF, [self._okx_row(o1, "2", "20", "3.9")])
        e2 = self._add_body(TF, [self._okx_row(o2, "3", "30", "4.0")])
        self._add_sidecar(TF, [self._link(930001, "CRO-USDT-SWAP", [e1], [o1]),
                               self._link(930002, "CRO-USDT-SWAP", [e2], [o2])])
        # 首次 apply（冻结完整 2 源计划）
        r1 = self._apply()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        frozen = json.loads(self.plan_file.read_text(encoding="utf-8"))
        self.assertEqual(len(frozen["plans"]), 2, "冻结计划必须完整含 2 源（不因完成而丢记账）")
        # 模拟「第 2 源未完成」：journal 回退 + 删其新版本行 + head 回旧
        j = json.loads(self.journal.read_text(encoding="utf-8"))
        nsid2 = int(j["sources"]["930002"]["new_source_id"])
        j["sources"]["930002"].update({"stage": "planned", "new_source_id": None,
                                       "repair_ms": None, "rows_written": 0, "heads_switched": 0})
        self.journal.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        self._fk_off()
        self.con.execute("DELETE FROM candle_versions WHERE source_id=?", (nsid2,))
        self.con.execute("UPDATE candle_heads SET source_id=930002 WHERE open_time=?", (o2,))
        self.con.commit()
        # 默认复跑 ⇒ 复用冻结计划，补齐第 2 源，且第 1 源仍被有牙校验（skip_complete）
        r2 = self._apply()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        d2 = json.loads((self.dir / "apply_out.json").read_text(encoding="utf-8"))
        self.assertTrue(d2["reused_frozen_plan"])
        by = {x["old_source_id"]: x for x in d2["apply"]["results"]}
        self.assertEqual(by[930001]["resumed"], "skip_complete")
        self.assertEqual(by[930002]["rows_written"], 1)
        # --rebuild-plan + 同 journal ⇒ 计划摘要不一致 ⇒ 非零退出
        rb = run_cli(FIX, *self._fix_args(extra=["--rebuild-plan"]))
        self.assertNotEqual(rb.returncode, 0, "重建计划改变 digest 必须拒绝复用旧 journal")
        self.assertIn("计划摘要", rb.stderr + rb.stdout)
        # --rebuild-plan + 新 journal ⇒ 全部已完成 ⇒ 0 源，成功
        nj = self.dir / "ck_new.json"
        rb2 = run_cli(FIX, "--mode", "apply", "--db", str(self.db), "--raw-dir", str(self.raw),
                      "--tfs", TF, "--journal", str(nj), "--out", str(self.dir / "rb2.json"),
                      "--batch-size", "1000", "--min-free-gb", "0", "--wal-budget-gb", "0",
                      "--rebuild-plan")
        self.assertEqual(rb2.returncode, 0, rb2.stderr)
        self.assertIn('"sources_planned":0', rb2.stdout.replace(" ", ""))

    def test_capacity_pause_then_resume(self):
        """容量暂停 ⇒ 干净停下（journal 已落盘）⇒ 提高额度后 --resume 续跑成功。"""
        ots = self._seed(n=3)
        r1 = run_cli(FIX, *self._fix_args(extra=["--min-free-gb", "999999"]))
        self.assertNotEqual(r1.returncode, 0, "容量暂停应导致非零退出（验收未通过）")
        d1 = json.loads((self.dir / "apply_out.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(d1["apply"]["paused"], "必须记录 paused")
        # journal 已落盘（planned 阶段）
        self.assertTrue(self.journal.exists())
        # 续跑（复用冻结计划）
        r2 = self._apply()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        j2 = json.loads(self.journal.read_text(encoding="utf-8"))
        nsid = int(j2["sources"]["920001"]["new_source_id"])
        for ot in ots:
            self.assertEqual(self._heads()[ot], nsid)

    def test_rerun_after_completion_and_revert_then_advance(self):
        """结束后复跑幂等；回退后再前进真实重切。"""
        ots = self._seed(n=3)
        self.assertEqual(self._apply().returncode, 0)
        nsid = int(json.loads(self.journal.read_text(encoding="utf-8"))
                   ["sources"]["920001"]["new_source_id"])
        # 结束后复跑
        r2 = self._apply()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        d2 = json.loads((self.dir / "apply_out.json").read_text(encoding="utf-8"))
        self.assertEqual(d2["apply"]["results"][0]["resumed"], "skip_complete")
        # 回退（真实 CLI 子进程）
        rr = run_cli(ROLL, "--db", str(self.db), "--journal", str(self.journal),
                     "--mode", "head-revert", "--batch-size", "2",
                     "--out", str(self.dir / "rev.json"))
        self.assertEqual(rr.returncode, 0, rr.stderr)
        for ot in ots:
            self.assertEqual(self._heads()[ot], 920001, "已回退到旧来源")
        # 再前进
        r3 = self._apply()
        self.assertEqual(r3.returncode, 0, r3.stderr)
        d3 = json.loads((self.dir / "apply_out.json").read_text(encoding="utf-8"))
        self.assertEqual(d3["apply"]["results"][0]["heads_switched"], 3, "必须真实重切 3 个 head")
        for ot in ots:
            self.assertEqual(self._heads()[ot], nsid)
            self.assertEqual(self._vol(nsid, ot), "20")


# ══════════════════════════ §2 恢复验收有牙（V2-02） ══════════════════════════
class ToothedVerify(Base):
    def test_close_corrupted_999_not_skip_complete(self):
        """V2-02：新版本 close 改 999 ⇒ 恢复不得 skip_complete，必须 fail closed 非零退出。"""
        ots = self._seed(n=3)
        self.assertEqual(self._apply().returncode, 0)
        nsid = int(json.loads(self.journal.read_text(encoding="utf-8"))
                   ["sources"]["920001"]["new_source_id"])
        self.con.execute("UPDATE candle_versions SET close='999' WHERE source_id=? AND open_time=?",
                         (nsid, ots[1]))
        self.con.commit()
        r2 = self._apply()                                # 默认复跑（复用冻结计划）
        self.assertNotEqual(r2.returncode, 0, "close 坏值必须 fail closed，不能 skip_complete")
        j2 = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(j2["sources"]["920001"]["stage"], "error")
        self.assertIn("写后验收失败", j2["sources"]["920001"]["error"] or "")

    def test_available_at_and_metadata_wrong_fail_closed(self):
        """新版本 available_at 被改 ⇒ 有牙验收必须捕获（非仅 volume）。"""
        ots = self._seed(n=3)
        self.assertEqual(self._apply().returncode, 0)
        j = json.loads(self.journal.read_text(encoding="utf-8"))
        nsid = int(j["sources"]["920001"]["new_source_id"])
        rm = int(j["sources"]["920001"]["repair_ms"])
        self.con.execute("UPDATE candle_versions SET available_at=? WHERE source_id=? AND open_time=?",
                         (rm + 12345, nsid, ots[0]))
        self.con.commit()
        r = run_cli(ACC, "--db", str(self.db), "--plan", str(self.plan_file),
                    "--journal", str(self.journal), "--phase", "accept",
                    "--out", str(self.dir / "acc.json"))
        self.assertNotEqual(r.returncode, 0, "available_at 不符必须 FAIL 非零退出")
        acc = json.loads((self.dir / "acc.json").read_text(encoding="utf-8"))
        self.assertEqual(acc["verdict"], "FAIL")
        self.assertGreaterEqual(acc["aggregate"]["rows_field_mismatch"], 1)


# ══════════════════════════ §3 验收 expected keys（V2-03） ══════════════════════════
class Acceptance(Base):
    def _complete(self, n=3):
        ots = self._seed(n=n)
        self.assertEqual(self._apply().returncode, 0)
        j = json.loads(self.journal.read_text(encoding="utf-8"))
        return ots, int(j["sources"]["920001"]["new_source_id"])

    def _acc(self, phase="accept"):
        return run_cli(ACC, "--db", str(self.db), "--plan", str(self.plan_file),
                       "--journal", str(self.journal), "--phase", phase,
                       "--out", str(self.dir / f"acc_{phase}.json"))

    def test_pass_when_complete(self):
        self._complete()
        r = self._acc()
        self.assertEqual(r.returncode, 0, r.stderr)
        acc = json.loads((self.dir / "acc_accept.json").read_text(encoding="utf-8"))
        self.assertEqual(acc["verdict"], "PASS")
        self.assertEqual(acc["aggregate"]["rows_field_mismatch"], 0)
        self.assertEqual(acc["aggregate"]["missing_new"], 0)

    def test_missing_rows_recorded_and_nonzero(self):
        """V2-03 反例：缺新行必须记 missing_new 且非零退出（不再静默 0）。"""
        ots, nsid = self._complete()
        self._fk_off()
        self.con.execute("DELETE FROM candle_versions WHERE source_id=? AND open_time=?",
                         (nsid, ots[1]))
        self.con.commit()
        r = self._acc()
        self.assertNotEqual(r.returncode, 0)
        acc = json.loads((self.dir / "acc_accept.json").read_text(encoding="utf-8"))
        self.assertEqual(acc["verdict"], "FAIL")
        self.assertGreaterEqual(acc["aggregate"]["missing_new"], 1)
        self.assertEqual(acc["aggregate"]["rows_expected"], 3)

    def test_extra_row_nonzero(self):
        ots, nsid = self._complete()
        extra_ot = ots[0] + 1                              # 范围内、非 expected 网格根
        self.con.execute(
            "INSERT INTO candle_versions VALUES ('okx','swap','CRO-USDT-SWAP','15m',?,?,"
            "'1','1','1','1','9','9',0,'','',?,NULL,1)",
            (extra_ot, extra_ot + IV - 1, nsid))
        self.con.commit()
        r = self._acc()
        self.assertNotEqual(r.returncode, 0)
        acc = json.loads((self.dir / "acc_accept.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(acc["aggregate"]["extra_new"], 1)

    def test_missing_head_nonzero(self):
        ots, nsid = self._complete()
        self.con.execute("UPDATE candle_heads SET source_id=920001 WHERE open_time=?", (ots[0],))
        self.con.commit()
        r = self._acc()
        self.assertNotEqual(r.returncode, 0)
        acc = json.loads((self.dir / "acc_accept.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(acc["aggregate"]["missing_head"], 1)

    def test_bad_metadata_nonzero(self):
        ots, nsid = self._complete()
        self.con.execute("UPDATE sources SET row_count=row_count+7 WHERE id=?", (nsid,))
        self.con.commit()
        r = self._acc()
        self.assertNotEqual(r.returncode, 0)
        acc = json.loads((self.dir / "acc_accept.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(acc["aggregate"]["metadata_mismatch"], 1)

    def test_baseline_not_pass(self):
        """apply 前：--phase baseline 报 BASELINE（不得误称 PASS）。"""
        self._seed(n=3)
        r = run_cli(FIX, "--mode", "dry-run", "--db", str(self.db), "--raw-dir", str(self.raw),
                    "--tfs", TF, "--out", str(self.dir / "dry.json"),
                    "--plan-file", str(self.plan_file))
        self.assertEqual(r.returncode, 0, r.stderr)
        rb = self._acc("baseline")
        self.assertEqual(rb.returncode, 0)
        acc = json.loads((self.dir / "acc_baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(acc["verdict"], "BASELINE")
        self.assertEqual(acc["aggregate"]["not_applied"], 1)

    def test_plan_without_fixed_rows_refused(self):
        """dry-run 产物剔除 fixed_rows ⇒ 拒绝作为 expected keys（合同 §3 反例来源）。"""
        self._seed(n=3)
        r = run_cli(FIX, "--mode", "dry-run", "--db", str(self.db), "--raw-dir", str(self.raw),
                    "--tfs", TF, "--out", str(self.dir / "dry.json"),
                    "--plan-file", str(self.plan_file))
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = run_cli(ACC, "--db", str(self.db), "--plan", str(self.dir / "dry.json"),
                     "--out", str(self.dir / "acc.json"))
        self.assertNotEqual(r2.returncode, 0, "缺 fixed_rows 的计划必须被拒绝")
        self.assertIn("fixed_rows", r2.stderr + r2.stdout)


# ══════════════════════════ §4 提交后 journal 未落盘 / 库身份 ══════════════════════════
class SourceCommitRecovery(Base):
    def test_recover_first_repair_time_when_journal_not_persisted(self):
        """V2-04：source 已提交、journal 尚未落盘 ⇒ CLI 恢复统一**首次**修复时点，不猜新时间。"""
        ots = self._seed(n=3)
        self.assertEqual(self._apply().returncode, 0)
        j = json.loads(self.journal.read_text(encoding="utf-8"))
        nsid = int(j["sources"]["920001"]["new_source_id"])
        imported = self.con.execute("SELECT imported_at FROM sources WHERE id=?",
                                    (nsid,)).fetchone()[0]
        # 模拟「source 已提交、journal os.replace 前中断」：journal 回退到 planned（无 new_source_id）
        # + 删除已写版本行 + head 回旧来源。
        j["sources"]["920001"].update({"stage": "planned", "new_source_id": None,
                                       "repair_ms": None, "rows_written": 0, "heads_switched": 0})
        self.journal.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        self._fk_off()
        self.con.execute("DELETE FROM candle_versions WHERE source_id=?", (nsid,))
        self.con.execute("UPDATE candle_heads SET source_id=920001")
        self.con.commit()
        # CLI 恢复（默认复用冻结计划）
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        # 同一来源被复用（不新建）
        self.assertEqual(self._new_sid(), [nsid], "必须复用已提交的修订来源，不新建")
        self.assertEqual(self.con.execute("SELECT imported_at FROM sources WHERE id=?",
                                          (nsid,)).fetchone()[0], imported,
                         "imported_at 必须保持首次真实时点")
        # 所有补写行 available_at 统一 == 首次时点
        avails = {r[0] for r in self.con.execute(
            "SELECT DISTINCT available_at FROM candle_versions WHERE source_id=?", (nsid,))}
        self.assertEqual(avails, {imported}, "所有版本行 available_at 必须统一为首次修复时点")
        self.assertIn("沿用首次时点", r.stdout + r.stderr)

    def test_db_identity_inode_change_rejected(self):
        """同路径重建（inode 变）⇒ 冻结计划不可重放。"""
        self._seed(n=3)
        r = run_cli(FIX, "--mode", "dry-run", "--db", str(self.db), "--raw-dir", str(self.raw),
                    "--tfs", TF, "--out", str(self.dir / "dry.json"),
                    "--plan-file", str(self.plan_file))
        self.assertEqual(r.returncode, 0, r.stderr)
        # 同路径重建：把原库改名，另建一个同 schema 的新库（inode 变）
        self.con.close()
        os.rename(str(self.db), str(self.db) + ".orig")
        con2 = sqlite3.connect(str(self.db))
        con2.executescript((PROJ / "data_hub" / "schema.sql").read_text(encoding="utf-8"))
        con2.commit()
        con2.close()
        r2 = self._apply()                                # 复用冻结计划 ⇒ 应被拒
        self.assertNotEqual(r2.returncode, 0, "inode 变化必须拒绝重放冻结计划")
        self.assertIn("inode", r2.stderr + r2.stdout)
        # 恢复连接供 tearDown
        self.con = sqlite3.connect(str(self.db))
        self.con.row_factory = sqlite3.Row


if __name__ == "__main__":
    unittest.main(verbosity=2)
