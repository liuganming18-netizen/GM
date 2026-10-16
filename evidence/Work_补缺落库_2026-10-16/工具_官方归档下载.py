#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance Vision 官方归档下载器（带 CHECKSUM 校验）。

提供 monthly_klines() / daily_klines()，返回与补缺行同构的 dict 列表。
校验：下载 `<file>.zip` 与其 `.CHECKSUM`，sha256 必须一致，否则拒绝使用。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

UA = {'User-Agent': 'wb-fill/1.0'}
VISION = 'https://data.binance.vision/data/futures/um'
COLS = ['exchange', 'market', 'symbol', 'timeframe', 'open_time', 'close_time', 'open',
        'high', 'low', 'close', 'volume', 'quote_volume', 'trade_count',
        'taker_buy_volume', 'taker_buy_quote_volume', 'source_url']
# 归档 CSV 列序：open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_vol,taker_buy_quote_vol,ignore
IDX = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def _get(url, tries=4, timeout=90):
    last = ''
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read(), 'OK'
        except urllib.error.HTTPError as e:
            return None, f'HTTP {e.code}'
        except Exception as e:      # noqa: BLE001
            last = f'{type(e).__name__}: {str(e)[:60]}'
            time.sleep(1.5 * (i + 1))
    return None, last


def _fetch_zip(kind, sym, tf, tag):
    base = f'{VISION}/{kind}/klines/{sym}/{tf}/{sym}-{tf}-{tag}'
    raw, st = _get(base + '.zip')
    if raw is None:
        return None, st, None
    ck, cst = _get(base + '.zip.CHECKSUM')
    if ck is None:
        return None, f'CHECKSUM 缺失({cst})', None
    want = ck.decode().split()[0].strip().lower()
    got = hashlib.sha256(raw).hexdigest()
    if want != got:
        return None, f'sha256 不符 want={want[:16]} got={got[:16]}', None
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.endswith('.csv'))
    text = z.read(name).decode('utf-8', 'replace')
    return text, 'OK', f'{base}.zip'


def _rows_from_csv(text, sym, tf, mk, url):
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().isdigit():
            continue
        p = [row[i] if i < len(row) else '' for i in IDX]
        out.append({
            'exchange': 'binance', 'market': mk, 'symbol': sym, 'timeframe': tf,
            'open_time': int(p[0]), 'open': p[1], 'high': p[2], 'low': p[3], 'close': p[4],
            'volume': p[5], 'close_time': int(p[6]), 'quote_volume': p[7],
            'trade_count': int(p[8]) if p[8].strip().isdigit() else 0,
            'taker_buy_volume': p[9], 'taker_buy_quote_volume': p[10], 'source_url': url,
        })
    return out


def monthly_klines(sym, tf, ym, mk='um'):
    """ym 形如 '2026-01'。"""
    text, st, url = _fetch_zip('monthly', sym, tf, ym)
    if text is None:
        return [], st, None
    return _rows_from_csv(text, sym, tf, mk, url), 'OK', url


def daily_klines(sym, tf, day, mk='um'):
    """day 形如 '2022-02-26'。"""
    text, st, url = _fetch_zip('daily', sym, tf, day)
    if text is None:
        return [], st, None
    return _rows_from_csv(text, sym, tf, mk, url), 'OK', url


def months_between(a_iso, b_iso):
    """[a_iso, b_iso] 闭区间内的 'YYYY-MM' 列表。"""
    a = datetime.fromisoformat(a_iso); b = datetime.fromisoformat(b_iso)
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f'{y}-{m:02d}')
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out
