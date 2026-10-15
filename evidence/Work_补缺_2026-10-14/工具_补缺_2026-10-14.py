#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐缺失数据（只读源，写入独立交付目录，不写生产库）。

来源优先级：本地原件(raw ZIP / raw_oi_metrics) > Binance API > 官方归档(data.binance.vision) > OKX API
输出：import-ready CSV/parquet + 清单 JSON；不修改 market_v2.sqlite3 与 archive/parquet。
"""
import argparse, collections, csv, datetime, hashlib, io, json, os, sys, time, zipfile, urllib.request, urllib.parse
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

FIXED_NOW = 1791980404548
SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
BE = Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现')
RAW = BE / '历史下载/raw'
CEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')      # Codex 原证据
WEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')  # Work 验收产出
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
OUT.mkdir(parents=True, exist_ok=True)
STAMP = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}
OKX_BAR = {'15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '12h': '12Hutc', '1d': '1Dutc'}
BIN_API = 'https://fapi.binance.com'
VISION = 'https://data.binance.vision'


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def http_json(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def checksum_ok(p):
    q = Path(str(p) + '.CHECKSUM')
    if not q.exists():
        return True  # 无校验文件不阻断，单独记账
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest() == q.read_text().split()[0].lower()
    except Exception:
        return False


# ---------------- Part 1: 资金费补齐（本地已证缺失事件 → 导入格式） ----------------
def part_funding():
    src = WEV / '资金费逐事件缺失.csv'
    rows = list(csv.DictReader(src.open(encoding='utf-8-sig')))
    out = OUT / '补缺_资金费_import.csv'
    n = 0
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['exchange', 'market', 'symbol', 'funding_time', 'funding_rate', 'mark_price', 'rate_type', 'source', 'fetched_at', 'evidence'])
        for r in rows:
            if r['checksum_status'] != 'MATCH':
                continue
            w.writerow(['binance', 'um', r['symbol'], r['funding_time_ms'], r['funding_rate'], '', 'settled',
                        'binance.archive.fundingRate', '', f"zip={Path(r['source_zip']).name};sha=MATCH"])
            n += 1
    log(f'funding import rows={n} -> {out.name}')
    return {'funding_events': n}


# ---------------- Part 2/3: K线补齐 ----------------
def affected_map():
    """返回 {(ex,mkt,sym,tf): set(month 'YYYY-MM')} 及标记"""
    aff = collections.defaultdict(set)
    reason = collections.defaultdict(set)
    # 旧年份分片（≤2025）
    for r in csv.DictReader((WEV / 'A_恢复前后_逐分片.csv').open(encoding='utf-8-sig')):
        if r['metric'] != 'candle_heads' or not r['year'].isdigit() or int(r['year']) > 2025:
            continue
        key = (r['exchange'], r['market'], r['symbol'], r['period'])
        for m in range(1, 13):
            aff[key].add(f"{r['year']}-{m:02d}")
        reason[key].add('oldyear_shard')
    # 主线内部缺口
    for g in csv.DictReader((CEV / '冷热合并内部缺槽.csv').open(encoding='utf-8-sig')):
        if g['metric'] != 'candle_heads' or g['period'] == '5m':
            continue
        key = (g['exchange'], g['market'], g['symbol'], g['period'])
        lo = datetime.datetime.fromtimestamp(int(g['previous_ts']) / 1000, datetime.timezone.utc)
        hi = datetime.datetime.fromtimestamp(int(g['next_ts']) / 1000, datetime.timezone.utc)
        d = lo.replace(day=1)
        while d <= hi:
            aff[key].add(d.strftime('%Y-%m'))
            d = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        reason[key].add('internal_gap')
    return aff, reason


def read_local_kline(ex, sym, tf, months):
    """从本地原件读取指定月份的K线；返回 {open_time: row12} 与是否覆盖"""
    rows = {}
    covered = False
    if ex == 'binance':
        d = RAW / tf / sym
        if d.is_dir():
            for p in d.glob('*.zip'):
                stem = p.stem
                key = stem[len(sym) + len(tf) + 2:]
                mm = key[:7] if len(key) >= 7 else key
                if mm not in months:
                    continue
                covered = True
                if not checksum_ok(p):
                    continue
                with zipfile.ZipFile(p) as z:
                    n = next(x for x in z.namelist() if x.endswith('.csv'))
                    with z.open(n) as s:
                        for row in csv.reader(io.TextIOWrapper(s, encoding='utf-8-sig', newline='')):
                            if not row or row[0] == 'open_time':
                                continue
                            rows[int(row[0])] = row
    return rows, covered


def part_candle_local(aff, reason):
    """本地原件提取 + 记录需 API 的键"""
    need_api = []
    out_rows = []
    stats = collections.Counter()
    LIMIT = int(os.environ.get('LIMIT', '0'))
    keys = sorted(aff.items())
    if LIMIT:
        keys = keys[:LIMIT]
    for i, (key, months) in enumerate(keys, 1):
        ex, mkt, sym, tf = key
        if ex == 'okx':
            need_api.append((key, months, 'okx_local_original_only_probe_1row'))
            continue
        rows, covered = read_local_kline(ex, sym, tf, months)
        if rows:
            stats['keys_local'] += 1
            out_rows.append((key, rows))
        # 本地原件即冷档来源(行数相等)，旧年份差额本地补不了 ⇒ 一律同时走 API
        need_api.append((key, months, 'local_zip_equals_cold__api_authoritative'))
        if i % 40 == 0:
            log(f'candle-local {i}/{len(keys)} local_keys={stats["keys_local"]} need_api={len(need_api)}')
    # 写出本地可得行（供后续与冷热差集）
    return out_rows, need_api, stats


def candle_diff_and_write(local_rows):
    """与冷热合并差集，输出真正缺失的K线行"""
    import sqlite3
    con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    out = OUT / '补缺_K线_local_import.csv'
    cols = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume', 'source']
    total = 0
    perkey = {}
    localmiss = {}
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(cols)
        for key, rows in local_rows:
            ex, mkt, sym, tf = key
            cold = set()
            cp = ARC / 'candle_heads' / ex / mkt / sym / tf
            for p in cp.glob('*.parquet'):
                tb = pq.read_table(p, columns=['open_time']).to_pydict()
                cold.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
            hot = {int(r[0]) for r in con.execute(
                'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
                (ex, mkt, sym, tf, FIXED_NOW))}
            have = cold | hot
            miss = [t for t in sorted(rows) if t not in have]
            for t in miss:
                r = rows[t]
                w.writerow([ex, mkt, sym, tf, t, r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10],
                            'binance.archive.klines'])
            total += len(miss)
            perkey[f'{ex}/{mkt}/{sym}/{tf}'] = len(miss)
            localmiss[f'{ex}/{mkt}/{sym}/{tf}'] = miss
    con.close()
    (OUT / '补缺_本地已补键.json').write_text(json.dumps(localmiss, ensure_ascii=False))
    log(f'candle local missing rows={total} -> {out.name}')
    return total, perkey


# ---------------- Part 3/4: API 补齐 ----------------
def binance_klines(sym, tf, start_ms, end_ms):
    iv = TF_MS[tf]
    out = []
    cur = start_ms
    while cur <= end_ms:
        url = f'{BIN_API}/fapi/v1/klines?symbol={sym}&interval={tf}&startTime={cur}&endTime={end_ms}&limit=1500'
        d = http_json(url)
        if not d:
            break
        for k in d:
            if start_ms <= int(k[0]) <= end_ms:
                out.append(k)
        last = int(d[-1][0])
        if last <= cur:
            break
        cur = last + iv
        time.sleep(0.12)
    return out


def okx_candles(inst, tf, start_ms, end_ms):
    bar = OKX_BAR.get(tf)
    if not bar:
        return []
    out = {}
    after = end_ms + TF_MS[tf]  # OKX after = 返回更早的数据
    while True:
        url = f'https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar={bar}&after={after}&limit=100'
        d = http_json(url)
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
        time.sleep(0.15)
    return out


def part_api(need_api):
    import sqlite3
    con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    lm = json.loads((OUT / '补缺_本地已补键.json').read_text()) if (OUT / '补缺_本地已补键.json').exists() else {}
    done = {k: set(v) for k, v in lm.items()}
    out = OUT / '补缺_K线_api_import.csv'
    cols = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume', 'source', 'api']
    total = 0
    fails = []
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(cols)
        for i, (key, months, why) in enumerate(need_api, 1):
            ex, mkt, sym, tf = key
            ms = sorted(months)
            start = int(datetime.datetime.strptime(ms[0] + '-01', '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
            end = int((datetime.datetime.strptime(ms[-1] + '-01', '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(days=32)).timestamp() * 1000) - 1
            end = min(end, FIXED_NOW)
            try:
                if ex == 'binance':
                    raw = binance_klines(sym, tf, start, end)
                    recs = {int(k[0]): k for k in raw}
                    src = 'binance.fapi.klines'
                else:
                    recs = okx_candles(sym, tf, start, end)
                    src = 'okx.api.history-candles'
            except Exception as e:
                fails.append((f'{ex}/{sym}/{tf}', repr(e))); log(f'API FAIL {ex}/{sym}/{tf} {e!r}'); continue
            cold = set()
            for p in (ARC / 'candle_heads' / ex / mkt / sym / tf).glob('*.parquet'):
                tb = pq.read_table(p, columns=['open_time']).to_pydict()
                cold.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
            hot = {int(r[0]) for r in con.execute(
                'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
                (ex, mkt, sym, tf, FIXED_NOW))}
            have = cold | hot
            key_s = f'{ex}/{mkt}/{sym}/{tf}'
            skip = done.get(key_s, set())
            miss = [t for t in sorted(recs) if t not in have and t not in skip]
            for t in miss:
                k = recs[t]
                if ex == 'binance':
                    w.writerow([ex, mkt, sym, tf, t, k[1], k[2], k[3], k[4], k[5], k[6], k[7], k[8], k[9], k[10], src, ''])
                else:
                    # okx: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
                    w.writerow([ex, mkt, sym, tf, t, k[1], k[2], k[3], k[4], k[5], '', k[7], '', '', '', src, ''])
            total += len(miss)
            if i % 15 == 0:
                log(f'candle-api {i}/{len(need_api)} rows={total} fails={len(fails)}')
    con.close()
    log(f'candle api missing rows={total} fails={len(fails)} -> {out.name}')
    return total, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--parts', default='funding,local,api')
    a = ap.parse_args()
    parts = set(a.parts.split(','))
    man = {'generated_at': STAMP, 'fixed_now_ms': FIXED_NOW, 'policy': '只读源；结果写本目录；不写生产库'}
    if 'funding' in parts:
        man.update(part_funding())
    if 'local' in parts or 'api' in parts:
        aff, reason = affected_map()
        man['affected_keys'] = len(aff)
        if 'local' in parts:
            local_rows, need_api, stats = part_candle_local(aff, reason)
            nloc, perkey = candle_diff_and_write(local_rows)
            man['candle_local_missing_rows'] = nloc
            man['need_api_keys'] = len(need_api)
            (OUT / '补缺_需API键.json').write_text(json.dumps(
                [{'key': '/'.join(k), 'months': sorted(m), 'why': w} for k, m, w in need_api], ensure_ascii=False, indent=2))
            if 'api' not in parts:
                man['api'] = 'skipped'
        if 'api' in parts:
            need = json.loads((OUT / '补缺_需API键.json').read_text()) if (OUT / '补缺_需API键.json').exists() else []
            napi, fails = part_api([(tuple(x['key'].split('/')), set(x['months']), x['why']) for x in need])
            man['candle_api_missing_rows'] = napi
            man['api_failures'] = fails
    (OUT / '补缺清单.json').write_text(json.dumps(man, ensure_ascii=False, indent=2))
    log('DONE ' + json.dumps({k: v for k, v in man.items() if not isinstance(v, (list, dict))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
