#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 资金费尾段抓取（官方定向 API）

fundingRate 官方 vision 只有月包；2026-04 月包跨界含 Holdout，不能用。
故 [2026-04-01, 2026-04-15) 的资金费改用官方 fapi/v1/fundingRate 定向抓取，
返回含真实 markPrice（结算 mark），不伪造、不补 0。

输出：_dl/funding_api/{sym}.json  {"symbol":..., "rows":[{fundingTime,fundingRate,markPrice,rateType}...], "src":"binance.fapi.fundingRate"}
断点：_dl/_fund_state.json
"""
from __future__ import annotations
import json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import urllib.request, urllib.parse, urllib.error

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
DL = OUT_ROOT / "_dl"
STATE = DL / "_fund_state.json"
POOL = OUT_ROOT / "pool.json"

START = int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)
END   = int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)   # 不含
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


def fetch_symbol(sym):
    rows = []
    cur = START
    for _ in range(20):
        q = urllib.parse.urlencode({"symbol": sym, "startTime": cur, "endTime": END, "limit": 100})
        d = get(f"{API}?{q}")
        if not d:
            break
        for it in d:
            ft = int(it["fundingTime"])
            if ft >= END:
                continue
            rows.append({
                "fundingTime": ft,
                "fundingRate": it.get("fundingRate"),
                "markPrice": it.get("markPrice"),
                "rateType": it.get("rateType"),
            })
        if len(d) < 100:
            break
        nxt = max(int(it["fundingTime"]) for it in d) + 1
        if nxt <= cur:
            break
        cur = nxt
    # 去重按 fundingTime
    seen = {}
    for r in rows:
        seen[r["fundingTime"]] = r
    rows = [seen[k] for k in sorted(seen)]
    dest = DL / "funding_api" / f"{sym}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"symbol": sym, "rows": rows, "src": "binance.fapi.fundingRate"},
                               ensure_ascii=False), encoding="utf-8")
    return len(rows)


def main():
    symbols = json.load(open(POOL))["symbols"]
    if len(sys.argv) > 1 and sys.argv[1]:
        symbols = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    state = json.load(open(STATE)) if STATE.is_file() else {"done": {}}
    todo = [s for s in symbols if s not in state["done"]]
    log(f"symbols={len(symbols)} todo={len(todo)} workers={workers}")
    t0 = time.time()
    ok = err = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_symbol, s): s for s in todo}
        for i, f in enumerate(as_completed(futs), 1):
            s = futs[f]
            try:
                n = f.result()
                state["done"][s] = n; ok += 1
            except Exception as e:
                state.setdefault("err", {})[s] = f"{type(e).__name__}: {e}"; err += 1
                log(f"ERR {s}: {e}")
            if i % 25 == 0:
                STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                log(f"progress {i}/{len(todo)} ok={ok} err={err} elapsed={time.time()-t0:.0f}s")
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    log(f"DONE ok={ok} err={err} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
