#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎冒烟：小窗口（少量标的、3 个月）跑 M0–M3_Step，打印逐层汇总。

用于快速验证引擎可运行、口径分列、段末强平生效；非验收交付。
运行：
  /Users/boos/.workbuddy-ai/binaries/python/envs/freqtrade/bin/python \
      回测日志/WyckoffMaster/scripts/smoke_engine.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

WM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WM))

from engine import paths as P          # noqa: E402
from engine import loader as LD        # noqa: E402
from engine.engine import Backtest, LevelSpec  # noqa: E402


def ms(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
               .timestamp() * 1000)


SPECS = {
    "M0": LevelSpec("M0", "donchian_only", "uniform", "none", "quote_vol_30d_median"),
    "M1": LevelSpec("M1", "three_buy_points", "uniform", "none", "quote_vol_30d_median", "M0"),
    "M2": LevelSpec("M2", "three_buy_points", "structural", "none", "quote_vol_30d_median", "M1"),
    "M2b": LevelSpec("M2b", "three_buy_points", "structural_m2b", "none",
                     "quote_vol_30d_median", "M2"),
    "M3_BE": LevelSpec("M3_BE", "three_buy_points", "structural", "naive_be",
                       "quote_vol_30d_median", "M2"),
    "M3_Step": LevelSpec("M3_Step", "three_buy_points", "structural", "step_swing",
                         "quote_vol_30d_median", "M2"),
}


def main():
    seg = "Val"
    tf = "1h"
    start = ms("2025-01-01T00:00:00Z")
    end = ms("2025-04-01T00:00:00Z")          # 3 个月小窗口
    sd = LD.SegmentData(seg)
    syms = sd.symbols(tf)
    subset = syms[:40]
    print(f"seg={seg} tf={tf} symbols={len(subset)} window=[{start},{end})")
    led_dir = WM / "ledgers"
    led_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in ("M0", "M1", "M2", "M2b", "M3_BE", "M3_Step"):
        t0 = time.time()
        bt = Backtest(seg_data=sd, spec=SPECS[name], timeframe=tf, start_ms=start, end_ms=end,
                      run_id=f"smoke_{name}", ledger_path=led_dir / f"smoke_{name}.sqlite3",
                      symbols=subset, commit_every=1)
        r = bt.run(); bt.led.close()
        out[name] = {"trades": r["trades"], "net": round(r["net"], 2),
                     "gross": round(r["gross"], 2), "fees": round(r["fees"], 2),
                     "funding": round(r["funding"], 2), "max_dd": round(r["max_dd"], 4),
                     "rejects": r["rejects"], "kinds": r["consumed"]["kinds"],
                     "secs": round(time.time() - t0, 1)}
        print(name, json.dumps(out[name], ensure_ascii=False))
    (WM / "state" / "smoke_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote state/smoke_result.json")


if __name__ == "__main__":
    main()
