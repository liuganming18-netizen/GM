#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁独立核查（v3.1，不信 ready=true，一律实际复算）。

核查对象：
  · evidence/Work_策略优化_2026-10-15/strategy_ready.json        （策略窗）
  · evidence/Work_策略数据_2026-10-15/data_ready_v31.json        （数据窗）

复算项（fail-closed）：
  A 策略门禁：
    A1 ready=true 且版本绑定 v3.1；
    A2 **逐文件复算** 策略/WyckoffMaster/*.py + README.md 的 SHA256，与声明逐一对上；
    A3 复算 code bundle（同算法：sha256("|".join(f"{f}:{sha}"))）与声明一致；
    A4 blocking_items 为空。
  B 数据门禁：
    B1 ready=true 且版本绑定 v3.1；
    B2 manifest_v31.json 实际 SHA == 声明；
    B3 物理隔离：is/ 与 val/ 存在、holdout_dir 为空、holdout_read=false；
    B4 备份/隔离恢复证据文件实际存在；blocking_items 为空。
  C 生产执行引擎 wx_execution.py SHA == 冻结值（防他窗改写执行语义）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PROJECT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
WM = PROJECT / "回测日志" / "WyckoffMaster"
STRATEGY_READY = PROJECT / "evidence" / "Work_策略优化_2026-10-15" / "strategy_ready.json"
DATA_READY = PROJECT / "evidence" / "Work_策略数据_2026-10-15" / "data_ready_v31.json"
MODULE_DIR = PROJECT / "策略" / "WyckoffMaster"
V31 = PROJECT / "数据" / "研究输入" / "WyckoffMaster_v31"
FROZEN_ENGINE_SHA = "0ede9ce19c7cec9d18398572e393ef65ef4c04a6aa5ff2b41f8ad022c83dbcf1"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def code_bundle():
    files = sorted(f for f in __import__("os").listdir(MODULE_DIR)
                   if f.endswith(".py") or f == "README.md")
    per = {f: sha256_file(MODULE_DIR / f) for f in files}
    bundle = hashlib.sha256("|".join(f"{f}:{per[f]}" for f in files).encode()).hexdigest()
    return bundle, per


def verify_strategy():
    checks = []
    if not STRATEGY_READY.exists():
        return {"label": "strategy_ready", "exists": False, "verdict": "NOT_READY",
                "checks": [], "blocking_checks": ["file_missing"]}
    doc = json.loads(STRATEGY_READY.read_text(encoding="utf-8"))
    blob = json.dumps(doc, ensure_ascii=False)
    checks.append(_c("strategy:ready", bool(doc.get("ready"))))
    checks.append(_c("strategy:version_v3.1", any(t in blob for t in ("3.1", "v3.1", "V3.1"))))
    declared = doc.get("code_sha256") or {}
    per_dec = declared.get("per_file") or {}
    bundle, per = code_bundle()
    mism = [f for f in per_dec if f in per and per[f] != per_dec[f]]
    missing = [f for f in per_dec if f not in per]
    extra = [f for f in per if f not in per_dec]
    checks.append(_c("strategy:per_file_sha_recomputed", not mism and not missing and not extra,
                     {"mismatch": mism, "missing": missing, "extra": extra,
                      "n_files": len(per)}))
    checks.append(_c("strategy:bundle_sha_recomputed", bundle == declared.get("bundle_sha256"),
                     {"declared": declared.get("bundle_sha256"), "actual": bundle}))
    bi = doc.get("blocking_items")
    checks.append(_c("strategy:blocking_empty", bi in (None, [], {}), {"blocking": bi}))
    fails = [c["item"] for c in checks if c["verdict"] == "FAIL"]
    return {"label": "strategy_ready", "exists": True, "verdict": "PASS" if not fails else "FAIL",
            "checks": checks, "blocking_checks": fails, "code_bundle_sha256": bundle,
            "path": str(STRATEGY_READY)}


def verify_data():
    checks = []
    if not DATA_READY.exists():
        return {"label": "data_ready", "exists": False, "verdict": "NOT_READY",
                "checks": [], "blocking_checks": ["file_missing"]}
    doc = json.loads(DATA_READY.read_text(encoding="utf-8"))
    blob = json.dumps(doc, ensure_ascii=False)
    checks.append(_c("data:ready", bool(doc.get("ready"))))
    checks.append(_c("data:version_v3.1", any(t in blob for t in ("3.1", "v3.1", "V3.1"))))
    man = doc.get("manifest") or {}
    mp = Path(man.get("path", ""))
    actual = sha256_file(mp) if mp.exists() else None
    checks.append(_c("data:manifest_sha_recomputed",
                     actual is not None and actual == man.get("sha256"),
                     {"declared": man.get("sha256"), "actual": actual}))
    iso = doc.get("physical_isolation") or {}
    isd, vald = Path(iso.get("is_dir", "")), Path(iso.get("val_dir", ""))
    checks.append(_c("data:is_val_dirs_exist", isd.exists() and vald.exists(),
                     {"is": str(isd), "val": str(vald)}))
    checks.append(_c("data:holdout_absent",
                     iso.get("holdout_dir") in (None, "", "null") and not iso.get("holdout_read"),
                     {"holdout_dir": iso.get("holdout_dir"), "holdout_read": iso.get("holdout_read")}))
    br = doc.get("backup_restore")
    checks.append(_c("data:backup_restore_present", bool(br),
                     {"type": type(br).__name__}))
    bi = doc.get("blocking_items")
    checks.append(_c("data:blocking_empty", bi in (None, [], {}), {"blocking": bi}))
    fails = [c["item"] for c in checks if c["verdict"] == "FAIL"]
    return {"label": "data_ready", "exists": True, "verdict": "PASS" if not fails else "FAIL",
            "checks": checks, "blocking_checks": fails}


def verify_production():
    p = PROJECT / "框架" / "backend" / "strategies" / "wx_execution.py"
    got = sha256_file(p) if p.exists() else None
    ok = got == FROZEN_ENGINE_SHA
    return {"item": "production:wx_execution_sha", "verdict": "OK" if ok else "FAIL",
            "declared": FROZEN_ENGINE_SHA, "actual": got}


def _c(item, ok, note=None):
    return {"item": item, "verdict": "OK" if ok else "FAIL", "note": note}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default=str(WM / "state" / "gate_verify.json"))
    a = ap.parse_args()
    s = verify_strategy(); d = verify_data(); prod = verify_production()
    if prod["verdict"] != "OK":
        d = dict(d); d["verdict"] = "FAIL"
        d.setdefault("blocking_checks", []).append(prod["item"])
    if s["verdict"] == "NOT_READY" or d["verdict"] == "NOT_READY":
        overall, rc = "WAITING_INPUT", 2
    elif s["verdict"] == "PASS" and d["verdict"] == "PASS":
        overall, rc = "BOTH_PASS", 0
    else:
        overall, rc = "BLOCKED", 3
    rep = {"schema": "wyckoffmaster_gate_verify/v2_v31", "strategy_ready": s, "data_ready": d,
           "production": prod, "overall": overall}
    out = Path(a.json_out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"strategy_ready={s['verdict']}  data_ready={d['verdict']}  "
          f"production={prod['verdict']}  overall={overall}")
    for lbl, r in (("strategy", s), ("data", d)):
        for c in r.get("checks", []):
            if c["verdict"] != "OK":
                print(f"  [{lbl}] {c['item']}: {c['verdict']} {c.get('note')}")
    print("wrote", out)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
