#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把补齐产物导入生产（需显式 --apply；默认 dry-run）。

安全：--backup 先备份库与受影响分片；按唯一键去重；--apply 才写入。
覆盖本轮全部补件：
  K线  : 补缺_K线_local_import.csv / _api_ / _okx前端_ / _okx补齐_ / _官方日包_
  资金费: 补缺_资金费_import.csv
  OI   : 补缺_OI_import.csv（归档口径）+ 补缺_OI_API_import.csv（API 口径）
用法：
  $PY 工具_导入补齐_2026-10-14.py                 # dry-run 报告
  $PY 工具_导入补齐_2026-10-14.py --backup --apply # 备份后写入
"""
import argparse, csv, datetime, json, shutil, sqlite3
from pathlib import Path

SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
DB = SAFE / 'data/market_v2.sqlite3'
ARC = SAFE / 'data/archive/parquet'
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
STAMP = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%dT%H%M%S')
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}

# 注：'补缺_K线_okx前端重核_import.csv' 已废弃（12h/1d UTC+8 口径缺陷 + B 轴已全覆），不导入。
KLINE_FILES = ['补缺_K线_local_import.csv', '补缺_K线_api_import.csv',
               '补缺_K线_okx前端_import.csv', '补缺_K线_okx补齐_import.csv',
               '补缺_K线_官方日包_import.csv']


def read_csv(name):
    p = OUT / name
    return list(csv.DictReader(p.open(encoding='utf-8-sig'))) if p.exists() else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--backup', action='store_true')
    a = ap.parse_args()
    plan = {}

    # 资金费
    fr = read_csv('补缺_资金费_import.csv')

    # K线（多文件合并，按 (ex,mkt,sym,tf,open_time) 去重，先到先得）
    kf = {}
    kline_src = {}
    for name in KLINE_FILES:
        rows = read_csv(name)
        kline_src[name] = len(rows)
        for r in rows:
            k = (r['exchange'], r['market'], r['symbol'], r['timeframe'], r['open_time'])
            kf.setdefault(k, r)
    kf = list(kf.values())

    # OI（全部补件的 ts 已是库内口径：采集器=桶终点；归档补件已按「归档 ts + 5min」换算）
    # 注意：补缺_OI_import.csv（7 行）已作废（标签错位一格），不导入。
    oi = {}
    oi_src = {}
    for name in ['补缺_OI_1008_1013_官方归档_import.csv', '补缺_OI_API_import.csv']:
        rows = read_csv(name)
        oi_src[name] = len(rows)
        for r in rows:
            ts = int(r['ts'])
            oi.setdefault((r['symbol'], ts), dict(
                exchange=r['exchange'], market=r['market'], symbol=r['symbol'],
                period=r.get('period') or '5m', ts=ts,
                open_interest=r.get('open_interest') or r.get('sum_open_interest'),
                open_interest_value=r.get('open_interest_value') or r.get('sum_open_interest_value'),
                oi_ccy=r.get('oi_ccy') or r.get('sum_open_interest'),
                oi_usd=r.get('oi_usd') or r.get('sum_open_interest_value'),
                source=r.get('source', 'binance.vision.daily.metrics')))
    oi = list(oi.values())

    plan['funding_rows'] = len(fr)
    plan['candle_rows_by_file'] = kline_src
    plan['candle_rows_unique'] = len(kf)
    plan['candle_keys'] = len({(r['exchange'], r['market'], r['symbol'], r['timeframe']) for r in kf})
    plan['oi_rows_by_file'] = oi_src
    plan['oi_rows_unique'] = len(oi)
    print('PLAN', json.dumps(plan, ensure_ascii=False, indent=2), flush=True)

    if not a.apply:
        print('DRY-RUN：未写入。加 --backup --apply 才落库。', flush=True)
        return

    if a.backup:
        bdir = OUT / f'backup_{STAMP}'
        bdir.mkdir(parents=True, exist_ok=True)
        for suf in ['', '-wal', '-shm']:
            s = Path(str(DB) + suf)
            if s.exists():
                shutil.copy2(s, bdir / s.name)
        print(f'backup db -> {bdir}', flush=True)
        keys = sorted({(r['exchange'], r['market'], r['symbol'], r['timeframe']) for r in kf})
        barc = bdir / 'parquet_touched'
        for ex, mkt, sym, tf in keys:
            d = ARC / 'candle_heads' / ex / mkt / sym / tf
            if d.is_dir():
                t = barc / 'candle_heads' / ex / mkt / sym / tf
                t.mkdir(parents=True, exist_ok=True)
                for p in d.glob('*.parquet'):
                    shutil.copy2(p, t / p.name)
        print(f'backup parquet -> {barc}', flush=True)

    con = sqlite3.connect(str(DB), timeout=60)
    cur = con.cursor()
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    n_f = 0
    for r in fr:
        cur.execute('INSERT OR IGNORE INTO funding_history(exchange,market,symbol,funding_time,funding_rate,mark_price,rate_type,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (r['exchange'], r['market'], r['symbol'], int(r['funding_time']), r['funding_rate'], None, 'settled', 'binance.archive.fundingRate', now))
        n_f += cur.rowcount

    n_k = 0
    for r in kf:
        ex, mkt, sym, tf = r['exchange'], r['market'], r['symbol'], r['timeframe']
        ot = int(r['open_time'])
        ct = int(r['close_time']) if r.get('close_time') else ot + TF_MS[tf] - 1
        cur.execute('INSERT OR IGNORE INTO candle_heads(exchange,market,symbol,timeframe,open_time,source_id) VALUES(?,?,?,?,?,?)',
                    (ex, mkt, sym, tf, ot, r.get('source', 'backfill')))
        n_k += cur.rowcount
        cur.execute('INSERT OR IGNORE INTO candles(exchange,market,symbol,timeframe,open_time,close_time,open,high,low,close,volume,quote_volume,trade_count,taker_buy_volume,taker_buy_quote_volume,source_id,available_at,is_closed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)',
                    (ex, mkt, sym, tf, ot, ct, r['open'], r['high'], r['low'], r['close'], r['volume'],
                     r.get('quote_volume'), r.get('trade_count'), r.get('taker_buy_volume'),
                     r.get('taker_buy_quote_volume'), r.get('source', 'backfill'), now))

    n_o = 0
    for r in oi:
        cur.execute('INSERT OR IGNORE INTO oi_history(exchange,market,symbol,period,ts,open_interest,open_interest_value,oi_ccy,oi_usd,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (r['exchange'], r['market'], r['symbol'], r['period'], r['ts'], r['open_interest'],
                     r['open_interest_value'], r['oi_ccy'], r['oi_usd'], r['source'], now))
        n_o += cur.rowcount

    con.commit()
    print(f'APPLIED funding={n_f} candle_heads={n_k} oi={n_o}', flush=True)
    con.close()


if __name__ == '__main__':
    main()
