#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冷层追加补缺 —— 把缺失 K 线写入冷档 Parquet（**只新增分片文件，绝不改写既有文件**）。

设计要点
--------
1. 落点 = 冷档 `parquet/candles/` + `parquet/candle_heads/`（按 `{YYYY}_补缺<tag>.parquet` 命名）。
   两者必须成对写入，且 `source_id` 一致 —— 读侧 `MarketDataRepository._cold_candles`
   用 `JOIN ... USING(exchange,market,symbol,timeframe,open_time,source_id)` 取 head 版本。
2. **只做加法**：既有 `{YYYY}.parquet` 一个字节都不动 ⇒ 回滚 = 删除本脚本列出的新增文件。
3. 写入前逐根核对「冷∪热」现状，已存在的 open_time 一律跳过 ⇒ 不产生重复版本。
4. 每个 (exchange,market,symbol,timeframe) 分配一个 `sources.id`（= 写入时 max(id)+n），
   并 INSERT 进 sqlite `sources`，供 `sources_index()` 的「更新时间/来源」显示。
5. 默认 dry-run；`--apply` 才落盘。

用法
----
  $PY 工具_冷层追加补缺.py --rows <rows.csv|rows.jsonl> --tag 20261016
  $PY 工具_冷层追加补缺.py --rows <rows.csv> --tag 20261016 --apply
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path('/Users/boos/Desktop/威科夫因子共振趋势')
DB = ROOT / '数据/市场数据/db/market_v2.sqlite3'
ARC = ROOT / '数据/市场数据/parquet'

CANDLE_COLS = ["exchange", "market", "symbol", "timeframe", "open_time", "close_time",
               "open", "high", "low", "close", "volume", "quote_volume", "trade_count",
               "taker_buy_volume", "taker_buy_quote_volume", "source_id", "available_at",
               "is_closed"]
CANDLE_TYPES = [pa.string(), pa.string(), pa.string(), pa.string(), pa.int64(), pa.int64(),
                pa.string(), pa.string(), pa.string(), pa.string(), pa.string(), pa.string(),
                pa.int64(), pa.string(), pa.string(), pa.int64(), pa.int32(), pa.int64()]
HEAD_COLS = ["exchange", "market", "symbol", "timeframe", "open_time", "source_id"]
HEAD_TYPES = [pa.string(), pa.string(), pa.string(), pa.string(), pa.int64(), pa.int64()]

TF_MS = {'1m': 60000, '5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000,
         '2h': 7200000, '4h': 14400000, '8h': 28800000, '12h': 43200000, '1d': 86400000}

SCHEMA_C = pa.schema(list(zip(CANDLE_COLS, CANDLE_TYPES)))
SCHEMA_H = pa.schema(list(zip(HEAD_COLS, HEAD_TYPES)))


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', flush=True)


