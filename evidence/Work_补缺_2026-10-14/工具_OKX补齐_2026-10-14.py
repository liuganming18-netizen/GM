#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OKX 综合补齐（只读源，写本目录）——只拉真实缺失区间（跳过冷/热/已有补件）。
 1) need_api 中 OKX 键：按 months 范围
 2) 上市前端 408 中 OKX fillable 项：按 listing~local_first
对每键取窗口并集，计算窗口内"冷|热|已有api补件"之外的缺失槽，按连续区间仅拉这些区间。
强重试，避免瞬时 SSL 抖动导致整键跳过。"""
import argparse, csv, datetime, json, sqlite3, time, urllib.request, collections
from pathlib import Path
import pyarrow.parquet as pq

FIXED_NOW = 1791980404548
SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
TF_MS = {'15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '12h': 43200000, '1d': 86400000}
OKX_BAR = {'15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '12h': '12Hutc', '1d': '1Dutc'}


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def get_json(url, tries=6):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.0 + 2.0 * i)
    raise last


def okx_fetch(inst, tf, start_ms, end_ms):
    """拉 [start_ms, end_ms] 内的 OKX K线（after 游标自新向旧）"""
    bar = OKX_BAR.get(tf)
    if not bar or end_ms < start_ms:
        return {}
    out = {}
    after = end_ms + TF_MS[tf]
    guard = 0
    while True:
        guard += 1
        if guard > 6000:
            break
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
        time.sleep(0.18)
    return out


def build_keys():
    win = {}
    for e in json.loads((OUT / '补缺_需API键.json').read_text()):
        if not e['key'].startswith('okx'):
            continue
        _, mkt, sym, tf = e['key'].split('/')
        ms = sorted(e['months'])
        lo = int(datetime.datetime.strptime(ms[0] + '-01', '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        hi = int((datetime.datetime.strptime(ms[-1] + '-01', '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
                  + datetime.timedelta(days=32)).timestamp() * 1000) - 1
        hi = min(hi, FIXED_NOW)
        k = f'{sym}/{tf}'
        if k in win:
            win[k]['lo'] = min(win[k]['lo'], lo); win[k]['hi'] = max(win[k]['hi'], hi)
        else:
            win[k] = {'lo': lo, 'hi': hi}
    fe = OUT / '校验_上市后前端408.csv'
    if fe.exists():
        for r in csv.DictReader(fe.open(encoding='utf-8-sig')):
            if r['exchange'] != 'okx' or r['verdict'] != 'fillable_real_gap':
                continue
            sym, tf = r['symbol'], r['timeframe']
            lo = int(datetime.datetime.fromisoformat(r['listing_utc']).timestamp() * 1000)
            hi = int(datetime.datetime.fromisoformat(r['actual_first_utc']).timestamp() * 1000) - TF_MS[tf]
            if hi < lo:
                continue
            k = f'{sym}/{tf}'
            if k in win:
                win[k]['lo'] = min(win[k]['lo'], lo); win[k]['hi'] = max(win[k]['hi'], hi)
            else:
                win[k] = {'lo': lo, 'hi': hi}
    return win


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--only', default='')
    ap.add_argument('--shard', type=int, default=0, help='分片序号（0基）')
    ap.add_argument('--shards', type=int, default=1, help='分片总数（>1 时启用并行）')
    ap.add_argument('--split-range', type=int, default=1, help='把每键缺失槽按序等分为 N 份（>1 时按槽并行）')
    ap.add_argument('--range-part', type=int, default=0, help='本 worker 处理第 K 份（0基）')
    ap.add_argument('--out', default='补缺_K线_okx补齐_import.csv', help='输出 CSV 文件名（本目录）')
    a = ap.parse_args()
    win = build_keys()
    if a.only:
        win = {k: v for k, v in win.items() if a.only in k}
    keys = sorted(win.items())
    if a.shards > 1:
        keys = [kv for i, kv in enumerate(keys) if i % a.shards == a.shard]
    if a.limit:
        keys = keys[:a.limit]
    log(f'OKX 待补键 {len(keys)} (shard {a.shard}/{a.shards})')

    con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')

    existing = collections.defaultdict(set)
    for name in ['补缺_K线_api_import.csv', '补缺_K线_okx补齐_import.csv', a.out]:
        p = OUT / name
        if p.exists():
            for r in csv.DictReader(p.open(encoding='utf-8-sig')):
                if r['exchange'] == 'okx':
                    existing[(r['symbol'], r['timeframe'])].add(int(r['open_time']))

    cold_cache = {}

    def cold(sym, tf):
        k = (sym, tf)
        if k not in cold_cache:
            s = set()
            for p in (ARC / 'candle_heads' / 'okx' / 'swap' / sym / tf).glob('*.parquet'):
                tb = pq.read_table(p, columns=['open_time']).to_pydict()
                s.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
            cold_cache[k] = s
        return cold_cache[k]

    def hot(sym, tf):
        return {int(r[0]) for r in con.execute(
            'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
            ('okx', 'swap', sym, tf, FIXED_NOW))}

    out = OUT / a.out
    cols = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume', 'source', 'api']
    total = 0
    perkey = {}
    fresh = not out.exists()
    with out.open('a', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(cols)
        for i, (k, w2) in enumerate(keys, 1):
            sym, tf = k.split('/')
            iv = TF_MS[tf]
            have = cold(sym, tf) | hot(sym, tf) | existing.get((sym, tf), set())
            lo, hi = w2['lo'], w2['hi']
            # 窗口内缺失槽（网格按周期对齐：t 为 iv 的整数倍，与交易所 K 线时间一致）
            first = (lo // iv + 1) * iv
            slots = list(range(first, hi + 1, iv))
            miss = [t for t in slots if t not in have]
            if a.split_range > 1:
                M = len(miss)
                st = a.range_part * M // a.split_range
                en = (a.range_part + 1) * M // a.split_range
                miss = miss[st:en]
            # 连续区间（间隔<=8槽合并）
            ranges = []
            for t in miss:
                if ranges and t - ranges[-1][1] <= iv * 8:
                    ranges[-1][1] = t
                else:
                    ranges.append([t, t])
            rows_written = 0
            try:
                for (rs, re_) in ranges:
                    recs = okx_fetch(sym, tf, rs, re_)
                    for t in sorted(recs):
                        if t in have:
                            continue
                        c = recs[t]
                        w.writerow(['okx', 'swap', sym, tf, t, c[1], c[2], c[3], c[4], c[5], '', c[7], '', '', '',
                                    'okx.api.history-candles', ''])
                        rows_written += 1
                    have |= set(recs.keys())
            except Exception as e:
                log(f'FAIL {k} {e!r}'); perkey[k] = -1; continue
            total += rows_written
            perkey[k] = rows_written
            log(f'  {i}/{len(keys)} {k} miss_slots={len(miss)} ranges={len(ranges)} rows={rows_written} total={total}')
    con.close()
    tag = f'_{a.shard}' if a.shards > 1 else ''
    (OUT / f'补缺_OKX补齐清单{tag}.json').write_text(json.dumps(
        {'generated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
         'shard': f'{a.shard}/{a.shards}', 'keys': len(keys), 'total_rows': total, 'perkey': perkey},
        ensure_ascii=False, indent=2))
    log(f'DONE OKX补齐 total={total}')


if __name__ == '__main__':
    main()
