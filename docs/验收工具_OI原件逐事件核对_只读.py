#!/usr/bin/env python3
import collections,csv,datetime,functools,hashlib,io,json,zipfile
from pathlib import Path
R=Path('/Users/boos/Desktop/威科夫因子共振趋势/evidence/数据完整度验收_2026-10-14')
RAW=Path('/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics')
g=[x for x in csv.DictReader((R/'冷热合并内部缺槽.csv').open(encoding='utf-8-sig')) if x['metric']=='oi' and x['exchange']=='binance']
@functools.lru_cache(maxsize=1024)
def load(path):
    p=Path(path);check=Path(path+'.CHECKSUM');cs='checksum_absent'
    if check.exists():cs='MATCH' if hashlib.sha256(p.read_bytes()).hexdigest()==check.read_text().split()[0].lower() else 'MISMATCH'
    ts=[]
    with zipfile.ZipFile(p) as z:
        n=next(x for x in z.namelist() if x.endswith('.csv'))
        with z.open(n) as src:
            for row in csv.reader(io.TextIOWrapper(src,encoding='utf-8-sig',newline='')):
                if not row or row[0]=='create_time':continue
                ts.append(int(datetime.datetime.strptime(row[0],'%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc).timestamp()*1000))
    return tuple(set(ts)),cs
counts=collections.Counter();indexes={};rows=[]
for i,x in enumerate(g,1):
    sym=x['symbol'];lo=int(x['previous_ts'])+1;hi=int(x['next_ts'])-1
    a=datetime.datetime.fromtimestamp(lo/1000,datetime.timezone.utc).date();b=datetime.datetime.fromtimestamp(hi/1000,datetime.timezone.utc).date()
    if sym not in indexes:indexes[sym]={p.stem[-10:]:str(p) for p in (RAW/sym).glob('*.zip')}
    paths=[p for d,p in indexes[sym].items() if str(a)<=d<=str(b)]
    found=set();bad=[];unverified=0
    for p in paths:
        try:
            ts,cs=load(p);witness={t for t in ts if lo<=t<=hi}
            if cs=='MATCH':found.update(witness)
            elif witness:unverified+=len(witness)
            if cs=='MISMATCH':bad.append(p)
        except Exception as e:bad.append(p+':'+repr(e))
    kind='proven_missing_raw_events' if found else 'source_zip_has_no_event_in_gap' if paths else 'no_surviving_day_zip'
    row=dict(symbol=sym,period='5m',from_utc=x['from_utc'],to_utc=x['to_utc'],missing_grid_slots_candidate=x['missing_slots'],surviving_day_zips=len(paths),verified_raw_events_absent=len(found),unverified_raw_events=unverified,status=kind,first_raw_event_ms=min(found) if found else '',last_raw_event_ms=max(found) if found else '',source_errors=';'.join(bad))
    rows.append(row);counts[kind]+=1;counts['proven_missing_raw_events_total']+=len(found);counts['source_errors']+=len(bad)
    if i%1000==0:print('OI_raw',i,'/',len(g),dict(counts),flush=True)
with (R/'OI缺槽原件逐事件核对.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
(R/'OI原件逐事件核对汇总.json').write_text(json.dumps(dict(counts=dict(counts),note='核对已发现的内部缺槽候选，不覆盖上市前端和缺失历史时段的所有应有记录；不平移、取整或补零'),ensure_ascii=False,indent=2))
print('OI_RAW_DONE',dict(counts),flush=True)
