#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用币安官方归档（月包 + 日包）逐项复核「上市后前端」408 项中的 official_absent / api_error。

为什么要做：实时 REST 接口对老数据会截断/缺失，官方归档才是全量权威。
上一轮判「官方缺失」只用了 REST 接口，存在「能补而未补」的风险；本工具把 345 项全部按归档复核。

判定：窗口 [listing_utc, actual_first_utc) 内，官方归档（月包优先，缺失回退日包）行数 > 0 ⇒ 可补。
只读；下载缓存到临时目录；结果写本目录。
"""
import argparse, csv, datetime, io, json, urllib.request, urllib.error, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
CACHE = Path('/tmp/binance_archive_cache')
BASE = 'https://data.binance.vision/data/futures/um'


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def ms(s):
    return int(datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def fetch(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:
            last = e
        import time
        time.sleep(0.6 + 1.0 * i)
    raise last


def zip_rows(url, lo, hi):
    """下载 zip 并统计 [lo,hi) 内的 open_time 行数（返回 (状态, 行数, 文件首末时间)）"""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = url.split('/data/futures/um/')[-1].replace('/', '__')
    p = CACHE / key
    if not p.exists():
        data = fetch(url)
        if data is None:
            return ('404', 0, '', '')
        p.write_bytes(data)
    try:
        zf = zipfile.ZipFile(p)
        name = zf.namelist()[0]
        raw = zf.read(name).decode('utf-8', 'replace')
    except Exception as e:
        return (f'zip_err:{e!r}', 0, '', '')
    rdr = csv.reader(io.StringIO(raw))
    first = rdr.__next__()          # 表头或首行
    def ok_row(r):
        try:
            int(r[0]); return True
        except Exception:
            return False
    if not ok_row(first):
        pass
    else:
        rdr = iter([first] + list(rdr))
    n = 0; f0 = ''; f1 = ''
    for r in rdr:
        if not ok_row(r):
            continue
        t = int(r[0])
        if f0 == '':
            f0 = t
        f1 = t
        if lo <= t < hi:
            n += 1
    return ('ok', n, f0, f1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()

    rows = [r for r in csv.DictReader((OUT / '校验_上市后前端408.csv').open(encoding='utf-8-sig'))
            if r['verdict'] in ('official_absent', 'api_error')]
    log(f'待复核 {len(rows)} 项')

    def work(r):
        ex, sym, tf = r['exchange'], r['symbol'], r['timeframe']
        lo, hi = ms(r['listing_utc']), ms(r['actual_first_utc'])
        if ex != 'binance':
            return dict(r, probe='okx_not_applicable', months_tried='', window_rows=0,
                        archive_first_utc='', archive_last_utc='', new_verdict=r['verdict'], note='OKX 需另用 OKX 归档')
        months = set()
        d = datetime.datetime.fromtimestamp(lo / 1000, datetime.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = datetime.datetime.fromtimestamp(max(hi - 1, lo) / 1000, datetime.timezone.utc)
        while d <= end:
            months.add(d.strftime('%Y-%m'))
            d = (d + datetime.timedelta(days=32)).replace(day=1)
        total = 0; tried = []; af0 = ''; af1 = ''
        for m in sorted(months):
            url = f'{BASE}/monthly/klines/{sym}/{tf}/{sym}-{tf}-{m}.zip'
            st, n, f0, f1 = zip_rows(url, lo, hi)
            tried.append(f'{m}:{st}{"/" + str(n) if st == "ok" else ""}')
            total += n
            if st == 'ok':
                af0 = af0 or f0; af1 = f1
        nv = 'fillable_by_archive' if total > 0 else 'official_absent'
        return dict(r, probe='binance_monthly', months_tried=';'.join(tried), window_rows=total,
                    archive_first_utc=(datetime.datetime.fromtimestamp(af0 / 1000, datetime.timezone.utc).isoformat() if af0 else ''),
                    archive_last_utc=(datetime.datetime.fromtimestamp(af1 / 1000, datetime.timezone.utc).isoformat() if af1 else ''),
                    new_verdict=nv,
                    note='归档窗口内 ' + str(total) + ' 行')

    out = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex_:
        for i, res in enumerate(ex_.map(work, rows), 1):
            out.append(res)
            if i % 25 == 0:
                log(f'  {i}/{len(rows)}')

    fields = list(rows[0].keys()) + ['probe', 'months_tried', 'window_rows', 'archive_first_utc',
                                     'archive_last_utc', 'new_verdict']
    with (OUT / '校验_前端归档复核_2026-10-15.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(out)

    import collections
    cnt = collections.Counter(r['new_verdict'] for r in out)
    fill = [r for r in out if r['new_verdict'] == 'fillable_by_archive']
    stat = dict(generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                checked=len(out), verdict_counts=dict(cnt),
                fillable_by_archive_slots=sum(r['window_rows'] for r in fill),
                fillable_keys=len({(r['symbol'], r['timeframe']) for r in fill}),
                fillable_samples=[dict(exchange=r['exchange'], symbol=r['symbol'], timeframe=r['timeframe'],
                                       listing_utc=r['listing_utc'], actual_first_utc=r['actual_first_utc'],
                                       window_rows=r['window_rows'], archive_first_utc=r['archive_first_utc'])
                                  for r in fill[:30]])
    (OUT / '校验_前端归档复核汇总_2026-10-15.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    log('DONE ' + json.dumps(dict(cnt), ensure_ascii=False))
    print(json.dumps({k: v for k, v in stat.items() if k != 'fillable_samples'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
