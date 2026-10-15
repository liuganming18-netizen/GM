#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 物理隔离 IS/Val 研究输入构建器

窗口（半开 UTC）：
  IS  = [2021-10-01T00:00Z, 2025-01-01T00:00Z)   # 含 IS 起点前 90 天+20 根预热
  Val = [2024-10-01T00:00Z, 2026-04-15T00:00Z)   # 含 Val 起点前 90 天+20 根预热
Holdout = [2026-04-15T00:00Z, 2026-10-15T00:00Z)  ← 本窗完全不读取

物理隔离来源（只读；不整读跨界文件）：
  OHLCV 1h/4h/15m : 冷档年分片 candles/.../{Y<=2025}.parquet（全部 < 2026-04-15）
                    + 2026-01..03 raw 月包（不跨界）
                    + 2026-04-01..14 官方 daily 日包（_dl/klines）
  OI + 4 比率      : raw_oi_metrics 官方日包（日粒度，精确截到 <=2026-04-14）
  资金费           : raw fundingRate 月包（<=2026-03）+ _dl/funding_api（API 尾段，含真实 markPrice）
  结算 mark        : raw mark1h 月包（<=2026-03）+ _dl/mark1h（daily 日包）
  合约身份/精度    : exchangeInfo + pool

输出：WyckoffMaster_v31/{is,val}/{ohlcv/{tf},oi_metrics,funding,mark1h,instruments.parquet}
禁止：写生产库、移动/删除原件、读取 Holdout。
"""
from __future__ import annotations
import argparse, csv, io, json, os, sys, time, zipfile, traceback
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# ---------- 路径 ----------
SRC = Path("/Users/boos/WorkBuddy AI")
ARCHIVE = SRC / "加密货币趋势判断系统/data/archive/parquet"
HIST = SRC / "2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"
RAW_METRICS = HIST / "raw_oi_metrics"
RAW_FUNDING = HIST / "raw/funding"
RAW_MARK1H = HIST / "raw/mark1h"

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
DL = OUT_ROOT / "_dl"
POOL_FILE = OUT_ROOT / "pool.json"

TFS = ("1h", "4h", "15m")
COMP = "zstd"
MS_DAY = 86400_000
HOLD_START = int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)  # 硬边界

WINDOWS = {
    "is":  (int(datetime(2021, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)),
    "val": (int(datetime(2024, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)),
}
MONTH_MAX = (2026, 3)      # 月包可用上限（2026-04 月包跨界，禁用）


def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def ms(dt):
    return int(datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------- OHLCV ----------
CAND_COLS = ["open_time", "close_time", "open", "high", "low", "close", "volume", "quote_volume",
             "trade_count", "taker_buy_volume", "taker_buy_quote_volume", "is_closed"]
ZIP_KLINE_HDR = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                 "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def _parse_zip_klines(path):
    """返回 [(open_time, close_time, o,h,l,c, vol, qvol, cnt, tbv, tbqv)]"""
    out = []
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0]).decode("utf-8")
    rd = csv.reader(io.StringIO(raw))
    first = next(rd, None)
    if first is None:
        return out
    has_hdr = not first[0].replace(".", "").isdigit()
    idx = {n: i for i, n in enumerate(ZIP_KLINE_HDR)}
    if has_hdr:
        try:
            idx = {n: first.index(n) for n in ZIP_KLINE_HDR}
        except ValueError:
            pass
    else:
        rd = iter([first] + list(rd))
    for row in rd:
        if len(row) < 12:
            continue
        try:
            ot = int(row[idx["open_time"]]); ct = int(row[idx["close_time"]])
            o = float(row[idx["open"]]); h = float(row[idx["high"]])
            l = float(row[idx["low"]]); c = float(row[idx["close"]])
            v = float(row[idx["volume"]]); qv = float(row[idx["quote_volume"]])
            cnt = int(float(row[idx["count"]]))
            tbv = float(row[idx["taker_buy_volume"]]); tbqv = float(row[idx["taker_buy_quote_volume"]])
        except (ValueError, TypeError, IndexError):
            continue
        out.append((ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv))
    return out


def build_ohlcv(sym, tf, w0, w1):
    rows = {}
    # 1) 冷档年分片（Y<=2025，全部 pre-Holdout）
    d = ARCHIVE / f"candles/binance/um/{sym}/{tf}"
    if d.is_dir():
        for f in sorted(d.glob("*.parquet")):
            y = int(f.stem) if f.stem.isdigit() else 0
            if y == 0 or y > 2025:
                continue
            t = pq.read_table(str(f), columns=CAND_COLS).to_pydict()
            for i in range(len(t["open_time"])):
                ot = t["open_time"][i]
                if ot < w0 or ot >= w1:
                    continue
                rows[ot] = (ot, t["close_time"][i], float(t["open"][i]), float(t["high"][i]),
                            float(t["low"][i]), float(t["close"][i]), float(t["volume"][i]),
                            float(t["quote_volume"][i]), int(t["trade_count"][i] or 0),
                            float(t["taker_buy_volume"][i] or 0), float(t["taker_buy_quote_volume"][i] or 0),
                            int(t["is_closed"][i] or 0), "cold_parquet")
    n_cold = len(rows)
    # 2) 2026-01..03 raw 月包（不跨界）
    if w1 > ms("2026-01-01"):
        for mo in ("2026-01", "2026-02", "2026-03"):
            z = HIST / f"raw/{tf}/{sym}/{sym}-{tf}-{mo}.zip"
            if not z.is_file():
                continue
            try:
                for (ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv) in _parse_zip_klines(z):
                    if ot < w0 or ot >= w1 or ot in rows:
                        continue
                    rows[ot] = (ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv, 1, f"raw.monthly.{mo}")
            except Exception:
                continue
    n_month = len(rows) - n_cold
    # 3) 2026-04-01..14 官方日包（_dl）
    n_daily = 0
    if w1 > ms("2026-04-01"):
        dd = DL / "klines" / tf / sym
        if dd.is_dir():
            for f in sorted(dd.glob(f"{sym}-{tf}-2026-04-*.zip")):
                try:
                    for (ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv) in _parse_zip_klines(f):
                        if ot < w0 or ot >= w1 or ot in rows:
                            continue
                        rows[ot] = (ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv, 1, "vision.daily")
                        n_daily += 1
                except Exception:
                    continue
    if not rows:
        return None
    keys = sorted(rows)
    cols = list(zip(*[rows[k] for k in keys]))
    tbl = pa.table({
        "open_time": pa.array(cols[0], pa.int64()),
        "close_time": pa.array(cols[1], pa.int64()),
        "open": pa.array(cols[2], pa.float64()),
        "high": pa.array(cols[3], pa.float64()),
        "low": pa.array(cols[4], pa.float64()),
        "close": pa.array(cols[5], pa.float64()),
        "volume": pa.array(cols[6], pa.float64()),
        "quote_volume": pa.array(cols[7], pa.float64()),
        "trade_count": pa.array(cols[8], pa.int64()),
        "taker_buy_volume": pa.array(cols[9], pa.float64()),
        "taker_buy_quote_volume": pa.array(cols[10], pa.float64()),
        "is_closed": pa.array(cols[11], pa.int64()),
        "source": pa.array(cols[12], pa.string()),
    })
    out = OUT_ROOT / f"{WIN}/ohlcv/{tf}/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "cold": n_cold, "month": n_month, "daily": n_daily,
            "first": keys[0], "last": keys[-1]}


# ---------- OI + 4 比率（raw_oi_metrics 官方日包，日粒度） ----------
MET_FIELDS = ["sum_open_interest", "sum_open_interest_value",
              "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
              "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]


def _parse_metrics_zip(path):
    out = []
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0]).decode("utf-8")
    rd = csv.reader(io.StringIO(raw))
    hdr = next(rd, None)
    if not hdr or "create_time" not in hdr:
        return out
    idx = {n: hdr.index(n) for n in MET_FIELDS}
    ci = hdr.index("create_time")
    for row in rd:
        if len(row) < len(hdr):
            continue
        try:
            ms_ = int(datetime.strptime(row[ci], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
        try:
            vals = [float(row[idx[n]]) for n in MET_FIELDS]
        except (ValueError, TypeError):
            continue
        out.append((ms_, vals))
    return out


def _dates_in(w0, w1):
    d0 = datetime.utcfromtimestamp(w0 / 1000).date()
    d1 = datetime.utcfromtimestamp((w1 - 1) / 1000).date()
    from datetime import timedelta
    out = []
    d = d0
    while d <= d1:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def build_oi_metrics(sym, w0, w1):
    d = RAW_METRICS / sym
    if not d.is_dir():
        return None
    rows = {}
    n_zip = 0
    for ds in _dates_in(w0, w1):
        f = d / f"{sym}-metrics-{ds}.zip"
        if not f.is_file():
            continue
        n_zip += 1
        try:
            for ms_, vals in _parse_metrics_zip(f):
                if ms_ < w0 or ms_ >= w1:
                    continue
                if ms_ not in rows:
                    rows[ms_] = [ms_] + vals + ["binance.vision.metrics.daily"]
        except Exception:
            continue
    if not rows:
        return None
    keys = sorted(rows)
    cols = list(zip(*[rows[k] for k in keys]))
    a = lambda i, t: pa.array(cols[i], t)
    tbl = pa.table({
        "ts": a(0, pa.int64()),
        "open_interest": a(1, pa.float64()),
        "open_interest_value": a(2, pa.float64()),
        "count_long_short_ratio": a(5, pa.float64()),           # M5 全网账户比（本轮前向，观察）
        "sum_toptrader_long_short_ratio": a(4, pa.float64()),   # M6 大户持仓比（本轮前向，观察）
        "count_toptrader_long_short_ratio": a(3, pa.float64()), # 大户账户比（观察）
        "sum_taker_long_short_vol_ratio": a(6, pa.float64()),   # Taker（观察）
        "source": a(7, pa.string()),
    })
    out = OUT_ROOT / f"{WIN}/oi_metrics/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "zips": n_zip, "first": keys[0], "last": keys[-1]}


# ---------- 结算 mark（mark1h：月包 <=2026-03 + daily 尾段） ----------
def build_mark1h(sym, w0, w1):
    rows = {}
    n_month = n_daily = 0
    d = RAW_MARK1H / sym
    if d.is_dir():
        for f in sorted(d.glob(f"{sym}-1h-2*.zip")):
            name = f.name
            # 只要 <=2026-03 的月包
            try:
                mo = name.replace(f"{sym}-1h-", "").replace(".zip", "")
            except Exception:
                continue
            if len(mo) == 7 and mo > "2026-03":
                continue
            if len(mo) == 10:      # daily 本地（仅 2026-10-*）—— 一律不用
                continue
            try:
                for (ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv) in _parse_zip_klines(f):
                    if ot < w0 or ot >= w1 or ot in rows:
                        continue
                    rows[ot] = (float(o), f"binance.vision.markPriceKlines.{mo}"); n_month += 1
            except Exception:
                continue
    dd = DL / "mark1h" / sym
    if dd.is_dir():
        for f in sorted(dd.glob(f"{sym}-1h-2026-04-*.zip")):
            try:
                for (ot, ct, o, h, l, c, v, qv, cnt, tbv, tbqv) in _parse_zip_klines(f):
                    if ot < w0 or ot >= w1 or ot in rows:
                        continue
                    rows[ot] = (float(o), "vision.daily"); n_daily += 1
            except Exception:
                continue
    if not rows:
        return None
    keys = sorted(rows)
    tbl = pa.table({
        "open_time": pa.array(keys, pa.int64()),
        "mark_open": pa.array([rows[k][0] for k in keys], pa.float64()),
        "source": pa.array([rows[k][1] for k in keys], pa.string()),
    })
    out = OUT_ROOT / f"{WIN}/mark1h/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "month": n_month, "daily": n_daily,
            "first": keys[0], "last": keys[-1]}


# ---------- 资金费（月包 <=2026-03 + API 尾段） ----------
def build_funding(sym, w0, w1, mark_map):
    rows = {}
    d = RAW_FUNDING / sym
    if d.is_dir():
        for f in sorted(d.glob(f"{sym}-fundingRate-2*.zip")):
            mo = f.name.replace(f"{sym}-fundingRate-", "").replace(".zip", "")
            if len(mo) != 7 or mo > "2026-03":
                continue
            try:
                with zipfile.ZipFile(f) as z:
                    raw = z.read(z.namelist()[0]).decode("utf-8")
                rd = csv.reader(io.StringIO(raw))
                hdr = next(rd, None)
                if not hdr:
                    continue
                ci = hdr.index("calc_time"); ri = hdr.index("last_funding_rate")
                for row in rd:
                    if len(row) <= max(ci, ri):
                        continue
                    ft = int(row[ci])
                    if ft < w0 or ft >= w1 or ft in rows:
                        continue
                    rows[ft] = [ft, float(row[ri]), None, "settled", f"binance.vision.fundingRate.{mo}"]
            except Exception:
                continue
    n_month = len(rows)
    # API 尾段（2026-04-01..14，含真实 markPrice）
    n_api = 0
    fj = DL / "funding_api" / f"{sym}.json"
    if fj.is_file():
        try:
            d = json.load(open(fj))
            for r in d.get("rows", []):
                ft = int(r["fundingTime"])
                if ft < w0 or ft >= w1 or ft in rows:
                    continue
                fr = float(r["fundingRate"]) if r.get("fundingRate") not in (None, "") else None
                mk = float(r["markPrice"]) if r.get("markPrice") not in (None, "") else None
                rows[ft] = [ft, fr, mk, r.get("rateType") or "settled", "binance.fapi.fundingRate"]
                n_api += 1
        except Exception:
            pass
    if not rows:
        return None
    # 结算 mark：优先 API 已带；否则用 mark1h 的整点桶 open
    n_mark = 0
    for ft, r in rows.items():
        if r[2] is not None:
            continue
        k = int(round(ft / 3600000.0)) * 3600000
        mv = mark_map.get(k)
        if mv is not None:
            r[2] = mv
            r[4] = r[4] + "+mark1h"
            n_mark += 1
    keys = sorted(rows)
    cols = list(zip(*[rows[k] for k in keys]))
    tbl = pa.table({
        "funding_time": pa.array(cols[0], pa.int64()),
        "funding_rate": pa.array(cols[1], pa.float64()),
        "mark_price": pa.array(cols[2], pa.float64()),
        "rate_type": pa.array(cols[3], pa.string()),
        "source": pa.array(cols[4], pa.string()),
    })
    out = OUT_ROOT / f"{WIN}/funding/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "month": n_month, "api": n_api, "mark_from_mark1h": n_mark,
            "mark_missing": sum(1 for r in rows.values() if r[2] is None),
            "first": keys[0], "last": keys[-1]}


# ---------- instruments ----------
def build_instruments():
    import glob
    ei = sorted(glob.glob(str(HIST / "标的池/exchangeInfo_futures_*.json")))
    filt = {}
    if ei:
        d = json.load(open(ei[-1]))
        for s in d.get("symbols", []):
            f = {x["filterType"]: x for x in s.get("filters", [])}
            filt[s["symbol"]] = (f.get("PRICE_FILTER", {}).get("tickSize"),
                                 f.get("LOT_SIZE", {}).get("stepSize"),
                                 s.get("pricePrecision"), s.get("quantityPrecision"),
                                 s.get("onboardDate"))
    pool = json.load(open(POOL_FILE))
    rows = []
    for c in pool["coins"]:
        s = c["symbol"]
        tick, step, pp, qp, ob = filt.get(s, (None, None, None, None, None))
        rows.append((s, c.get("base_asset"), c.get("quote_asset"), c.get("contract_type"),
                     c.get("status"), c.get("onboard_time") or (int(ob) if ob else None),
                     tick, step, pp, qp))
    tbl = pa.table({
        "symbol": pa.array([r[0] for r in rows], pa.string()),
        "base_asset": pa.array([r[1] for r in rows], pa.string()),
        "quote_asset": pa.array([r[2] for r in rows], pa.string()),
        "contract_type": pa.array([r[3] for r in rows], pa.string()),
        "status": pa.array([r[4] for r in rows], pa.string()),
        "onboard_time": pa.array([r[5] for r in rows], pa.int64()),
        "tick_size": pa.array([r[6] for r in rows], pa.string()),
        "step_size": pa.array([r[7] for r in rows], pa.string()),
        "price_precision": pa.array([r[8] for r in rows], pa.int64()),
        "quantity_precision": pa.array([r[9] for r in rows], pa.int64()),
    })
    out = OUT_ROOT / "instruments.parquet"
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(rows), "exchange_info": ei[-1] if ei else None,
            "with_tick": sum(1 for r in rows if r[6])}


# ---------- 主流程 ----------
_G = {}


def _init_worker(win, w0, w1, datasets):
    global WIN
    WIN = win
    _G["win"] = win; _G["w0"] = w0; _G["w1"] = w1; _G["ds"] = datasets


def _load_mark_map(sym, w0, w1):
    p = OUT_ROOT / f"{WIN}/mark1h/{sym}.parquet"
    if not p.is_file():
        return {}
    t = pq.read_table(str(p)).to_pydict()
    return {t["open_time"][i]: t["mark_open"][i] for i in range(len(t["open_time"]))}


def _work(args):
    sym, datasets = args
    try:
        win = _G["win"]; w0 = _G["w0"]; w1 = _G["w1"]
        res = {}
        if "ohlcv" in datasets:
            for tf in TFS:
                r = build_ohlcv(sym, tf, w0, w1)
                if r: res[f"ohlcv_{tf}"] = r
        if "oi_metrics" in datasets:
            r = build_oi_metrics(sym, w0, w1)
            if r: res["oi_metrics"] = r
        if "mark" in datasets:
            r = build_mark1h(sym, w0, w1)
            if r: res["mark1h"] = r
        if "funding" in datasets:
            mk = _load_mark_map(sym, w0, w1) if "mark" not in datasets else None
            r = build_funding(sym, w0, w1, mk if mk is not None else _mark_map_from_disk(sym))
            if r: res["funding"] = r
        return (sym, datasets, res, None)
    except Exception as e:
        return (sym, datasets, None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")


def _mark_map_from_disk(sym):
    p = OUT_ROOT / f"{WIN}/mark1h/{sym}.parquet"
    if not p.is_file():
        return {}
    t = pq.read_table(str(p)).to_pydict()
    return {t["open_time"][i]: t["mark_open"][i] for i in range(len(t["open_time"]))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True, choices=["is", "val"])
    ap.add_argument("--symbols", default="")
    ap.add_argument("--datasets", default="ohlcv,oi_metrics,mark,funding")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--state", default="")
    ap.add_argument("--pilot", action="store_true")
    a = ap.parse_args()

    global WIN
    WIN = a.window
    w0, w1 = WINDOWS[a.window]
    assert w1 <= HOLD_START, "窗口不得触及 Holdout"
    datasets = [d.strip() for d in a.datasets.split(",") if d.strip()]
    pool = json.load(open(POOL_FILE))
    syms = pool["symbols"]
    if a.symbols:
        syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    if a.limit:
        syms = syms[:a.limit]

    state_path = Path(a.state) if a.state else (OUT_ROOT / f"_state/build_{WIN}.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.load(open(state_path)) if state_path.is_file() else {"done": {}, "results": {}}

    log(f"window={WIN} [{w0},{w1}) symbols={len(syms)} datasets={datasets} workers={a.workers}")

    if "instruments" not in state.get("meta_done", []):
        try:
            r = build_instruments()
            state.setdefault("meta_done", []).append("instruments")
            state.setdefault("meta_results", {})["instruments"] = r
            log(f"instruments rows={r['rows']} with_tick={r['with_tick']}")
        except Exception as e:
            log(f"instruments ERR {e}")

    todo = []
    for sym in syms:
        done_ds = set(state["done"].get(sym, []))
        need = [d for d in datasets if d not in done_ds]
        if need:
            todo.append((sym, need))

    if a.pilot:
        _init_worker(WIN, w0, w1, datasets)
        for sym, need in todo[:a.limit or 5]:
            s, ds, r, e = _work((sym, need))
            print(s, ds, "ERR" if e else "OK", json.dumps(r, ensure_ascii=False)[:260], e or "")
        return

    from multiprocessing import Pool
    t0 = time.time(); ok = err = 0
    with Pool(processes=a.workers, initializer=_init_worker, initargs=(WIN, w0, w1, datasets)) as p:
        for i, (sym, need, r, e) in enumerate(p.imap_unordered(_work, todo, chunksize=1)):
            if e:
                err += 1; state.setdefault("errors", {})[sym] = e; log(f"ERR {sym}: {e.splitlines()[0]}")
            else:
                ok += 1
                state["done"][sym] = sorted(set(state["done"].get(sym, [])) | set(need))
                state["results"][sym] = r
            if (i + 1) % 20 == 0:
                state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                log(f"progress {i+1}/{len(todo)} ok={ok} err={err} elapsed={time.time()-t0:.0f}s")
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    log(f"DONE window={WIN} ok={ok} err={err} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
