#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冷档 parquet 有界抽样：funding.mark_price 填充率、oi.period 分布、candle_heads 时间范围。只读。"""
import json, os, random, sys
import pyarrow.parquet as pq

ARCH = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"
random.seed(20261015)

def main():
    cat = json.load(open(os.path.join(ARCH, "_catalog.json")))
    paths = list(cat["shards"].keys())
    res = {}

    # --- funding mark_price 填充率（抽样 60 币，只读 mark_price / funding_time 列）---
    fp = sorted(p for p in paths if p.startswith("funding/"))
    sample = random.sample(fp, min(60, len(fp)))
    tot = filled = 0
    mn = None; mx = None
    per = []
    for p in sample:
        t = pq.read_table(os.path.join(ARCH, p), columns=["mark_price", "funding_time"])
        mp = t.column("mark_price").to_pylist()
        ft = t.column("funding_time").to_pylist()
        n = len(mp)
        f = sum(1 for x in mp if x is not None and str(x).strip() != "")
        tot += n; filled += f
        for x in ft:
            if mn is None or x < mn: mn = x
            if mx is None or x > mx: mx = x
        per.append({"file": p, "rows": n, "mark_filled": f})
    res["funding_mark_sample"] = {"files_sampled": len(sample), "total_files": len(fp),
                                  "rows": tot, "mark_filled": filled,
                                  "fill_rate": round(filled / tot, 4) if tot else None,
                                  "funding_time_min": mn, "funding_time_max": mx,
                                  "per_file": per[:10]}

    # --- oi period 分布（抽样 40 个 shard）---
    op = sorted(p for p in paths if p.startswith("oi/"))
    sample = random.sample(op, min(40, len(op)))
    import collections
    pc = collections.Counter(); src = collections.Counter(); rows = 0
    tmin = None; tmax = None
    for p in sample:
        t = pq.read_table(os.path.join(ARCH, p), columns=["period", "ts", "source"])
        pc.update(t.column("period").to_pylist())
        src.update(t.column("source").to_pylist())
        ts = t.column("ts").to_pylist()
        rows += len(ts)
        for x in ts:
            if tmin is None or x < tmin: tmin = x
            if tmax is None or x > tmax: tmax = x
    res["oi_cold_sample"] = {"files_sampled": len(sample), "total_files": len(op), "rows": rows,
                             "period_counts": dict(pc), "source_counts": dict(src),
                             "ts_min": tmin, "ts_max": tmax}

    # --- candle_heads 覆盖：抽 3 个主流币全周期 ---
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        d = {}
        for tf in ("1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"):
            sub = [p for p in paths if p.startswith(f"candle_heads/binance/um/{sym}/{tf}/")]
            if not sub:
                d[tf] = None; continue
            tmn = None; tmx = None; n = 0
            for p in sorted(sub):
                t = pq.read_table(os.path.join(ARCH, p), columns=["open_time"])
                col = t.column("open_time").to_pylist()
                n += len(col)
                if col:
                    if tmn is None or min(col) < tmn: tmn = min(col)
                    if tmx is None or max(col) > tmx: tmx = max(col)
            d[tf] = {"rows": n, "first": tmn, "last": tmx, "files": len(sub)}
        res[f"candle_{sym}"] = d

    with open(os.path.join(OUT, "盘点_冷档抽样.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:4000])

if __name__ == "__main__":
    main()
