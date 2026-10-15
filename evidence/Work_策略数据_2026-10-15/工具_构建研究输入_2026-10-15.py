#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WyckoffMaster 研究输入构建器（只读源 → 研究输入 Parquet/ZSTD）。

职责：把已核验的只读源（热库 market_v2 / 冷档 parquet / 补件 / raw_oi_metrics 原件）
按明确字段 schema 整理为逐币 Parquet，写入
  /Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster/

只读边界：所有源以 mode=ro / query_only 打开；不写、不移动、不删除任何原件。
断点：_state/build_state.json 记录已完成 (symbol,dataset)，重跑跳过。
"""
from __future__ import annotations
import argparse, csv, io, json, os, sqlite3, sys, time, zipfile, traceback
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# ---------- 路径常量（只读源） ----------
SRC_ROOT = Path("/Users/boos/WorkBuddy AI")
HOT_DB   = SRC_ROOT / "加密货币趋势判断系统/data/market_v2.sqlite3"
ARCHIVE  = SRC_ROOT / "加密货币趋势判断系统/data/archive/parquet"
BACKEND  = SRC_ROOT / "2026-10-03-16-11-14/outputs/网站逆向研究/后端实现"
HIST     = BACKEND / "历史下载"
RAW_METRICS = HIST / "raw_oi_metrics"
RAW_MARK1H  = HIST / "raw/mark1h"
RAW_FUNDING = HIST / "raw/funding"
POOL_FILE = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster/pool.json")
PATCH_DIR = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_补缺_2026-10-14")

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")

TFS = ("1h", "4h", "15m")
COMP = "zstd"

# ---------- 工具 ----------
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def hot_conn():
    uri = HOT_DB.as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con

def read_parquet_cols(path, cols):
    return pq.read_table(str(path), columns=cols).to_pydict()

# ---------- OHLCV ----------
CAND_COLS = ["open_time","close_time","open","high","low","close","volume","quote_volume",
             "trade_count","taker_buy_volume","taker_buy_quote_volume","is_closed"]

def build_ohlcv(sym, tf, con):
    rows = {}
    # 冷档
    d = ARCHIVE / f"candles/binance/um/{sym}/{tf}"
    if d.is_dir():
        for f in sorted(d.glob("*.parquet")):
            t = read_parquet_cols(f, CAND_COLS)
            for i in range(len(t["open_time"])):
                ot = t["open_time"][i]
                rows[ot] = (ot, t["close_time"][i],
                            float(t["open"][i]), float(t["high"][i]), float(t["low"][i]), float(t["close"][i]),
                            float(t["volume"][i]), float(t["quote_volume"][i]), int(t["trade_count"][i] or 0),
                            float(t["taker_buy_volume"][i] or 0), float(t["taker_buy_quote_volume"][i] or 0),
                            int(t["is_closed"][i]), "cold_parquet")
    n_cold = len(rows)
    # 热库（仅补冷档没有的 open_time）
    cur = con.execute(
        "SELECT open_time,close_time,open,high,low,close,volume,quote_volume,trade_count,"
        "taker_buy_volume,taker_buy_quote_volume,is_closed FROM candles "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND timeframe=?", (sym, tf))
    n_hot_new = 0
    for r in cur:
        ot = r[0]
        if ot in rows:
            continue
        rows[ot] = (ot, r[1], float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                    float(r[6]), float(r[7]), int(r[8] or 0), float(r[9] or 0), float(r[10] or 0),
                    int(r[11] or 0), "hot_db")
        n_hot_new += 1
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
    out = OUT_ROOT / f"ohlcv/{tf}/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "cold": n_cold, "hot_new": n_hot_new,
            "first_open": keys[0], "last_open": keys[-1]}

# ---------- funding（结算时间/rate；mark 由 mark1h 归档补） ----------
def build_funding(sym, con, patch_rows):
    rows = {}
    # 冷档 funding parquet
    f = ARCHIVE / f"funding/binance/um/{sym}/funding.parquet"
    if f.is_file():
        t = read_parquet_cols(f, ["funding_time","funding_rate","rate_type","source"])
        for i in range(len(t["funding_time"])):
            ft = t["funding_time"][i]
            rows[ft] = [ft, float(t["funding_rate"][i]), None, t["rate_type"][i] or "settled",
                        t["source"][i] or "binance.archive.fundingRate"]
    n_cold = len(rows)
    # 热库
    cur = con.execute(
        "SELECT funding_time,funding_rate,mark_price,rate_type,source FROM funding_history "
        "WHERE exchange='binance' AND market='um' AND symbol=?", (sym,))
    n_hot_new = 0
    for r in cur:
        ft = r[0]
        mrk = float(r[2]) if r[2] not in (None, "") else None
        if ft in rows:
            if rows[ft][2] is None and mrk is not None:
                rows[ft][2] = mrk
            continue
        rows[ft] = [ft, float(r[1]) if r[1] not in (None,"") else None, mrk,
                    r[3] or "settled", r[4] or "binance.fapi"]
        n_hot_new += 1
    # 补件
    n_patch = 0
    for r in patch_rows.get(sym, []):
        ft = r[0]
        if ft not in rows:
            rows[ft] = [ft, r[1], None, "settled", "binance.archive.fundingRate(patch)"]
            n_patch += 1
    if not rows:
        return None
    keys = sorted(rows)
    cols = list(zip(*[rows[k] for k in keys]))
    tbl = pa.table({
        "funding_time": pa.array(cols[0], pa.int64()),
        "funding_rate": pa.array(cols[1], pa.float64()),
        "mark_price": pa.array(cols[2], pa.float64()),
        "rate_type": pa.array(cols[3], pa.string()),
        "source": pa.array(cols[4], pa.string()),
    })
    out = OUT_ROOT / f"funding/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "cold": n_cold, "hot_new": n_hot_new, "patch": n_patch,
            "first": keys[0], "last": keys[-1]}

# ---------- OI + metrics 四比率（同源同事件：raw_oi_metrics 日包） ----------
MET_FIELDS = ["sum_open_interest","sum_open_interest_value",
              "count_toptrader_long_short_ratio","sum_toptrader_long_short_ratio",
              "count_long_short_ratio","sum_taker_long_short_vol_ratio"]

def _parse_metrics_zip(path):
    out = []
    with zipfile.ZipFile(path) as z:
        inner = z.namelist()[0]
        raw = z.read(inner).decode("utf-8")
    rd = csv.reader(io.StringIO(raw))
    hdr = next(rd)
    idx = {name: hdr.index(name) for name in MET_FIELDS}
    ci = hdr.index("create_time")
    for row in rd:
        if len(row) < len(hdr):
            continue
        ts = row[ci]
        try:
            ms = int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
        try:
            vals = [float(row[idx[n]]) for n in MET_FIELDS]
        except (ValueError, TypeError):
            continue
        out.append((ms, vals))
    return out

def build_oi_metrics(sym, con, patch_rows_oi):
    d = RAW_METRICS / sym
    rows = {}
    n_zip = 0
    if d.is_dir():
        for f in sorted(d.glob("*.zip")):
            n_zip += 1
            try:
                for ms, vals in _parse_metrics_zip(f):
                    if ms not in rows:      # 旧年份存在整行重复 → 保留首个
                        rows[ms] = [ms] + vals + ["binance.vision.metrics.daily"]
            except Exception:
                continue
    n_archive = len(rows)
    # 热库 OI（2026-10-08 起 API 口径 + 快照），仅补归档没有的 ts
    n_hot_new = 0
    cur = con.execute(
        "SELECT ts,open_interest,open_interest_value,source FROM oi_history "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND period='5m'", (sym,))
    for r in cur:
        ts = r[0]
        if ts in rows:
            continue
        rows[ts] = [ts, float(r[1] or 0), float(r[2] or 0), None, None, None, None,
                    r[3] or "binance.fapi.openInterestHist"]
        n_hot_new += 1
    # 补件 OI（归档 7 行 + API 13,745 行）
    n_patch = 0
    for r in patch_rows_oi.get(sym, []):
        ts = r[0]
        if ts not in rows:
            rows[ts] = r
            n_patch += 1
    if not rows:
        return None
    keys = sorted(rows)
    cols = list(zip(*[rows[k] for k in keys]))
    def arr(i, typ):
        return pa.array(cols[i], typ)
    # 行布局: [ts, oi, oi_value, count_toptrader, sum_toptrader, count_lsr, sum_taker, source]
    tbl = pa.table({
        "ts": arr(0, pa.int64()),
        "open_interest": arr(1, pa.float64()),
        "open_interest_value": arr(2, pa.float64()),
        "count_long_short_ratio": arr(5, pa.float64()),          # M5 全网账户比
        "sum_toptrader_long_short_ratio": arr(4, pa.float64()),  # M6 大户持仓比
        "count_toptrader_long_short_ratio": arr(3, pa.float64()),# 大户账户比（观察）
        "sum_taker_long_short_vol_ratio": arr(6, pa.float64()),  # Taker（观察）
        "source": arr(7, pa.string()),
    })
    out = OUT_ROOT / f"oi_metrics/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "zips": n_zip, "archive": n_archive, "hot_new": n_hot_new,
            "patch": n_patch, "first": keys[0], "last": keys[-1]}

# ---------- 结算 mark（mark1h 官方归档：结算时刻所在 1h 桶的 open） ----------
def build_mark1h(sym):
    d = RAW_MARK1H / sym
    if not d.is_dir():
        return None
    rows = {}
    for f in sorted(d.glob("*.zip")):
        try:
            with zipfile.ZipFile(f) as z:
                raw = z.read(z.namelist()[0]).decode("utf-8")
            rd = csv.reader(io.StringIO(raw))
            for row in rd:
                if not row or not row[0].isdigit():
                    continue
                ot = int(row[0])
                rows[ot] = float(row[1])   # open = 该小时起始 mark
        except Exception:
            continue
    if not rows:
        return None
    keys = sorted(rows)
    tbl = pa.table({
        "open_time": pa.array(keys, pa.int64()),
        "mark_open": pa.array([rows[k] for k in keys], pa.float64()),
    })
    out = OUT_ROOT / f"mark1h/{sym}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression=COMP)
    return {"rows": len(keys), "first": keys[0], "last": keys[-1]}

# ---------- 补件加载 ----------
def load_patch_funding():
    out = {}
    f = PATCH_DIR / "补缺_资金费_import.csv"
    if not f.is_file():
        return out
    with open(f, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            s = row.get("symbol")
            try:
                out.setdefault(s, []).append((int(row["funding_time"]), float(row["funding_rate"])))
            except (ValueError, KeyError, TypeError):
                continue
    return out

def _f(x):
    try:
        return float(x)
    except (ValueError, TypeError):
        return None

def load_patch_oi():
    """补件 OI：两种表头。
      A) period/ts + open_interest/open_interest_value        （API / 官方归档）
      B) timeframe/create_time_utc + sum_open_interest/... 含 4 比率（archive.metrics）
    统一为 [ts, oi, oi_value, c_top, s_top, c_lsr, s_taker, source]
    """
    out = {}
    specs = [
        ("补缺_OI_API_import.csv", "ts", "open_interest", "open_interest_value", "binance.fapi.openInterestHist(patch)"),
        ("补缺_OI_1008_1013_官方归档_import.csv", "ts", "open_interest", "open_interest_value", "binance.vision.metrics.daily(patch)"),
        ("补缺_OI_import.csv", "create_time_utc", "sum_open_interest", "sum_open_interest_value", "binance.vision.daily.metrics(patch)"),
    ]
    for name, tcol, oicol, oivcol, src in specs:
        f = PATCH_DIR / name
        if not f.is_file():
            continue
        with open(f, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                s = row.get("symbol")
                if not s:
                    continue
                tval = row.get(tcol)
                if tval is None:
                    continue
                if tcol == "ts":
                    try:
                        ts = int(tval)
                    except ValueError:
                        continue
                else:
                    try:
                        ts = int(datetime.strptime(tval, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                    except ValueError:
                        continue
                oi = _f(row.get(oicol)); oiv = _f(row.get(oivcol))
                ctop = _f(row.get("count_toptrader_long_short_ratio"))
                stop = _f(row.get("sum_toptrader_long_short_ratio"))
                clsr = _f(row.get("count_long_short_ratio"))
                stak = _f(row.get("sum_taker_long_short_vol_ratio"))
                out.setdefault(s, []).append([ts, oi, oiv, ctop, stop, clsr, stak, src])
    return out

# ---------- instruments（合约身份与价格/数量精度） ----------
def process_symbol(sym, con, patch_f, patch_o, datasets):
    res = {}
    if "ohlcv" in datasets:
        for tf in TFS:
            r = build_ohlcv(sym, tf, con)
            if r: res[f"ohlcv_{tf}"] = r
    if "funding" in datasets:
        r = build_funding(sym, con, patch_f)
        if r: res["funding"] = r
    if "oi_metrics" in datasets:
        r = build_oi_metrics(sym, con, patch_o)
        if r: res["oi_metrics"] = r
    if "mark" in datasets:
        r = build_mark1h(sym)
        if r: res["mark1h"] = r
    return res

def build_instruments():
    import glob
    ei_files = sorted(glob.glob(str(HIST / "标的池/exchangeInfo_futures_*.json")))
    filt = {}
    if ei_files:
        d = json.load(open(ei_files[-1]))
        for s in d.get("symbols", []):
            f = {x["filterType"]: x for x in s.get("filters", [])}
            tick = f.get("PRICE_FILTER", {}).get("tickSize")
            step = f.get("LOT_SIZE", {}).get("stepSize")
            filt[s["symbol"]] = (tick, step, s.get("pricePrecision"), s.get("quantityPrecision"),
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
    return {"rows": len(rows), "exchange_info": ei_files[-1] if ei_files else None,
            "with_tick": sum(1 for r in rows if r[6])}

# ---------- 主流程 ----------
_G = {}

def _init_worker():
    _G["con"] = hot_conn()
    _G["pf"] = load_patch_funding()
    _G["po"] = load_patch_oi()

def _work(args):
    sym, datasets = args
    try:
        r = process_symbol(sym, _G["con"], _G["pf"], _G["po"], datasets)
        return (sym, datasets, r, None)
    except Exception as e:
        return (sym, datasets, None, f"{type(e).__name__}: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔；空=全部池")
    ap.add_argument("--datasets", default="ohlcv,funding,oi_metrics,mark")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--state", default=str(OUT_ROOT / "_state/build_state.json"))
    ap.add_argument("--pilot", action="store_true")
    a = ap.parse_args()

    datasets = [d.strip() for d in a.datasets.split(",") if d.strip()]
    pool = json.load(open(POOL_FILE))
    syms = pool["symbols"]
    if a.symbols:
        syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    if a.limit:
        syms = syms[:a.limit]

    state_path = Path(a.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.load(open(state_path)) if state_path.is_file() else {"done": {}, "results": {}}

    log(f"start symbols={len(syms)} datasets={datasets} workers={a.workers} pilot={a.pilot}")
    t0 = time.time()

    # instruments（幂等）
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
    log(f"todo symbols={len(todo)} (already done={len(syms)-len(todo)})")

    if a.pilot:
        for sym, need in todo:
            sym_, ds_, r, err = _work((sym, need)) if not _G else _work((sym, need))
            print(sym, ds_, "ERR" if err else "OK", json.dumps(r, ensure_ascii=False)[:300])
        return

    from multiprocessing import Pool
    ok = err = 0
    with Pool(processes=a.workers, initializer=_init_worker) as p:
        for i, (sym, need, r, e) in enumerate(p.imap_unordered(_work, todo, chunksize=1)):
            if e:
                err += 1
                state.setdefault("errors", {})[sym] = e
                log(f"ERR {sym}: {e}")
            else:
                ok += 1
                state["done"][sym] = sorted(set(state["done"].get(sym, [])) | set(need))
                state["results"][sym] = r
            if (i + 1) % 10 == 0:
                state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                log(f"progress {i+1}/{len(todo)} ok={ok} err={err} elapsed={time.time()-t0:.0f}s")
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    log(f"DONE ok={ok} err={err} elapsed={time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()

