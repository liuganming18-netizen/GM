#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics 原件内容全扫（只读本地 ZIP，不下载）：逐币多空比字段覆盖/空值/缺口。
输出：metrics原件字段覆盖.csv / metrics原件字段覆盖.json
"""
import csv, glob, io, json, os, sys, time, zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"

RATIO = ["count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
         "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
OI = ["sum_open_interest", "sum_open_interest_value"]
ALL = OI + RATIO


def scan_symbol(sym):
    files = sorted(glob.glob(os.path.join(RAW, sym, "*.zip")))
    n_rows = 0; uniq = set(); empty = Counter(); nonnum = Counter()
    first_ts = None; last_ts = None; dates = []; dup_rows = 0
    for p in files:
        date = os.path.basename(p).replace(f"{sym}-metrics-", "").replace(".zip", "")
        dates.append(date)
        try:
            z = zipfile.ZipFile(p)
            nm = z.namelist()[0]
            txt = z.read(nm).decode("utf-8", "replace")
        except Exception as e:
            empty["__read_error__"] += 1
            continue
        rd = csv.DictReader(io.StringIO(txt))
        local = set()
        for r in rd:
            n_rows += 1
            ts = (r.get("create_time") or "").strip()
            if ts:
                if first_ts is None or ts < first_ts: first_ts = ts
                if last_ts is None or ts > last_ts: last_ts = ts
                if ts in local:
                    dup_rows += 1
                local.add(ts); uniq.add(ts)
            for c in ALL:
                v = (r.get(c) or "").strip()
                if v == "" or v.lower() in ("nan", "null", "none"):
                    empty[c] += 1
                else:
                    try:
                        float(v)
                    except ValueError:
                        nonnum[c] += 1
    return {"symbol": sym, "zip_files": len(files), "date_first": dates[0] if dates else "",
            "date_last": dates[-1] if dates else "", "rows": n_rows,
            "uniq_ts": len(uniq), "dup_rows": dup_rows,
            "first_ts": first_ts, "last_ts": last_ts,
            "empty": dict(empty), "nonnum": dict(nonnum)}


def main():
    syms = sorted(d for d in os.listdir(RAW) if os.path.isdir(os.path.join(RAW, d)))
    print(f"symbols={len(syms)}", flush=True)
    t0 = time.time(); out = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(scan_symbol, s): s for s in syms}
        done = 0
        for f in as_completed(futs):
            out.append(f.result()); done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(syms)}  {time.time()-t0:.0f}s", flush=True)
    out.sort(key=lambda r: r["symbol"])

    fields = ["symbol", "zip_files", "date_first", "date_last", "rows", "uniq_ts", "dup_rows",
              "first_ts", "last_ts"] + [f"empty_{c}" for c in ALL] + [f"nonnum_{c}" for c in ALL]
    with open(os.path.join(OUT, "metrics原件字段覆盖.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(fields)
        for r in out:
            w.writerow([r["symbol"], r["zip_files"], r["date_first"], r["date_last"], r["rows"],
                        r["uniq_ts"], r["dup_rows"], r["first_ts"], r["last_ts"]]
                       + [r["empty"].get(c, 0) for c in ALL]
                       + [r["nonnum"].get(c, 0) for c in ALL])

    tot_rows = sum(r["rows"] for r in out)
    tot_uniq = sum(r["uniq_ts"] for r in out)
    summary = {
        "scanned_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - t0, 1),
        "symbols": len(out), "zip_files": sum(r["zip_files"] for r in out),
        "rows_total": tot_rows, "uniq_ts_total": tot_uniq,
        "dup_rows_total": sum(r["dup_rows"] for r in out),
        "date_min": min(r["date_first"] for r in out if r["date_first"]),
        "date_max": max(r["date_last"] for r in out if r["date_last"]),
        "empty_by_column": {c: sum(r["empty"].get(c, 0) for r in out) for c in ALL},
        "nonnum_by_column": {c: sum(r["nonnum"].get(c, 0) for r in out) for c in ALL},
        "symbols_with_any_empty_ratio": sum(1 for r in out if any(r["empty"].get(c, 0) for c in RATIO)),
        "per_symbol_min_date": min((r for r in out if r["date_first"]), key=lambda r: r["date_first"])["date_first"],
        "symbols_starting_2020_09_01": sum(1 for r in out if r["date_first"] == "2020-09-01"),
    }
    json.dump({"summary": summary, "per_symbol": out},
              open(os.path.join(OUT, "metrics原件字段覆盖.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
