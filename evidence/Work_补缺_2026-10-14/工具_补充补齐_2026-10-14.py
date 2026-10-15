#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补充补齐（只读源，写本目录）：
 A. OKX 上市前端 40 项：按 listing~local_first 用 OKX API 补，过滤本地冷热 -> 补缺_K线_okx前端_import.csv
 B. ICPUSDT 15m 2022-09-01~09-21：官方日包补齐 -> 补缺_K线_官方日包_import.csv
 C. OI 7 槽 2026-10-10~10-13：官方 metrics 归档补齐 -> 补缺_OI_import.csv
"""
import csv, io, json, datetime, time, zipfile, urllib.request, sqlite3, collections
from pathlib import Path
import pyarrow.parquet as pq

SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
WEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
CEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
FIXED_NOW = 1791980404548
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}
OKX_BAR = {'15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '12h': '12Hutc', '1d': '1Dutc'}


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def get_json(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(1.2 * (i + 1))


def get_bytes(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1.2 * (i + 1))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.2 * (i + 1))


def okx_candles(inst, tf, start_ms, end_ms):
    bar = OKX_BAR.get(tf)
    if not bar:
        return {}
    out = {}
    after = end_ms + TF_MS[tf]
    while True:
        url = f'https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar={bar}&after={after}&limit=100'
        d = get_json(url)
        rows = d.get('data', []) if isinstance(d, dict) else []
        if not rows:
            break
        oldest = None
        for r in rows:
            ts = int(r[0])
            if start_ms <= ts <= end_ms:
                out[ts] = r
            oldest = ts if oldest is None else min(oldest, ts)
        if oldest is None or oldest <= start_ms:
            break
        after = oldest
        time.sleep(0.25)
    return out


def local_have(con, ex, mkt, sym, tf):
    cold = set()
    for p in (ARC / 'candles' / ex / mkt / sym / tf).glob('*.parquet'):
        tb = pq.read_table(p, columns=['open_time']).to_pydict()
        cold.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
    hot = {int(r[0]) for r in con.execute(
        'SELECT open_time FROM candles WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
        (ex, mkt, sym, tf, FIXED_NOW))}
    return cold | hot


# ---------- A. OKX 上市前端 ----------
def part_okx_front():
    src = OUT / '校验_上市后前端408.csv'
    rows = [r for r in csv.DictReader(src.open(encoding='utf-8-sig'))
            if r['exchange'] == 'okx' and r['verdict'] == 'fillable_real_gap']
    log(f'OKX 上市前端可补 {len(rows)} 项')
    con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    out = OUT / '补缺_K线_okx前端_import.csv'
    cols = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume', 'source', 'api']
    total = 0
    perkey = {}
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(cols)
        for i, r in enumerate(rows, 1):
            sym, tf = r['symbol'], r['timeframe']
            lo = int(datetime.datetime.fromisoformat(r['listing_utc']).timestamp() * 1000)
            hi = int(datetime.datetime.fromisoformat(r['actual_first_utc']).timestamp() * 1000) - TF_MS[tf]
            if hi < lo:
                perkey[f'{sym}/{tf}'] = 0; continue
            recs = okx_candles(sym, tf, lo, hi)
            have = local_have(con, 'okx', 'swap', sym, tf)
            miss = [t for t in sorted(recs) if t not in have and t <= FIXED_NOW]
            for t in miss:
                k = recs[t]
                w.writerow(['okx', 'swap', sym, tf, t, k[1], k[2], k[3], k[4], k[5], '', k[7], '', '', '',
                            'okx.api.history-candles', ''])
            total += len(miss)
            perkey[f'{sym}/{tf}'] = len(miss)
            if i % 5 == 0:
                log(f'  okx-front {i}/{len(rows)} rows={total}')
    con.close()
    log(f'OKX前端合计 {total} 行 -> {out.name}')
    return total, perkey


# ---------- B. ICPUSDT 15m 官方日包 ----------
def part_icpusdt_daily():
    sym, tf = 'ICPUSDT', '15m'
    lo = int(datetime.datetime(2022, 9, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    hi = int(datetime.datetime(2022, 9, 27, 2, 15, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    have = local_have(con, 'binance', 'um', sym, tf)
    con.close()
    out = OUT / '补缺_K线_官方日包_import.csv'
    cols = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume', 'source', 'api']
    total = 0; days_absent = []
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(cols)
        d = datetime.date(2022, 9, 1)
        while d <= datetime.date(2022, 9, 27):
            u = f'https://data.binance.vision/data/futures/um/daily/klines/{sym}/{tf}/{sym}-{tf}-{d.isoformat()}.zip'
            b = get_bytes(u)
            if b is None:
                days_absent.append(d.isoformat()); d += datetime.timedelta(days=1); continue
            z = zipfile.ZipFile(io.BytesIO(b)); n = [x for x in z.namelist() if x.endswith('.csv')][0]
            cnt = 0
            for row in csv.reader(io.TextIOWrapper(z.open(n), encoding='utf-8-sig', newline='')):
                if not row or row[0] == 'open_time':
                    continue
                t = int(row[0])
                if lo <= t <= hi and t not in have:
                    w.writerow(['binance', 'um', sym, tf, t, row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                                row[8], row[9], row[10], 'binance.vision.daily.klines', ''])
                    cnt += 1
            total += cnt
            d += datetime.timedelta(days=1)
    log(f'ICPUSDT 15m 日包补齐 {total} 行；无日包日期={days_absent}')
    return total, days_absent


# ---------- C. OI 7 槽 ----------
def part_oi():
    src = WEV / 'C_OI缺槽分类复核.csv'
    rows = [r for r in csv.DictReader(src.open(encoding='utf-8-sig'))
            if r['status'] == 'no_surviving_day_zip' and r['from_utc'][:10] != '2026-10-14']
    log(f'OI 待补段 {len(rows)}')
    out = OUT / '补缺_OI_import.csv'
    cols = ['exchange', 'market', 'symbol', 'timeframe', 'create_time_utc', 'sum_open_interest',
            'sum_open_interest_value', 'count_toptrader_long_short_ratio', 'sum_toptrader_long_short_ratio',
            'count_long_short_ratio', 'sum_taker_long_short_vol_ratio', 'source']
    total = 0; detail = []
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(cols)
        for r in rows:
            sym = r['symbol']; day = r['from_utc'][:10]; tm = r['from_utc'][11:16]
            u = f'https://data.binance.vision/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day}.zip'
            b = get_bytes(u)
            if b is None:
                detail.append(f'{sym} {day} {tm}: 归档404'); continue
            z = zipfile.ZipFile(io.BytesIO(b)); n = [x for x in z.namelist() if x.endswith('.csv')][0]
            want = f'{day} {tm}:00'
            hit = None
            for row in csv.reader(io.TextIOWrapper(z.open(n), encoding='utf-8-sig', newline='')):
                if row and row[0] == want:
                    hit = row; break
            if hit:
                w.writerow(['binance', 'um', sym, '5m', hit[0], hit[2], hit[3], hit[4], hit[5], hit[6], hit[7],
                            'binance.vision.daily.metrics'])
                total += 1
                detail.append(f'{sym} {day} {tm}: 补齐')
            else:
                detail.append(f'{sym} {day} {tm}: 归档无该槽')
    log(f'OI 补齐 {total} 槽')
    return total, detail


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--parts', default='okx,icp,oi')
    a = ap.parse_args()
    parts = set(a.parts.split(','))
    man = {'generated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
           'policy': '只读源；写本目录；不写生产库'}
    if 'okx' in parts:
        t, pk = part_okx_front(); man['okx_front_rows'] = t; man['okx_front_perkey'] = pk
    if 'icp' in parts:
        t, ab = part_icpusdt_daily(); man['icpusdt_daily_rows'] = t; man['icpusdt_daily_absent_days'] = ab
    if 'oi' in parts:
        t, de = part_oi(); man['oi_rows'] = t; man['oi_detail'] = de
    (OUT / '补缺_补充清单.json').write_text(json.dumps(man, ensure_ascii=False, indent=2))
    log('DONE ' + json.dumps({k: v for k, v in man.items() if not isinstance(v, (list, dict))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
