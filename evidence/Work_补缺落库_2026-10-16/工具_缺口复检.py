#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺口复检 —— 对 196 段缺口逐段重算「冷∪热」合并后的实际缺失根数。

口径与 10-16 定性完全一致：冷层 `parquet/candle_heads` + 热层 sqlite `candle_heads`，
按 open_time 网格逐根比对（只数缺口窗口内的槽位）。

用法：$PY 工具_缺口复检.py [--gaps /tmp/gaps.json] [--out 复检结果.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import duckdb

ROOT = Path('/Users/boos/Desktop/威科夫因子共振趋势')
DB = ROOT / '数据/市场数据/db/market_v2.sqlite3'
ARC = ROOT / '数据/市场数据/parquet'
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000,
         '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gaps', default='/tmp/gaps.json')
    ap.add_argument('--out', default=str(ROOT / 'evidence/Work_补缺落库_2026-10-16/复检结果.json'))
    a = ap.parse_args()

    gaps = json.loads(Path(a.gaps).read_text())
    duck = duckdb.connect()
    duck.execute('PRAGMA threads=4')
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=30)

    cache_cold, cache_hot = {}, {}
    remain, fixed = [], []
    for g in gaps:
        ex, mk, sym, tf = g['ex'], g['mk'], g['sym'], g['tf']
        k = (ex, mk, sym, tf)
        if k not in cache_cold:
            d = ARC / 'candle_heads' / ex / mk / sym / tf
            files = sorted(str(p) for p in d.glob('*.parquet')) if d.is_dir() else []
            s = set()
            if files:
                try:
                    s = {int(r[0]) for r in duck.execute('SELECT open_time FROM read_parquet(?)', [files]).fetchall()}
                except Exception as e:      # noqa: BLE001
                    print(f'! 冷层读取失败 {k}: {e}')
            cache_cold[k] = s
            cache_hot[k] = {int(r[0]) for r in con.execute(
                'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=?',
                k)}
        have = cache_cold[k] | cache_hot[k]
        iv = TF_MS[tf]
        miss = sum(1 for t in range(g['start'], g['end'] + 1, iv) if t not in have)
        rec = {'ex': ex, 'mk': mk, 'sym': sym, 'tf': tf, 'from': g['start'], 'to': g['end'],
               'was': g['n'], 'now': miss}
        (fixed if miss == 0 else remain).append(rec)

    con.close(); duck.close()
    tot_was = sum(r['was'] for r in remain) + sum(r['was'] for r in fixed)
    tot_now = sum(r['now'] for r in remain)
    print(f'段：已补齐 {len(fixed)} / 仍缺 {len(remain)}  (共 {len(gaps)})')
    print(f'根：原缺 {tot_was} → 仍缺 {tot_now}')
    print()
    print('=== 仍缺（前 40）===')
    for r in sorted(remain, key=lambda x: -x['now'])[:40]:
        print(f"  {r['ex']}/{r['sym']:16s}{r['tf']:4s} 原缺{r['was']:>6} 仍缺{r['now']:>6}")
    if len(remain) > 40:
        print(f'  ... 共 {len(remain)} 段')
    Path(a.out).write_text(json.dumps({'fixed': fixed, 'remain': remain,
                                       'tot_was': tot_was, 'tot_now': tot_now},
                                      ensure_ascii=False, indent=1))
    print(f'\n明细 -> {a.out}')


if __name__ == '__main__':
    main()
