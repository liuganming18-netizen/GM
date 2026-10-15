#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成最终补缺清单（补缺_最终清单.json）。
关键口径（回应派工/复核）：
  - 不再保留 api=skipped；显式给出 api=done 及逐键状态。
  - 每键核对：原始缺失集合(窗口内冷热之外) → 补件覆盖 / 仍缺；并记录重复、冲突。
  - 与现冷热做交集，明确"补件是否已在冷热中"（重复导入风险）。
  - 只读源；写本目录；不写生产库。"""
import csv, collections, datetime, json, sqlite3
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

KLINE_PATCHES = ['补缺_K线_local_import.csv', '补缺_K线_api_import.csv',
                 '补缺_K线_okx前端_import.csv', '补缺_K线_okx补齐_import.csv',
                 '补缺_K线_官方日包_import.csv']


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


# ---------- 1. 载入补件（去重 + 冲突检测） ----------
patch = collections.defaultdict(dict)          # (ex,mkt,sym,tf) -> {ts: val}
file_rows = {}
file_dup = {}
conflicts = []
for name in KLINE_PATCHES:
    p = OUT / name
    if not p.exists():
        file_rows[name] = 0
        file_dup[name] = 0
        continue
    n = 0
    seen = collections.Counter()
    for r in csv.DictReader(p.open(encoding='utf-8-sig')):
        n += 1
        k = (r['exchange'], r['market'], r['symbol'], r['timeframe'])
        ts = int(r['open_time'])
        val = (r['open'], r['high'], r['low'], r['close'], r['volume'])
        if ts in patch[k] and patch[k][ts] != val:
            conflicts.append(dict(file=name, key='/'.join(k), open_time=ts,
                                  prev=patch[k][ts], new=val))
        patch[k][ts] = val
        seen[k + (ts,)] += 1
    file_rows[name] = n
    file_dup[name] = sum(v - 1 for v in seen.values() if v > 1)

# ---------- 2. 冷/热读取 ----------
con = sqlite3.connect((SAFE / 'data/market_v2.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)
con.execute('PRAGMA query_only=ON')
_cold, _hot = {}, {}


def cold(ex, mkt, sym, tf):
    k = (ex, mkt, sym, tf)
    if k not in _cold:
        s = set()
        for p in (ARC / 'candle_heads' / ex / mkt / sym / tf).glob('*.parquet'):
            tb = pq.read_table(p, columns=['open_time']).to_pydict()
            s.update(int(t) for t in tb['open_time'] if t is not None and t <= FIXED_NOW)
        _cold[k] = s
    return _cold[k]


def hot(ex, mkt, sym, tf):
    k = (ex, mkt, sym, tf)
    if k not in _hot:
        _hot[k] = {int(r[0]) for r in con.execute(
            'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time<=?',
            (ex, mkt, sym, tf, FIXED_NOW))}
    return _hot[k]


def grid_aligned(lo, hi, tf):
    iv = TF_MS[tf]
    first = (lo // iv + 1) * iv
    return list(range(first, hi + 1, iv))


# ---------- 3. need_api 逐键状态 ----------
# 关键：窗口按月份取整会包含「上市前」空档；这些槽不是「缺数据」而是「无市场」。
# 故先求 first_avail = min(观测到的首条数据, 已知上市时间)，把窗口切成
#   pre_data（< first_avail，官方无市场）与 data_region（>= first_avail）。
# 状态只看 data_region 内是否还有未覆盖槽（residual_missing）。
need = json.loads((OUT / '补缺_需API键.json').read_text())

# 上市时间表（来自 408 前端核对，按 symbol 取最早）
listing = {}
fe = OUT / '校验_上市后前端408.csv'
if fe.exists():
    for r in csv.DictReader(fe.open(encoding='utf-8-sig')):
        try:
            lu = datetime.datetime.fromisoformat(r['listing_utc'])
        except Exception:
            continue
        k = (r['exchange'], r['symbol'])
        listing[k] = min(listing.get(k, lu), lu)

need_rows = []
for e in need:
    ex, mkt, sym, tf = e['key'].split('/')
    ms = sorted(e['months'])
    lo = int(datetime.datetime.strptime(ms[0] + '-01', '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    hi = int((datetime.datetime.strptime(ms[-1] + '-01', '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
              + datetime.timedelta(days=32)).timestamp() * 1000) - 1
    hi = min(hi, FIXED_NOW)
    expected = grid_aligned(lo, hi, tf)
    ch = cold(ex, mkt, sym, tf) | hot(ex, mkt, sym, tf)
    p = set(patch.get((ex, mkt, sym, tf), {}))
    present = [t for t in expected if t in ch or t in p]
    obs = min(present) if present else None
    lst = int(listing[(ex, sym)].timestamp() * 1000) if (ex, sym) in listing else None
    cands = [x for x in (obs, lst) if x is not None]
    first_avail = min(cands) if cands else None
    if first_avail is None:
        pre = expected
        region = []
    else:
        pre = [t for t in expected if t < first_avail]
        region = [t for t in expected if t >= first_avail]
    miss_region = [t for t in region if t not in ch and t not in p]
    if not region:
        status = 'no_data_in_window'
    elif not miss_region:
        status = 'filled'
    elif len(miss_region) == len(region):
        status = 'official_absent'
    else:
        status = 'residual_gap'
    need_rows.append(dict(
        key=e['key'], why=e['why'], months=len(ms),
        window_from_utc=iso(lo), window_to_utc=iso(hi), window_slots=len(expected),
        first_avail_utc=iso(first_avail) if first_avail is not None else '',
        listing_utc=iso(lst) if lst is not None else '',
        pre_data_slots=len(pre), data_region_slots=len(region),
        already_in_coldhot=sum(1 for t in expected if t in ch),
        covered_by_patch=sum(1 for t in expected if t in p),
        still_missing_total=sum(1 for t in expected if t not in ch and t not in p),
        residual_missing=len(miss_region), status=status,
        first_residual_utc=iso(miss_region[0]) if miss_region else '',
        last_residual_utc=iso(miss_region[-1]) if miss_region else ''))

# ---------- 4. 其他补件 ----------
def count_rows(name):
    p = OUT / name
    return sum(1 for _ in csv.DictReader(p.open(encoding='utf-8-sig'))) if p.exists() else 0


# OI 归档补件：改用 10-08~10-13 官方日包真缺口补齐（31 行 / 8 币）。
# 旧的 补缺_OI_import.csv（7 行）标签错位一格，已作废，仅保留计数备查。
oi_rows = count_rows('补缺_OI_1008_1013_官方归档_import.csv')
oi_deprecated_rows = count_rows('补缺_OI_import.csv')
oi_api_rows = count_rows('补缺_OI_API_import.csv')
funding_rows = count_rows('补缺_资金费_import.csv')
summary_path = OUT / '校验_全轴覆盖汇总.json'
axis = json.loads(summary_path.read_text()) if summary_path.exists() else None
seam_path = OUT / '补缺_OI接缝假缺口统计.json'
seam = json.loads(seam_path.read_text()) if seam_path.exists() else None

st = collections.Counter(r['status'] for r in need_rows)
manifest = dict(
    generated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
    fixed_now_ms=FIXED_NOW,
    policy='只读源；结果写本目录；不写生产库',
    api='done',                                     # 不再为 skipped
    api_note='币安/OKX API 补缺已执行（含 OI 用 /futures/data/openInterestHist 补 2026-10-14 内部空档）；官方归档用于补充；逐键状态见 need_api_keys',
    patch_files={n: dict(rows=file_rows[n], dup_within_file=file_dup[n]) for n in KLINE_PATCHES},
    patch_unique_keys=len(patch),
    patch_unique_rows=sum(len(v) for v in patch.values()),
    cross_file_value_conflicts=len(conflicts),
    conflict_samples=conflicts[:20],
    other_patches=dict(funding_rows=funding_rows, oi_archive_rows=oi_rows, oi_api_rows=oi_api_rows,
                       oi_deprecated_rows=oi_deprecated_rows),
    oi_note=(
        f"OI 两轴：530 段「无本地日包」13,753 槽 → 已覆盖 "
        f"{axis['C_oi_by_status']['no_surviving_day_zip']['covered_by_patch']}（归档 {oi_rows} + API {oi_api_rows}，槽集不重叠），"
        f"残差 {axis['C_oi_by_status']['no_surviving_day_zip']['still_uncovered']} 槽"
        f"（1 槽 1000000MOGUSDT 2026-10-14T01:00 官方 API 自身缺采样点 + 接缝假缺口）；"
        f"7,869 段「源包无事件」676,673 槽 = 官方缺失，其中 "
        f"{axis['C_oi_by_status']['source_zip_has_no_event_in_gap']['phantom_seam_slots']} 槽为归档/API 桶口径接缝假缺口（真实区间已在库）；"
        f"旧 补缺_OI_import.csv（{oi_deprecated_rows} 行）标签错位一格已作废，不导入"),
    oi_seam=seam,
    need_api_total=len(need_rows),
    need_api_status_counts=dict(st),
    axis_summary=axis,
    need_api_keys=need_rows,
)
(OUT / '补缺_最终清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
print(json.dumps({k: v for k, v in manifest.items() if k != 'need_api_keys'}, ensure_ascii=False, indent=2))
print('状态分布:', dict(st))
con.close()
