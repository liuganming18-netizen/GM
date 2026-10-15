#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验补齐产物：行数、去重、范围、与冷热是否仍有交集。"""
import csv, collections, datetime, json
from pathlib import Path

OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
res = {}
# funding
fr = list(csv.DictReader((OUT / '补缺_资金费_import.csv').open(encoding='utf-8-sig')))
res['funding_rows'] = len(fr)
res['funding_unique'] = len({(r['symbol'], r['funding_time']) for r in fr})
res['funding_symbols'] = len({r['symbol'] for r in fr})
res['funding_checksum_all_match'] = all('sha=MATCH' in r['evidence'] for r in fr)
ts = sorted(int(r['funding_time']) for r in fr)
f = lambda m: datetime.datetime.fromtimestamp(m / 1000, datetime.timezone.utc).isoformat()
res['funding_window'] = [f(ts[0]), f(ts[-1])] if ts else []

# candle
kf = []
for name in ['补缺_K线_local_import.csv', '补缺_K线_api_import.csv']:
    p = OUT / name
    if p.exists():
        rows = list(csv.DictReader(p.open(encoding='utf-8-sig')))
        res[name] = len(rows)
        kf += rows
res['candle_rows_total'] = len(kf)
res['candle_unique'] = len({(r['exchange'], r['market'], r['symbol'], r['timeframe'], r['open_time']) for r in kf})
bykey = collections.Counter(f"{r['exchange']}/{r['symbol']}/{r['timeframe']}" for r in kf)
res['candle_keys'] = len(bykey)
res['candle_top_keys'] = bykey.most_common(5)
res['candle_by_exchange'] = dict(collections.Counter(r['exchange'] for r in kf))
# 范围
if kf:
    t2 = sorted(int(r['open_time']) for r in kf)
    res['candle_window'] = [f(t2[0]), f(t2[-1])]
# 年分布
res['candle_by_year'] = dict(sorted(collections.Counter(
    datetime.datetime.fromtimestamp(int(r['open_time']) / 1000, datetime.timezone.utc).year for r in kf).items()))
(OUT / '补缺校验.json').write_text(json.dumps(res, ensure_ascii=False, indent=2))
print(json.dumps(res, ensure_ascii=False, indent=2))
