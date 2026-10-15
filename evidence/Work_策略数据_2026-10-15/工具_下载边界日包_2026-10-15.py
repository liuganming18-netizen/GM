#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 边界日包下载器（物理隔离 Val 尾段）

背景：Holdout = [2026-04-15T00:00Z, 2026-10-15T00:00Z)。本地 raw 的 2026-04 月包
跨界包含 Holdout，冷档 2026.parquet 同样跨界。为物理隔离，Val 尾段
[2026-04-01, 2026-04-15) 改从官方 daily 包取（日包不跨界）。

下载：
  klines 1h/4h/15m      : data.binance.vision/data/futures/um/daily/klines/{sym}/{tf}/{sym}-{tf}-{date}.zip
  markPriceKlines 1h    : data.binance.vision/data/futures/um/daily/markPriceKlines/{sym}/1h/{sym}-1h-{date}.zip
缓存：_dl/klines/{tf}/{sym}/... 与 _dl/mark1h/{sym}/...
断点：_dl/_dl_state.json  (done / absent)
只读源、只写 _dl/；不触碰原件、不写生产库。
"""
from __future__ import annotations
import json, sys, time, zipfile, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
import urllib.error
import urllib.parse

BASE = "https://data.binance.vision/data/futures/um/daily"
OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
DL = OUT_ROOT / "_dl"
STATE = DL / "_dl_state.json"
POOL = OUT_ROOT / "pool.json"

HIST = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载")

# 边界尾段（不含 Holdout 起点 2026-04-15）
D0 = datetime(2026, 4, 1, tzinfo=timezone.utc)
D1 = datetime(2026, 4, 15, tzinfo=timezone.utc)   # 不含
DAYS = [(D0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((D1 - D0).days)]

KLINE_TFS = ("1h", "4h", "15m")

UA = {"User-Agent": "workbuddy-research/1.0"}


def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def url_for(kind, sym, tf, date):
    q = urllib.parse.quote(sym, safe="")
    if kind == "klines":
        return f"{BASE}/klines/{q}/{tf}/{q}-{tf}-{date}.zip"
    else:
        return f"{BASE}/markPriceKlines/{q}/1h/{q}-1h-{date}.zip"


def dest_for(kind, sym, tf, date):
    if kind == "klines":
        return DL / "klines" / tf / sym / f"{sym}-{tf}-{date}.zip"
    return DL / "mark1h" / sym / f"{sym}-1h-{date}.zip"


def fetch(url, dest, retries=3):
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            # 校验 zip 可读
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if not names:
                    raise ValueError("empty zip")
                z.read(names[0])[:64]
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".zip.part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return ("ok", len(data))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ("absent", 0)
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (k + 1))
                continue
            return ("err", f"HTTP {e.code}")
        except Exception as e:
            time.sleep(0.8 * (k + 1))
            last = f"{type(e).__name__}: {e}"
    return ("err", last)


def local_monthly_present(kind, sym, tf):
    if kind == "klines":
        return (HIST / f"raw/{tf}/{sym}/{sym}-{tf}-2026-04.zip").is_file()
    return (HIST / f"raw/mark1h/{sym}/{sym}-1h-2026-04.zip").is_file()


def build_tasks(symbols):
    tasks = []
    for kind, tf in [("klines", "1h"), ("klines", "4h"), ("klines", "15m"), ("mark", "1h")]:
        for sym in symbols:
            if not local_monthly_present(kind, sym, tf):
                continue
            for d in DAYS:
                tasks.append((kind, sym, tf, d))
    return tasks


def main():
    ap_symbols = sys.argv[1] if len(sys.argv) > 1 else ""
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    pool = json.load(open(POOL))
    symbols = pool["symbols"]
    if ap_symbols:
        symbols = [s.strip() for s in ap_symbols.split(",") if s.strip()]

    DL.mkdir(parents=True, exist_ok=True)
    state = json.load(open(STATE)) if STATE.is_file() else {"done": {}, "absent": {}, "err": {}}
    done = state["done"]; absent = state["absent"]

    tasks = build_tasks(symbols)
    todo = []
    for kind, sym, tf, d in tasks:
        dest = dest_for(kind, sym, tf, d)
        key = str(dest.relative_to(DL))
        if dest.is_file() or key in absent:
            continue
        todo.append((kind, sym, tf, d, key))

    log(f"symbols={len(symbols)} tasks={len(tasks)} todo={len(todo)} workers={workers}")
    t0 = time.time()
    n_ok = n_abs = n_err = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, url_for(k, s, t, d), dest_for(k, s, t, d)): (k, s, t, d, key)
                for (k, s, t, d, key) in todo}
        for i, f in enumerate(as_completed(futs), 1):
            k, s, t, d, key = futs[f]
            st, info = f.result()
            if st == "ok":
                done[key] = info; n_ok += 1
            elif st == "absent":
                absent[key] = 1; n_abs += 1
            else:
                state["err"][key] = str(info); n_err += 1
            if i % 500 == 0:
                STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                log(f"progress {i}/{len(todo)} ok={n_ok} absent={n_abs} err={n_err} elapsed={time.time()-t0:.0f}s")
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    log(f"DONE ok={n_ok} absent={n_abs} err={n_err} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
