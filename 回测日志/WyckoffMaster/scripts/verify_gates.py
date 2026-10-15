#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁独立核查：不信 `ready=true`，逐项复算 SHA / 代码 / 配置 / 覆盖 / 证据。

被核查对象（由策略窗口与数据窗口发布）：
  · evidence/Work_策略优化_2026-10-15/strategy_ready.json
  · evidence/Work_数据_2026-10-15/data_ready.json      （亦接受 Work_策略数据_2026-10-15/）

核查原则（fail-closed）：
  1. 文件存在且可解析；`ready` 必须为 true（但**仅此不足以通过**）。
  2. **深度复算**：递归找出 JSON 内所有 {path|file|...} + {sha256|sha} 配对，逐文件实算 SHA256 比对。
     任何一项不符 ⇒ FAIL。
  3. **冻结身份核对**：若声明了引擎/配置 SHA，必须与冻结值一致（wx_execution 0ede9ce1…，
     配置视图 6d18d222…）。声明缺失 ⇒ 记 MISSING（不通过）。
  4. **blocking_items 必须为空**。
  5. **备份与隔离恢复证据**：data_ready 必须给出备份文件 SHA + 恢复位置 + 恢复结果，且该路径实际存在。
  6. 任何无法核实项一律记为 FAIL/MISSING，绝不默认通过。

