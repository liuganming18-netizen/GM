#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并 OKX 补齐分片（主文件 + p0..p5）为规范单文件，去重并检测值冲突。
输入：补缺_K线_okx补齐_import.csv + 补缺_K线_okx补齐_p*.csv
输出：补缺_K线_okx补齐_import.csv（去重、按时间排序），并打印统计。"""
import csv, collections, json, datetime
from pathlib import Path

OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
COLS = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume', 'source', 'api']

inputs = ['补缺_K线_okx补齐_import.csv'] + sorted(p.name for p in OUT.glob('补缺_K线_okx补齐_p*.csv'))
rows = {}                 # (ex,mkt,sym,tf,ts) -> row
conflicts = []
per_file = {}
for name in inputs:
    p = OUT / name
    if not p.exists():
        continue
    n = 0
    for r in csv.DictReader(p.open(encoding='utf-8-sig')):
        n += 1
        k = (r['exchange'], r['market'], r['symbol'], r['timeframe'], int(r['open_time']))
        val = (r['open'], r['high'], r['low'], r['close'], r['volume'])
        if k in rows and (rows[k]['open'], rows[k]['high'], rows[k]['low'], rows[k]['close'], rows[k]['volume']) != val:
            conflicts.append(dict(key='/'.join(k[:4]), open_time=k[4],
                                  prev=(rows[k]['open'], rows[k]['close']), new=(r['open'], r['close'])))
        rows[k] = r
    per_file[name] = n

tmp = OUT / '补缺_K线_okx补齐_import.csv.tmp'
with tmp.open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=COLS)
    w.writeheader()
    for k in sorted(rows):
        w.writerow({c: rows[k][c] for c in COLS})
tmp.replace(OUT / '补缺_K线_okx补齐_import.csv')

perkey = collections.Counter()
for (ex, mkt, sym, tf, ts) in rows:
    perkey[f'{sym}/{tf}'] += 1
stat = dict(merged_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
            inputs={k: v for k, v in per_file.items()},
            input_rows=sum(per_file.values()), unique_rows=len(rows),
            duplicates_removed=sum(per_file.values()) - len(rows),
            value_conflicts=len(conflicts), conflict_samples=conflicts[:10],
            keys=len(perkey), perkey=dict(perkey))
(OUT / '补缺_OKX补齐合并统计.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
print(json.dumps({k: v for k, v in stat.items() if k != 'perkey'}, ensure_ascii=False, indent=2))
