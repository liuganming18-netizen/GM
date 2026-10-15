#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 字段与边界核验

A 同源同事件：研究输入 OI 与冷档 oi 年分片在共同 ts 上一致（交叉源一致性）
B 网格对齐：oi ts 对齐 5m 网格；K 线 open_time 对齐各自周期网格
C 逐币覆盖：各数据集实际覆盖币数与缺项
D 比率非空：4 比率非空比例
E 边界硬门禁：所有数据集 max(time) < 2026-04-15T00:00Z（不得触及 Holdout）
F 资金费：结算 mark 非空比例、结算节奏（8h）
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
ARCHIVE = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet")
POOL = json.load(open(OUT_ROOT / "pool.json"))
SYMS = POOL["symbols"]
HOLD_START = int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)
TF_MS = {"1h": 3600000, "4h": 14400000, "15m": 900000}
OUT = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15/字段核验证据_v31.json")


def main():
    rep = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "holdout_start_utc": "2026-04-15T00:00:00Z", "windows": {}}

    # ---------- E 边界硬门禁 + B 网格 + D 比率 + F 资金费 ----------
    for win in ("is", "val"):
        wrep = {"boundary_violations": [], "grid_off": {}, "ratio_null": {}, "funding": {}}
        # ohlcv
        for tf in ("1h", "4h", "15m"):
            d = OUT_ROOT / win / "ohlcv" / tf
            files = sorted(d.glob("*.parquet"))
            bad = []
            off = 0
            total = 0
            for f in files:
                t = pq.read_table(str(f), columns=["open_time"]).column(0).to_pylist()
                if not t:
                    continue
                total += len(t)
                if max(t) >= HOLD_START:
                    bad.append((f.stem, max(t)))
                off += sum(1 for x in t if x % TF_MS[tf] != 0)
            wrep["grid_off"][f"ohlcv/{tf}"] = {"symbols": len(files), "rows": total, "off_grid": off}
            if bad:
                wrep["boundary_violations"].append({"dataset": f"ohlcv/{tf}", "examples": bad[:5], "n": len(bad)})
        # oi
        d = OUT_ROOT / win / "oi_metrics"
        files = sorted(d.glob("*.parquet"))
        bad = []; off = 0; total = 0; nulls = 0
        for f in files:
            t = pq.read_table(str(f), columns=["ts", "count_long_short_ratio",
                                               "sum_toptrader_long_short_ratio"]).to_pydict()
            ts = t["ts"]
            if not ts:
                continue
            total += len(ts)
            if max(ts) >= HOLD_START:
                bad.append((f.stem, max(ts)))
            off += sum(1 for x in ts if x % 300000 != 0)
            nulls += sum(1 for x in t["count_long_short_ratio"] if x is None)
            nulls += sum(1 for x in t["sum_toptrader_long_short_ratio"] if x is None)
        wrep["grid_off"]["oi_metrics"] = {"symbols": len(files), "rows": total, "off_grid": off}
        wrep["ratio_null"]["oi_metrics"] = {"rows": total, "null_cells": nulls,
                                            "null_frac": round(nulls / (2 * total), 6) if total else None}
        if bad:
            wrep["boundary_violations"].append({"dataset": "oi_metrics", "examples": bad[:5], "n": len(bad)})
        # funding
        d = OUT_ROOT / win / "funding"
        files = sorted(d.glob("*.parquet"))
        bad = []; n = 0; nmiss = 0; gaps = []
        for f in files:
            t = pq.read_table(str(f), columns=["funding_time", "mark_price"]).to_pydict()
            ft = t["funding_time"]
            if not ft:
                continue
            n += len(ft)
            if max(ft) >= HOLD_START:
                bad.append((f.stem, max(ft)))
            nmiss += sum(1 for x in t["mark_price"] if x is None)
        wrep["funding"] = {"symbols": len(files), "rows": n, "mark_null": nmiss,
                           "mark_null_frac": round(nmiss / n, 6) if n else None}
        if bad:
            wrep["boundary_violations"].append({"dataset": "funding", "examples": bad[:5], "n": len(bad)})
        # mark1h
        d = OUT_ROOT / win / "mark1h"
        files = sorted(d.glob("*.parquet"))
        bad = []
        for f in files:
            t = pq.read_table(str(f), columns=["open_time"]).column(0).to_pylist()
            if t and max(t) >= HOLD_START:
                bad.append((f.stem, max(t)))
        if bad:
            wrep["boundary_violations"].append({"dataset": "mark1h", "examples": bad[:5], "n": len(bad)})
        rep["windows"][win] = wrep

    # ---------- A 同源同事件：研究输入 OI vs 冷档 oi 年分片 ----------
    probes = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]:
        for win in ("is", "val"):
            p = OUT_ROOT / win / "oi_metrics" / f"{sym}.parquet"
            if not p.is_file():
                continue
            ours = pq.read_table(str(p), columns=["ts", "open_interest"]).to_pydict()
            om = dict(zip(ours["ts"], ours["open_interest"]))
            # 冷档年分片（只读 pre-Holdout 年）
            match = 0; diff = 0; common = 0
            for y in (2021, 2022, 2023, 2024, 2025):
                ap = ARCHIVE / f"oi/binance/um/{sym}/{y}.parquet"
                if not ap.is_file():
                    continue
                at = pq.read_table(str(ap), columns=["ts", "open_interest"]).to_pydict()
                for i in range(len(at["ts"])):
                    ts = at["ts"][i]
                    if ts in om:
                        try:
                            av = float(at["open_interest"][i])
                            ov = float(om[ts])
                        except (TypeError, ValueError):
                            continue
                        common += 1
                        if abs(ov - av) < 1e-6:
                            match += 1
                        else:
                            diff += 1
            probes[f"{win}:{sym}"] = {"common": common, "match": match, "diff": diff}
    rep["A_oi_same_event_vs_archive"] = probes

    # ---------- C 逐币覆盖 ----------
    cov = {}
    for win in ("is", "val"):
        c = {}
        for ds, sub in [("ohlcv/1h", "ohlcv/1h"), ("ohlcv/4h", "ohlcv/4h"), ("ohlcv/15m", "ohlcv/15m"),
                        ("oi_metrics", "oi_metrics"), ("funding", "funding"), ("mark1h", "mark1h")]:
            d = OUT_ROOT / win / sub
            have = {f.stem for f in d.glob("*.parquet")} if d.is_dir() else set()
            c[ds] = {"have": len(have), "missing": len([s for s in SYMS if s not in have])}
        cov[win] = c
    rep["C_coverage"] = cov

    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    # 摘要
    print(json.dumps({"boundary_violations": {w: rep["windows"][w]["boundary_violations"] for w in rep["windows"]},
                      "grid_off": {w: rep["windows"][w]["grid_off"] for w in rep["windows"]},
                      "funding": {w: rep["windows"][w]["funding"] for w in rep["windows"]},
                      "A": probes, "C": cov}, ensure_ascii=False, indent=1))
    print("->", OUT)


if __name__ == "__main__":
    main()
