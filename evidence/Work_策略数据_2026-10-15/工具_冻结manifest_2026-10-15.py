#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结 WyckoffMaster 研究输入 manifest：逐文件 SHA256 + 行数 + 时间范围 + 来源。
输出：数据/研究输入/WyckoffMaster/manifest.json 与 逐币覆盖.csv
只读扫描，不改任何研究输入。
"""
from __future__ import annotations
import csv, hashlib, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")
EV = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")

def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def range_col(path, tcol):
    try:
        t = pq.read_table(str(path), columns=[tcol])
        col = t.column(tcol).to_pylist()
        if not col:
            return None, None, 0
        return min(col), max(col), len(col)
    except Exception:
        return None, None, None

def main():
    t0 = time.time()
    files = []
    coverage = []
    datasets = {
        "instruments.parquet": ("instruments", None),
        "ohlcv/1h": ("ohlcv_1h", "open_time"),
        "ohlcv/4h": ("ohlcv_4h", "open_time"),
        "ohlcv/15m": ("ohlcv_15m", "open_time"),
        "funding": ("funding", "funding_time"),
        "oi_metrics": ("oi_metrics", "ts"),
        "mark1h": ("mark1h", "open_time"),
    }
    for p in sorted(OUT_ROOT.rglob("*.parquet")):
        if "_backup" in p.parts or "_state" in p.parts:
            continue
        rel = p.relative_to(OUT_ROOT).as_posix()
        parts = rel.split("/")
        if rel == "instruments.parquet":
            ds, tcol = datasets["instruments.parquet"]
        else:
            ds, tcol = datasets.get(parts[0], (parts[0], None))
        first = last = rows = None
        if tcol:
            first, last, rows = range_col(p, tcol)
        else:
            rows = pq.ParquetFile(str(p)).metadata.num_rows
        st = p.stat()
        files.append({
            "path": rel, "dataset": ds, "bytes": st.st_size, "rows": rows,
            "first_ts": first, "last_ts": last, "sha256": sha256(p),
        })
        coverage.append({"dataset": ds, "symbol": (parts[-1][:-8] if len(parts) > 1 else "ALL"),
                         "rows": rows, "first_ts": first, "last_ts": last, "bytes": st.st_size})

    # 汇总
    summary = {}
    for f in files:
        s = summary.setdefault(f["dataset"], {"files": 0, "rows": 0, "bytes": 0,
                                              "min_ts": None, "max_ts": None})
        s["files"] += 1
        s["rows"] += (f["rows"] or 0)
        s["bytes"] += f["bytes"]
        if f["first_ts"] is not None:
            s["min_ts"] = f["first_ts"] if s["min_ts"] is None else min(s["min_ts"], f["first_ts"])
        if f["last_ts"] is not None:
            s["max_ts"] = f["last_ts"] if s["max_ts"] is None else max(s["max_ts"], f["last_ts"])
    for k, v in summary.items():
        for kk in ("min_ts", "max_ts"):
            if v[kk] is not None:
                v[kk + "_utc"] = datetime.fromtimestamp(v[kk] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = {
        "dataset": "WyckoffMaster 研究输入",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "out_root": str(OUT_ROOT),
        "n_files": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "summary": summary,
        "files": files,
    }
    (OUT_ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with open(EV / "逐币覆盖.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["dataset", "symbol", "rows", "first_ts", "last_ts", "bytes"])
        w.writeheader()
        w.writerows(sorted(coverage, key=lambda r: (r["dataset"], r["symbol"])))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"files={len(files)} bytes={manifest['total_bytes']} elapsed={time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
