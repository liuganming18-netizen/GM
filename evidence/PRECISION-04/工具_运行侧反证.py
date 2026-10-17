#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-04 工具：运行侧反证 —— 六标的既有 run 用了什么价格步长。

不读规格、不猜 tick，只做一件事：
  把 run 的 trades.jsonl 里的成交价，与同一时刻 chart.json 的 K 线区间比对。
成交价落在 [low, high] 之外 ⇒ 这个价格不是当时市场能成交的价格 ⇒ 该 run 的
撮合用了**不属于该合约的价格网格**（实测是写死的 0.01）。

这只用于证伪，不用于推断 tick。
"""
import datetime
import glob
import json
import os

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
RUNS = os.path.join(ROOT, "回测日志/event_trend_v0_1/runs")
HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]


def norm(c):
    if isinstance(c, dict):
        return (c.get("time") or c.get("t"), c.get("open") or c.get("o"),
                c.get("high") or c.get("h"), c.get("low") or c.get("l"))
    return c[0], c[1], c[2], c[3]


def main():
    out = []
    for sym in SYMBOLS:
        d = glob.glob(os.path.join(RUNS, f"run_WX_EVENT_TREND_V0_1_{sym}_*"))
        if not d:
            continue
        d = d[0]
        ch = json.load(open(os.path.join(d, "chart.json"), encoding="utf-8"))
        bars = [norm(c) for c in (ch.get("candles") or [])]
        trades = [json.loads(l) for l in open(os.path.join(d, "trades.jsonl"), encoding="utf-8") if l.strip()]
        rec = {"symbol": sym, "run_id": os.path.basename(d),
               "has_instrument_spec_file": os.path.exists(os.path.join(d, "instrument_spec.json")),
               "fills": []}
        for t in trades:
            for tag, ts, px in (("entry", t["entry_time"], t["entry_price"]),
                                ("exit", t["exit_time"], t["exit_price"])):
                b = min(bars, key=lambda x: abs(x[0] - ts)) if bars else None
                if not b:
                    continue
                inside = b[3] <= px <= b[2]
                rec["fills"].append({
                    "leg": tag, "fill_price": px, "bar_time_ms": b[0],
                    "bar_low": b[3], "bar_high": b[2],
                    "bar_utc": datetime.datetime.fromtimestamp(b[0] / 1000, datetime.UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "inside_bar_range": inside,
                    "note": "" if inside else "成交价落在该 K 线区间之外 ⇒ 物理上不可能成交",
                })
        rec["all_inside"] = all(f["inside_bar_range"] for f in rec["fills"])
        out.append(rec)

    with open(os.path.join(HERE, "运行侧反证.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "method": "成交价 vs 同刻 15m K 线 [low, high]；仅证伪，不推断 tick",
                   "runs": out}, f, ensure_ascii=False, indent=1)

    for r in out:
        print(f"{r['symbol']:<13} spec文件={r['has_instrument_spec_file']} 全部在区间内={r['all_inside']}")
        for f in r["fills"]:
            print(f"   {f['leg']:<5} {f['fill_price']:<12} 区间[{f['bar_low']}, {f['bar_high']}] "
                  f"{'OK' if f['inside_bar_range'] else '越界'}")


if __name__ == "__main__":
    main()
