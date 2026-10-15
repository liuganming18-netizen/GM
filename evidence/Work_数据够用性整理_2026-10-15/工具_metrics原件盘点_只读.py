#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics 原件（OI + 4 个多空比列）逐币盘点。只读，复用既有 oi_state 断点证据，不下载。"""
import json, os, csv, glob, datetime as dt
from collections import defaultdict

RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"

def d2s(s):  # 'YYYY-MM-DD' -> date
    return dt.date.fromisoformat(s)

def main():
    states = sorted(glob.glob(os.path.join(RAW, "oi_state", "*.json")))
    rows = []
    g = {"files": 0, "raw_rows": 0, "uniq_rows": 0, "dup_same": 0, "conflict_labels": 0,
         "invalid_rows": 0, "imported": 0, "verified": 0}
    imp_dbs = defaultdict(int)
    gap_total = 0
    for sp in states:
        d = json.load(open(sp))
        sym = d.get("symbol")
        s = d.get("summary", {})
        files = d.get("files", {})
        first = s.get("archive_first"); last = s.get("archive_last")
        nfiles = s.get("files_total", len(files))
        # 逐币缺口：first..last 之间的日历日是否缺 zip
        missing = 0
        if first and last:
            have = set(files.keys())
            cur = d2s(first); end = d2s(last)
            while cur <= end:
                if cur.isoformat() not in have:
                    missing += 1
                cur += dt.timedelta(days=1)
        rr = sum(v.get("raw_rows", 0) for v in files.values())
        ur = sum(v.get("uniq_rows", 0) for v in files.values())
        ds = sum(v.get("dup_same", 0) for v in files.values())
        cl = sum(v.get("conflict_labels", 0) for v in files.values())
        iv = sum(v.get("invalid_rows", 0) for v in files.values())
        im = sum(1 for v in files.values() if v.get("imported"))
        vf = sum(1 for v in files.values() if v.get("status") == "verified")
        for v in files.values():
            if v.get("import_db"): imp_dbs[v["import_db"].split("|")[0]] += 1
        g["files"] += nfiles; g["raw_rows"] += rr; g["uniq_rows"] += ur
        g["dup_same"] += ds; g["conflict_labels"] += cl; g["invalid_rows"] += iv
        g["imported"] += im; g["verified"] += vf
        gap_total += missing
        rows.append({
            "symbol": sym, "原件数": nfiles, "已核验": vf, "已导入": im,
            "起": first or "", "止": last or "",
            "raw_rows": rr, "uniq_rows": ur, "重复同值行": ds,
            "标签冲突": cl, "非法行": iv, "区间内缺日": missing,
            "import_db": next((v.get("import_db", "") for v in files.values() if v.get("import_db")), ""),
        })
    rows.sort(key=lambda r: r["symbol"])
    with open(os.path.join(OUT, "metrics原件逐币覆盖.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    firsts = [r["起"] for r in rows if r["起"]]
    lasts = [r["止"] for r in rows if r["止"]]
    summary = {
        "source_state_dir": os.path.join(RAW, "oi_state"),
        "symbols": len(rows),
        "zip_files_total": g["files"], "verified": g["verified"], "imported_flags": g["imported"],
        "raw_rows": g["raw_rows"], "uniq_rows": g["uniq_rows"],
        "dup_same_rows": g["dup_same"], "conflict_labels": g["conflict_labels"],
        "invalid_rows": g["invalid_rows"],
        "date_min": min(firsts) if firsts else None, "date_max": max(lasts) if lasts else None,
        "symbols_with_internal_missing_days": sum(1 for r in rows if r["区间内缺日"] > 0),
        "internal_missing_days_total": gap_total,
        "import_db_paths": dict(imp_dbs),
        "columns_in_original_csv": ["create_time", "symbol", "sum_open_interest",
                                    "sum_open_interest_value", "count_toptrader_long_short_ratio",
                                    "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                                    "sum_taker_long_short_vol_ratio"],
        "importer_takes_only": ["sum_open_interest", "sum_open_interest_value"],
    }
    json.dump(summary, open(os.path.join(OUT, "metrics原件汇总.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
