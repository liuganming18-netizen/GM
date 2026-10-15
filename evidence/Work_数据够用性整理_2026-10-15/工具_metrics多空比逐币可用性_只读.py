#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics 多空比 4 列的**逐币非空连续性**扫描（只读本地 ZIP，不下载）。

对每个币、每一列，统计：
  · 首个非空日期 / 最后非空日期
  · 整日全空日数（该列当日 288 行全为空串）、部分空日数
  · 最长连续「该列完全非空」天数及区间
  · 最长连续「该列至少部分非空」天数及区间
  · 非空行数 / 总行数（非空率）

用于回答「每币覆盖起止 / 空值 / 缺口」，并支撑 M5/M6 的 30 日窗口判断。
输出：metrics多空比逐币可用性.csv / metrics多空比逐币可用性.json
"""
import csv
import datetime as dt
import glob
import io
import json
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor

RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"
COLS = ["count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]


def longest_run(days):
    """days: 已排序的 date 字符串列表，返回 (最长连续天数, 起, 止)"""
    if not days:
        return 0, None, None
    best = cur = 1
    bs = cs = days[0]
    for i in range(1, len(days)):
        if (dt.date.fromisoformat(days[i]) - dt.date.fromisoformat(days[i - 1])).days == 1:
            cur += 1
        else:
            cur, cs = 1, days[i]
        if cur > best:
            best, bs = cur, cs
    be = (dt.date.fromisoformat(bs) + dt.timedelta(days=best - 1)).isoformat()
    return best, bs, be


def scan_symbol(sym):
    files = sorted(glob.glob(os.path.join(RAW, sym, f"{sym}-metrics-*.zip")))
    per = {c: {"full_empty": [], "any_empty": [], "ok": [], "nonempty_rows": 0, "rows": 0}
           for c in COLS}
    for p in files:
        d = os.path.basename(p)[len(sym) + len("-metrics-"):-len(".zip")]
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
        for c in COLS:
            e = sum(1 for r in rows[1:] if r[idx[c]] == "")
            rec = per[c]
            rec["rows"] += n
            rec["nonempty_rows"] += n - e
            if e == n:
                rec["full_empty"].append(d)
            elif e > 0:
                rec["any_empty"].append(d)
            else:
                rec["ok"].append(d)
    out = {"symbol": sym, "files": len(files)}
    for c in COLS:
        rec = per[c]
        r_all, a0, a1 = longest_run(sorted(rec["ok"] + rec["any_empty"]))  # 至少部分非空
        r_ok, o0, o1 = longest_run(sorted(rec["ok"]))                      # 完全非空
        out[c] = {
            "rows": rec["rows"], "nonempty_rows": rec["nonempty_rows"],
            "nonempty_pct": round(rec["nonempty_rows"] / rec["rows"] * 100, 3) if rec["rows"] else None,
            "full_empty_days": len(rec["full_empty"]),
            "partial_empty_days": len(rec["any_empty"]),
            "full_empty_first": rec["full_empty"][0] if rec["full_empty"] else None,
            "full_empty_last": rec["full_empty"][-1] if rec["full_empty"] else None,
            "longest_partial_ok_days": r_all, "longest_partial_ok_from": a0, "longest_partial_ok_to": a1,
            "longest_fully_ok_days": r_ok, "longest_fully_ok_from": o0, "longest_fully_ok_to": o1,
            "first_ok_day": min(rec["ok"] + rec["any_empty"]) if (rec["ok"] or rec["any_empty"]) else None,
            "last_ok_day": max(rec["ok"] + rec["any_empty"]) if (rec["ok"] or rec["any_empty"]) else None,
        }
    return out


def main():
    syms = sorted(d for d in os.listdir(RAW) if os.path.isdir(os.path.join(RAW, d)))
    results = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for r in ex.map(scan_symbol, syms, chunksize=4):
            results.append(r)

    hdr = ["symbol", "files"]
    for c in COLS:
        hdr += [f"{c}__非空率%", f"{c}__非空行", f"{c}__总行",
                f"{c}__整日全空日数", f"{c}__部分空日数",
                f"{c}__全空起", f"{c}__全空止",
                f"{c}__最长连续完全非空天", f"{c}__起", f"{c}__止",
                f"{c}__最长连续至少部分非空天", f"{c}__起", f"{c}__止",
                f"{c}__首非空日", f"{c}__末非空日"]
    with open(os.path.join(OUT, "metrics多空比逐币可用性.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        for r in results:
            row = [r["symbol"], r["files"]]
            for c in COLS:
                d = r[c]
                row += [d["nonempty_pct"], d["nonempty_rows"], d["rows"],
                        d["full_empty_days"], d["partial_empty_days"],
                        d["full_empty_first"], d["full_empty_last"],
                        d["longest_fully_ok_days"], d["longest_fully_ok_from"], d["longest_fully_ok_to"],
                        d["longest_partial_ok_days"], d["longest_partial_ok_from"], d["longest_partial_ok_to"],
                        d["first_ok_day"], d["last_ok_day"]]
            w.writerow(row)

    summ = {"symbols": len(results), "columns": {}}
    for c in COLS:
        pcts = sorted(r[c]["nonempty_pct"] for r in results if r[c]["nonempty_pct"] is not None)
        runs = sorted(r[c]["longest_fully_ok_days"] for r in results)
        summ["columns"][c] = {
            "zero_empty_symbols": sum(1 for r in results if r[c]["full_empty_days"] == 0
                                      and r[c]["partial_empty_days"] == 0),
            "symbols_with_full_empty_days": sum(1 for r in results if r[c]["full_empty_days"] > 0),
            "nonempty_pct_min": pcts[0], "nonempty_pct_median": pcts[len(pcts) // 2], "nonempty_pct_max": pcts[-1],
            "longest_fully_ok_days_median": runs[len(runs) // 2],
            "symbols_longest_fully_ok_ge_30": sum(1 for x in runs if x >= 30),
            "symbols_longest_fully_ok_ge_90": sum(1 for x in runs if x >= 90),
            "symbols_longest_fully_ok_ge_365": sum(1 for x in runs if x >= 365),
        }
    with open(os.path.join(OUT, "metrics多空比逐币可用性.json"), "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
