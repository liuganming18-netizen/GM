#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上市后至首条数据 408 项：逐项用官方API核对该区间是否真有数据。
有则记为可补真实缺口，无则记官方缺失（附证据）。只读。"""
import csv, json, time, urllib.request, datetime
from pathlib import Path

CEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
OUT.mkdir(parents=True, exist_ok=True)
OKX_BAR = {'15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '12h': '12Hutc', '1d': '1Dutc', '8h': None}


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                return {'_error': repr(e)}
            time.sleep(1.0 * (i + 1))


def first_binance(sym, tf, start_ms):
    d = get(f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={tf}&startTime={start_ms}&limit=5')
    if isinstance(d, dict):
        return None, d.get('_error')
    return (int(d[0][0]) if d else None), None


def first_okx(inst, tf, start_ms):
    bar = OKX_BAR.get(tf)
    if not bar:
        return None, 'okx_bar_unsupported'
    d = get(f'https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar={bar}&after={start_ms + 60000}&limit=100')
    rows = d.get('data', []) if isinstance(d, dict) else []
    ts = [int(x[0]) for x in rows]
    return (min(ts) if ts else None), None


rows = list(csv.DictReader((CEV / '上市至首条数据前端缺口候选.csv').open(encoding='utf-8-sig')))
print('408 rows', len(rows), flush=True)
out = []
for i, r in enumerate(rows, 1):
    ex, sym, tf = r['exchange'], r['symbol'], r['timeframe']
    listing = int(datetime.datetime.fromisoformat(r['listing_utc']).timestamp() * 1000)
    actual_first = int(datetime.datetime.fromisoformat(r['actual_first_utc']).timestamp() * 1000)
    if ex == 'binance':
        got, err = first_binance(sym, tf, listing)
    else:
        got, err = first_okx(sym, tf, listing)
    if err:
        verdict, note = 'api_error', err
    elif got is None:
        verdict, note = 'official_absent', 'API该区间无数据'
    elif got < actual_first:
        verdict, note = 'fillable_real_gap', f'API最早 {iso(got)} 早于首条 {r["actual_first_utc"]}'
    else:
        verdict, note = 'official_absent', f'API最早 {iso(got)} 不早于首条'
    out.append(dict(exchange=ex, symbol=sym, timeframe=tf, listing_utc=r['listing_utc'],
                    actual_first_utc=r['actual_first_utc'], theoretical_slots=r['theoretical_early_slots'],
                    api_first_utc=iso(got) if got else '', verdict=verdict, note=note))
    if i % 40 == 0:
        print(f'  {i}/{len(rows)}', flush=True)

with (OUT / '校验_上市后前端408.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
import collections
summ = collections.Counter(x['verdict'] for x in out)
(OUT / '校验_上市后前端408汇总.json').write_text(json.dumps(dict(counts=dict(summ), total=len(out)), ensure_ascii=False, indent=2))
print('DONE', dict(summ), flush=True)
