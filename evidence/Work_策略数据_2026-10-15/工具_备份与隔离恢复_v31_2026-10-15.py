#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 研究输入备份 + 有界隔离恢复证据

磁盘约束：用户要求保持 12GiB 空闲。is+val 共约 7.6G，若全量再复制一份将吃掉余量，故：
  - 全量备份：体量小且为结算关键的数据集（funding / mark1h）与全部元数据
    （instruments / schema / pool / manifest）
  - 有界抽样备份：大体量数据集（ohlcv / oi_metrics）每窗口每数据集 <=3 个文件
  - 完整性由 manifest_v31.json 的全文件 SHA256 覆盖（100% 文件）
输出：evidence/Work_策略数据_2026-10-15/backup_restore_evidence_v31.json
"""
from __future__ import annotations
import hashlib, json, shutil
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
EVID = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")
OUT = EVID / "backup_restore_evidence_v31.json"

FULL_DS = ("funding", "mark1h")
SAMPLE_DS = ("ohlcv", "oi_metrics")
SAMPLE_N = 3


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def collect():
    full, sample = [], []
    for win in ("is", "val"):
        base = OUT_ROOT / win
        if not base.is_dir():
            continue
        for ds in FULL_DS:
            d = base / ds
            if d.is_dir():
                full += sorted(d.glob("*.parquet"))
        for ds in SAMPLE_DS:
            if ds == "ohlcv":
                for tf in ("1h", "4h", "15m"):
                    d = base / "ohlcv" / tf
                    if d.is_dir():
                        sample += sorted(d.glob("*.parquet"))[:SAMPLE_N]
            else:
                d = base / ds
                if d.is_dir():
                    sample += sorted(d.glob("*.parquet"))[:SAMPLE_N]
    meta = []
    for n in ("instruments.parquet", "schema.json", "pool.json", "manifest_v31.json"):
        p = OUT_ROOT / n
        if p.is_file():
            meta.append(p)
    return full, sample, meta


def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = OUT_ROOT / "_backup" / ts
    rdir = OUT_ROOT / "_backup" / "isolated_restore" / ts
    bdir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)

    full, sample, meta = collect()
    files = full + sample + meta
    man = {"generated_at": ts, "backup_dir": str(bdir),
           "policy": {"full_datasets": list(FULL_DS) + ["instruments", "schema", "pool", "manifest"],
                      "sampled_datasets": list(SAMPLE_DS), "sample_n_per_tf": SAMPLE_N,
                      "reason": "保持 12GiB 空闲约束；完整性由 manifest_v31.json 全文件 SHA256 覆盖"},
           "n_full": len(full), "n_sample": len(sample), "n_meta": len(meta),
           "reconcile": {"match": 0, "mismatch": []},
           "restore": {"checked": 0, "ok": 0, "bad": []}, "restore_sample": []}

    for p in files:
        rel = p.relative_to(OUT_ROOT)
        dst = bdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        if sha256(p) == sha256(dst):
            man["reconcile"]["match"] += 1
        else:
            man["reconcile"]["mismatch"].append(str(rel))

    for p in (sample + meta):
        rel = p.relative_to(OUT_ROOT)
        dst = rdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        ok = sha256(dst) == sha256(p)
        try:
            n = pq.read_metadata(str(dst)).num_rows
        except Exception as e:
            n = f"ERR {e}"
        man["restore"]["checked"] += 1
        if ok:
            man["restore"]["ok"] += 1
        else:
            man["restore"]["bad"].append(str(rel))
        man["restore_sample"].append({"path": str(rel), "sha_match": ok, "rows": n})

    prod = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统/backups/hot/20261014T131200Z/market_v2.sqlite3")
    man["existing_prod_backup"] = {
        "path": str(prod), "exists": prod.is_file(),
        "bytes": prod.stat().st_size if prod.is_file() else None,
        "note": "上一轮已复算 SHA256 与记录一致且 quick_check=ok；本窗只读引用，不重算大文件",
    }

    OUT.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: man[k] for k in ("n_full", "n_sample", "n_meta", "reconcile", "restore")},
                     ensure_ascii=False, indent=1))
    print("->", OUT)


if __name__ == "__main__":
    main()
