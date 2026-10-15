#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集合覆盖校验：证明补件是否覆盖原缺口时间键，并与现冷热做交集/重复/冲突核对。
只读；不修改任何补件或生产数据。"""
import collections, csv, datetime, json, sqlite3, sys
from pathlib import Path
import pyarrow.parquet as pq

FIXED_NOW = 1791980404548
SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
CEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
WEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


# 补件（K线）
patch = collections.defaultdict(set)
for name in ['补缺_K线_local_import.csv', '补缺_K线_api_import.csv']:
    p = OUT / name
    if p.exists():
        for r in csv.DictReader(p.open(encoding='utf-8-sig')):
            patch[(r['exchange'], r['market'], r['symbol'], r['timeframe'])].add(int(r['open_time']))
patch_dup = {}
for name in ['补缺_K线_local_import.csv', '补缺_K线_api_import.csv']:
    p = OUT / name
    if p.exists():
        seen = collections.Counter()
        for r in csv.DictReader(p.open(encoding='utf-8-sig')):
            seen[(r['exchange'], r['market'], r['symbol'], r['timeframe'], r['open_time'])] += 1
        patch_dup[name] = sum(v - 1 for v in seen.values() if v > 1)

con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
con.execute('PRAGMA query_only=ON')
cold_cache = {}
hot_cache = {}


def cold(ex, mkt, sym, tf):
    k = (ex, mkt, sym, tf)
    if k not in cold_cache:
        s = set()
        for p in (ARC / 'candle_heads' / ex / mkt / sym / tf).glob('*.parquet'):
            tb = pq.read_table(p, columns=['open_time']).to_pydict()
            s.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
        cold_cache[k] = s
    return cold_cache[k]


def hot(ex, mkt, sym, tf):
    k = (ex, mkt, sym, tf)
    if k not in hot_cache:
        hot_cache[k] = {int(r[0]) for r in con.execute(
            'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
            (ex, mkt, sym, tf, FIXED_NOW))}
    return hot_cache[k]


# 主线内部缺口逐时间键覆盖
gaps = [g for g in csv.DictReader((CEV / '冷热合并内部缺槽.csv').open(encoding='utf-8-sig'))
        if g['metric'] == 'candle_heads' and g['period'] != '5m']
rows = []
for g in gaps:
    ex, mkt, sym, tf = g['exchange'], g['market'], g['symbol'], g['period']
    prev, nxt = int(g['previous_ts']), int(g['next_ts'])
    iv = TF_MS[tf]
    expected = [prev + iv * (i + 1) for i in range((nxt - prev) // iv - 1)]
    have = cold(ex, mkt, sym, tf) | hot(ex, mkt, sym, tf) | patch.get((ex, mkt, sym, tf), set())
    unc = [t for t in expected if t not in have]
    rows.append(dict(exchange=ex, symbol=sym, period=tf, from_utc=g['from_utc'], to_utc=g['to_utc'],
                     expected_slots=len(expected), missing_slots=g['missing_slots'],
                     covered_by_patch=sum(1 for t in expected if t in patch.get((ex, mkt, sym, tf), set())),
                     still_uncovered=len(unc),
                     first_uncovered_utc=iso(unc[0]) if unc else '', last_uncovered_utc=iso(unc[-1]) if unc else ''))
with (OUT / '校验_主线缺口覆盖.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

# 补件 vs 冷热 交集/重复
inter = 0
for k, ts in patch.items():
    inter += len(ts & (cold(*k) | hot(*k)))
summary = dict(
    patch_keys=len(patch), patch_rows=sum(len(v) for v in patch.values()),
    patch_duplicates=patch_dup, patch_vs_coldhot_overlap=inter,
    gap_segments=len(rows),
    gap_expected_slots=sum(r['expected_slots'] for r in rows),
    gap_covered_by_patch=sum(r['covered_by_patch'] for r in rows),
    gap_still_uncovered=sum(r['still_uncovered'] for r in rows),
    gap_segments_fully_covered=sum(1 for r in rows if r['still_uncovered'] == 0),
    gap_segments_uncovered=sum(1 for r in rows if r['still_uncovered'] > 0),
)
con.close()
(OUT / '校验_覆盖汇总.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))
