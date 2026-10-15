#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · mark1h 空洞官方补齐

本地 raw mark1h 月包存在整天空洞（官方归档缺失），例如 BTCUSDT 的
2022-07-31 / 2022-10-02 / 2023-02-24。这些空洞会导致对应结算资金费缺真实 mark。
处理：仅对确实缺失的小时，从官方 daily markPriceKlines 日包补齐（pre-Holdout 干净日包），
      并给该行 source 追加 "+vision.daily.gap"。不补 0、不插值、不用普通成交价冒充。
输出：逐窗口逐币补齐，重写 mark1h parquet；报告 _state/mark_gap_report.json
"""
from __future__ import annotations
import json, sys, zipfile, io, csv, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import urllib.request, urllib.error

import pyarrow as pa
import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
DL = OUT_ROOT / "_dl"
GAP = DL / "markgap"
REPORT = OUT_ROOT / "_state/mark_gap_report.json"

HOLD_START = int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)
WINDOWS = {
    "is":  (int(datetime(2021, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)),
    "val": (int(datetime(2024, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)),
}
BASE = "https://data.binance.vision/data/futures/um/daily/markPriceKlines"
UA = {"User-Agent": "workbuddy-research/1.0"}


def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def fetch_day(sym, date):
    dest = GAP / sym / f"{sym}-1h-{date}.zip"
    if dest.is_file():
        return dest, "cached"
    url = f"{BASE}/{sym}/1h/{sym}-1h-{date}.zip"
    for k in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.read(z.namelist()[0])[:64]
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return dest, "ok"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "absent"
            time.sleep(1.0 * (k + 1))
        except Exception:
            time.sleep(0.8 * (k + 1))
    return None, "err"


def parse_mark(path):
    out = {}
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0]).decode("utf-8")
    rd = csv.reader(io.StringIO(raw))
    first = next(rd, None)
    if first is None:
        return out
    if first[0].replace(".", "").isdigit():
        rd = iter([first] + list(rd))
    for row in rd:
        if len(row) < 5:
            continue
        try:
            out[int(row[0])] = float(row[1])
        except (ValueError, TypeError):
            continue
    return out


def process_window(win, symbols):
    w0, w1 = WINDOWS[win]
    rep = {"window": win, "w0": w0, "w1": w1, "symbols": {}, "filled": 0, "unresolved": 0, "abs_day": 0}
    for si, sym in enumerate(symbols, 1):
        p = OUT_ROOT / f"{win}/mark1h/{sym}.parquet"
        if not p.is_file():
            continue
        t = pq.read_table(str(p)).to_pydict()
        have = dict(zip(t["open_time"], t["mark_open"]))
        src = dict(zip(t["open_time"], t["source"])) if "source" in t else {}
        if not have:
            continue
        # 只补“内部空洞”：首末已有数据之间缺失的小时；不补上线前/窗口外的空档
        lo = max(w0, min(have))
        hi = min(w1 - 3600000, max(have))
        expected = list(range(lo, hi + 3600000, 3600000))
        missing = [x for x in expected if x not in have]
        if not missing:
            continue
        days = sorted({datetime.fromtimestamp(m / 1000, timezone.utc).strftime("%Y-%m-%d") for m in missing})
        for d in days:
            f, st = fetch_day(sym, d)
            if f is None:
                rep["unresolved"] += len([m for m in missing
                                          if datetime.fromtimestamp(m / 1000, timezone.utc).strftime("%Y-%m-%d") == d])
                continue
            md = parse_mark(f)
            for m, v in md.items():
                if w0 <= m < w1 and m not in have:
                    have[m] = v
                    src[m] = (src.get(m, "") + "+vision.daily.gap").strip("+")
                    rep["filled"] += 1
        keys = sorted(have)
        tbl = pa.table({
            "open_time": pa.array(keys, pa.int64()),
            "mark_open": pa.array([have[k] for k in keys], pa.float64()),
            "source": pa.array([src.get(k, "") for k in keys], pa.string()),
        })
        pq.write_table(tbl, str(p), compression="zstd")
        still = [m for m in expected if m not in have]
        rep["symbols"][sym] = {"missing_before": len(missing), "missing_after": len(still),
                               "days": days}
        if si % 25 == 0:
            log(f"{win} {si}/{len(symbols)} filled={rep['filled']} unresolved={rep['unresolved']}")
    return rep


def main():
    wins = [w for w in (sys.argv[1].split(",") if len(sys.argv) > 1 else ["is", "val"]) if w]
    pool = json.load(open(OUT_ROOT / "pool.json"))
    symbols = pool["symbols"]
    reports = {}
    for win in wins:
        log(f"window={win} symbols={len(symbols)}")
        reports[win] = process_window(win, symbols)
        log(f"{win} DONE filled={reports[win]['filled']} unresolved={reports[win]['unresolved']}")
    REPORT.write_text(json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8")
    log("report -> " + str(REPORT))


if __name__ == "__main__":
    main()
