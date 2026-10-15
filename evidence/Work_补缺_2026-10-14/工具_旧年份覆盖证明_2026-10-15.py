#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
旧年份 K 线「清单差」覆盖证明（只读，产出证据 CSV/JSON）

问题：
  验收基线《旧年份K线未恢复清单.csv》记录 184 个分片在恢复前后 catalog 之间的
  行数差，合计 449,048 行（old_path_absent 302,245 + rows_reduced 146,803）。
  需要回答：这 449,048 行是「能补而未补」，还是「已经补过」？

方法（集合证明，不看行数看集合）：
  对每个分片，取
    A = 恢复后归档 parquet 的 open_time 集合（candle_heads/<ex>/<...>/<sym>/<tf>/<year>.parquet）
    P = 各补件 CSV 中同 (exchange,symbol,timeframe) 且落在该年的 open_time 集合
  证明：
    ① A ∪ P 的槽数 ≥ old_rows（覆盖达标）
    ② A ∩ P = ∅（补件与归档无重叠，不是重复计数）
  两项同时成立 ⇒ 清单差已被补件完整覆盖，不存在「能补而未补」。

实测锚点：
  Binance 官方月度归档 BTCUSDT/15m/2022 全 12 月合计 35,040 行 = 清单 old_rows
  ⇒ old_rows 是官方真实行数，不是填充值。
"""
import csv, glob, json, datetime, collections
from pathlib import Path

import pyarrow.parquet as pq

ARC = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet/candle_heads')
EV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
SRC = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14/旧年份K线未恢复清单.csv')

OUT_CSV = EV / '校验_旧年份覆盖证明_2026-10-15.csv'
OUT_JSON = EV / '校验_旧年份覆盖证明汇总_2026-10-15.json'

EXDIR = {'binance': 'binance/um', 'okx': 'okx/swap'}


def year_ms(y):
    return (int(datetime.datetime(y, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000),
            int(datetime.datetime(y + 1, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000))


def main():
    patch = collections.defaultdict(set)
    for fn in sorted(glob.glob(str(EV / '补缺_K线_*_import.csv'))):
        with open(fn, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                try:
                    t = int(r['open_time'])
                except Exception:
                    continue
                patch[(r['exchange'], r['symbol'], r['timeframe'])].add(t)

    def arch(ex, sym, tf, year):
        p = ARC / EXDIR[ex] / sym / tf / f'{year}.parquet'
        if not p.exists():
            return set()
        d = pq.read_table(p, columns=['open_time']).to_pydict()
        return {int(t) for t in d['open_time'] if t is not None}

    rows, nok, nbad = [], 0, 0
    for r in csv.DictReader(open(SRC, encoding='utf-8-sig')):
        parts = r['path'].split('/')
        sym, tf, year = parts[-3], parts[-2], int(parts[-1].replace('.parquet', ''))
        ex, old, new = r['exchange'], int(r['old_rows']), int(r['new_rows'])
        lo, hi = year_ms(year)
        A = arch(ex, sym, tf, year)
        P = {t for t in patch.get((ex, sym, tf), ()) if lo <= t < hi}
        U, inter = A | P, A & P
        ok = (len(U) >= old) and (not inter)
        nok += ok
        nbad += (not ok)
        rows.append({
            'exchange': ex, 'symbol': sym, 'timeframe': tf, 'year': year,
            'status': r['status'], 'old_rows': old, 'arch_rows': len(A),
            'patch_rows': len(P), 'union_rows': len(U), 'overlap_rows': len(inter),
            'delta_rows': int(r['delta_rows']),
            'verdict': 'covered' if ok else ('overlap' if inter else 'short'),
        })

    with open(OUT_CSV, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    by_status = collections.defaultdict(lambda: {'shards': 0, 'old_rows': 0, 'union_rows': 0, 'delta_rows': 0})
    for x in rows:
        b = by_status[x['status']]
        b['shards'] += 1
        b['old_rows'] += x['old_rows']
        b['union_rows'] += x['union_rows']
        b['delta_rows'] += x['delta_rows']
    summary = {
        'generated_at': datetime.datetime.now().astimezone().isoformat(),
        'shards': len(rows),
        'covered_shards': nok,
        'abnormal_shards': nbad,
        'old_rows_sum': sum(x['old_rows'] for x in rows),
        'delta_rows_sum': sum(x['delta_rows'] for x in rows),
        'overlap_rows_sum': sum(x['overlap_rows'] for x in rows),
        'by_status': {k: dict(v) for k, v in by_status.items()},
        'conclusion': ('清单差 449,048 行已被现有补件完整覆盖（归档 ∪ 补件 ≥ old_rows 且零重叠），'
                       '不存在「能补而未补」' if nbad == 0 else '存在未覆盖分片，需补'),
        'patch_files_scanned': [Path(p).name for p in sorted(glob.glob(str(EV / '补缺_K线_*_import.csv')))],
    }
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if nbad:
        print('--- 异常分片 ---')
        for x in rows:
            if x['verdict'] != 'covered':
                print(' ', x)


if __name__ == '__main__':
    main()
