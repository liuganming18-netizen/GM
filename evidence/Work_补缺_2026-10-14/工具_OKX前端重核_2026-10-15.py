#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重核「上市后前端 408」的 OKX 侧（42 项）：修正两个缺陷后重新判定并直接补齐。

修正的缺陷（上一轮 工具_核对上市后前端408 的 OKX 分支）：
  1. first_okx 把请求异常吞掉 → 返回 (None, None) → 被误判为 official_absent「API该区间无数据」。
  2. 只取一页（limit=100）就取 min(ts)，长前端缺口（如 CRO 12h 跨 5 年）根本拉不到。

做法：自 actual_first 起用 after 游标**逐页向旧**拉到 listing，收集 [listing, actual_first) 内全部 bar，
与本地冷+热逐时间键比对，缺的写成补件。只读源库；结果写本目录。
"""
import collections, csv, datetime, json, sqlite3, time, urllib.request
from pathlib import Path
import pyarrow.parquet as pq

SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
FIXED_NOW = 1791980404548
BAR = {'15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '12h': '12H', '1d': '1D'}
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000,
         '8h': 28800000, '12h': 43200000, '1d': 86400000}
COLS = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close',
        'volume', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume',
        'close_time', 'source']


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def get(url, tries=5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode()), None
        except Exception as e:
            last = repr(e)
            time.sleep(0.6 + 1.0 * i)
    return None, last


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def main():
    rows = [r for r in csv.DictReader((OUT / '校验_上市后前端408.csv').open(encoding='utf-8-sig'))
            if r['exchange'] == 'okx']
    log(f'OKX 项 {len(rows)}')

    con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    _c, _h = {}, {}

    def cold(sym, tf):
        k = (sym, tf)
        if k not in _c:
            s = set()
            for p in (ARC / 'candle_heads' / 'okx' / 'swap' / sym / tf).glob('*.parquet'):
                tb = pq.read_table(p, columns=['open_time']).to_pydict()
                s.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
            _c[k] = s
        return _c[k]

    def hot(sym, tf):
        k = (sym, tf)
        if k not in _h:
            _h[k] = {int(r[0]) for r in con.execute(
                'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
                ('okx', 'swap', sym, tf, FIXED_NOW))}
        return _h[k]

    out = []
    patch = []
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    for i, r in enumerate(rows, 1):
        sym, tf = r['symbol'], r['timeframe']
        lo = int(datetime.datetime.fromisoformat(r['listing_utc']).timestamp() * 1000)
        hi = int(datetime.datetime.fromisoformat(r['actual_first_utc']).timestamp() * 1000)
        bar = BAR.get(tf)
        got = {}; err = None
        if not bar:
            err = 'okx_bar_unsupported'
        else:
            after = str(hi)
            pages = 0
            while True:
                u = (f'https://www.okx.com/api/v5/market/history-candles?instId={sym}'
                     f'&bar={bar}&after={after}&limit=100')
                d, e = get(u)
                if e:
                    err = e; break
                arr = d.get('data', [])
                if not arr:
                    break
                for x in arr:
                    t = int(x[0])
                    if lo <= t < hi:
                        got[t] = x
                oldest = min(int(x[0]) for x in arr)
                after = str(oldest)
                pages += 1
                if oldest <= lo or pages > 200:
                    break
                time.sleep(0.05)
        have = cold(sym, tf) | hot(sym, tf)
        miss = sorted(t for t in got if t not in have)
        for t in miss:
            x = got[t]
            patch.append(dict(exchange='okx', market='swap', symbol=sym, timeframe=tf, open_time=t,
                              open=x[1], high=x[2], low=x[3], close=x[4], volume=x[5],
                              quote_volume=x[6], trade_count='', taker_buy_volume='',
                              taker_buy_quote_volume='', close_time=t + TF_MS[tf] - 1,
                              source='okx.api.history-candles'))
        verdict = ('api_error' if err else
                   'fillable_real_gap' if got else 'official_absent')
        out.append(dict(exchange='okx', symbol=sym, timeframe=tf, listing_utc=r['listing_utc'],
                        actual_first_utc=r['actual_first_utc'], theoretical_slots=r['theoretical_slots'],
                        api_first_utc=iso(min(got)) if got else '',
                        api_bars_in_window=len(got), missing_vs_local=len(miss),
                        verdict=verdict, note=err or ''))
        log(f'  {i}/{len(rows)} {sym}/{tf} 窗口内API={len(got)} 本地缺={len(miss)} -> {verdict}')

    con.close()
    with (OUT / '校验_OKX前端重核_2026-10-15.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)
    if patch:
        with (OUT / '补缺_K线_okx前端重核_import.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            for x in sorted(patch, key=lambda z: (z['symbol'], z['timeframe'], z['open_time'])):
                w.writerow(x)
    cnt = collections.Counter(x['verdict'] for x in out)
    stat = dict(generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                policy='只读源；写本目录；不写生产库', checked=len(out),
                verdict_counts=dict(cnt),
                bars_in_window_total=sum(x['api_bars_in_window'] for x in out),
                missing_vs_local_total=len(patch),
                missing_keys=sorted({(x['symbol'], x['timeframe']) for x in out if x['missing_vs_local']}),
                detail=out)
    (OUT / '校验_OKX前端重核汇总_2026-10-15.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    log(f'DONE 重核 {len(out)} 项；新补 {len(patch)} 行')
    print(json.dumps({k: v for k, v in stat.items() if k != 'detail'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
