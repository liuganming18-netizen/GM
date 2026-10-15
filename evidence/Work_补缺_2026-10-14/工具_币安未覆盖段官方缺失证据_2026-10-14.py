#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安主线未覆盖段（48段/3,625槽）：逐段用三源取证是否"官方确实没有"。
源A: Binance API fapi/v1/klines 窗口行数
源B: 官方月包 monthly/klines 窗口行数（权威）
源C: 官方日包 daily/klines 边界日窗口行数（抽查）
判定: 任一源窗口内有行 -> fillable_real_gap；三源窗口内均无行 -> official_absent
只读：仅下载到临时目录，产出证据 CSV/JSON 到本目录。"""
import csv, io, json, datetime, time, zipfile, urllib.request, collections
from pathlib import Path

OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
TMP = Path('/tmp/wb_gap_evi'); TMP.mkdir(parents=True, exist_ok=True)
VISION = 'https://data.binance.vision'
BIN_API = 'https://fapi.binance.com'
TF_MS = {'15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'wb-audit/1.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                return {'_error': repr(e)}
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
            if i == tries - 1:
                return None
            time.sleep(1.2 * (i + 1))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.2 * (i + 1))


def zip_ts_in_window(zbytes, lo, hi):
    """返回 (窗口内行数, 全文件最早ts, 全文件最晚ts, 全文件行数)"""
    if not zbytes:
        return 0, None, None, 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(zbytes))
    except Exception:
        return 0, None, None, 0
    names = [n for n in zf.namelist() if n.endswith('.csv')]
    if not names:
        return 0, None, None, 0
    inw = 0; first = None; last = None; total = 0
    with zf.open(names[0]) as s:
        for row in csv.reader(io.TextIOWrapper(s, encoding='utf-8-sig', newline='')):
            if not row or row[0] == 'open_time':
                continue
            try:
                ts = int(row[0])
            except ValueError:
                continue
            total += 1
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
            if lo <= ts <= hi:
                inw += 1
    return inw, first, last, total


def api_window(sym, tf, lo, hi):
    iv = TF_MS[tf]
    url = f'{BIN_API}/fapi/v1/klines?symbol={sym}&interval={tf}&startTime={lo}&endTime={hi}&limit=1500'
    d = get_json(url)
    if isinstance(d, dict):
        return None, d.get('_error')
    n = sum(1 for k in d if lo <= int(k[0]) <= hi)
    return n, None


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat() if ms else ''


def main():
    rows = list(csv.DictReader((OUT / '校验_主线缺口覆盖.csv').open(encoding='utf-8-sig')))
    segs = [r for r in rows if r['exchange'] == 'binance' and int(r['still_uncovered']) > 0]
    log(f'币安未覆盖段 {len(segs)} 段')
    out = []
    for i, r in enumerate(segs, 1):
        sym, tf = r['symbol'], r['period']
        lo = int(datetime.datetime.fromisoformat(r['from_utc']).timestamp() * 1000)
        hi = int(datetime.datetime.fromisoformat(r['to_utc']).timestamp() * 1000)
        lo_d = datetime.datetime.fromtimestamp(lo / 1000, datetime.timezone.utc)
        hi_d = datetime.datetime.fromtimestamp(hi / 1000, datetime.timezone.utc)

        # 源A: API
        api_n, api_err = api_window(sym, tf, lo, hi)

        # 源B: 月包（覆盖 from 月 ~ to 月）
        m_inw = 0; m_exist = 0; m_first = None; m_last = None
        m = lo_d.replace(day=1)
        while m <= hi_d:
            u = f'{VISION}/data/futures/um/monthly/klines/{sym}/{tf}/{sym}-{tf}-{m.strftime("%Y-%m")}.zip'
            zb = get_bytes(u)
            if zb is not None:
                m_exist += 1
                n, f, l, _t = zip_ts_in_window(zb, lo, hi)
                m_inw += n
                if f is not None:
                    m_first = f if m_first is None else min(m_first, f)
                    m_last = l if m_last is None else max(m_last, l)
            m = (m.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)

        # 源C: 日包 边界日（首日+末日）
        d_inw = 0; d_exist = 0; d_probe = []
        for dd in {lo_d.date(), hi_d.date()}:
            u = f'{VISION}/data/futures/um/daily/klines/{sym}/{tf}/{sym}-{tf}-{dd.isoformat()}.zip'
            zb = get_bytes(u)
            if zb is not None:
                d_exist += 1
                n, f, l, _t = zip_ts_in_window(zb, lo, hi)
                d_inw += n
                d_probe.append(f'{dd}: exist rows_in_window={n} file_first={iso(f)}')
            else:
                d_probe.append(f'{dd}: 404')

        if api_err:
            verdict = 'api_error'; note = api_err
        elif api_n and api_n > 0:
            verdict = 'fillable_real_gap'; note = f'API窗口内{api_n}行'
        elif m_inw > 0:
            verdict = 'fillable_real_gap'; note = f'月包窗口内{m_inw}行'
        elif d_inw > 0:
            verdict = 'fillable_real_gap'; note = f'日包窗口内{d_inw}行'
        else:
            verdict = 'official_absent'; note = '三源窗口内均0行'

        out.append(dict(symbol=sym, period=tf, from_utc=r['from_utc'], to_utc=r['to_utc'],
                        expected_slots=r['expected_slots'],
                        api_rows=('' if api_n is None else api_n), api_err=(api_err or ''),
                        monthly_zip_exist=m_exist, monthly_rows_in_window=m_inw,
                        monthly_file_first=iso(m_first), monthly_file_last=iso(m_last),
                        daily_exist=d_exist, daily_rows_in_window=d_inw,
                        daily_probe=' | '.join(d_probe), verdict=verdict, note=note))
        if i % 5 == 0:
            log(f'  {i}/{len(segs)}')
    cols = list(out[0])
    with (OUT / '币安未覆盖段官方缺失证据.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
    summ = collections.Counter(x['verdict'] for x in out)
    (OUT / '币安未覆盖段官方缺失证据汇总.json').write_text(json.dumps(
        {'total_segments': len(out), 'counts': dict(summ),
         'expected_slots_total': sum(int(x['expected_slots']) for x in out)}, ensure_ascii=False, indent=2))
    log('DONE ' + json.dumps(dict(summ), ensure_ascii=False))


if __name__ == '__main__':
    main()
