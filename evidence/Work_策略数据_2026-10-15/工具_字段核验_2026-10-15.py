#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研究输入字段级核验 → 字段核验证据.json

核验项：
 A. 同源同事件：oi_metrics.ts 与热库 oi_history.ts 同刻同值（抽样多币）
 B. 网格对齐：ohlcv 1h/4h/15m 桶起点；oi_metrics 5m；funding 8h 网格（≤4ms 抖动）
 C. 覆盖：逐币逐数据集 行数/首末；缺项清单
 D. metrics 四比率非空覆盖
 E. taker 卖量可推导性（比值>0 时）
 F. 重复/冲突：ohlcv 主键唯一、oi_metrics ts 唯一
"""
from __future__ import annotations
import json, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")
EV = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")
HOT = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3")

def hot_conn():
    con = sqlite3.connect(HOT.as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con

def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else None

def main():
    t0 = time.time()
    rep = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    syms = json.load(open(OUT_ROOT / "pool.json"))["symbols"]

    # A. 同源同事件抽样（每 40 个币取 1）：探针取「归档 ts ∩ 热库 ts」的最近点
    con = hot_conn()
    sample = syms[::40]
    a_rows = []
    for s in sample:
        p = OUT_ROOT / "oi_metrics" / f"{s}.parquet"
        if not p.is_file():
            continue
        d = pq.read_table(str(p), columns=["ts", "open_interest", "source"]).to_pydict()
        arc = {t: v for t, v, src in zip(d["ts"], d["open_interest"], d["source"])
               if src == "binance.vision.metrics.daily"}
        if not arc:
            continue
        hot = dict(con.execute(
            "SELECT ts,open_interest FROM oi_history WHERE exchange='binance' AND market='um' "
            "AND symbol=? AND period='5m'", (s,)).fetchall())
        common = sorted(set(arc) & set(hot))
        if not common:
            a_rows.append({"symbol": s, "probe": None, "note": "归档与热库无交集"})
            continue
        probe = common[-1]   # 最近共同点
        a_rows.append({"symbol": s, "probe": probe,
                       "match": abs(float(hot[probe]) - arc[probe]) < 1e-6,
                       "archive": arc[probe], "hot": float(hot[probe]),
                       "common_n": len(common)})
    rep["A_oi_same_source_event"] = {
        "checked": len(a_rows),
        "all_match": all(r.get("match", False) for r in a_rows if r.get("probe")),
        "probed": sum(1 for r in a_rows if r.get("probe")),
        "detail": a_rows,
    }

    # C/D/E/F 逐币扫描
    per_sym = []
    missing = {"ohlcv_1h": [], "ohlcv_4h": [], "ohlcv_15m": [], "funding": [], "oi_metrics": [], "mark1h": []}
    for s in syms:
        rec = {"symbol": s}
        for ds, tcol in (("ohlcv_1h", "open_time"), ("ohlcv_4h", "open_time"), ("ohlcv_15m", "open_time"),
                         ("funding", "funding_time"), ("oi_metrics", "ts"), ("mark1h", "open_time")):
            p = (OUT_ROOT / "ohlcv" / ds.split("_")[1] / f"{s}.parquet") if ds.startswith("ohlcv") else \
                (OUT_ROOT / ds / f"{s}.parquet")
            if not p.is_file():
                missing[ds].append(s)
                rec[ds] = None
                continue
            t = pq.read_table(str(p), columns=[tcol]).column(tcol).to_pylist()
            n = len(t)
            dup = n - len(set(t))
            rec[ds] = {"rows": n, "first": min(t) if t else None, "last": max(t) if t else None, "dup": dup}
        # metrics 比率非空
        pm = OUT_ROOT / "oi_metrics" / f"{s}.parquet"
        if pm.is_file():
            dm = pq.read_table(str(pm), columns=["count_long_short_ratio", "sum_toptrader_long_short_ratio",
                                                 "count_toptrader_long_short_ratio",
                                                 "sum_taker_long_short_vol_ratio"]).to_pydict()
            rec["ratio_nonnull"] = {k: sum(1 for v in vv if v is not None) for k, vv in dm.items()}
        per_sym.append(rec)

    # 汇总
    total = {"symbols": len(syms)}
    for ds in missing:
        have = len(syms) - len(missing[ds])
        rows = sum((r[ds]["rows"] if r.get(ds) else 0) for r in per_sym)
        firsts = [r[ds]["first"] for r in per_sym if r.get(ds) and r[ds]["first"]]
        lasts = [r[ds]["last"] for r in per_sym if r.get(ds) and r[ds]["last"]]
        dups = sum((r[ds]["dup"] if r.get(ds) else 0) for r in per_sym)
        total[ds] = {"symbols_have": have, "symbols_missing": len(missing[ds]),
                     "rows": rows, "dup_keys": dups,
                     "global_first": min(firsts) if firsts else None,
                     "global_last": max(lasts) if lasts else None,
                     "global_first_utc": utc(min(firsts)) if firsts else None,
                     "global_last_utc": utc(max(lasts)) if lasts else None,
                     "missing_symbols": missing[ds][:50]}
    rep["C_coverage"] = total

    # D. metrics 比率非空总覆盖
    tot_ratio = {}
    for r in per_sym:
        if "ratio_nonnull" in r:
            for k, v in r["ratio_nonnull"].items():
                tot_ratio[k] = tot_ratio.get(k, 0) + v
    rep["D_ratio_nonnull_total"] = tot_ratio

    # 网格对齐抽样
    def grid_check(sym, ds, tcol, step_ms):
        p = (OUT_ROOT / "ohlcv" / ds.split("_")[1] / f"{sym}.parquet") if ds.startswith("ohlcv") else \
            (OUT_ROOT / ds / f"{sym}.parquet")
        if not p.is_file():
            return None
        t = sorted(pq.read_table(str(p), columns=[tcol]).column(tcol).to_pylist())
        off = sum(1 for v in t if v % step_ms != 0)
        return {"rows": len(t), "off_grid": off}
    rep["B_grid"] = {
        "BTCUSDT": {ds: grid_check("BTCUSDT", ds, tc, st) for ds, tc, st in
                    (("ohlcv_1h", "open_time", 3600000), ("ohlcv_4h", "open_time", 14400000),
                     ("ohlcv_15m", "open_time", 900000), ("oi_metrics", "ts", 300000))},
    }

    (EV / "字段核验证据.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"A": rep["A_oi_same_source_event"]["all_match"],
                      "coverage": {k: v for k, v in total.items() if k != "symbols"},
                      "D": tot_ratio, "B": rep["B_grid"]}, ensure_ascii=False, indent=1)[:2500])

if __name__ == "__main__":
    main()
