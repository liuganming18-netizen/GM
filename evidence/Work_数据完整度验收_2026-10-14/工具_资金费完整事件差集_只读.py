#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：资金费完整事件差集（覆盖短空档，不做固定8h/>24h阈值）。

源 = 冻结池幸存月ZIP内的真实结算事件（calc_time, last_funding_rate）
对比 = 冷档 parquet funding + 热库 funding_history
规则：不取整、不平移、不补0；同结算秒不同毫秒单独记为时间偏移；
      校验未通过的源事件不进入"已证实缺失"，另列 unverified。
产物只写交付目录；源库 mode=ro + query_only。
"""
import collections, csv, datetime, hashlib, io, json, sqlite3, sys, time, zipfile
from pathlib import Path
import pyarrow.parquet as pq

FIXED_NOW = 1791980404548  # Codex 清单基线.json 绑定截点 2026-10-14 20:20:04 +08
SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
BE = Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现')
RAWF = BE / '历史下载/raw/funding'
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
OUT.mkdir(parents=True, exist_ok=True)
STAMP = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
EXCLUDED = {'BTCDOMUSDT', 'ALLUSDT', 'DEFIUSDT'}


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def rnd(x):
    try:
        return round(float(x), 12)
    except Exception:
        return None


def checksum(p):
    q = Path(str(p) + '.CHECKSUM')
    if not q.exists():
        return 'checksum_file_absent'
    try:
        want = q.read_text().split()[0].lower()
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        return 'MATCH' if got == want else 'MISMATCH'
    except Exception as e:
        return 'ERROR:' + repr(e)


def read_zip_events(p):
    """返回 [(calc_time_ms, rate_str), ...]"""
    out = []
    with zipfile.ZipFile(p) as z:
        n = next(x for x in z.namelist() if x.endswith('.csv'))
        with z.open(n) as src:
            for row in csv.reader(io.TextIOWrapper(src, encoding='utf-8-sig', newline='')):
                if not row or row[0] == 'calc_time':
                    continue
                try:
                    out.append((int(row[0]), row[2] if len(row) > 2 else (row[1] if len(row) > 1 else '')))
                except Exception:
                    continue
    return out


scope = json.loads((BE / '历史下载/标的池/scope_crypto.json').read_text())
binance_syms = sorted({i['symbol'] for i in scope['binance_items']} - EXCLUDED)
okx_syms = sorted(set(scope.get('okx_symbols', [])))

con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
con.execute('PRAGMA query_only=ON')
con.execute('PRAGMA cache_size=-16384')

miss_f = (OUT / '资金费逐事件缺失.csv').open('w', encoding='utf-8-sig', newline='')
miss_w = csv.DictWriter(miss_f, fieldnames=['symbol', 'funding_time_ms', 'funding_time_utc', 'funding_rate',
                                            'source_zip', 'checksum_status', 'month', 'in_cold', 'in_hot',
                                            'evidence_level'])
miss_w.writeheader()
conf_f = (OUT / '资金费时间与率冲突.csv').open('w', encoding='utf-8-sig', newline='')
conf_w = csv.DictWriter(conf_f, fieldnames=['symbol', 'conflict_kind', 'source_ms', 'source_utc', 'other_ms',
                                            'other_utc', 'ms_delta', 'source_rate', 'other_rate', 'where',
                                            'source_zip', 'checksum_status', 'note'])
conf_w.writeheader()
sum_rows = []
counts = collections.Counter()
prog = time.time()
for i, sym in enumerate(binance_syms, 1):
    zips = sorted((RAWF / sym).glob('*.zip')) if (RAWF / sym).is_dir() else []
    src = {}
    prov = {}
    cs_ok = cs_bad = cs_absent = 0
    for z in zips:
        cs = checksum(z)
        if cs == 'MATCH':
            cs_ok += 1
        elif cs == 'MISMATCH':
            cs_bad += 1
        else:
            cs_absent += 1
        for t, r in read_zip_events(z):
            if t > FIXED_NOW:
                continue
            if t in src and rnd(src[t]) != rnd(r):
                conf_w.writerow(dict(symbol=sym, conflict_kind='source_internal_same_ms_diff_rate',
                                     source_ms=t, source_utc=iso(t), other_ms=t, other_utc=iso(t), ms_delta=0,
                                     source_rate=src[t], other_rate=r, where='source_zip', source_zip=str(z),
                                     checksum_status=cs, note='同一calc_time在不同源件率不一致'))
                counts['source_internal_conflicts'] += 1
            src[t] = r
            prov[t] = (str(z), cs)
    # cold
    cold = {}
    cp = ARC / 'funding/binance/um' / sym / 'funding.parquet'
    if cp.exists():
        tb = pq.read_table(cp, columns=['funding_time', 'funding_rate']).to_pydict()
        for t, r in zip(tb['funding_time'], tb['funding_rate']):
            if t is not None and t <= FIXED_NOW:
                cold[int(t)] = r
    # hot
    hot = {}
    for t, r in con.execute('SELECT funding_time, funding_rate FROM funding_history WHERE exchange=? AND market=? '
                            'AND symbol=? AND funding_time<=?', ('binance', 'um', sym, FIXED_NOW)):
        hot[int(t)] = r
    # cold+hot union, detect cold/hot rate conflict
    coldhot = dict(cold)
    for t, r in hot.items():
        if t in coldhot and rnd(coldhot[t]) != rnd(r):
            conf_w.writerow(dict(symbol=sym, conflict_kind='cold_hot_same_ms_diff_rate', source_ms=t,
                                 source_utc=iso(t), other_ms=t, other_utc=iso(t), ms_delta=0,
                                 source_rate=coldhot[t], other_rate=r, where='cold_vs_hot', source_zip='',
                                 checksum_status='', note='冷档与热库同一时刻率不一致'))
            counts['cold_hot_conflicts'] += 1
        coldhot[t] = r
    sec_index = collections.defaultdict(list)
    for t in coldhot:
        sec_index[t // 1000].append(t)

    n_missing = n_shift = n_rateconf = n_unverified = 0
    for t in sorted(src):
        r = src[t]
        z, cs = prov[t]
        month = Path(z).stem.split('-')[-2] + '-' + Path(z).stem.split('-')[-1] if '-' in Path(z).stem else ''
        if t in coldhot:
            if rnd(coldhot[t]) != rnd(r):
                conf_w.writerow(dict(symbol=sym, conflict_kind='matched_same_ms_diff_rate', source_ms=t,
                                     source_utc=iso(t), other_ms=t, other_utc=iso(t), ms_delta=0, source_rate=r,
                                     other_rate=coldhot[t], where='cold+hot', source_zip=z, checksum_status=cs,
                                     note='同一毫秒率不一致'))
                counts['matched_rate_conflicts'] += 1
                n_rateconf += 1
        elif t // 1000 in sec_index:
            other = sec_index[t // 1000][0]
            conf_w.writerow(dict(symbol=sym, conflict_kind='same_second_ms_shift', source_ms=t, source_utc=iso(t),
                                 other_ms=other, other_utc=iso(other), ms_delta=other - t, source_rate=r,
                                 other_rate=coldhot[other], where='cold+hot', source_zip=z, checksum_status=cs,
                                 note='同结算秒但毫秒不同，未证明等价，不平移'))
            counts['same_second_ms_shift'] += 1
            n_shift += 1
        else:
            lvl = 'proven_missing' if cs == 'MATCH' else 'unverified_source'
            miss_w.writerow(dict(symbol=sym, funding_time_ms=t, funding_time_utc=iso(t), funding_rate=r,
                                 source_zip=z, checksum_status=cs, month=month, in_cold=('Y' if t in cold else 'N'),
                                 in_hot=('Y' if t in hot else 'N'), evidence_level=lvl))
            if cs == 'MATCH':
                counts['proven_missing_events'] += 1
                n_missing += 1
            else:
                counts['unverified_missing_events'] += 1
                n_unverified += 1
    extra = len(set(coldhot) - set(src))
    sum_rows.append(dict(symbol=sym, source_events=len(src), cold_events=len(cold), hot_events=len(hot),
                         coldhot_union=len(coldhot), matched_exact=len(set(src) & set(coldhot)),
                         missing_events=n_missing, unverified_missing=n_unverified, same_second_ms_shift=n_shift,
                         rate_conflicts=n_rateconf, extra_in_coldhot=extra, zips=len(zips), cs_match=cs_ok,
                         cs_mismatch=cs_bad, cs_absent=cs_absent,
                         first_src_utc=iso(min(src)) if src else '', last_src_utc=iso(max(src)) if src else ''))
    if n_missing:
        counts['symbols_with_proven_missing'] += 1
    if time.time() - prog > 20:
        print(f'[{i}/{len(binance_syms)}] {sym} missing={n_missing} shift={n_shift} conf={n_rateconf} '
              f'cum_proven={counts["proven_missing_events"]}', flush=True)
        prog = time.time()
    if i % 50 == 0:
        miss_f.flush(); conf_f.flush()

with (OUT / '资金费逐币差集汇总.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(sum_rows[0]))
    w.writeheader(); w.writerows(sum_rows)

# OKX：无本地月ZIP原件
okx_summary = []
for sym in okx_syms:
    hotn = con.execute('SELECT COUNT(*) FROM funding_history WHERE exchange=? AND market=? AND symbol=? AND '
                       'funding_time<=?', ('okx', 'swap', sym, FIXED_NOW)).fetchone()[0]
    cp = ARC / 'funding/okx/swap' / sym / 'funding.parquet'
    coldn = 0
    if cp.exists():
        coldn = pq.read_table(cp, columns=['funding_time']).num_rows
    okx_summary.append(dict(symbol=sym, source='none_local_monthly_zip', cold_events=coldn, hot_events=hotn))
con.close(); miss_f.close(); conf_f.close()

summary = dict(
    generated_at=STAMP, fixed_now_ms=FIXED_NOW, fixed_now_utc=iso(FIXED_NOW),
    scope=dict(binance=len(binance_syms), okx=len(okx_syms), excluded=sorted(EXCLUDED)),
    totals=dict(counts), binance_symbols_with_source=sum(1 for r in sum_rows if r['zips'] > 0),
    binance_symbols_without_source=sum(1 for r in sum_rows if r['zips'] == 0),
    dedup_rule='唯一资金费事件=(symbol, funding_time 精确到毫秒)；同结算秒不同毫秒不合并，单列为时间偏移；'
               '同毫秒率不一致单列为冲突；不平移、不取整、不补零。',
    okx_no_local_source=okx_summary,
    notes=['已证实缺失=源件校验MATCH且冷+热均无该毫秒；校验未过单列unverified',
           'extra_in_coldhot=冷/热有而源件无(源件覆盖期之外)，不是丢失',
           '仅币安有本地月度资金费ZIP原件；OKX无本地原件，只能记无原件'],
)
(OUT / '资金费事件差集汇总.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print('FUNDING_DIFF_DONE', json.dumps(dict(counts), ensure_ascii=False), flush=True)
