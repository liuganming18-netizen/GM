#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：A恢复前后对账 + C OI覆盖复核 + 02全部缺失项目清单。
复用 Codex 已产证据与清单快照，不重扫原件。
"""
import collections, csv, json, datetime
from pathlib import Path

SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE / 'data/archive/parquet'
BE = Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现')
R = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据完整度验收_2026-10-14')
OUT.mkdir(parents=True, exist_ok=True)
STAMP = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
EXCLUDED = {'BTCDOMUSDT', 'ALLUSDT', 'DEFIUSDT'}
MAIN_TFS = ['1d', '12h', '8h', '4h', '1h', '30m', '15m']


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


scope = json.loads((BE / '历史下载/标的池/scope_crypto.json').read_text())
POOL = {('binance', 'um', i['symbol']) for i in scope['binance_items']}
for s in scope.get('okx_symbols', []):
    POOL.add(('okx', 'swap', s))
POOL = {x for x in POOL if x[2] not in EXCLUDED}

OLD = json.loads((ARC / '_catalog.json.1791969466.bak').read_text())['shards']
NEW = json.loads((ARC / '_catalog.json').read_text())['shards']


def parts(k):
    p = k.split('/')
    return p


# ---------- A: 逐分片对账 ----------
A1 = []
for k in sorted(set(OLD) | set(NEW)):
    p = parts(k)
    metric = p[0]
    if metric not in ('candle_heads', 'funding', 'oi'):
        continue
    ex, mkt, sym = p[1], p[2], p[3]
    if (ex, mkt, sym) not in POOL:
        continue
    tf = p[4] if metric == 'candle_heads' else ('events' if metric == 'funding' else '5m')
    year = p[-1].replace('.parquet', '') if metric != 'funding' else 'all'
    a = OLD.get(k)
    b = NEW.get(k)
    old_rows = a['rows'] if a else 0
    new_rows = b['rows'] if b else 0
    delta = new_rows - old_rows
    if a is None:
        status, nature = 'new_only', '恢复新增'
    elif b is None:
        status, nature = 'old_path_absent', '现目录完全缺失'
    elif delta < 0:
        status, nature = 'rows_reduced', '现行数减少'
    elif delta > 0:
        status, nature = 'rows_increased', '恢复新增行'
    else:
        continue
    A1.append(dict(path=k, metric=metric, exchange=ex, market=mkt, symbol=sym, period=tf, year=year,
                   old_rows=old_rows, new_rows=new_rows, delta_rows=delta, status=status, nature=nature))

with (OUT / 'A_恢复前后_逐分片.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(A1[0]))
    w.writeheader(); w.writerows(A1)

# ---------- A: 按币/周期汇总（结合冷热覆盖与缺槽） ----------
cov = list(csv.DictReader((R / '逐币逐周期冷热覆盖.csv').open(encoding='utf-8-sig')))
covmap = {(r['exchange'], r['symbol'], r['period']): r for r in cov}
gaps = list(csv.DictReader((R / '冷热合并内部缺槽.csv').open(encoding='utf-8-sig')))
gseg = collections.Counter()
gslot = collections.Counter()
for g in gaps:
    key = (g['exchange'], g['symbol'], g['period'])
    gseg[key] += 1
    if g['missing_slots']:
        gslot[key] += int(g['missing_slots'])

# old/new rows per symbol+tf (candle_heads)
oldby = collections.Counter()
newby = collections.Counter()
for r in A1:
    if r['metric'] == 'candle_heads':
        key = (r['exchange'], r['symbol'], r['period'])
        oldby[key] += r['old_rows']
        newby[key] += r['new_rows']

A2 = []
for (ex, mkt, sym) in sorted(POOL):
    for tf in MAIN_TFS:
        if ex == 'okx' and tf == '8h':
            A2.append(dict(exchange=ex, symbol=sym, period=tf, old_rows='', new_cold_rows='', delta_rows='',
                           hot_rows='', merged_unique='', internal_gap_segments=0, missing_slots=0,
                           first_utc='', last_utc='', annotation='OKX原生不支持8h'))
            continue
        key = (ex, sym, tf)
        c = covmap.get(key)
        A2.append(dict(exchange=ex, symbol=sym, period=tf,
                       old_rows=oldby.get(key, ''), new_cold_rows=newby.get(key, ''),
                       delta_rows=(newby.get(key, 0) - oldby.get(key, 0)) if key in oldby or key in newby else '',
                       hot_rows=c['hot_rows'] if c else '', merged_unique=c['merged_unique_timestamps'] if c else '',
                       internal_gap_segments=gseg.get(key, 0), missing_slots=gslot.get(key, 0),
                       first_utc=c['first_utc'] if c else '', last_utc=c['last_utc'] if c else '',
                       annotation='主线'))
with (OUT / 'A_恢复前后_按币周期.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(A2[0]))
    w.writeheader(); w.writerows(A2)

# ---------- C: OI覆盖复核 ----------
oi = list(csv.DictReader((R / 'OI缺槽原件逐事件核对.csv').open(encoding='utf-8-sig')))
C = collections.Counter()
for r in oi:
    C[r['status']] += 1
with (OUT / 'C_OI缺槽分类复核.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(oi[0]))
    w.writeheader(); w.writerows(oi)

# ---------- 02 全部缺失项目 ----------
rows02 = []
# 1) 旧年份K线分片缺失
oldy = [r for r in A1 if r['metric'] == 'candle_heads' and r['year'].isdigit() and int(r['year']) <= 2025]
byex = collections.Counter(r['exchange'] for r in oldy)
byex_rows = collections.Counter()
for r in oldy:
    byex_rows[r['exchange']] += -r['delta_rows']
for r in sorted(oldy, key=lambda x: (x['exchange'], x['symbol'], x['period'], x['year'])):
    rows02.append(dict(名称=r['symbol'], 交易所=r['exchange'], 类型='K线头分片', 周期=r['period'],
                       起止时间=f"{r['year']}年(UTC)", 旧数量=r['old_rows'], 现数量=r['new_rows'],
                       缺失或候选数量=-r['delta_rows'], 证据等级='清单快照行数差(未逐时刻证明)',
                       来源='_catalog.json.1791969466.bak vs _catalog.json', 可否本地补齐='需原件逐时刻核对',
                       待核对原因=r['nature']))

# 2) 主线内部K线缺槽
for g in [x for x in gaps if x['metric'] == 'candle_heads' and x['period'] != '5m']:
    rows02.append(dict(名称=g['symbol'], 交易所=g['exchange'], 类型='K线内部网格缺槽', 周期=g['period'],
                       起止时间=f"{g['from_utc']} ~ {g['to_utc']}(UTC)", 旧数量='', 现数量='',
                       缺失或候选数量=g['missing_slots'], 证据等级='冷热合并网格候选',
                       来源='冷热合并内部缺槽.csv', 可否本地补齐='需原件逐事件核对',
                       待核对原因='官方范围/停牌/退市或404证据待补'))

# 3) 已证漏恢复K线
pc = [r for r in csv.DictReader((R / '缺口与原始归档核对.csv').open(encoding='utf-8-sig'))
      if r['metric'] == 'candle_heads' and r['evidence_kind'] == 'raw_events_absent_from_cold_hot_gap']
for r in pc:
    rows02.append(dict(名称=r['symbol'], 交易所=r['exchange'], 类型='K线已证漏恢复', 周期=r['period'],
                       起止时间=f"{r['from_utc']} ~ {r['to_utc']}(UTC)", 旧数量='', 现数量='',
                       缺失或候选数量=r['raw_events_in_gap'], 证据等级=f"源件校验{r['checksum_status']}+冷热为空",
                       来源=r['source_file'], 可否本地补齐='可(本地ZIP存在)',
                       待核对原因='文件存在曾被当已恢复'))
# 3b) K线缺口无原件见证
pcn = [r for r in csv.DictReader((R / '缺口与原始归档核对.csv').open(encoding='utf-8-sig'))
       if r['metric'] == 'candle_heads' and r['evidence_kind'] == 'no_surviving_raw_event_witness']
for r in pcn:
    rows02.append(dict(名称=r['symbol'], 交易所=r['exchange'], 类型='K线缺口无原件见证', 周期=r['period'],
                       起止时间=f"{r['from_utc']} ~ {r['to_utc']}(UTC)", 旧数量='', 现数量='',
                       缺失或候选数量='', 证据等级='无幸存原件事件见证', 来源=r.get('source_file', ''),
                       可否本地补齐='否(本地无原件)', 待核对原因='不能据此判官方不可得'))

# 4) 资金费已证漏恢复（逐段聚合）
pf = [r for r in csv.DictReader((R / '缺口与原始归档核对.csv').open(encoding='utf-8-sig'))
      if r['metric'] == 'funding' and r['evidence_kind'] == 'raw_events_absent_from_cold_hot_gap']
for r in pf:
    rows02.append(dict(名称=r['symbol'], 交易所=r['exchange'], 类型='资金费已证漏恢复', 周期='events',
                       起止时间=f"{r['from_utc']} ~ {r['to_utc']}(UTC)", 旧数量='', 现数量='',
                       缺失或候选数量=r['raw_events_in_gap'], 证据等级=f"源件校验{r['checksum_status']}+冷热为空",
                       来源=r['source_file'], 可否本地补齐='可(本地月ZIP存在)',
                       待核对原因='短空档需完整事件差集'))

# 5) OI 候选缺槽（按段聚合，分类）
for r in oi:
    rows02.append(dict(名称=r['symbol'], 交易所='binance', 类型='OI候选缺槽', 周期='5m',
                       起止时间=f"{r['from_utc']} ~ {r['to_utc']}(UTC)", 旧数量='', 现数量='',
                       缺失或候选数量=r['missing_grid_slots_candidate'], 证据等级=f"逐日包核对:{r['status']}",
                       来源=f"幸存日包{r['surviving_day_zips']}个", 可否本地补齐='否(源包无空档内事件)' if r['status'] == 'source_zip_has_no_event_in_gap' else '待核',
                       待核对原因='不能把候选槽当删除数量'))

# 6) 上市至首条前端缺口候选
for r in csv.DictReader((R / '上市至首条数据前端缺口候选.csv').open(encoding='utf-8-sig')):
    rows02.append(dict(名称=r['symbol'], 交易所=r['exchange'], 类型='上市前端覆盖候选缺口', 周期=r['timeframe'],
                       起止时间=f"{r['listing_utc']} ~ {r['actual_first_utc']}(UTC)", 旧数量='', 现数量='',
                       缺失或候选数量=r['theoretical_early_slots'], 证据等级=f"上市时间vs首条:{r['classification']}",
                       来源='上市至首条数据前端缺口候选.csv', 可否本地补齐='待核',
                       待核对原因='需逐项解释官方上市/停牌/退市'))

with (OUT / '02_全部缺失项目.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['名称', '交易所', '类型', '周期', '起止时间', '旧数量', '现数量',
                                      '缺失或候选数量', '证据等级', '来源', '可否本地补齐', '待核对原因'])
    w.writeheader(); w.writerows(rows02)

meta = dict(generated_at=STAMP, pool=len(POOL),
            A1_shards=len(A1), A2_rows=len(A2),
            oldyear_shards=len(oldy), oldyear_by_exchange=dict(byex),
            oldyear_lost_rows=dict(byex_rows),
            main_gap_segments=len([x for x in gaps if x['metric'] == 'candle_heads' and x['period'] != '5m']),
            funding_segments=len([x for x in gaps if x['metric'] == 'funding']),
            oi_segments=len(oi), oi_status=dict(C),
            rows_02=len(rows02),
            rows02_by_type=dict(collections.Counter(r['类型'] for r in rows02)))
(OUT / 'AC_对账汇总.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2))
print('AC_DONE', json.dumps(meta, ensure_ascii=False, indent=2))