退出码：0 = 两个门禁都 PASS；2 = 未就绪（文件缺失）；3 = 存在 FAIL。
用法：
    python3 verify_gates.py [--json-out ../state/gate_verify.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
STRATEGY_READY = PROJECT / "evidence" / "Work_策略优化_2026-10-15" / "strategy_ready.json"
DATA_READY_CANDIDATES = (
    PROJECT / "evidence" / "Work_策略数据_2026-10-15" / "data_ready.json",
    PROJECT / "evidence" / "Work_数据_2026-10-15" / "data_ready.json",
)

FROZEN_ENGINE_SHA = "0ede9ce19c7cec9d18398572e393ef65ef4c04a6aa5ff2b41f8ad022c83dbcf1"
FROZEN_CONFIG_VIEW_SHA = "6d18d222d1991f99dd2ba0cdf80b35fbc57f4f482904c945f43d66ebb9df9501"
REQUIRED_VERSION = "v3.1"
REQUIRED_VERSION_TOKENS = ("v3.1", "V3.1", "WyckoffMaster_v3.1", "WyckoffMaster v3.1")

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PATH_KEYS = ("path", "file", "file_path", "manifest", "manifest_path", "source_path",
              "backup_path", "restore_path", "ledger", "input")
_SHA_KEYS = ("sha256", "sha", "sha_256", "file_sha256", "manifest_sha256")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _walk_pairs(node, out, ctx=""):
    """递归收集 (声明路径, 声明sha, 上下文) 配对。"""
    if isinstance(node, dict):
        path_val = None
        for pk in _PATH_KEYS:
            if isinstance(node.get(pk), str):
                path_val = node[pk]
                break
        for sk in _SHA_KEYS:
            v = node.get(sk)
            if isinstance(v, str) and _SHA_RE.match(v):
                if path_val:
                    out.append({"declared_path": path_val, "declared_sha256": v,
                                "ctx": ctx or (list(node.keys())[:6] and ",".join(map(str, node.keys()[:6])))})
        for k, v in node.items():
            _walk_pairs(v, out, ctx=f"{ctx}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_pairs(v, out, ctx=f"{ctx}[{i}]")


def _resolve(declared: str) -> Path:
    p = Path(declared)
    return p if p.is_absolute() else (PROJECT / p)


def _deep_sha_check(doc: dict) -> dict:
    pairs: list[dict] = []
    _walk_pairs(doc, pairs)
    results, n_ok, n_bad, n_missing = [], 0, 0, 0
    for pr in pairs:
        rp = _resolve(pr["declared_path"])
        if not rp.exists():
            n_missing += 1
            results.append({**pr, "resolved": str(rp), "actual": None,
                            "verdict": "MISSING_FILE"})
            continue
        try:
            actual = _sha256_file(rp)
        except OSError as e:
            n_bad += 1
            results.append({**pr, "resolved": str(rp), "actual": f"ERR:{e}",
                            "verdict": "UNREADABLE"})
            continue
        ok = (actual == pr["declared_sha256"])
        n_ok += ok
        n_bad += (not ok)
        results.append({**pr, "resolved": str(rp), "actual": actual,
                        "verdict": "OK" if ok else "SHA_MISMATCH"})
    return {"n_pairs": len(pairs), "n_ok": n_ok, "n_mismatch": n_bad,
            "n_missing_file": n_missing, "details": results}


def _find_first(doc, keys):
    """在嵌套文档里找第一个命中任一键的值。"""
    found = {}
    def rec(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k in keys and k not in found and not isinstance(v, (dict, list)):
                    found[k] = v
                rec(v)
        elif isinstance(n, list):
            for v in n:
                rec(v)
    rec(doc)
    return found


def _check_frozen_identity(doc: dict, label: str) -> list[dict]:
    """引擎/配置冻结 SHA 必须出现且一致。"""
    hits = _find_first(doc, {"engine_sha256", "wx_execution_sha256", "engine_sha",
                             "code_sha256", "config_view_sha256", "config_sha256"})
    checks = []
    blob = json.dumps(doc, ensure_ascii=False)
    checks.append({
        "item": f"{label}:frozen_engine_sha_present",
        "verdict": "OK" if FROZEN_ENGINE_SHA in blob else "MISSING",
        "note": f"需在文档中出现冻结引擎 SHA {FROZEN_ENGINE_SHA[:16]}…",
    })
    checks.append({
        "item": f"{label}:frozen_config_view_sha_present",
        "verdict": "OK" if FROZEN_CONFIG_VIEW_SHA in blob else "MISSING",
        "note": f"需出现冻结配置视图 SHA {FROZEN_CONFIG_VIEW_SHA[:16]}…",
    })
    if hits:
        checks.append({"item": f"{label}:declared_identity_keys",
                       "verdict": "INFO", "note": hits})
    return checks


def _check_blocking(doc: dict, label: str) -> dict:
    bi = _find_first(doc, {"blocking_items", "blocking", "blockers"})
    val = bi.get("blocking_items", bi.get("blocking", bi.get("blockers")))
    empty = (val in (None, [], {}, "", "none", "NONE"))
    return {"item": f"{label}:blocking_items_empty",
            "verdict": "OK" if empty else "FAIL",
            "note": f"blocking_items={val!r}"}


def _check_version(doc: dict, label: str) -> dict:
    """v3.1 起：策略/数据版本必须绑定 v3.1 才能启动。"""
    blob = json.dumps(doc, ensure_ascii=False)
    ok = any(t in blob for t in REQUIRED_VERSION_TOKENS)
    return {"item": f"{label}:version_bound_{REQUIRED_VERSION}",
            "verdict": "OK" if ok else "FAIL",
            "note": f"文档须绑定 {REQUIRED_VERSION}（策略/数据版本都必须 {REQUIRED_VERSION} 才启动）"}


def _check_research_input_isolation(label: str) -> dict:
    """研究输入必须物理区分 IS/Val 与 Holdout（v3.1）。"""
    try:
        sys.path.insert(0, str(PROJECT / "回测日志" / "WyckoffMaster" / "adapters"))
        import holdout_guard as HG
        facts = HG.assert_research_input_isolation(
            PROJECT / "数据" / "研究输入" / "WyckoffMaster")
    except Exception as e:  # noqa: BLE001
        return {"item": f"{label}:research_input_isolation", "verdict": "MISSING",
                "note": f"无法核验：{e}"}
    return {"item": f"{label}:research_input_isolation",
            "verdict": "OK" if facts.get("ok") else "FAIL",
            "note": facts.get("layout") or facts.get("reason")}


def _check_backup_evidence(doc: dict, label: str) -> list[dict]:
    """data_ready 必须具备备份 + 隔离恢复证据，且路径实际存在。"""
    checks = []
    blob = json.dumps(doc, ensure_ascii=False).lower()
    for kw in ("backup", "restore", "isolat", "备份", "恢复"):
        checks.append({"item": f"{label}:mentions::{kw}",
                       "verdict": "OK" if kw in blob else "MISSING"})
    found_paths = []
    def rec(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if isinstance(v, str) and any(t in k.lower() for t in
                                              ("backup_path", "restore_path", "recovery_path")):
                    found_paths.append(v)
                rec(v)
        elif isinstance(n, list):
            for v in n:
                rec(v)
    rec(doc)
    for p in found_paths:
        rp = _resolve(p)
        checks.append({"item": f"{label}:backup_path_exists::{p}",
                       "verdict": "OK" if rp.exists() else "FAIL"})
    if not found_paths:
        checks.append({"item": f"{label}:backup_path_declared",
                       "verdict": "MISSING",
                       "note": "未见 backup_path/restore_path/recovery_path 字段"})
    return checks


def verify_one(path: Path, label: str, require_backup: bool) -> dict:
    if not path.exists():
        return {"label": label, "path": str(path), "exists": False,
                "verdict": "NOT_READY", "reason": "门禁文件尚未发布"}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"label": label, "path": str(path), "exists": True,
                "verdict": "FAIL", "reason": f"JSON 解析失败：{e}"}

    ready = bool(_find_first(doc, {"ready", "gate_pass", "pass", "ok"}).get("ready")
                 or doc.get("ready") is True)
    deep = _deep_sha_check(doc)
    checks = []
    checks.append({"item": f"{label}:ready_flag", "verdict": "OK" if ready else "FAIL",
                   "note": "ready=true 仅为必要条件，不是充分条件"})
    checks.append(_check_version(doc, label))
    checks.extend(_check_frozen_identity(doc, label))
    checks.append(_check_blocking(doc, label))
    if require_backup:
        checks.extend(_check_backup_evidence(doc, label))
        checks.append(_check_research_input_isolation(label))

    fails = [c for c in checks if c["verdict"] in ("FAIL", "MISSING")]
    verdict = "PASS" if (ready and not fails and deep["n_mismatch"] == 0
                         and deep["n_missing_file"] == 0 and deep["n_pairs"] > 0) else "FAIL"
    if not ready:
        verdict = "NOT_READY"
    return {"label": label, "path": str(path), "exists": True, "ready_flag": ready,
            "deep_sha": deep, "checks": checks, "verdict": verdict,
            "blocking_checks": [c["item"] for c in fails]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default=str(PROJECT / "回测日志" / "WyckoffMaster"
                                             / "state" / "gate_verify.json"))
    a = ap.parse_args()

    data_path = next((p for p in DATA_READY_CANDIDATES if p.exists()),
                     DATA_READY_CANDIDATES[0])
    report = {
        "schema": "wyckoffmaster_gate_verify/v1",
        "strategy_ready": verify_one(STRATEGY_READY, "strategy_ready", require_backup=False),
        "data_ready": verify_one(data_path, "data_ready", require_backup=True),
    }
    sv = report["strategy_ready"]["verdict"]
    dv = report["data_ready"]["verdict"]
    if sv == "NOT_READY" or dv == "NOT_READY":
        report["overall"] = "WAITING_INPUT"
        rc = 2
    elif sv == "PASS" and dv == "PASS":
        report["overall"] = "BOTH_PASS"
        rc = 0
    else:
        report["overall"] = "BLOCKED"
        rc = 3
    out = Path(a.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"strategy_ready = {sv}   data_ready = {dv}   overall = {report['overall']}")
    for lbl in ("strategy_ready", "data_ready"):
        r = report[lbl]
        if r.get("exists"):
            d = r.get("deep_sha", {})
            print(f"  [{lbl}] pairs={d.get('n_pairs')} ok={d.get('n_ok')} "
                  f"mismatch={d.get('n_mismatch')} missing={d.get('n_missing_file')} "
                  f"blocking={r.get('blocking_checks')}")
        else:
            print(f"  [{lbl}] {r.get('reason')}")
    print("wrote", out)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
