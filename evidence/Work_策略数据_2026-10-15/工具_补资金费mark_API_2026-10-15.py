#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 资金费结算 mark 定向补齐（官方 fapi/v1/fundingRate）

用于两类缺口：
  1) 无 mark1h 归档的符号（如 3 个中文名符号）→ 其全部结算 mark 缺
  2) 前沿缺口：结算时刻早于该币 mark1h 首个可用小时
官方 fundingRate 接口自 2024 起返回真实 markPrice；<2024 返回空串（视为官方缺失，保持 null）。
不补 0、不插值、不用普通成交价冒充。

只对确实缺失的结算事件发起查询；匹配用「最近 API 时刻且 |Δ|<=60s」。
输出：就地更新 funding parquet 的 mark_price 与 source(追加 +fapi)；报告 _state/funding_mark_api_report.json
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request, urllib.parse, urllib.error

import pyarrow as pa
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
REPORT = OUT_ROOT / "_state/funding_mark_api_report.json"
API = "https://fapi.binance.com/fapi/v1/fundingRate"
UA = {"User-Agent": "workbuddy-research/1.0"}


def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def get(url, retries=3):
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 418, 500, 502, 503, 504):
                time.sleep(2.0 * (k + 1)); last = f"HTTP {e.code}"; continue
            raise
        except Exception as e:
            time.sleep(1.0 * (k + 1)); last = f"{type(e).__name__}: {e}"
    raise RuntimeError(last)


def fetch_range(sym, t0, t1):
    q = urllib.parse.quote(sym, safe="")
    rows = []
    cur = t0
    for _ in range(60):
        u = f"{API}?symbol={q}&startTime={cur}&endTime={t1}&limit=100"
        d = get(u)
        if not d:
            break
        rows += d
        if len(d) < 100:
            break
        nxt = max(int(x["fundingTime"]) for x in d) + 1
        if nxt <= cur:
            break
        cur = nxt
    return rows


def main():
    wins = [w for w in (sys.argv[1].split(",") if len(sys.argv) > 1 else ["is", "val"]) if w]
    rep = {}
    for win in wins:
        d = OUT_ROOT / win / "funding"
        if not d.is_dir():
            continue
        files = sorted(d.glob("*.parquet"))
        wr = {"symbols_scanned": len(files), "symbols_fixed": 0, "filled": 0,
              "still_missing": 0, "api_no_mark": 0, "err": {}}
        for i, f in enumerate(files, 1):
            t = pq.read_table(str(f)).to_pydict()
            ft = t["funding_time"]
            miss = [j for j, x in enumerate(t["mark_price"]) if x is None]
            if not miss:
                continue
            mt = [ft[j] for j in miss]
            t0 = min(mt) - 3600000
            t1 = max(mt) + 3600000
            try:
                api = fetch_range(f.stem, t0, t1)
            except Exception as e:
                wr["err"][f.stem] = f"{type(e).__name__}: {e}"
                continue
            # api 时刻 → mark
            amap = {}
            for r in api:
                mp = r.get("markPrice")
                if mp not in (None, ""):
                    amap[int(r["fundingTime"])] = float(mp)
            ak = sorted(amap)
            nfill = 0
            for j in miss:
                lt = ft[j]
                # 最近 API 时刻（|Δ|<=60s）
                best = None; bd = 61000
                import bisect
                p = bisect.bisect_left(ak, lt)
                for cand in (ak[p-1] if p > 0 else None, ak[p] if p < len(ak) else None):
                    if cand is None:
                        continue
                    dd = abs(cand - lt)
                    if dd < bd:
                        bd = dd; best = cand
                if best is not None:
                    t["mark_price"][j] = amap[best]
                    t["source"][j] = (t["source"][j] or "") + "+fapi"
                    nfill += 1
                else:
                    wr["api_no_mark"] += 1
            if nfill:
                tbl = pa.table({
                    "funding_time": pa.array(ft, pa.int64()),
                    "funding_rate": pa.array(t["funding_rate"], pa.float64()),
                    "mark_price": pa.array(t["mark_price"], pa.float64()),
                    "rate_type": pa.array(t["rate_type"], pa.string()),
                    "source": pa.array(t["source"], pa.string()),
                })
                pq.write_table(tbl, str(f), compression="zstd")
                wr["symbols_fixed"] += 1
                wr["filled"] += nfill
            wr["still_missing"] += len(miss) - nfill
            if i % 40 == 0:
                log(f"{win} {i}/{len(files)} filled={wr['filled']} still={wr['still_missing']}")
        rep[win] = wr
        log(f"{win} DONE fixed={wr['symbols_fixed']} filled={wr['filled']} still_missing={wr['still_missing']} api_no_mark={wr['api_no_mark']}")
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    log("report -> " + str(REPORT))


if __name__ == "__main__":
    main()
