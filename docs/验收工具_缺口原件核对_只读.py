#!/usr/bin/env python3
"""只读将缺口与幸存原件对照；不修复、不取数、不改生产文件。"""
import collections,csv,datetime,hashlib,io,json,re,sqlite3,zipfile
from pathlib import Path
import pyarrow.parquet as pq
ROOT=Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
SAFE=Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC=SAFE/'data/archive/parquet'
RAW=Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw')
OIRAW=RAW.parent/'raw_oi_metrics'
gaps=list(csv.DictReader((ROOT/'冷热合并内部缺槽.csv').open(encoding='utf-8-sig')))
con=sqlite3.connect((SAFE/'data/market_v2.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=30);con.execute('pragma query_only=on')
con.execute('pragma cache_size=-16384')
fields=['metric','exchange','symbol','period','from_utc','to_utc','source_file','checksum_status','raw_events_in_gap','evidence_kind','notes']
f=(ROOT/'缺口与原始归档核对.csv').open('w',encoding='utf-8-sig',newline='');w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
counts=collections.Counter();examples=[]
def checksum(p):
    q=Path(str(p)+'.CHECKSUM')
    if not q.exists():return 'checksum_file_absent'
    try:
        want=q.read_text().split()[0].lower();got=hashlib.sha256(p.read_bytes()).hexdigest()
        return 'MATCH' if got==want else 'MISMATCH'
    except Exception as e:return 'ERROR:'+repr(e)
for i,g in enumerate([x for x in gaps if x['metric']=='funding' and x['exchange']=='binance'],1):
    sym=g['symbol'];lo=int(g['previous_ts'])+1;hi=int(g['next_ts'])-1
    dates=[datetime.datetime.fromtimestamp(x/1000,datetime.timezone.utc) for x in [lo,hi]]
    lo_m=dates[0].strftime('%Y-%m');hi_m=dates[1].strftime('%Y-%m')
    ps=[p for p in (RAW/'funding'/sym).glob('*.zip') if lo_m<=p.stem[-7:]<=hi_m]
    total=0
    for p in sorted(ps):
        vals=set()
        try:
            with zipfile.ZipFile(p) as z:
                n=next(n for n in z.namelist() if n.endswith('.csv'))
                with z.open(n) as src:
                    for row in csv.reader(io.TextIOWrapper(src,encoding='utf-8-sig',newline='')):
                        if not row or row[0]=='calc_time':continue
                        t=int(row[0])
                        if lo<=t<=hi:vals.add(t)
            total+=len(vals);cs=checksum(p)
            w.writerow(dict(metric='funding',exchange='binance',symbol=sym,period='events',from_utc=g['from_utc'],to_utc=g['to_utc'],source_file=str(p),checksum_status=cs,raw_events_in_gap=len(vals),evidence_kind='raw_events_absent_from_cold_hot_gap' if vals else 'raw_has_no_events_in_gap',notes='按原件真实结算时刻，不用固定8h网格推算'))
            if vals and cs=='MATCH':
                counts['funding_proven_missing_events']+=len(vals);counts['funding_proven_missing_symbol_gaps']+=1
                if len(examples)<8:examples.append(dict(symbol=sym,events=len(vals),from_utc=g['from_utc'],to_utc=g['to_utc'],file=str(p)))
        except Exception as e:
            counts['source_read_errors']+=1;w.writerow(dict(metric='funding',exchange='binance',symbol=sym,period='events',source_file=str(p),evidence_kind='source_read_error',notes=repr(e)))
    if not ps:counts['funding_gap_no_surviving_monthly_zip']+=1
    if i%100==0:print('funding_raw',i,dict(counts),flush=True);f.flush()

# 只对主线K线缺槽读取相应ZIP，验证幸存原件是否含缺失开盘时间。
for g in [x for x in gaps if x['metric']=='candle_heads' and x['exchange']=='binance' and x['period']!='5m']:
    sym,tf=g['symbol'],g['period'];lo=int(g['previous_ts'])+1;hi=int(g['next_ts'])-1
    lo_d=datetime.datetime.fromtimestamp(lo/1000,datetime.timezone.utc).strftime('%Y-%m-%d');hi_d=datetime.datetime.fromtimestamp(hi/1000,datetime.timezone.utc).strftime('%Y-%m-%d')
    ps=[]
    for p in (RAW/tf/sym).glob('*.zip'):
        date=p.stem[len(sym)+len(tf)+2:]
        if len(date)==7:
            if lo_d[:7]<=date<=hi_d[:7]:ps.append(p)
        elif len(date)==10 and lo_d<=date<=hi_d:ps.append(p)
    witness=set()
    for p in ps:
        try:
            with zipfile.ZipFile(p) as z:
                n=next(n for n in z.namelist() if n.endswith('.csv'))
                with z.open(n) as src:
                    vals={int(row[0]) for row in csv.reader(io.TextIOWrapper(src,encoding='utf-8-sig',newline='')) if row and row[0]!='open_time' and lo<=int(row[0])<=hi}
            if vals:
                cs=checksum(p);w.writerow(dict(metric='candle_heads',exchange='binance',symbol=sym,period=tf,from_utc=g['from_utc'],to_utc=g['to_utc'],source_file=str(p),checksum_status=cs,raw_events_in_gap=len(vals),evidence_kind='raw_events_absent_from_cold_hot_gap',notes='月包与日包事件按开盘时间去重'))
                if cs=='MATCH':witness.update(vals)
        except Exception as e:counts['source_read_errors']+=1
    counts['candle_proven_missing_events']+=len(witness)
    if witness:counts['candle_proven_missing_symbol_gaps']+=1
    else:
        counts['candle_gap_without_raw_witness']+=1;w.writerow(dict(metric='candle_heads',exchange='binance',symbol=sym,period=tf,from_utc=g['from_utc'],to_utc=g['to_utc'],raw_events_in_gap=0,evidence_kind='no_surviving_raw_event_witness',notes='不能据此称官方不可得；仍需源范围/退市证据'))

# OI检查所有缺槽候选对应日包是否幸存。长空档不逐日解压，按月目录索引盘点。
oi_index={}
for g in [x for x in gaps if x['metric']=='oi' and x['exchange']=='binance']:
    sym=g['symbol'];lo=datetime.datetime.fromtimestamp((int(g['previous_ts'])+300000)/1000,datetime.timezone.utc).date();hi=datetime.datetime.fromtimestamp((int(g['next_ts'])-300000)/1000,datetime.timezone.utc).date()
    if sym not in oi_index:
        oi_index[sym]={p.stem[-10:]:p for p in (OIRAW/sym).glob('*.zip')}
    days=(hi-lo).days+1;present=[]
    for d,p in oi_index[sym].items():
        if str(lo)<=d<=str(hi):present.append(p)
    counts['oi_candidate_gap_segments']+=1
    counts['oi_candidate_calendar_days']+=max(days,0)
    counts['oi_candidate_days_with_zip']+=len(present)
    counts['oi_candidate_days_without_zip']+=max(days-len(present),0)
    w.writerow(dict(metric='oi',exchange='binance',symbol=sym,period='5m',from_utc=g['from_utc'],to_utc=g['to_utc'],source_file=';'.join(str(p) for p in present[:3]),raw_events_in_gap='',evidence_kind='oi_candidate_day_zip_inventory',notes=f'涉及{days}日；有ZIP {len(present)}日，无ZIP {max(days-len(present),0)}日；有ZIP不代表每槽有数据；非对齐时刻不平移'))
con.close();f.close()
(ROOT/'原件核对汇总.json').write_text(json.dumps(dict(counts=dict(counts),funding_examples=examples,notes=['这里只将校验通过且原件确有事件、冷热区间完全为空的情况称为已证实漏恢复','OI本步骤是缺槽候选对应日包库存，不是逐事件完整度通过','跨段同日的日包统计可能重复，不是唯一文件数']),ensure_ascii=False,indent=2))
print('RAW_DONE',dict(counts),flush=True)
