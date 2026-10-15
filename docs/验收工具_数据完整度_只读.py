#!/usr/bin/env python3
"""只读审核：不导入生产模块，不修改行情库/分片，产物只写指定审核目录。"""
import collections, concurrent.futures, csv, datetime, hashlib, json, os, sqlite3, time
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq

SAFE = Path('/Users/boos/WorkBuddy AI/加密货币趋势判断系统')
ARC = SAFE/'data/archive/parquet'
BE = Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现')
OUT = Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
OUT.mkdir(parents=True, exist_ok=True)
STAMP = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
FIXED_NOW = int(time.time()*1000)
IV = {'5m':300000,'15m':900000,'30m':1800000,'1h':3600000,'4h':14400000,'8h':28800000,'12h':43200000,'1d':86400000}
MAIN_TFS = ('1d','12h','8h','4h','1h','30m','15m')

def dump(name,obj):
    (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2))
def csvout(name,rows,fields):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def iso(ms):
    return datetime.datetime.fromtimestamp(ms/1000,datetime.timezone.utc).isoformat() if ms is not None else None
def ro():
    c=sqlite3.connect((SAFE/'data/market_v2.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=30)
    c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-16384')
    return c
def rowsum(d):
    a=collections.defaultdict(lambda:dict(files=0,rows=0,bytes=0))
    for k,v in d.items():
        x=a[k.split('/')[0]];x['files']+=1;x['rows']+=v['rows'];x['bytes']+=v['bytes']
    return dict(a)

oldpath=ARC/'_catalog.json.1791969466.bak';newpath=ARC/'_catalog.json'
oldraw=oldpath.read_bytes();newraw=newpath.read_bytes()
OLD=json.loads(oldraw)['shards'];NEW=json.loads(newraw)['shards']
scope=json.loads((BE/'历史下载/标的池/scope_crypto.json').read_text())
POOL={('binance','um',i['symbol']) for i in scope['binance_items']}
okxitems=scope.get('okx_items',scope.get('okx_supplement',[]))
if not okxitems:
    supp=json.loads((BE/'历史下载/标的池/cross_exchange_supplement.json').read_text())
    okxitems=supp.get('supplement',[]) if isinstance(supp,dict) else supp
for i in okxitems:
    sym=i.get('inst_id') or i.get('instId') or i.get('symbol') or i.get('market_id')
    if sym:POOL.add(('okx','swap',sym))
EXCLUDED={'BTCDOMUSDT','ALLUSDT','DEFIUSDT'}
POOL={x for x in POOL if x[2] not in EXCLUDED}
print('scope',len(POOL),collections.Counter(x[0] for x in POOL),flush=True)

diff=[]
for k in sorted(OLD.keys()|NEW.keys()):
    a,b=OLD.get(k),NEW.get(k)
    if a is not None and (b is None or a['rows']!=b['rows']):
        parts=k.split('/');key=tuple(parts[1:4]);status='old_path_absent' if b is None else 'rows_reduced' if b['rows']<a['rows'] else 'rows_increased'
        diff.append(dict(path=k,metric=parts[0],exchange=parts[1],market=parts[2],symbol=parts[3],in_frozen_pool=key in POOL,status=status,old_rows=a['rows'],new_rows=b['rows'] if b else 0,delta_rows=(b['rows'] if b else 0)-a['rows'],old_bytes=a['bytes'],new_bytes=b['bytes'] if b else 0))
csvout('恢复前后逐分片差异.csv',diff,list(diff[0]) if diff else ['path'])
dump('清单基线.json',dict(checked_at=STAMP,fixed_now_ms=FIXED_NOW,old_catalog=str(oldpath),old_sha256=hashlib.sha256(oldraw).hexdigest(),current_catalog=str(newpath),current_sha256=hashlib.sha256(newraw).hexdigest(),old=rowsum(OLD),current=rowsum(NEW),pool_count=len(POOL),pool_by_exchange=dict(collections.Counter(x[0] for x in POOL)),exclusions=sorted(EXCLUDED),note='清单行数差不是唯一事件丢失数；需冷热合并及实际时间集合核对'))

PHYS={}
def inspect(item):
    k,v=item;p=ARC/k
    try:
        pf=pq.ParquetFile(p);return k,dict(rows=pf.metadata.num_rows,bytes=p.stat().st_size,columns=pf.schema_arrow.names,readable=True)
    except Exception as e:return k,dict(readable=False,error=repr(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
    for i,(k,v) in enumerate(ex.map(inspect,NEW.items()),1):
        PHYS[k]=v
        if i%5000==0:print('footer',i,'/',len(NEW),flush=True)
disk={str(p.relative_to(ARC)) for p in ARC.rglob('*.parquet')}
bad=[dict(path=k,**v) for k,v in PHYS.items() if not v['readable'] or v.get('rows')!=NEW[k]['rows'] or v.get('bytes')!=NEW[k]['bytes']]
dump('物理分片核对.json',dict(checked_at=STAMP,catalog_files=len(NEW),disk_files=len(disk),missing=sorted(NEW.keys()-disk),extra=sorted(disk-NEW.keys()),footer_or_byte_mismatches=bad,actual=rowsum({k:v for k,v in PHYS.items() if v.get('readable')}),sha_recomputed=False))
print('footer_done','bad',len(bad),flush=True)

# 冷热读取均只读；以固定当前时刻排除尚未闭合的K线。
GROUPS=collections.defaultdict(list)
for k in sorted(NEW):
    parts=k.split('/');metric=parts[0]
    if metric in ('candle_heads','oi','funding'):
        key=tuple(parts[1:4])
        if key in POOL:
            tf=parts[4] if metric=='candle_heads' else '5m' if metric=='oi' else 'events'
            GROUPS[(metric,*key,tf)].append(k)
for key in sorted(POOL):
    ex,mkt,sym=key
    for tf in MAIN_TFS:
        if ex=='okx' and tf=='8h':continue
        GROUPS.setdefault(('candle_heads',*key,tf),[])
    GROUPS.setdefault(('oi',*key,'5m' if ex=='binance' else 'snapshot'),[])
    GROUPS.setdefault(('funding',*key,'events'),[])

gapfile=(OUT/'冷热合并内部缺槽.csv').open('w',encoding='utf-8-sig',newline='')
gw=csv.DictWriter(gapfile,fieldnames=['metric','exchange','market','symbol','period','previous_ts','next_ts','from_utc','to_utc','missing_slots','classification']);gw.writeheader()
coverage=[];anomaly=[];empty=[];meta=collections.Counter()
c=ro()
for idx,((metric,ex,mkt,sym,tf),paths) in enumerate(sorted(GROUPS.items()),1):
    arrays=[];coldrows=0;offgrid=0
    col='open_time' if metric=='candle_heads' else 'ts' if metric=='oi' else 'funding_time'
    for path in paths:
        pf=pq.ParquetFile(ARC/path)
        for batch in pf.iter_batches(batch_size=65536,columns=[col]):
            a=batch.column(0).to_numpy(zero_copy_only=False);a=a[a<=FIXED_NOW];arrays.append(a);coldrows+=len(a)
    if metric=='candle_heads':
        hot=c.execute('SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time+?<=? ORDER BY open_time',(ex,mkt,sym,tf,IV[tf],FIXED_NOW)).fetchall()
    elif metric=='oi':
        hot=c.execute('SELECT ts FROM oi_history WHERE exchange=? AND market=? AND symbol=? AND period=? AND ts<=? ORDER BY ts',(ex,mkt,sym,tf,FIXED_NOW)).fetchall()
    else:
        hot=c.execute('SELECT funding_time FROM funding_history WHERE exchange=? AND market=? AND symbol=? AND funding_time<=? ORDER BY funding_time',(ex,mkt,sym,FIXED_NOW)).fetchall()
    h=np.fromiter((x[0] for x in hot),dtype=np.int64,count=len(hot));arrays.append(h)
    times=np.unique(np.concatenate(arrays)) if arrays else np.array([],dtype=np.int64)
    if metric=='candle_heads':times=times[times+IV[tf]<=FIXED_NOW]
    gaps=slots=0;iv=IV.get(tf)
    if len(times)>1:
        dif=np.diff(times)
        if metric=='funding':
            # 结算周期会改变，只列长间隔候选，不假设固定8小时。
            gi=np.flatnonzero(dif>86400000)
            for j in gi:
                gw.writerow(dict(metric=metric,exchange=ex,market=mkt,symbol=sym,period=tf,previous_ts=int(times[j]),next_ts=int(times[j+1]),from_utc=iso(int(times[j])+1),to_utc=iso(int(times[j+1])-1),missing_slots='',classification='funding_gap_over_24h_candidate'))
            gaps=len(gi)
        elif iv and tf!='snapshot':
            offgrid=int(np.count_nonzero(times%iv))
            # OKX原生网格可具有固定偏移；只在相邻时间差是整数周期时计缺槽。
            gi=np.flatnonzero((dif>iv)&(dif%iv==0))
            for j in gi:
                n=int(dif[j]//iv-1);slots+=n
                gw.writerow(dict(metric=metric,exchange=ex,market=mkt,symbol=sym,period=tf,previous_ts=int(times[j]),next_ts=int(times[j+1]),from_utc=iso(int(times[j])+iv),to_utc=iso(int(times[j+1])-iv),missing_slots=n,classification='candle_missing_grid' if metric=='candle_heads' else 'oi_source_grid_candidate_not_semantic_proof'))
            gaps=len(gi)
            irregular=int(np.count_nonzero(dif%iv))
            if irregular:anomaly.append(dict(metric=metric,exchange=ex,market=mkt,symbol=sym,period=tf,offgrid_timestamps=offgrid,irregular_intervals=irregular))
    row=dict(metric=metric,exchange=ex,market=mkt,symbol=sym,period=tf,cold_files=len(paths),cold_rows=coldrows,hot_rows=len(hot),merged_unique_timestamps=len(times),overlap_or_duplicates=coldrows+len(hot)-len(times),first_utc=iso(int(times[0])) if len(times) else None,last_utc=iso(int(times[-1])) if len(times) else None,internal_gap_segments=gaps,missing_grid_slots_candidate=slots,offgrid_timestamps=offgrid)
    coverage.append(row)
    if not len(times):empty.append(row)
    meta[metric+'_unique_timestamps']+=len(times);meta[metric+'_gap_segments']+=gaps;meta[metric+'_missing_slots_candidate']+=slots
    if idx%300==0:print('coverage',idx,'/',len(GROUPS),dict(meta),flush=True);gapfile.flush()
c.close();gapfile.close()
csvout('逐币逐周期冷热覆盖.csv',coverage,list(coverage[0]))
csvout('全无数据项目.csv',empty,list(coverage[0]))
csvout('时间网格异常.csv',anomaly,list(anomaly[0]) if anomaly else ['metric'])
dump('时间覆盖汇总.json',dict(checked_at=STAMP,fixed_now_ms=FIXED_NOW,groups=len(coverage),empty_groups=len(empty),totals=dict(meta),warnings=['内部缺槽不包含上市前或末端缺失；仍需按要求起止区间验收','OI缺槽候选必须结合原件时间语义，不自动平移或补零','资金费只列超过24h的间隔候选；不能据此宣布其余结算完整','恢复前清单仅有行数，差额不是唯一事件丢失数','5m K线保留库存审计，不改变历史冻结策略']))
print('DONE',json.dumps(dict(meta),ensure_ascii=False),'empty',len(empty),flush=True)
