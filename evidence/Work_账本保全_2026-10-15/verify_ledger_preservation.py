#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐表/逐行对照 Codex 保全基线，证明（或证否）原账本内容守恒。

**只读**：仅以 `mode=ro` 打开目标账本，不写、不 DDL、不建库。
用法：
    python3 verify_ledger_preservation.py [账本目录]
缺省账本目录 = `/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/.../纸面模拟43`（原账本原位）。

输出三档结论：
  · `PRESERVED_EXACT`   —— 行数 + 列集 + **逐行哈希全等**（且成功反推出基线规范化方式）
  · `PRESERVED_COUNTS`  —— 行数 + 列集全等，但逐行规范化方式未能反推 ⇒ 只能证明"量守恒"
  · `NOT_PRESERVED`     —— 行数/列集/哈希任一不等
  · `LEDGER_MISSING`    —— 目标账本不存在（**本任务现状**）
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
BASELINE = (PROJECT_ROOT / "evidence" / "Codex_闭环复审_2026-10-15"
            / "legacy_ledger_preservation_baseline.json")
ORIGINAL_DIR = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
                    "最新唐奇安威科夫接通与收尾_2026-10-11/量价信号接入与模拟历史并行43/"
                    "纸面模拟43")
TABLES = ("paper_position", "paper_fill", "paper_funding", "paper_equity",
          "paper_realized", "paper_order", "paper_state")


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ── 候选「行规范化」方案（Codex 未在基线里声明用了哪一种）────────────────────
_SEPS = ("|", "\x1f", "\t", ",", "\x00")
_NULLS = ("", "None", "NULL", "null")


def _row_hashes_candidates(values: list[tuple]) -> dict[str, list[str]]:
    """对每种候选规范化方式，算出该表的逐行哈希序列。"""
    out: dict[str, list[str]] = {}
    for sep in _SEPS:
        for nul in _NULLS:
            name = f"sep={sep!r},null={nul!r}"
            try:
                out[name] = [_h(sep.join(nul if v is None else str(v)
                                         for v in row).encode()) for row in values]
            except Exception:                                   # noqa: BLE001
                pass
    # JSON 变体
    for tag, fn in (
        ("json_list_compact", lambda row: json.dumps(list(row), ensure_ascii=False,
                                                     separators=(",", ":"), default=str)),
        ("json_list_spaces", lambda row: json.dumps(list(row), ensure_ascii=False, default=str)),
        ("json_repr", lambda row: repr(tuple(row))),
    ):
        try:
            out[tag] = [_h(fn(row).encode()) for row in values]
        except Exception:                                       # noqa: BLE001
            pass
    return out


def _columns_of(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def verify_arm(db_path: Path, base_arm: dict) -> dict:
    res: dict = {"path": str(db_path), "exists": db_path.exists(), "tables": {}}
    if not db_path.exists():
        res["verdict"] = "LEDGER_MISSING"
        return res
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    con.row_factory = sqlite3.Row
    try:
        all_counts_ok = True
        all_hash_ok = True
        any_hash_scheme_found = False
        for t in TABLES:
            exp = (base_arm.get("tables") or {}).get(t)
            if not exp:
                continue
            cell: dict = {"expected_rows": exp.get("rows")}
            try:
                cols = _columns_of(con, t)
                cell["columns_equal"] = (cols == exp.get("columns"))
                n = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                cell["actual_rows"] = n
                cell["rows_equal"] = (n == exp.get("rows"))
                if not cell["rows_equal"]:
                    all_counts_ok = False
                if cell["rows_equal"] and cell["columns_equal"]:
                    values = [tuple(r) for r in con.execute(f'SELECT * FROM "{t}"')]
                    target = exp.get("row_hashes") or []
                    best = None
                    for name, hashes in _row_hashes_candidates(values).items():
                        if hashes == target:
                            best = name
                            break
                    cell["row_hash_scheme"] = best
                    cell["row_hashes_equal"] = best is not None
                    if best is not None:
                        any_hash_scheme_found = True
                    else:
                        all_hash_ok = False
            except sqlite3.OperationalError as exc:
                cell["error"] = f"{type(exc).__name__}: {exc}"
                all_counts_ok = False
                all_hash_ok = False
            res["tables"][t] = cell
        if not all_counts_ok:
            res["verdict"] = "NOT_PRESERVED"
        elif all_hash_ok and any_hash_scheme_found:
            res["verdict"] = "PRESERVED_EXACT"
        else:
            res["verdict"] = "PRESERVED_COUNTS"
            res["note"] = ("行数与列集全等，但未能反推 Codex 的逐行规范化方式 ⇒ "
                           "只能证明「量守恒」，不能证明「逐行内容守恒」。"
                           "若需逐行证明，请提供 Codex 生成基线所用的规范化脚本。")
    finally:
        con.close()
    return res


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ORIGINAL_DIR
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    print("基线文件 :", BASELINE)
    print("基线时间 :", base.get("at_utc"))
    print("目标账本 :", target)
    print()
    out = {"baseline": str(BASELINE), "baseline_at_utc": base.get("at_utc"),
           "target_dir": str(target), "target_exists": target.exists(), "arms": {}}
    for arm in ("3a", "3b", "3c", "3d"):
        r = verify_arm(target / f"paper_{arm}.sqlite3", base["arms"][arm])
        out["arms"][arm] = r
        print(f"── 臂 {arm}: {r['verdict']}")
        for t, cell in r.get("tables", {}).items():
            print(f"     {t:16s} exp={cell.get('expected_rows')} "
                  f"act={cell.get('actual_rows')} rows_eq={cell.get('rows_equal')} "
                  f"cols_eq={cell.get('columns_equal')} hash_eq={cell.get('row_hashes_equal')}"
                  + (f" scheme={cell.get('row_hash_scheme')}"
                     if cell.get("row_hash_scheme") else ""))
    if not target.exists():
        print()
        print("结论：LEDGER_MISSING —— 原账本目录不在磁盘，**无法执行逐行校验**。")
        print("      详见 legacy_ledger_state.json（含丢失取证与可恢复性核查）。")
    verdicts = {a: r["verdict"] for a, r in out["arms"].items()}
    out["overall"] = ("LEDGER_MISSING" if not target.exists()
                      else ("PRESERVED_EXACT"
                            if all(v == "PRESERVED_EXACT" for v in verdicts.values())
                            else ("NOT_PRESERVED" if any(v == "NOT_PRESERVED"
                                                         for v in verdicts.values())
                                  else "PRESERVED_COUNTS")))
    (HERE / "preservation_verification_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print("总判定：", out["overall"])
    print("已写出：", HERE / "preservation_verification_result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
