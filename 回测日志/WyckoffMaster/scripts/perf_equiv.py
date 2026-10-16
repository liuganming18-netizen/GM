#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能测量 → 优化 → **等价性证明**（优化前后逐笔逐位一致）。

三态对照（同一数据、同一配置、同一窗口、同一标的集）：
  A 基线：`funding_rescan=True`（每根对**全量**资金费事件重扫）+ `commit_every=1`（每行提交）
  B 优化①：`funding_rescan=False`（资金费**单调游标**，只结算新增事件）+ `commit_every=1`
  C 优化②：`funding_rescan=False` + `commit_every=1000`（批量提交）

要求：A、B、C 的 signals / orders / positions / funding / equity 逐行**完全一致**，
review 汇总字段完全一致；否则优化不合格（不承认任何"差不多"）。

优化点（均不改变决定）：
  1. 资金费事件：单调游标，避免每根 15m 重扫全部历史结算事件。
  2. 账本提交：`commit_every>1` 批量提交（SQLite WAL）。
  3. 标记价每根只算一次；入场候选按 (symbol, bar) 建索引，避免 O(n) 反查。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

WM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WM))

from engine import paths as P             # noqa: E402
from engine import loader as LD           # noqa: E402
from engine.engine import Backtest, LevelSpec  # noqa: E402

UTC = dt.timezone.utc
TABLES = {
    "signals": ["symbol", "signal_time", "entry_bar_time", "kind", "decision", "reject_reason"],
    "orders": ["symbol", "side", "status", "ts", "fill_price", "qty", "fee", "slippage",
               "notional", "reason", "position_id", "stage"],
    "positions": ["position_id", "symbol", "entry_time", "entry_price", "quantity", "stop_price",
                  "entry_fee", "exit_time", "exit_price", "exit_reason", "exit_fee", "funding",
                  "gross_pnl", "net_pnl"],
    "funding": ["position_id", "funding_time", "rate", "mark", "quantity", "amount"],
    "equity": ["ts", "cash", "equity", "peak", "drawdown", "halted", "n_positions"],
}
REVIEW_KEYS = ("trades", "final_equity", "gross_pnl", "fees", "funding_net", "net_pnl",
               "max_drawdown", "halted")


def ms(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
               .timestamp() * 1000)


def dump(path):
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    out = {}
    for t, cols in TABLES.items():
        rows = con.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()
        out[t] = [tuple(r[c] for c in cols) for r in rows]
    rev = con.execute("SELECT * FROM review LIMIT 1").fetchone()
    out["review"] = {k: rev[k] for k in REVIEW_KEYS}
    con.close()
    return out


def compare(a, b):
    diff = {}
    for t in TABLES:
        if a[t] != b[t]:
            n = min(len(a[t]), len(b[t]))
            first = next((i for i in range(n) if a[t][i] != b[t][i]), n)
            diff[t] = {"n_a": len(a[t]), "n_b": len(b[t]), "first_diff_row": first}
    for k in REVIEW_KEYS:
        if a["review"].get(k) != b["review"].get(k):
            diff[f"review.{k}"] = {"a": a["review"].get(k), "b": b["review"].get(k)}
    return diff


def run(tag, spec, tf, seg, start, end, symbols, *, commit_every, funding_rescan):
    sd = LD.SegmentData(seg)
    path = WM / "ledgers" / f"perf_{tag}.sqlite3"
    t0 = time.time()
    bt = Backtest(seg_data=sd, spec=spec, timeframe=tf, start_ms=start, end_ms=end,
                  run_id=tag, ledger_path=path, symbols=symbols,
                  commit_every=commit_every, funding_rescan=funding_rescan)
    r = bt.run(); bt.led.close()
    return {"secs": round(time.time() - t0, 3), "result": r, "path": path}


def main():
    seg = "Val"; tf = "1h"
    start = ms("2025-01-01T00:00:00Z"); end = ms("2025-04-01T00:00:00Z")
    sd = LD.SegmentData(seg)
    symbols = sd.symbols(tf)[:40]
    spec = LevelSpec("M2", "three_buy_points", "structural", "none", "quote_vol_30d_median", "M1")

    A = run("A_rescan_c1", spec, tf, seg, start, end, symbols, commit_every=1, funding_rescan=True)
    B = run("B_cursor_c1", spec, tf, seg, start, end, symbols, commit_every=1, funding_rescan=False)
    C = run("C_cursor_c1000", spec, tf, seg, start, end, symbols, commit_every=1000,
            funding_rescan=False)

    da, db, dc = dump(A["path"]), dump(B["path"]), dump(C["path"])
    diff_ab = compare(da, db)      # 资金费游标优化：必须无差异
    diff_bc = compare(db, dc)      # 批量提交优化：必须无差异
    diff_ac = compare(da, dc)
    rep = {
        "schema": "wyckoffmaster_perf_equiv/v2",
        "segment": seg, "timeframe": tf, "n_symbols": len(symbols), "window": [start, end],
        "configs": {
            "A_baseline_rescan_perrow": {"secs": A["secs"], "commit_every": 1,
                                         "funding_rescan": True,
                                         "counts": A["result"]["counts"]},
            "B_cursor_perrow": {"secs": B["secs"], "commit_every": 1, "funding_rescan": False,
                                "counts": B["result"]["counts"]},
            "C_cursor_batch": {"secs": C["secs"], "commit_every": 1000, "funding_rescan": False,
                               "counts": C["result"]["counts"]},
        },
        "row_counts": {t: len(da[t]) for t in TABLES},
        "speedup_funding_cursor": round(A["secs"] / B["secs"], 3) if B["secs"] else None,
        "speedup_batch_commit": round(B["secs"] / C["secs"], 3) if C["secs"] else None,
        "speedup_total": round(A["secs"] / C["secs"], 3) if C["secs"] else None,
        "equivalent_A_vs_B": len(diff_ab) == 0, "diffs_A_vs_B": diff_ab,
        "equivalent_B_vs_C": len(diff_bc) == 0, "diffs_B_vs_C": diff_bc,
        "equivalent_A_vs_C": len(diff_ac) == 0, "diffs_A_vs_C": diff_ac,
        "equivalent": len(diff_ab) == 0 and len(diff_bc) == 0 and len(diff_ac) == 0,
    }
    (WM / "state" / "perf_equiv.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(json.dumps({k: rep[k] for k in (
        "configs", "row_counts", "speedup_funding_cursor", "speedup_batch_commit",
        "speedup_total", "equivalent_A_vs_B", "equivalent_B_vs_C", "equivalent_A_vs_C",
        "equivalent", "diffs_A_vs_B", "diffs_B_vs_C", "diffs_A_vs_C")},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
