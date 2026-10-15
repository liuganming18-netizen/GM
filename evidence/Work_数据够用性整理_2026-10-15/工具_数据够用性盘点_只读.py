#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据够用性整理 —— 只读盘点（2026-10-15）
- 源库一律 file URI + mode=ro + PRAGMA query_only=ON
- 只读；不写生产库/冷档/补件；结果只写本证据目录
"""
import json, os, sqlite3, sys, csv, time, datetime as dt
from collections import defaultdict
from urllib.parse import quote

DB = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"
ARCH = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet"
RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"
PREV = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14"
CODEX = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14"

MAINLINE = ["1d", "12h", "8h", "4h", "1h", "30m", "15m"]
ALLTF = MAINLINE + ["5m"]

def uri(path):
    return "file://" + quote(os.path.abspath(path)) + "?mode=ro"

def connect():
    con = sqlite3.connect(uri(DB), uri=True)
    con.execute("PRAGMA query_only=ON")
    return con

def utc(ms):
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def main():
    t0 = time.time()
    res = {"generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "db_path": DB, "archive_path": ARCH, "raw_path": RAW}
    con = connect()
    cur = con.cursor()

    # ---- 0. 库文件规模与截点 ----
    wal = DB + "-wal"
    res["db_file_bytes"] = os.path.getsize(DB)
    res["db_wal_bytes"] = os.path.getsize(wal) if os.path.exists(wal) else 0
    res["db_mtime_utc"] = dt.datetime.fromtimestamp(os.path.getmtime(DB), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur.execute("PRAGMA data_version"); res["data_version"] = cur.fetchone()[0]
    cur.execute("SELECT sqlite_version()"); res["sqlite_version"] = cur.fetchone()[0]

    # ---- 1. candle_heads 热层 按 exchange×market×timeframe ----
    hot = {}
    cur.execute("""SELECT exchange,market,timeframe,COUNT(DISTINCT symbol),COUNT(*),
                          MIN(open_time),MAX(open_time)
                   FROM candle_heads GROUP BY exchange,market,timeframe""")
    for ex, mk, tf, nsym, n, mn, mx in cur.fetchall():
        hot[f"{ex}|{mk}|{tf}"] = {"symbols": nsym, "rows": n, "first_utc": utc(mn), "last_utc": utc(mx)}
    res["hot_candle_heads"] = hot

    # ---- 2. funding_history 热层 ----
    cur.execute("""SELECT exchange,COUNT(*),COUNT(DISTINCT symbol),MIN(funding_time),MAX(funding_time),
                          SUM(CASE WHEN mark_price IS NOT NULL AND mark_price<>'' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN funding_rate IS NOT NULL AND funding_rate<>'' THEN 1 ELSE 0 END)
                   FROM funding_history GROUP BY exchange""")
    res["hot_funding"] = {}
    for ex, n, nsym, mn, mx, nmark, nrate in cur.fetchall():
        res["hot_funding"][ex] = {"rows": n, "symbols": nsym, "first_utc": utc(mn), "last_utc": utc(mx),
                                  "mark_price_nonempty": nmark, "funding_rate_nonempty": nrate}
    # 各 exchange 的 rate_type / source 分布
    cur.execute("SELECT exchange,rate_type,source,COUNT(*) FROM funding_history GROUP BY exchange,rate_type,source")
    res["hot_funding_meta"] = [{"exchange": a, "rate_type": b, "source": c, "rows": d} for a, b, c, d in cur.fetchall()]

    # ---- 3. oi_history 热层 按 exchange×market×period ----
    oi = {}
    cur.execute("""SELECT exchange,market,period,COUNT(DISTINCT symbol),COUNT(*),MIN(ts),MAX(ts)
                   FROM oi_history GROUP BY exchange,market,period""")
    for ex, mk, pd_, nsym, n, mn, mx in cur.fetchall():
        oi[f"{ex}|{mk}|{pd_}"] = {"symbols": nsym, "rows": n, "first_utc": utc(mn), "last_utc": utc(mx)}
    res["hot_oi"] = oi
    cur.execute("SELECT exchange,source,period,COUNT(*) FROM oi_history GROUP BY exchange,source,period")
    res["hot_oi_source"] = [{"exchange": a, "source": b, "period": c, "rows": d} for a, b, c, d in cur.fetchall()]

    # ---- 4. 多空比：全库检索 ----
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tabs = [r[0] for r in cur.fetchall()]
    ls_hits = []
    for t in tabs:
        cur.execute(f"PRAGMA table_info('{t}')")
        for c in cur.fetchall():
            cn = c[1].lower()
            if any(k in cn for k in ("long_short", "longshort", "ratio", "toptrader")):
                ls_hits.append({"table": t, "column": c[1]})
    res["long_short_ratio_columns_in_db"] = ls_hits
    res["long_short_ratio_table_exists"] = any("ratio" in h["table"].lower() for h in ls_hits)

    # ---- 5. 实时层 ----
    cur.execute("SELECT exchange,COUNT(*),COUNT(DISTINCT symbol),MAX(updated_at) FROM realtime_klines GROUP BY exchange")
    res["realtime_klines"] = [{"exchange": a, "rows": b, "symbols": c, "max_updated_utc": utc(d)} for a, b, c, d in cur.fetchall()]
    cur.execute("""SELECT exchange,COUNT(*),COUNT(DISTINCT symbol),
                          SUM(CASE WHEN mark_price IS NOT NULL AND mark_price<>'' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN funding_rate IS NOT NULL AND funding_rate<>'' THEN 1 ELSE 0 END),
                          MAX(updated_at) FROM realtime_quotes GROUP BY exchange""")
    res["realtime_quotes"] = [{"exchange": a, "rows": b, "symbols": c, "mark_price_nonempty": d,
                               "funding_rate_nonempty": e, "max_updated_utc": utc(f)} for a, b, c, d, e, f in cur.fetchall()]

    # ---- 6. 标的池 ----
    cur.execute("SELECT exchange,COUNT(*) FROM symbol_pool GROUP BY exchange")
    res["symbol_pool"] = {a: b for a, b in cur.fetchall()}
    cur.execute("SELECT COUNT(*) FROM symbol_classification WHERE in_active_range=1")
    res["active_range_symbols"] = cur.fetchone()[0]
    con.close()

    # ---- 7. 冷档 catalog 聚合 ----
    cat = json.load(open(os.path.join(ARCH, "_catalog.json")))
    res["archive_catalog_updated_at_utc"] = utc(cat.get("updated_at"))
    agg = defaultdict(lambda: {"files": 0, "rows": 0, "bytes": 0})
    for path, meta in cat["shards"].items():
        parts = path.split("/")
        top = parts[0]
        if top in ("candle_heads", "candles") and len(parts) >= 5:
            key = f"{top}|{parts[1]}|{parts[2]}|{parts[3]}"
        elif top in ("funding", "oi") and len(parts) >= 3:
            key = f"{top}|{parts[1]}|{parts[2] if top=='funding' else parts[2]}"
        else:
            key = f"{top}|-|-|{parts[1] if len(parts)>1 else ''}"
        a = agg[key]
        a["files"] += 1; a["rows"] += meta.get("rows", 0); a["bytes"] += meta.get("bytes", 0)
    res["archive_by_key"] = {k: v for k, v in sorted(agg.items())}

    # 冷档 parquet 磁盘实际大小（du）
    res["archive_dir_bytes"] = int(os.popen(f'du -sk "{ARCH}" 2>/dev/null | cut -f1').read().strip() or 0) * 1024

    # ---- 8. Codex 逐币逐周期冷热覆盖 聚合 ----
    cov = defaultdict(lambda: {"symbols": 0, "cold_rows": 0, "hot_rows": 0, "merged": 0,
                               "gap_segments": 0, "missing_slots": 0, "first": None, "last": None})
    per_sym = []
    with open(os.path.join(CODEX, "逐币逐周期冷热覆盖.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["metric"] != "candle_heads":
                continue
            k = f'{r["exchange"]}|{r["period"]}'
            a = cov[k]
            a["symbols"] += 1
            a["cold_rows"] += int(r["cold_rows"]); a["hot_rows"] += int(r["hot_rows"])
            a["merged"] += int(r["merged_unique_timestamps"])
            a["gap_segments"] += int(r["internal_gap_segments"])
            a["missing_slots"] += int(r["missing_grid_slots_candidate"])
            fu, lu = r["first_utc"], r["last_utc"]
            if fu and (a["first"] is None or fu < a["first"]): a["first"] = fu
            if lu and (a["last"] is None or lu > a["last"]): a["last"] = lu
            per_sym.append(r)
    res["codex_coldhot_by_exchange_period"] = {k: v for k, v in sorted(cov.items())}
    res["codex_coldhot_metric_rows"] = len(per_sym)

    # ---- 9. Codex 冷热合并内部缺槽 按 exchange×period 聚合 ----
    gaps = defaultdict(lambda: {"segments": 0, "slots": 0})
    with open(os.path.join(CODEX, "冷热合并内部缺槽.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["metric"] != "candle_heads":
                continue
            k = f'{r["exchange"]}|{r["period"]}'
            gaps[k]["segments"] += 1
            gaps[k]["slots"] += int(r["missing_slots"])
    res["codex_internal_gaps_by_exchange_period"] = {k: v for k, v in sorted(gaps.items())}

    # ---- 10. 补件逐文件聚合 ----
    patch_files = {
        "K线_api": "补缺_K线_api_import.csv",
        "K线_okx补齐": "补缺_K线_okx补齐_import.csv",
        "K线_okx前端": "补缺_K线_okx前端_import.csv",
        "K线_官方日包": "补缺_K线_官方日包_import.csv",
        "K线_local": "补缺_K线_local_import.csv",
    }
    patch = {}
    for label, fn in patch_files.items():
        p = os.path.join(PREV, fn)
        if not os.path.exists(p):
            continue
        d = defaultdict(lambda: {"rows": 0, "symbols": set(), "first": None, "last": None})
        keys = set(); dup = 0; nrows = 0
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                nrows += 1
                k = f'{r["exchange"]}|{r["timeframe"]}'
                a = d[k]
                a["rows"] += 1; a["symbols"].add(r["symbol"])
                t = int(r["open_time"])
                if a["first"] is None or t < a["first"]: a["first"] = t
                if a["last"] is None or t > a["last"]: a["last"] = t
                tk = (r["exchange"], r["market"], r["symbol"], r["timeframe"], r["open_time"])
                if tk in keys: dup += 1
                keys.add(tk)
        patch[label] = {"rows": nrows, "dup": dup, "unique": len(keys), "sha_file": fn,
                        "by_exchange_tf": {k: {"rows": v["rows"], "symbols": len(v["symbols"]),
                                               "first_utc": utc(v["first"]), "last_utc": utc(v["last"])}
                                           for k, v in sorted(d.items())}}
    res["patch_kline"] = patch

    # 资金费补件
    p = os.path.join(PREV, "补缺_资金费_import.csv")
    d = {"rows": 0, "symbols": set(), "keys": set(), "dup": 0, "mark_nonempty": 0, "first": None, "last": None}
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d["rows"] += 1
            d["symbols"].add(r["symbol"])
            if r.get("mark_price", "").strip(): d["mark_nonempty"] += 1
            t = int(r["funding_time"])
            if d["first"] is None or t < d["first"]: d["first"] = t
            if d["last"] is None or t > d["last"]: d["last"] = t
            k = (r["exchange"], r["market"], r["symbol"], r["funding_time"])
            if k in d["keys"]: d["dup"] += 1
            d["keys"].add(k)
    res["patch_funding"] = {"rows": d["rows"], "unique": len(d["keys"]), "dup": d["dup"],
                            "symbols": len(d["symbols"]), "mark_price_nonempty": d["mark_nonempty"],
                            "first_utc": utc(d["first"]), "last_utc": utc(d["last"])}

    # OI 补件
    oip = {}
    for label, fn in {"OI_API": "补缺_OI_API_import.csv",
                      "OI_官方归档": "补缺_OI_1008_1013_官方归档_import.csv",
                      "OI_旧7行作废": "补缺_OI_import.csv"}.items():
        p = os.path.join(PREV, fn)
        if not os.path.exists(p):
            oip[label] = {"exists": False}
            continue
        rows = 0; keys = set(); syms = set(); periods = defaultdict(int); first = None; last = None
        with open(p, encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            cols = set(rd.fieldnames or [])
            tcol = "ts" if "ts" in cols else ("create_time_utc" if "create_time_utc" in cols else None)
            for r in rd:
                rows += 1
                syms.add(r["symbol"]); periods[r.get("period", r.get("timeframe", "?"))] += 1
                if tcol:
                    raw_t = r[tcol]
                    try:
                        t = int(raw_t)
                    except ValueError:
                        s = raw_t.replace("Z", "").strip()
                        dtx = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                        t = int(dtx.timestamp() * 1000)
                    if first is None or t < first: first = t
                    if last is None or t > last: last = t
                    keys.add((r["exchange"], r["market"], r["symbol"], r.get("period"), raw_t))
        oip[label] = {"exists": True, "file": fn, "rows": rows, "unique": len(keys),
                      "symbols": len(syms), "periods": dict(periods),
                      "first_utc": utc(first), "last_utc": utc(last),
                      "bytes": os.path.getsize(p)}
    res["patch_oi"] = oip

    # ---- 11. 原始归档 raw 规模 ----
    raw = {}
    for sub in ("raw", "raw_oi_metrics", "raw_okx"):
        p = os.path.join(RAW, sub)
        if os.path.isdir(p):
            kb = int(os.popen(f'du -sk "{p}" 2>/dev/null | cut -f1').read().strip() or 0) * 1024
            n = int(os.popen(f'find "{p}" -type f 2>/dev/null | wc -l').read().strip() or 0)
            subs = sorted(os.listdir(p))[:3]
            raw[sub] = {"bytes": kb, "files": n, "entries": len(os.listdir(p)), "sample": subs}
    res["raw_archive"] = raw

    # raw_oi_metrics 覆盖日期范围（按文件名）
    import re
    dates = []
    d = os.path.join(RAW, "raw_oi_metrics")
    for sym in os.listdir(d):
        sp = os.path.join(d, sym)
        if not os.path.isdir(sp):
            continue
        for fn in os.listdir(sp):
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.zip$", fn)
            if m: dates.append(m.group(1))
    if dates:
        res["raw_oi_metrics_date_range"] = {"min": min(dates), "max": max(dates), "n_zip": len(dates)}

    with open(os.path.join(OUT, "盘点_原始数据.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print("elapsed", round(time.time() - t0, 1), "s")
    print(json.dumps({k: res[k] for k in ("db_file_bytes", "db_wal_bytes", "data_version",
                                          "long_short_ratio_columns_in_db", "symbol_pool",
                                          "archive_catalog_updated_at_utc", "raw_oi_metrics_date_range")},
                     ensure_ascii=False, indent=1, default=str))

if __name__ == "__main__":
    main()
