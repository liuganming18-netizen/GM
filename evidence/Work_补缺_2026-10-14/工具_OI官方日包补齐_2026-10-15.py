#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OI「源包无事件」多槽段 → 用官方归档（S3 全量清单 + 日包）逐日复核，找出真正可补的槽。

背景：上一轮的 source_zip_has_no_event_in_gap 只用**本地** raw_oi_metrics 判定；
跨多天的段里，只要有一天本地有包就判「无事件」，其余天从未核对。
本工具对每个多槽段：列出官方归档在该段日期范围内**实际存在**的天，逐天下载并抽取缺口内事件。

只读；缓存到 /tmp；结果写本目录。
"""
import collections, csv, datetime, io, json, re, urllib.error, urllib.parse, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
CACHE = Path('/tmp/binance_oi_archive')
S3 = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
DL = 'https://data.binance.vision/data/futures/um/daily/metrics'
COLS = ['exchange', 'market', 'symbol', 'period', 'ts', 'open_interest', 'open_interest_value',
        'oi_ccy', 'oi_usd', 'source', 'fetched_at']


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def list_days(sym):
    keys = []; marker = ''
    while True:
        q = urllib.parse.urlencode({'delimiter': '/', 'prefix': f'data/futures/um/daily/metrics/{sym}/', 'marker': marker})
        req = urllib.request.Request(S3 + '?' + q, headers={'User-Agent': 'wb-audit/1.0'})
        xml = urllib.request.urlopen(req, timeout=60).read().decode()
        keys += re.findall(r'<Key>([^<]+)</Key>', xml)
        if '<IsTruncated>true</IsTruncated>' not in xml:
            break
        m = re.search(r'<NextMarker>([^<]*)</NextMarker>', xml)
        if not m or not m.group(1):
            break
        marker = m.group(1)
    return {k[-14:-4] for k in keys if k.endswith('.zip')}


def fetch_day(sym, d):
    """下载某天日包并返回 {ts_ms: row}"""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f'{sym}-{d}.zip'
    if not p.exists():
        url = f'{DL}/{sym}/{sym}-metrics-{d}.zip'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                p.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            return ('404' if e.code == 404 else f'HTTP {e.code}', {})
        except Exception as e:
            return (repr(e), {})
    try:
        zf = zipfile.ZipFile(p)
        raw = zf.read(zf.namelist()[0]).decode('utf-8-sig', 'replace')
    except Exception as e:
        return (f'zip_err {e!r}', {})
    out = {}
    for row in csv.reader(io.StringIO(raw)):
        if not row or row[0] == 'create_time':
            continue
        ts = int(datetime.datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        out[ts] = row
    return ('ok', out)


def main():
    big = json.loads(Path('/tmp/oi_big_segs.json').read_text())
    syms = sorted({s['sym'] for s in big})
    log(f'多槽段 {len(big)} / 币 {len(syms)}')
    with ThreadPoolExecutor(max_workers=8) as ex:
        offlist = dict(zip(syms, ex.map(list_days, syms)))
    json.dump({k: sorted(v) for k, v in offlist.items()}, open(OUT / '补缺_OI官方归档可用天_2026-10-15.json', 'w'))

    # 汇总每天要下载的 (sym, day)
    todo = set()
    per_seg = []
    for s in big:
        off = [d for d in s['miss'] if d in offlist[s['sym']]]
        per_seg.append((s, off))
        for d in off:
            todo.add((s['sym'], d))
    log(f'需下载官方日包 {len(todo)} 个（{len(per_seg)} 段中 {sum(1 for _, o in per_seg if o)} 段有官方天）')

    fetched = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for k, v in zip(todo, ex.map(lambda x: fetch_day(*x), sorted(todo))):
            fetched[k] = v
    ok = sum(1 for st, _ in fetched.values() if st == 'ok')
    log(f'下载完成：ok={ok} 其他={len(fetched) - ok}')

    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    rows_out = []
    detail = []
    for s, off in per_seg:
        lo = int(datetime.datetime.fromisoformat(s['from_utc']).timestamp() * 1000)
        hi = int(datetime.datetime.fromisoformat(s['to_utc']).timestamp() * 1000)
        found = {}
        for d in off:
            st, m = fetched.get((s['sym'], d), ('miss', {}))
            for ts, row in m.items():
                if lo <= ts <= hi:
                    found[ts] = row
        if found:
            detail.append(dict(symbol=s['sym'], from_utc=s['from_utc'], to_utc=s['to_utc'],
                               slots=s['slots'], official_days=len(off), events_in_gap=len(found)))
        for ts, row in sorted(found.items()):
            rows_out.append(dict(exchange='binance', market='um', symbol=s['sym'], period='5m', ts=ts,
                                 open_interest=row[2], open_interest_value=row[3],
                                 oi_ccy=row[2], oi_usd=row[3],
                                 source='binance.vision.metrics.daily', fetched_at=now))

    with (OUT / '补缺_OI_官方日包_import.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in sorted(rows_out, key=lambda x: (x['symbol'], x['ts'])):
            w.writerow(r)

    uniq = len({(r['symbol'], r['ts']) for r in rows_out})
    stat = dict(generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                policy='只读源；写本目录；不写生产库',
                source='binance.vision.metrics.daily（官方归档日包）',
                big_segments=len(big), symbols=len(syms),
                official_days_in_span=sum(len(o) for _, o in per_seg),
                downloaded=len(fetched), download_ok=ok,
                segments_with_events=len(detail), rows=len(rows_out), unique=uniq,
                duplicates=len(rows_out) - uniq,
                slots_still_absent=sum(s['slots'] for s, o in per_seg) - len(rows_out),
                detail=detail)
    (OUT / '补缺_OI_官方日包清单_2026-10-15.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    log(f'DONE 官方日包补 {len(rows_out)} 行 / {len(detail)} 段')


if __name__ == '__main__':
    main()
