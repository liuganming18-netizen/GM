#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定向用币安官方 API 补 funding 结算 mark（仅补缺，不重下全池）。

依据：GET /fapi/v1/fundingRate 返回 fundingTime/fundingRate/markPrice。
实测 markPrice 仅 ≥2024 起有值（2022/2023 返回空串），故只对可补区间补齐；
不足部分如实记为仍缺（官方不可得）。
"""
from __future__ import annotations
import json, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")
EV = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"

def api(sym, st, et, limit=1000, retry=4):
    url = f"{BASE}?symbol={urllib.parse.quote(sym)}&startTime={st}&endTime={et}&limit={limit}"
    for k in range(retry):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if k == retry - 1:
                raise
            time.sleep(1.5 * (k + 1))
    return []

def fetch_range(sym, st, et):
    """分页取 [st,et] 全部结算记录。"""
    out = {}
    cur = st
    while cur <= et:
        d = api(sym, cur, et)
        if not d:
            break
        for x in d:
            ft = int(x["fundingTime"]); mp = x.get("markPrice")
            out[ft] = float(mp) if mp not in (None, "") else None
        nxt = max(int(x["fundingTime"]) for x in d) + 1
        if nxt <= cur:
            break
        cur = nxt
        if len(d) < 1000:
            break
    return out

def main():
    t0 = time.time()
    files = sorted((OUT_ROOT / "funding").glob("*.parquet"))
    filled_total = 0
    api_calls = 0
    still = {}
    for fp in files:
        sym = fp.stem
        t = pq.read_table(str(fp)).to_pydict()
        ft = t["funding_time"]; mp = list(t["mark_price"])
        miss = [ft[i] for i, v in enumerate(mp) if v is None]
        if not miss:
            continue
        # 实测 markPrice 仅 ≥2024 有值 → 只查 2024 起的缺失区间，避免无谓全history分页
        st = max(min(miss), int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))
        et = max(miss)
        if st > et:
            still[sym] = {"missing": len(miss), "note": "缺失均 <2024，API 无 markPrice"}
            continue
        try:
            got = fetch_range(sym, st, et)
            api_calls += 1
        except Exception as e:
            still[sym] = {"missing": len(miss), "err": str(e)}
            continue
        # 按最近匹配（±5s）填
        keys = sorted(got)
        def nearest(ms):
            import bisect
            i = bisect.bisect_left(keys, ms)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(keys) and abs(keys[j] - ms) <= 5000 and got[keys[j]] is not None:
                    if best is None or abs(keys[j] - ms) < abs(best - ms):
                        best = keys[j]
            return got.get(best) if best is not None else None
        f = 0
        filled_idx = set()
        for i, v in enumerate(mp):
            if v is None:
                mv = nearest(ft[i])
                if mv is not None:
                    mp[i] = mv
                    filled_idx.add(i)
                    f += 1
        filled_total += f
        rem = sum(1 for v in mp if v is None)
        if rem:
            still[sym] = {"missing": rem}
        if f:
            srcs = list(t["source"])
            for i in filled_idx:
                if "fapi" not in srcs[i]:
                    srcs[i] = srcs[i] + "+fapi"
            tbl = pa.table({
                "funding_time": pa.array(ft, pa.int64()),
                "funding_rate": pa.array(t["funding_rate"], pa.float64()),
                "mark_price": pa.array(mp, pa.float64()),
                "rate_type": pa.array(t["rate_type"], pa.string()),
                "source": pa.array(srcs, pa.string()),
            })
            pq.write_table(tbl, str(fp), compression="zstd")
        time.sleep(0.05)
    rep = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "api_filled_mark_total": filled_total,
           "symbols_queried": api_calls,
           "symbols_still_missing": still,
           "n_symbols_still_missing": len(still),
           "total_still_missing": sum(v.get("missing", 0) for v in still.values()),
           "elapsed_s": round(time.time() - t0, 1)}
    (EV / "API补结算mark报告.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("api_filled_mark_total", "symbols_queried",
                                          "n_symbols_still_missing", "total_still_missing", "elapsed_s")},
                     ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
