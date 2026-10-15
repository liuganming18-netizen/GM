#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全轴集合覆盖校验：证明补件覆盖原缺口时间键，并与现冷热做交集/重复/冲突核对。
轴A 主线内部缺口（195段）；轴B 上市前端408可补项；轴C OI缺槽。
只读；不修改任何补件或生产数据。"""
import collections, csv, datetime, json, sqlite3
from pathlib import Path
import pyarrow.parquet as pq

FIXED_NOW = 1791980404548
SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
CEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
WEV = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14')
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000,
         '8h': 28800000, '12h': 43200000, '1d': 86400000}
# 注：原计划的 '补缺_K线_okx前端重核_import.csv' 已废弃 —— 该重核路径存在 12h/1d 口径缺陷
#     （OKX `12H`/`1D` 是 UTC+8 口径，与库内 UTC 口径不一致），且 B 轴 63 段已 100% 覆盖，
#     重核属冗余。详见 01_补缺总报告 §本轮继续补。
KLINE_PATCHES = ['补缺_K线_local_import.csv', '补缺_K线_api_import.csv',
                 '补缺_K线_okx前端_import.csv', '补缺_K线_okx补齐_import.csv',
                 '补缺_K线_官方日包_import.csv']


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


# ---------- K线补件集合 ----------
patch = collections.defaultdict(set)
patch_dup = {}
for name in KLINE_PATCHES:
    p = OUT / name
    if not p.exists():
        continue
    seen = collections.Counter()
    for r in csv.DictReader(p.open(encoding='utf-8-sig')):
        key = (r['exchange'], r['market'], r['symbol'], r['timeframe'])
        patch[key].add(int(r['open_time']))
        seen[key + (r['open_time'],)] += 1
    patch_dup[name] = sum(v - 1 for v in seen.values() if v > 1)

con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
con.execute('PRAGMA query_only=ON')
cold_cache = {}
hot_cache = {}


def cold(ex, mkt, sym, tf):
    k = (ex, mkt, sym, tf)
    if k not in cold_cache:
        s = set()
        for p in (ARC / 'candle_heads' / ex / mkt / sym / tf).glob('*.parquet'):
            tb = pq.read_table(p, columns=['open_time']).to_pydict()
            s.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
        cold_cache[k] = s
    return cold_cache[k]


def hot(ex, mkt, sym, tf):
    k = (ex, mkt, sym, tf)
    if k not in hot_cache:
        hot_cache[k] = {int(r[0]) for r in con.execute(
            'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
            (ex, mkt, sym, tf, FIXED_NOW))}
    return hot_cache[k]


def grid(lo_ms, hi_ms, tf):
    """严格区间内的槽（lo < t < hi），用于轴A（prev/next 已对齐）"""
    iv = TF_MS[tf]
    return [lo_ms + iv * (i + 1) for i in range((hi_ms - lo_ms) // iv - 1)]


def grid_aligned(lo_ms, hi_ms, tf):
    """[lo, hi] 内按周期对齐的槽（t 为 iv 整数倍，与交易所 K 线时间一致）"""
    iv = TF_MS[tf]
    first = (lo_ms // iv + 1) * iv
    return list(range(first, hi_ms + 1, iv))


# ---------- 轴A: 主线内部缺口 ----------
gapsA = [g for g in csv.DictReader((CEV / '冷热合并内部缺槽.csv').open(encoding='utf-8-sig'))
         if g['metric'] == 'candle_heads' and g['period'] != '5m']
rowsA = []
for g in gapsA:
    ex, mkt, sym, tf = g['exchange'], g['market'], g['symbol'], g['period']
    expected = grid(int(g['previous_ts']), int(g['next_ts']), tf)
    p = patch.get((ex, mkt, sym, tf), set())
    have = cold(ex, mkt, sym, tf) | hot(ex, mkt, sym, tf) | p
    unc = [t for t in expected if t not in have]
    rowsA.append(dict(axis='A_mainline', exchange=ex, symbol=sym, period=tf, from_utc=g['from_utc'],
                      to_utc=g['to_utc'], expected_slots=len(expected),
                      covered_by_patch=sum(1 for t in expected if t in p),
                      still_uncovered=len(unc), first_uncovered_utc=iso(unc[0]) if unc else '',
                      last_uncovered_utc=iso(unc[-1]) if unc else ''))

# ---------- 轴B: 上市前端 408 可补项 ----------
rowsB = []
fe = OUT / '校验_上市后前端408.csv'
if fe.exists():
    for r in csv.DictReader(fe.open(encoding='utf-8-sig')):
        if r['verdict'] != 'fillable_real_gap':
            continue
        ex = r['exchange']; mkt = 'um' if ex == 'binance' else 'swap'
        sym, tf = r['symbol'], r['timeframe']
        lo = int(datetime.datetime.fromisoformat(r['listing_utc']).timestamp() * 1000)
        hi = int(datetime.datetime.fromisoformat(r['actual_first_utc']).timestamp() * 1000)
        expected = grid_aligned(lo, hi, tf)
        p = patch.get((ex, mkt, sym, tf), set())
        have = cold(ex, mkt, sym, tf) | hot(ex, mkt, sym, tf) | p
        unc = [t for t in expected if t not in have]
        rowsB.append(dict(axis='B_frontend', exchange=ex, symbol=sym, period=tf, from_utc=r['listing_utc'],
                          to_utc=r['actual_first_utc'], expected_slots=len(expected),
                          covered_by_patch=sum(1 for t in expected if t in p),
                          still_uncovered=len(unc), first_uncovered_utc=iso(unc[0]) if unc else '',
                          last_uncovered_utc=iso(unc[-1]) if unc else ''))

# ---------- 轴C: OI ----------
# 补件口径：库内 2026-10-08 起的 5m 行来自采集器（ts=桶终点）。故：
#   · 补缺_OI_1008_1013_官方归档_import.csv / 补缺_OI_API_import.csv 的 ts 已是「归档 ts + 5min」，直接可用；
#   · 补缺_OI_import.csv（7 行）已作废（用桶起点标签，会错位+重复），见 补缺_OI_旧7行作废说明_2026-10-15.json。
oi_patch = set()
for name in ['补缺_OI_1008_1013_官方归档_import.csv', '补缺_OI_API_import.csv']:
    p = OUT / name
    if p.exists():
        for r in csv.DictReader(p.open(encoding='utf-8-sig')):
            oi_patch.add((r['symbol'], int(r['ts'])))


def oi_have(sym, lo, hi):
    """库内 5m 时间键（用于识别接缝假缺口）"""
    return {r[0] for r in con.execute(
        "SELECT ts FROM oi_history WHERE exchange='binance' AND symbol=? AND period='5m' AND ts>=? AND ts<=?",
        (sym, lo, hi))}


rowsC = []
oi = list(csv.DictReader((WEV / 'C_OI缺槽分类复核.csv').open(encoding='utf-8-sig')))
# 先按币聚合全部候选槽，再一次性取该币 ±1 桶范围内的库内时间键（避免逐段窗口漏掉相邻桶）
slots_by_sym = collections.defaultdict(set)
for r in oi:
    _lo = int(datetime.datetime.fromisoformat(r['from_utc']).timestamp() * 1000)
    _hi = int(datetime.datetime.fromisoformat(r['to_utc']).timestamp() * 1000)
    slots_by_sym[r['symbol']].update(range(_lo, _hi + 1, TF_MS['5m']))
have_by_sym = {s: oi_have(s, min(v) - TF_MS['5m'], max(v) + TF_MS['5m'])
               for s, v in slots_by_sym.items()}

for r in oi:
    lo = int(datetime.datetime.fromisoformat(r['from_utc']).timestamp() * 1000)
    hi = int(datetime.datetime.fromisoformat(r['to_utc']).timestamp() * 1000)
    slots = list(range(lo, hi + 1, TF_MS['5m']))  # OI 缺槽含两端点
    sym = r['symbol']
    cov = 0
    phantom = 0            # 接缝假缺口：孤立缺槽（前后 5m 桶都已存）⇒ 真实区间已以邻桶标签存在
    resid = []
    have = have_by_sym[sym]
    for t in slots:
        if (sym, t) in oi_patch:
            cov += 1
            continue
        if (t - TF_MS['5m']) in have and (t + TF_MS['5m']) in have:
            phantom += 1
        resid.append(t)
    rowsC.append(dict(axis='C_oi', exchange='binance', symbol=sym, period='5m', from_utc=r['from_utc'],
                      to_utc=r['to_utc'], expected_slots=len(slots), covered_by_patch=cov,
                      still_uncovered=len(slots) - cov, status=r['status'],
                      phantom_seam_slots=phantom,
                      first_uncovered_utc=iso(resid[0]) if resid else '',
                      last_uncovered_utc=iso(resid[-1]) if resid else ''))

allrows = rowsA + rowsB + rowsC
FIELDS = ['axis', 'exchange', 'symbol', 'period', 'from_utc', 'to_utc', 'expected_slots',
          'covered_by_patch', 'still_uncovered', 'phantom_seam_slots', 'first_uncovered_utc',
          'last_uncovered_utc', 'status']
with (OUT / '校验_全轴覆盖明细.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
    w.writeheader()
    for r in allrows:
        r.setdefault('status', '')
        r.setdefault('phantom_seam_slots', 0)
        r.setdefault('first_uncovered_utc', '')
        r.setdefault('last_uncovered_utc', '')
        w.writerow(r)

# 补件 vs 冷热 交集
inter = 0
for k, ts in patch.items():
    inter += len(ts & (cold(*k) | hot(*k)))


def agg(rows, name):
    exp = sum(r['expected_slots'] for r in rows)
    unc = sum(r['still_uncovered'] for r in rows)
    return {f'{name}_segments': len(rows),
            f'{name}_expected': exp,
            f'{name}_covered_by_patch': sum(r['covered_by_patch'] for r in rows),
            f'{name}_still_uncovered': unc,
            f'{name}_covered_total': exp - unc,   # 补件 ∪ 冷热
            f'{name}_segments_fully_covered': sum(1 for r in rows if r['still_uncovered'] == 0),
            f'{name}_segments_uncovered': sum(1 for r in rows if r['still_uncovered'] > 0)}


summary = dict(patch_files={n: (sum(1 for _ in csv.DictReader((OUT / n).open(encoding='utf-8-sig'))) if (OUT / n).exists() else 0) for n in KLINE_PATCHES},
               patch_dup=patch_dup, patch_keys=len(patch),
               patch_rows=sum(len(v) for v in patch.values()),
               patch_vs_coldhot_overlap=inter)
summary.update(agg(rowsA, 'A_mainline'))
summary.update(agg(rowsB, 'B_frontend'))
summary.update(agg(rowsC, 'C_oi'))

# 轴C 按 status 拆分（区分「源包无事件=官方缺失」与「无本地日包=待官方发布」）
by_status = {}
for st in sorted({r['status'] for r in rowsC}):
    sub = [r for r in rowsC if r['status'] == st]
    unc = sum(r['still_uncovered'] for r in sub)
    ph = sum(r['phantom_seam_slots'] for r in sub)
    by_status[st] = dict(segments=len(sub),
                         expected=sum(r['expected_slots'] for r in sub),
                         covered_by_patch=sum(r['covered_by_patch'] for r in sub),
                         still_uncovered=unc,
                         phantom_seam_slots=ph,
                         real_uncovered=unc - ph,
                         segments_fully_covered=sum(1 for r in sub if r['still_uncovered'] == 0))
summary['C_oi_by_status'] = by_status
summary['C_oi_phantom_seam_slots'] = sum(r['phantom_seam_slots'] for r in rowsC)
summary['C_oi_real_uncovered'] = sum(r['still_uncovered'] - r['phantom_seam_slots'] for r in rowsC)
con.close()
(OUT / '校验_全轴覆盖汇总.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))