def load_rows(p: Path):
    if p.suffix == '.jsonl':
        return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
    with p.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def existing_times(duck, ex, mk, sym, tf):
    """冷∪热 已存在的 open_time 集合。"""
    have = set()
    d = ARC / 'candle_heads' / ex / mk / sym / tf
    files = sorted(str(p) for p in d.glob('*.parquet')) if d.is_dir() else []
    if files:
        try:
            for (t,) in duck.execute('SELECT open_time FROM read_parquet(?)', [files]).fetchall():
                have.add(int(t))
        except Exception as e:      # noqa: BLE001
            log(f'  ! 冷层读取失败 {ex}/{mk}/{sym}/{tf}: {e}')
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=30)
    try:
        for (t,) in con.execute(
                'SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? AND timeframe=?',
                (ex, mk, sym, tf)):
            have.add(int(t))
    finally:
        con.close()
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rows', required=True)
    ap.add_argument('--tag', required=True, help='如 20261016')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--out-dir', default=None)
    a = ap.parse_args()

    out_dir = Path(a.out_dir) if a.out_dir else (ROOT / 'evidence/Work_补缺落库_2026-10-16')
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(Path(a.rows))

    # 归一化
    norm = []
    for r in rows:
        tf = r['timeframe']
        ot = int(r['open_time'])
        ct = int(r['close_time']) if str(r.get('close_time') or '').strip() else ot + TF_MS[tf] - 1
        norm.append({
            'exchange': r['exchange'], 'market': r['market'], 'symbol': r['symbol'],
            'timeframe': tf, 'open_time': ot, 'close_time': ct,
            'open': str(r['open']), 'high': str(r['high']), 'low': str(r['low']),
            'close': str(r['close']), 'volume': str(r['volume']),
            # 空字符串**原样保留**：OKX 无 taker_buy_* 字段，库内既有约定就是 ''
            # （见 candle_field_notes：stored_as=''），不得用 '0' 冒充。
            'quote_volume': '' if r.get('quote_volume') is None else str(r['quote_volume']),
            'trade_count': int(r['trade_count']) if str(r.get('trade_count') or '').strip() else 0,
            'taker_buy_volume': '' if r.get('taker_buy_volume') is None else str(r['taker_buy_volume']),
            'taker_buy_quote_volume': '' if r.get('taker_buy_quote_volume') is None else str(r['taker_buy_quote_volume']),
            'source_url': r.get('source_url') or r.get('source') or 'backfill',
        })

    groups = {}
    for r in norm:
        groups.setdefault((r['exchange'], r['market'], r['symbol'], r['timeframe']), []).append(r)

    log(f'输入 {len(norm)} 行 / {len(groups)} 个组合')

    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=30)
    duck = duckdb.connect()
    duck.execute('PRAGMA threads=2')

    base_id = con.execute('SELECT COALESCE(MAX(id),0) FROM sources').fetchone()[0]
    # 预留整百万段，避免与采集器/下载器并发自增抢 id（采集器每几分钟就在插 sources 行）
    band_base = (base_id // 1_000_000 + 1) * 1_000_000
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    plan, created, skipped_dup, total_new = [], [], 0, 0
    for i, (key, rs) in enumerate(sorted(groups.items()), 1):
        ex, mk, sym, tf = key
        have = existing_times(duck, ex, mk, sym, tf)
        new = sorted([r for r in rs if r['open_time'] not in have], key=lambda r: r['open_time'])
        dup = len(rs) - len(new)
        skipped_dup += dup
        if not new:
            continue
        total_new += len(new)
        sid = band_base + len(plan) + 1
        plan.append({'key': key, 'sid': sid, 'n': len(new), 'dup': dup,
                     'first': new[0]['open_time'], 'last': new[-1]['open_time'],
                     'source_url': new[0]['source_url'], 'rows': new})
        if i % 25 == 0:
            log(f'  规划 {i}/{len(groups)} 新增={total_new}')

    log(f'规划完成：新增 {total_new} 行 / {len(plan)} 个组合；已存在跳过 {skipped_dup} 行')
    for p in plan[:5]:
        log(f"    {p['key']} +{p['n']} (跳过 {p['dup']}) sid={p['sid']}")
    if len(plan) > 5:
        log(f'    ... 共 {len(plan)} 个组合')

    if not a.apply:
        log('DRY-RUN：未写盘。加 --apply 才落盘。')
        con.close(); duck.close()
        return

    # ---------- 落盘 ----------
    sid_con = sqlite3.connect(str(DB), timeout=120)
    sid_con.execute('PRAGMA busy_timeout=120000')
    for p in plan:
        ex, mk, sym, tf = p['key']
        sid = p['sid']
        rs = p['rows']
        by_year = {}
        for r in rs:
            y = datetime.datetime.fromtimestamp(r['open_time'] / 1000, datetime.timezone.utc).year
            by_year.setdefault(y, []).append(r)
        for y, yrows in sorted(by_year.items()):
            cpath = ARC / 'candles' / ex / mk / sym / tf / f'{y}_补缺{a.tag}.parquet'
            hpath = ARC / 'candle_heads' / ex / mk / sym / tf / f'{y}_补缺{a.tag}.parquet'
            for path in (cpath, hpath):
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    raise SystemExit(f'拒绝覆盖已存在文件：{path}')
            ct = pa.Table.from_pylist(
                [{**{c: r[c] for c in CANDLE_COLS if c not in ('source_id', 'available_at', 'is_closed')},
                  'source_id': sid, 'available_at': None, 'is_closed': 1} for r in yrows],
                schema=SCHEMA_C)
            pq.write_table(ct, cpath, compression='zstd')
            ht = pa.Table.from_pylist(
                [{**{c: r[c] for c in HEAD_COLS if c != 'source_id'}, 'source_id': sid} for r in yrows],
                schema=SCHEMA_H)
            pq.write_table(ht, hpath, compression='zstd')
            created.append({'metric': 'candles', 'path': str(cpath.relative_to(ROOT)), 'rows': len(yrows)})
            created.append({'metric': 'candle_heads', 'path': str(hpath.relative_to(ROOT)), 'rows': len(yrows)})
        # sources 登记
        blob = json.dumps([[r['open_time'], r['close']] for r in rs], separators=(',', ':')).encode()
        sha = hashlib.sha256(blob).hexdigest()
        sid_con.execute(
            'INSERT INTO sources(id,exchange,market,symbol,timeframe,source_url,file_path,sha256,'
            'imported_at,downloaded_at,first_open,last_close,row_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (sid, ex, mk, sym, tf, rs[0]['source_url'],
             f'parquet/candles/{ex}/{mk}/{sym}/{tf}/{a.tag}_补缺', sha, now_ms, now_ms,
             rs[0]['open_time'], rs[-1]['close_time'], len(rs)))
        sid_con.commit()

    sid_con.close(); con.close(); duck.close()

    man = {'tag': a.tag, 'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'rows_in': len(norm), 'rows_new': total_new, 'rows_skipped_existing': skipped_dup,
           'combos': len(plan), 'files_created': len(created),
           'sources_id_range': [band_base + 1, band_base + len(plan)],
           'per_combo': [{'key': '/'.join(p['key']), 'sid': p['sid'], 'n': p['n']} for p in plan],
           'created': created}
    (out_dir / f'落盘清单_{a.tag}.json').write_text(json.dumps(man, ensure_ascii=False, indent=1))
    log(f'APPLIED 新增 {total_new} 行 / 新建 {len(created)} 个分片文件 / sources.id {band_base+1}..{band_base+len(plan)}')
    log(f'清单 -> 落盘清单_{a.tag}.json')


if __name__ == '__main__':
    main()
