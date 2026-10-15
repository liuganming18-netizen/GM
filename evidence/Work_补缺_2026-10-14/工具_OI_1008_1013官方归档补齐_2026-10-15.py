#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用官方归档（data.binance.vision daily/metrics）复核并补齐 OI 的 2026-10-08 ~ 10-13 窗口。

为什么：本地 raw_oi_metrics 只到 2026-10-07；10-08 起库内全部来自采集器
（binance.fapi.openInterestHist）。官方归档对 10-08~10-13 是**存在**的，且是权威源。

关键口径（已逐槽实测）：归档[t] ≡ 采集器[t+5min]，即归档 ts 为桶起点、采集器 ts 为桶终点。
故本工具：
  · 期望槽 = { 归档 ts + 5min }（对齐库内采集器口径），只补库内真正没有的槽；
  · 同时输出「口径归一化映射」说明（把采集器行 ts 统一 -5min 即可与归档/历史一致）。

只读源库；结果写本目录；不写生产库。
"""
import collections, csv, datetime, io, json, sqlite3, urllib.error, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DB = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
RAW = Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics')
CACHE = Path('/tmp/binance_oi_10_08')
BASE = 'https://data.binance.vision/data/futures/um/daily/metrics'
IV = 300000
COLS = ['exchange', 'market', 'symbol', 'period', 'ts', 'open_interest', 'open_interest_value',
        'oi_ccy', 'oi_usd', 'source', 'fetched_at']
DAYS = ['2026-10-08', '2026-10-09', '2026-10-10', '2026-10-11', '2026-10-12', '2026-10-13']
LO = int(datetime.datetime(2026, 10, 8, tzinfo=datetime.timezone.utc).timestamp() * 1000)
HI = int(datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def fetch(sym, d):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f'{sym}-{d}.zip'
    if not p.exists():
        last = None
        for i in range(4):
            try:
                req = urllib.request.Request(f'{BASE}/{sym}/{sym}-metrics-{d}.zip',
                                             headers={'User-Agent': 'wb-audit/1.0'})
                with urllib.request.urlopen(req, timeout=60) as r:
                    p.write_bytes(r.read())
                last = None
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return ('404', {})
                last = f'HTTP {e.code}'
            except Exception as e:
                last = repr(e)
            import time
            time.sleep(0.8 + 1.2 * i)
        if last is not None:
            return (last, {})
    try:
        zf = zipfile.ZipFile(p)
        raw = zf.read(zf.namelist()[0]).decode('utf-8-sig', 'replace')
    except Exception as e:
        return (f'zip_err {e!r}', {})
    out = {}
    for row in csv.reader(io.StringIO(raw)):
        if not row or row[0] == 'create_time':
            continue
        ts = int(datetime.datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
                 .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        out[ts] = row
    return ('ok', out)


def main():
    syms = sorted(p.name for p in RAW.iterdir() if p.is_dir())
    log(f'币数 {len(syms)}，日包 {len(DAYS)} 天 → {len(syms) * len(DAYS)} 个任务')

    jobs = [(s, d) for s in syms for d in DAYS]
    res = {}
    with ThreadPoolExecutor(max_workers=14) as ex:
        for k, v in zip(jobs, ex.map(lambda x: fetch(*x), jobs)):
            res[k] = v
    ok = sum(1 for st, _ in res.values() if st == 'ok')
    n404 = sum(1 for st, _ in res.values() if st == '404')
    log(f'下载完成 ok={ok} 404={n404} 其他={len(res) - ok - n404}')

    # 归档 ts 汇总（仅 10-08~10-13 窗口）
    arc = collections.defaultdict(dict)
    for (s, d), (st, m) in res.items():
        if st != 'ok':
            continue
        for ts, row in m.items():
            if LO <= ts < HI:
                arc[s][ts] = row

    con = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=60)
    con.execute('PRAGMA query_only=ON')
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    patch = []
    per_sym = []
    for s in syms:
        a = arc.get(s, {})
        have = {r[0] for r in con.execute(
            "SELECT ts FROM oi_history WHERE exchange='binance' AND symbol=? AND period='5m' AND ts>=? AND ts<?",
            (s, LO, HI))}
        # 期望槽（对齐采集器口径 = 归档 ts + 5min），且必须落在本窗口 [LO, HI) 内
        # 否则 10-13T23:55 会推出 10-14T00:00，而 have 按 ts<HI 取，会造出边界假阳性
        want = {t + IV for t in a if LO <= t + IV < HI}
        miss = sorted(want - have)
        extra = sorted(have - want)
        per_sym.append(dict(symbol=s, archive_rows=len(a), db_rows=len(have),
                            missing_vs_archive=len(miss), db_extra_vs_archive=len(extra),
                            archive_days_ok=sum(1 for d in DAYS if res.get((s, d), ('', {}))[0] == 'ok'),
                            archive_days_404=sum(1 for d in DAYS if res.get((s, d), ('', {}))[0] == '404')))
        for t in miss:
            row = a[t - IV]
            patch.append(dict(exchange='binance', market='um', symbol=s, period='5m', ts=t,
                              open_interest=row[2], open_interest_value=row[3],
                              oi_ccy=row[2], oi_usd=row[3],
                              source='binance.vision.metrics.daily', fetched_at=now))
    con.close()

    with (OUT / '补缺_OI_1008_1013_官方归档_import.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in sorted(patch, key=lambda x: (x['symbol'], x['ts'])):
            w.writerow(r)
    with (OUT / '补缺_OI_1008_1013_逐币核对_2026-10-15.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(per_sym[0].keys()))
        w.writeheader()
        w.writerows(per_sym)

    uniq = len({(r['symbol'], r['ts']) for r in patch})
    stat = dict(generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                policy='只读源；写本目录；不写生产库',
                source='binance.vision.metrics.daily',
                window_utc='2026-10-08T00:00 ~ 2026-10-13T23:55',
                symbols=len(syms), downloads=len(res), download_ok=ok, download_404=n404,
                archive_rows_total=sum(len(v) for v in arc.values()),
                db_rows_total=sum(x['db_rows'] for x in per_sym),
                missing_total=len(patch), unique=uniq, duplicates=len(patch) - uniq,
                symbols_with_missing=sum(1 for x in per_sym if x['missing_vs_archive']),
                normalization_note='归档[t] ≡ 采集器[t+5min]；期望槽已按归档 ts+5min 对齐库内采集器口径',
                per_symbol=per_sym)
    (OUT / '补缺_OI_1008_1013_清单_2026-10-15.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    log(f'DONE 补 {len(patch)} 行 / 涉及 {stat["symbols_with_missing"]} 币')
    print(json.dumps({k: v for k, v in stat.items() if k != 'per_symbol'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
