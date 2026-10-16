#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 coverage_cache —— 按「冷∪热」实测库存重算每个 (交易所,市场,标的,周期) 的覆盖。

字段语义（与 `realtime_collector._update_coverage_cache_delta` 保持一致）：
  rows        = 实际存在的 open_time 个数（冷层 parquet/candle_heads ∪ 热层 sqlite candle_heads）
  first_open  = 最小 open_time
  last_close  = 最大 open_time + 周期毫秒 - 1
  expected    = (last_close + 1 - first_open) // 周期毫秒     ← 首尾之间的完整网格槽位数
  missing     = max(0, expected - rows)                       ← 首尾之间的真实空洞
  source_count= 该组合在 sources 表中的登记条数
  updated_at  = 本次重算时间

默认 dry-run；`--apply` 才写。写入前自动把旧表整表备份到 CSV。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sqlite3
from pathlib import Path

import duckdb

ROOT = Path('/Users/boos/Desktop/威科夫因子共振趋势')
DB = ROOT / '数据/市场数据/db/market_v2.sqlite3'
ARC = ROOT / '数据/市场数据/parquet'
EV = ROOT / 'evidence/Work_补缺落库_2026-10-16'
TF_MS = {'1m': 60000, '5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000,
         '2h': 7200000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    duck = duckdb.connect()
    duck.execute('PRAGMA threads=4')
    print('扫描冷层 candle_heads 分片 ...', flush=True)
    cold = {(r[0], r[1], r[2], r[3]): (int(r[4]), int(r[5]), int(r[6])) for r in duck.execute(f"""
        SELECT exchange, market, symbol, timeframe,
               COUNT(DISTINCT open_time), MIN(open_time), MAX(open_time)
        FROM read_parquet('{ARC}/candle_heads/*/*/*/*/*.parquet')
        GROUP BY 1,2,3,4""").fetchall()}
    duck.close()
    print(f'  冷层组合 {len(cold)} 个', flush=True)

    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    print('扫描热层 candle_heads ...', flush=True)
    hot = {(r[0], r[1], r[2], r[3]): (int(r[4]), int(r[5]), int(r[6])) for r in con.execute("""
        SELECT exchange, market, symbol, timeframe,
               COUNT(DISTINCT open_time), MIN(open_time), MAX(open_time)
        FROM candle_heads GROUP BY 1,2,3,4""").fetchall()}
    print(f'  热层组合 {len(hot)} 个', flush=True)
    old = {tuple(r[:4]): dict(r) for r in con.execute('SELECT * FROM coverage_cache')}
    src_cnt = {(r[0], r[1], r[2], r[3]): int(r[4]) for r in con.execute(
        'SELECT exchange,market,symbol,timeframe,COUNT(*) FROM sources GROUP BY 1,2,3,4')}
    con.close()

    combos = set(cold) | set(hot)
    new = {}
    for k in combos:
        tf = k[3]
        if tf not in TF_MS:
            continue
        c, h = cold.get(k), hot.get(k)
        rows = (c[0] if c else 0) + (h[0] if h else 0)
        first = min([x[1] for x in (c, h) if x])
        last_ot = max([x[2] for x in (c, h) if x])
        new[k] = {'rows': rows, 'first_open': first, 'last_close': last_ot + TF_MS[tf] - 1}

    # 重叠段精确扣除（冷热在归档水位附近有重叠，COUNT(DISTINCT) 相加会重复）
    duck = duckdb.connect()
    duck.execute('PRAGMA threads=4')
    duck.execute("INSTALL sqlite; LOAD sqlite;")
    duck.execute(f"ATTACH '{DB}' AS m (TYPE sqlite, READ_ONLY)")
    print('计算冷热重叠段 ...', flush=True)
    ov_rows = duck.execute(f"""
        WITH c AS (SELECT exchange,market,symbol,timeframe,open_time
                   FROM read_parquet('{ARC}/candle_heads/*/*/*/*/*.parquet')),
             h AS (SELECT exchange,market,symbol,timeframe,open_time FROM m.candle_heads)
        SELECT c.exchange,c.market,c.symbol,c.timeframe,COUNT(*)
        FROM c JOIN h USING(exchange,market,symbol,timeframe,open_time)
        GROUP BY 1,2,3,4""").fetchall()
    duck.close()
    for ex, mk, sym, tf, n in ov_rows:
        k = (ex, mk, sym, tf)
        if k in new:
            new[k]['rows'] -= int(n)
    print(f'  重叠组合 {len(ov_rows)} 个，已扣除', flush=True)

    for k, v in new.items():
        iv = TF_MS[k[3]]
        v['expected'] = max(0, (v['last_close'] + 1 - v['first_open']) // iv)
        v['missing'] = max(0, v['expected'] - v['rows'])
        v['source_count'] = src_cnt.get(k, 0)

    changed = [k for k in new if k not in old or any(
        old[k][f] != new[k][f] for f in ('rows', 'first_open', 'last_close', 'expected', 'missing'))]
    print(f'\n组合总数 {len(new)}；与旧表相比发生变化 {len(changed)} 个；旧表 {len(old)} 行')
    print(f"  旧表 missing 合计 = {sum(r['missing'] or 0 for r in old.values()):,}")
    print(f"  新表 missing 合计 = {sum(v['missing'] for v in new.values()):,}")
    print(f"  旧表 rows    合计 = {sum(r['rows'] or 0 for r in old.values()):,}")
    print(f"  新表 rows    合计 = {sum(v['rows'] for v in new.values()):,}")

    if not a.apply:
        print('\nDRY-RUN：未写入。加 --apply 才写。')
        return

    EV.mkdir(parents=True, exist_ok=True)
    bak = EV / f'backup_coverage_cache_apply_{datetime.datetime.now().strftime("%H%M%S")}.csv'
    with bak.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['exchange', 'market', 'symbol', 'timeframe', 'rows', 'first_open',
                    'last_close', 'expected', 'missing', 'source_count', 'updated_at'])
        for k, r in old.items():
            w.writerow(list(k) + [r['rows'], r['first_open'], r['last_close'], r['expected'],
                                  r['missing'], r['source_count'], r['updated_at']])
    print(f'旧表已备份 -> {bak}')

    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    wcon = sqlite3.connect(str(DB), timeout=120)
    wcon.execute('PRAGMA busy_timeout=120000')
    for k, v in new.items():
        wcon.execute(
            'INSERT INTO coverage_cache(exchange,market,symbol,timeframe,rows,first_open,last_close,'
            'expected,missing,source_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(exchange,market,symbol,timeframe) DO UPDATE SET '
            'rows=excluded.rows,first_open=excluded.first_open,last_close=excluded.last_close,'
            'expected=excluded.expected,missing=excluded.missing,source_count=excluded.source_count,'
            'updated_at=excluded.updated_at',
            (k[0], k[1], k[2], k[3], v['rows'], v['first_open'], v['last_close'],
             v['expected'], v['missing'], v['source_count'], now))
    wcon.commit()
    n = wcon.execute('SELECT COUNT(*),SUM(missing) FROM coverage_cache').fetchone()
    wcon.close()
    print(f'APPLIED coverage_cache 现有 {n[0]} 行，missing 合计 {n[1]:,}')


if __name__ == '__main__':
    main()
