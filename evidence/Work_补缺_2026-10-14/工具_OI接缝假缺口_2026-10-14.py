#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位 OI「接缝假缺口」：孤立缺槽（前后 5m 桶都已存在）——真实 5 分钟区间其实已入库，
只是 ts 标签差一格（归档=桶起点 / API=桶终点）。用于披露 2026-10-08 00:00 UTC 源切换接缝。

只读源库；结果写本目录。
"""
import collections, csv, datetime, json, sqlite3
from pathlib import Path

IV = 300000
DB = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3')
WEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def main():
    con = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    oi = list(csv.DictReader((WEV / 'C_OI缺槽分类复核.csv').open(encoding='utf-8-sig')))

    bysym = collections.defaultdict(set)
    for r in oi:
        lo = int(datetime.datetime.fromisoformat(r['from_utc']).timestamp() * 1000)
        hi = int(datetime.datetime.fromisoformat(r['to_utc']).timestamp() * 1000)
        bysym[r['symbol']].update(range(lo, hi + 1, IV))

    out = []
    for sym, slots in bysym.items():
        lo, hi = min(slots), max(slots)
        src = {r[0]: r[1] for r in con.execute(
            "SELECT ts,source FROM oi_history WHERE exchange='binance' AND symbol=? AND period='5m' AND ts>=? AND ts<=?",
            (sym, lo - IV, hi + IV))}
        for t in sorted(slots):
            if (t - IV) in src and (t + IV) in src:
                out.append(dict(symbol=sym, hole_ts_utc=iso(t),
                                prev_ts_utc=iso(t - IV), prev_source=src[t - IV],
                                next_ts_utc=iso(t + IV), next_source=src[t + IV]))
    con.close()

    with (OUT / '补缺_OI接缝假缺口.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['symbol', 'hole_ts_utc', 'prev_ts_utc', 'prev_source',
                                          'next_ts_utc', 'next_source'])
        w.writeheader()
        w.writerows(out)

    # 按 hole 时刻分布
    c = collections.Counter(r['hole_ts_utc'][:16] for r in out)
    seam = sum(1 for r in out if r['hole_ts_utc'].startswith('2026-10-08T00:00'))
    stat = dict(generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                policy='只读源；写本目录；不写生产库',
                isolated_holes=len(out),
                at_source_switch_2026_10_08T00_00=seam,
                other_isolated=len(out) - seam,
                by_hole_time=dict(sorted(c.items())),
                note='孤立缺槽=前后 5m 桶都已入库；归档桶起点 vs API 桶终点相差一格，属口径假缺口，不补')
    (OUT / '补缺_OI接缝假缺口统计.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    print(json.dumps(stat, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
