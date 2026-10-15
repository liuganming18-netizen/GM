#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics 原件（多空比/OI 衍生字段）逐币「日覆盖连续性」只读盘点。

只做文件名级统计（不读 ZIP 内容），用于回答：
  - 每个币的原件日覆盖起止、缺日、最长连续日数
  - 有多少币满足 M5/M6 的 30 天窗口、M4 的 90 天窗口
输出：metrics原件逐币连续性.csv / metrics原件连续性汇总.json
"""
import csv
import datetime as dt
import glob
import json
import os
from collections import Counter

RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"

syms = sorted(d for d in os.listdir(RAW) if os.path.isdir(os.path.join(RAW, d)))
rows = []
for s in syms:
    days = set()
    for p in glob.glob(os.path.join(RAW, s, f"{s}-metrics-*.zip")):
        base = os.path.basename(p)
        d = base[len(s) + len("-metrics-"):-len(".zip")]
        days.add(d)
    if not days:
        rows.append({"symbol": s, "原件数": 0})
        continue
    ds = sorted(days)
    d0 = dt.date.fromisoformat(ds[0])
    d1 = dt.date.fromisoformat(ds[-1])
    span = (d1 - d0).days + 1
    # 最长连续日
    best = cur = 1
    best_start = cur_start = ds[0]
    for i in range(1, len(ds)):
        if (dt.date.fromisoformat(ds[i]) - dt.date.fromisoformat(ds[i - 1])).days == 1:
            cur += 1
        else:
            cur = 1
            cur_start = ds[i]
        if cur > best:
            best, best_start = cur, cur_start
    best_end = (dt.date.fromisoformat(best_start) + dt.timedelta(days=best - 1)).isoformat()
    rows.append({"symbol": s, "原件数": len(ds), "起": ds[0], "止": ds[-1],
                 "跨天数": span, "缺日": span - len(ds),
                 "最长连续天": best, "最长连续起": best_start, "最长连续止": best_end})

with open(os.path.join(OUT, "metrics原件逐币连续性.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["symbol", "原件数", "起", "止", "跨天数", "缺日",
                                      "最长连续天", "最长连续起", "最长连续止"])
    w.writeheader()
    w.writerows(rows)

ok = [r for r in rows if r.get("原件数")]
summary = {
    "source": RAW,
    "symbols": len(rows),
    "symbols_with_files": len(ok),
    "start_year_hist": dict(sorted(Counter(r["起"][:4] for r in ok).items())),
    "end_date_hist": dict(sorted(Counter(r["止"] for r in ok).items())),
    "span_days": {"min": min(r["跨天数"] for r in ok),
                  "median": sorted(r["跨天数"] for r in ok)[len(ok) // 2],
                  "max": max(r["跨天数"] for r in ok)},
    "files_per_symbol": {"min": min(r["原件数"] for r in ok),
                         "median": sorted(r["原件数"] for r in ok)[len(ok) // 2],
                         "max": max(r["原件数"] for r in ok)},
    "missing_days_total": sum(r["缺日"] for r in ok),
    "symbols_missing_zero": sum(1 for r in ok if r["缺日"] == 0),
    "symbols_missing_gt0": sum(1 for r in ok if r["缺日"] > 0),
    "meet_30d_window": sum(1 for r in ok if r["最长连续天"] >= 30),
    "meet_90d_window": sum(1 for r in ok if r["最长连续天"] >= 90),
    "meet_365d_window": sum(1 for r in ok if r["最长连续天"] >= 365),
    "worst_missing_top10": [
        {"symbol": r["symbol"], "起": r["起"], "止": r["止"], "缺日": r["缺日"],
         "最长连续天": r["最长连续天"]}
        for r in sorted(ok, key=lambda x: -x["缺日"])[:10]],
}
with open(os.path.join(OUT, "metrics原件连续性汇总.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1))
