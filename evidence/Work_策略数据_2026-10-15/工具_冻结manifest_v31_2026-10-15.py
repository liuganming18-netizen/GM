#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 冻结 manifest（is/val 物理隔离输入）

逐文件 SHA256 / 行数 / 首末时间 / 来源分布，冻结为 manifest.json 与逐币覆盖.csv。
扫描排除 _dl / _state / _backup。
"""
from __future__ import annotations
import csv, hashlib, json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
MANIFEST = OUT_ROOT / "manifest_v31.json"
COVERAGE = OUT_ROOT / "逐币覆盖_v31.csv"

TIME_COL = {"ohlcv/1h": "open_time", "ohlcv/4h": "open_time", "ohlcv/15m": "open_time",
            "oi_metrics": "ts", "funding": "funding_time", "mark1h": "open_time"}


def sha256(p, buf=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fmt(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dataset_of(rel):
    parts = rel.parts
    if parts[0] in ("is", "val"):
        if len(parts) >= 3 and parts[1] == "ohlcv":
            return f"{parts[0]}/ohlcv/{parts[2]}"
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return parts[0]


TIME_COL_LOOKUP = {"ohlcv": "open_time", "oi_metrics": "ts", "funding": "funding_time", "mark1h": "open_time"}


def time_col_for(ds):
    leaf = ds.split("/")[-1]
    return TIME_COL_LOOKUP.get(leaf, "open_time")


def scan():
    files = []
    for win in ("is", "val"):
        base = OUT_ROOT / win
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.parquet")):
            files.append(p)
    for p in sorted(OUT_ROOT.glob("instruments.parquet")):
        files.append(p)
    return files


def main():
    man = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "root": str(OUT_ROOT), "holdout_start_utc": "2026-04-15T00:00:00Z",
           "windows": {"is": ["2021-10-01T00:00:00Z", "2025-01-01T00:00:00Z"],
                       "val": ["2024-10-01T00:00:00Z", "2026-04-15T00:00:00Z"]},
           "files": [], "by_dataset": {}}
    rows_csv = []
    for p in scan():
        rel = p.relative_to(OUT_ROOT)
        ds = dataset_of(rel)
        tcol = time_col_for(ds)
        schema_names = pq.read_schema(str(p)).names
        if tcol not in schema_names:
            # instruments 等无时间列：用行数统计，不做时间范围
            n = pq.read_metadata(str(p)).num_rows
            mn = mx = None
            dup = 0
        else:
            t = pq.read_table(str(p), columns=[tcol])
            col = t.column(0).to_pylist()
            mn = min(col) if col else None
            mx = max(col) if col else None
            dup = len(col) - len(set(col))
            n = len(col)
        rec = {"path": str(rel), "dataset": ds, "rows": n,
               "first": fmt(mn), "last": fmt(mx), "dup_keys": dup,
               "bytes": p.stat().st_size, "sha256": sha256(p)}
        man["files"].append(rec)
        d = man["by_dataset"].setdefault(ds, {"files": 0, "rows": 0, "_min": None, "_max": None})
        d["files"] += 1
        d["rows"] += n
        if mn is not None:
            d["_min"] = mn if d["_min"] is None else min(d["_min"], mn)
        if mx is not None:
            d["_max"] = mx if d["_max"] is None else max(d["_max"], mx)
        rows_csv.append((str(rel), ds, n, fmt(mn), fmt(mx), dup, rec["sha256"]))
    # 整理为可读范围
    for d in man["by_dataset"].values():
        d["min_first"] = fmt(d.pop("_min"))
        d["max_last"] = fmt(d.pop("_max"))
    MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(COVERAGE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "dataset", "rows", "first", "last", "dup_keys", "sha256"])
        w.writerows(rows_csv)
    print(f"files={len(man['files'])} datasets={len(man['by_dataset'])}")
    for k, v in sorted(man["by_dataset"].items()):
        print(f"  {k:14s} files={v['files']:4d} rows={v['rows']:>10d} {v['min_first']} .. {v['max_last']}")
    print("->", MANIFEST)
    print("->", COVERAGE)


if __name__ == "__main__":
    main()
