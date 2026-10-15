#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用币安官方 API（/futures/data/openInterestHist，与采集器同源同字段）补齐 oi_history 的 5m 内部空档。

背景（本轮实测）：
  · 官方 metrics 日包 2026-10-08 起未发布/未入库；库内 10-08 起的 5m 行全部来自
    `binance.fapi.openInterestHist`（采集器）。
  · 归档行的 create_time 与 API 行的 timestamp 相差一个 5m 桶（实测 288/288 全部错一格），
    故 10-08 之后「按归档口径算出的缺槽」有相当部分是口径错位造成的**假缺口**。
  · 本工具只补**库内自身时间域里的内部空档**（不猜边界、不改口径），行构造完全复刻
    `realtime_collector.oi_bucket_rows`。

只读源库；结果写本目录；不写生产库。
"""
import csv, datetime, json, sqlite3, time, urllib.parse, urllib.request
from pathlib import Path

FIXED_NOW = 1791980404548
DAY = 1791936000000          # 2026-10-14 00:00 UTC
OI_5M_MS = 300000
SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
DB = SAFE / 'data/market_v2.sqlite3'
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
WEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
COLS = ['exchange', 'market', 'symbol', 'period', 'ts', 'open_interest', 'open_interest_value',
        'oi_ccy', 'oi_usd', 'source', 'fetched_at']


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def get(url, tries=6):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(0.8 + 1.2 * i)
    raise last


def main():
    rows = [r for r in csv.DictReader((WEV / 'C_OI缺槽分类复核.csv').open(encoding='utf-8-sig'))
            if r['status'] == 'no_surviving_day_zip']
    syms = sorted({r['symbol'] for r in rows})
    log(f'候选币 {len(syms)}')

    con = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')

    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    fetched_at = now_ms
    out_rows = []
    per = {}
    holes_total = 0
    for i, sym in enumerate(syms, 1):
        have = {r[0] for r in con.execute(
            "SELECT ts FROM oi_history WHERE exchange='binance' AND symbol=? AND period='5m' AND ts>=? AND ts<?",
            (sym, DAY, DAY + 86400000))}
        if len(have) < 2:
            per[sym] = 0
            continue
        lo, hi = min(have), max(have)
        expected = list(range(lo, hi + 1, OI_5M_MS))
        miss = [t for t in expected if t not in have]
        per[sym] = len(miss)
        holes_total += len(miss)
        if not miss:
            continue
        try:
            url = (f'https://fapi.binance.com/futures/data/openInterestHist?symbol={urllib.parse.quote(sym)}'
                   f'&period=5m&startTime={lo}&endTime={hi + OI_5M_MS}&limit=500')
            arr = get(url)
        except Exception as e:
            log(f'  FAIL {sym} {e!r}')
            per[sym] = -1
            continue
        got = {int(x['timestamp']): x for x in arr}
        w = 0
        for t in miss:
            x = got.get(t)
            if not x or t + OI_5M_MS > FIXED_NOW:
                continue
            out_rows.append(dict(exchange='binance', market='um', symbol=sym, period='5m', ts=t,
                                 open_interest=x.get('sumOpenInterest'),
                                 open_interest_value=x.get('sumOpenInterestValue'),
                                 oi_ccy=x.get('sumOpenInterest'), oi_usd=x.get('sumOpenInterestValue'),
                                 source='binance.fapi.openInterestHist', fetched_at=fetched_at))
            w += 1
        per[sym] = w
        if i % 50 == 0 or w:
            log(f'  {i}/{len(syms)} {sym} 空档={len(miss)} 补={w} 累计={len(out_rows)}')
        time.sleep(0.05)
    con.close()

    p = OUT / '补缺_OI_API_import.csv'
    with p.open('w', encoding='utf-8-sig', newline='') as f:
        wr = csv.DictWriter(f, fieldnames=COLS)
        wr.writeheader()
        for r in sorted(out_rows, key=lambda x: (x['symbol'], x['ts'])):
            wr.writerow(r)

    uniq = len({(r['symbol'], r['ts']) for r in out_rows})
    stat = dict(generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                policy='只读源；写本目录；不写生产库',
                source='binance.fapi.openInterestHist',
                day_utc='2026-10-14', candidates=len(syms),
                internal_holes=holes_total, rows=len(out_rows), unique=uniq,
                duplicates=len(out_rows) - uniq,
                symbols_filled=sum(1 for v in per.values() if v > 0),
                note='仅补库内自身时间域的内部空档；行构造复刻 realtime_collector.oi_bucket_rows')
    (OUT / '补缺_OI_API补齐清单.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    log(f'DONE OI-API 补 {len(out_rows)} 行 / {stat["symbols_filled"]} 币（内部空档合计 {holes_total}）')


if __name__ == '__main__':
    main()
