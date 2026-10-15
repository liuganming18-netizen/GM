#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位 metrics 多空比 4 列的「日历级缺口窗口」（只读本地 ZIP，不下载）。

对 2021-12-25 ~ 2023-01-05 窗口内、每个日期，统计：
  · 有多少个币在该日**整日全空**（该列 288 行全为空串）
  · 有多少个币**部分空**
  · 有多少个币该日**完整**
用于判断这些空值是「币安侧全局缺口」还是「逐币个别缺口」。

输出：metrics多空比缺口日历.csv / metrics多空比缺口窗口.json
"""
import csv
import glob
import io
import json
import os
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"
COLS = ["count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
D0, D1 = "2021-12-25", "2023-01-05"


def scan_symbol(sym):
    out = []
    for p in glob.glob(os.path.join(RAW, sym, f"{sym}-metrics-*.zip")):
        d = os.path.basename(p)[len(sym) + len("-metrics-"):-len(".zip")]
        if not (D0 <= d <= D1):
            continue
        try:
            z = zipfile.ZipFile(p)
            rows = list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))))
        except Exception:
            continue
        if len(rows) < 2:
            continue
        hdr = rows[0]
        try:
            idx = {c: hdr.index(c) for c in COLS}
        except ValueError:
            continue
        n = len(rows) - 1
        rec = [d, sym, n]
        for c in COLS:
            e = sum(1 for r in rows[1:] if r[idx[c]] == "")
            rec.append(e)
        out.append(rec)
    return out


syms = sorted(d for d in os.listdir(RAW) if os.path.isdir(os.path.join(RAW, d)))


def main():
    allrows = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for res in ex.map(scan_symbol, syms, chunksize=4):
            allrows.extend(res)

    byday = defaultdict(lambda: {"n": 0, "full": [0] * 4, "part": [0] * 4})
    for d, sym, n, *emp in allrows:
        b = byday[d]
        b["n"] += 1
        for i, e in enumerate(emp):
            if e == n:
                b["full"][i] += 1
            elif e > 0:
                b["part"][i] += 1

    with open(os.path.join(OUT, "metrics多空比缺口日历.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "该日有原件的币数"]
                   + [f"整日全空_{c}" for c in COLS] + [f"部分空_{c}" for c in COLS])
        for d in sorted(byday):
            b = byday[d]
            w.writerow([d, b["n"]] + b["full"] + b["part"])

    summary = {"window": [D0, D1], "symbols_scanned": len(syms), "day_records": len(allrows)}
    for i, c in enumerate(COLS):
        days = [d for d in sorted(byday) if byday[d]["full"][i] > 0]
        summ = [d for d in sorted(byday) if byday[d]["n"] > 0 and byday[d]["full"][i] == byday[d]["n"]]
        summary[c] = {"days_with_any_full_empty": len(days),
                      "days_where_ALL_symbols_empty": len(summ),
                      "first_day_with_full_empty": days[0] if days else None,
                      "last_day_with_full_empty": days[-1] if days else None,
                      "all_symbols_empty_window": [summ[0], summ[-1]] if summ else None}
    with open(os.path.join(OUT, "metrics多空比缺口窗口.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
