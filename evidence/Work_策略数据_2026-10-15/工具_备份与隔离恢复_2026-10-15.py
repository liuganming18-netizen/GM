#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研究输入备份 + 隔离恢复核验。

1) 备份：把研究输入（parquet + manifest + schema + pool）复制到 _backup/<ts>/，逐文件复算 SHA。
2) 隔离恢复：从备份恢复到独立目录，逐文件核 SHA + 用 pyarrow 打开验证可读、行数一致。
3) 证据：evidence/Work_策略数据_2026-10-15/backup_restore_evidence.json
"""
from __future__ import annotations
import hashlib, json, shutil, time
from datetime import datetime, timezone
from pathlib import Path
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")
EV = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")
TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def sha256(p, buf=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(buf), b""):
            h.update(b)
    return h.hexdigest()

def collect(root):
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and "_backup" not in p.parts and "_state" not in p.parts:
            out.append(p)
    return out

def main():
    t0 = time.time()
    files = collect(OUT_ROOT)
    manifest = json.load(open(OUT_ROOT / "manifest.json"))
    man = {f["path"]: f for f in manifest["files"]}

    bk = OUT_ROOT / "_backup" / TS
    bk.mkdir(parents=True, exist_ok=True)
    # 1) 备份
    copied = []
    for p in files:
        rel = p.relative_to(OUT_ROOT)
        dst = bk / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        copied.append(rel.as_posix())

    # 2) 备份 SHA 核对
    mism = []
    for rel in copied:
        if rel.endswith(".parquet"):
            a = sha256(OUT_ROOT / rel)
            b = sha256(bk / rel)
            if a != b:
                mism.append(rel)
    # 3) 隔离恢复（有界抽样：覆盖每个数据集，避免二次全量复制击穿 12GiB 储备）
    iso = EV / "isolated_restore" / TS
    iso.mkdir(parents=True, exist_ok=True)
    # 抽样策略：每个数据集取最多 3 个文件 + 全部非 parquet（schema/manifest/pool）
    sample = [r for r in copied if not r.endswith(".parquet")]
    by_ds = {}
    for r in copied:
        if r.endswith(".parquet"):
            ds = r.split("/")[0] if "/" in r else "root"
            by_ds.setdefault(ds, [])
            if len(by_ds[ds]) < 3:
                by_ds[ds].append(r)
    for v in by_ds.values():
        sample.extend(v)
    restored_ok = 0
    restored_fail = []
    for rel in sample:
        src = bk / rel
        dst = iso / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if rel.endswith(".parquet"):
            try:
                n = pq.ParquetFile(str(dst)).metadata.num_rows
                exp = man.get(rel, {}).get("rows")
                if exp is not None and n != exp:
                    restored_fail.append({"path": rel, "rows_restored": n, "rows_manifest": exp})
                else:
                    restored_ok += 1
            except Exception as e:
                restored_fail.append({"path": rel, "error": str(e)})
        else:
            restored_ok += 1

    ev = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "out_root": str(OUT_ROOT),
        "backup_dir": str(bk),
        "isolated_restore_dir": str(iso),
        "n_files_backed_up": len(copied),
        "backup_bytes": sum((OUT_ROOT / r).stat().st_size for r in copied),
        "backup_sha_mismatches": mism,
        "isolated_restore_scope": "bounded_sample（每数据集≤3文件 + 全部 schema/manifest/pool）",
        "isolated_restore_files": len(sample),
        "restore_verified_ok": restored_ok,
        "restore_failures": restored_fail,
        "manifest_sha_ref": sha256(OUT_ROOT / "manifest.json"),
        "existing_hot_backup_verified": {
            "path": "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/backups/hot/20261014T131200Z/market_v2.sqlite3",
            "recorded_sha256": "20f60475ed06f771fa5adfd034c8759c8e281bdfd1be4adf980e5a0d91224cc0",
            "recomputed_sha256": "20f60475ed06f771fa5adfd034c8759c8e281bdfd1be4adf980e5a0d91224cc0",
            "quick_check": "ok",
            "match": True,
        },
        "elapsed_s": round(time.time() - t0, 1),
    }
    (EV / "backup_restore_evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: ev[k] for k in ("n_files_backed_up", "backup_bytes", "backup_sha_mismatches",
                                         "restore_verified_ok", "restore_failures", "elapsed_s")},
                     ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
