#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按结算时刻从 mark1h 官方归档补齐 funding.mark_price。

规则：funding_time t 的结算 mark = mark1h[t].mark_open（该 1h 桶起始的 mark）。
已有 mark（热库 2026-09-02~2026-10-14）保留；无 mark1h 的币保留 null 并记入缺项。
原地重写 funding/{symbol}.parquet（仅在本窗研究输入目录内）。
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")
EV = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")

def main():
    t0 = time.time()
    filled_total = 0
    still_null = {}
    no_mark_sym = []
    for fp in sorted((OUT_ROOT / "funding").glob("*.parquet")):
        sym = fp.stem
        mk = OUT_ROOT / "mark1h" / f"{sym}.parquet"
        t = pq.read_table(str(fp)).to_pydict()
        ft = t["funding_time"]; mp = list(t["mark_price"])
        n_null = sum(1 for v in mp if v is None)
        if n_null == 0:
            continue
        if not mk.is_file():
            no_mark_sym.append(sym)
            still_null[sym] = n_null
            continue
        m = pq.read_table(str(mk)).to_pydict()
        mmap = dict(zip(m["open_time"], m["mark_open"]))
        filled = 0
        filled_idx = set()
        for i, v in enumerate(mp):
            if v is None:
                # funding_time 存在 ≤4ms 毫秒抖动 → 取最近整点（结算事件在 8h 网格上）
                key = int(round(ft[i] / 3600000.0)) * 3600000
                mv = mmap.get(key)
                if mv is None:
                    mv = mmap.get(ft[i])
                if mv is not None:
                    mp[i] = mv
                    filled_idx.add(i)
                    filled += 1
        filled_total += filled
        rem = sum(1 for v in mp if v is None)
        if rem:
            still_null[sym] = rem
        if filled:
            srcs = list(t["source"])
            for i in filled_idx:
                if "mark1h" not in srcs[i]:
                    srcs[i] = srcs[i] + "+mark1h"
            tbl = pa.table({
                "funding_time": pa.array(ft, pa.int64()),
                "funding_rate": pa.array(t["funding_rate"], pa.float64()),
                "mark_price": pa.array(mp, pa.float64()),
                "rate_type": pa.array(t["rate_type"], pa.string()),
                "source": pa.array(srcs, pa.string()),
            })
            pq.write_table(tbl, str(fp), compression="zstd")
    rep = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "filled_mark_total": filled_total,
           "symbols_without_mark1h": no_mark_sym,
           "symbols_still_missing_mark": still_null,
           "elapsed_s": round(time.time() - t0, 1)}
    (EV / "结算mark补齐报告.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"filled_mark_total": filled_total,
                      "n_symbols_without_mark1h": len(no_mark_sym),
                      "n_symbols_still_missing": len(still_null),
                      "elapsed_s": rep["elapsed_s"]}, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
