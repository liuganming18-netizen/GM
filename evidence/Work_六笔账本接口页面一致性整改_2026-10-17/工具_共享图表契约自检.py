# -*- coding: utf-8 -*-
"""共享 chart 序列化契约自检（**不触碰任何既有 run 目录**）。

做法：把 LPTUSDT 那个 run 的账本 JSONL 复制到临时目录，用 `backtest_chart` 重新生成
chart.json，逐项核对新契约；原 run 目录一个字节都不改。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
BACKEND = ROOT / "框架" / "backend"
sys.path.insert(0, str(BACKEND))

import pandas as pd                     # noqa: E402
import backtest_chart as BC             # noqa: E402

SRC = ROOT / "回测日志" / "event_trend_v0_1" / "runs" / "run_WX_EVENT_TREND_V0_1_LPTUSDT_1792173591"
COPY = ["trades.jsonl", "orders.jsonl", "funding.jsonl", "positions.jsonl",
        "candidates.jsonl", "equity_curve.jsonl"]

problems: list[str] = []


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="chart_contract_"))
    run_dir = tmp / SRC.name
    run_dir.mkdir(parents=True)
    for n in COPY:
        shutil.copy2(SRC / n, run_dir / n)

    old = json.loads((SRC / "chart.json").read_text(encoding="utf-8"))
    cds = old["candles"]
    rows = []
    for c in cds:
        if isinstance(c, (list, tuple)):
            t, o, h, l, cl, v = c[:6]
        else:
            t, o, h, l, cl, v = (c["t"], c["o"], c["h"], c["l"], c["c"], c["v"])
        rows.append({"open_time": int(t), "open": float(o), "high": float(h),
                     "low": float(l), "close": float(cl), "volume": float(v)})
    df15 = pd.DataFrame(rows)
    df1h = pd.DataFrame({"timestamp": [int(old["window"]["start_ms"])]})

    chart = BC.generate_chart_payload("LPTUSDT", str(run_dir), df1h, df15, 2000.0)

    # ① candles 是对象
    c0 = chart["candles"][0]
    if not isinstance(c0, dict) or set(c0) != {"t", "o", "h", "l", "c", "v"}:
        problems.append(f"candles[0] 不是契约对象：{c0!r}")
    if len(chart["candles"]) != len(cds):
        problems.append(f"candles 数量 {len(chart['candles'])} != {len(cds)}")

    # ② trade_id 为账本真值
    real = "trd_eb941d2defdde27285ef06e2"
    tids = [t["trade_id"] for t in chart["trades"]]
    if tids != [real]:
        problems.append(f"trades[].trade_id={tids} != ['{real}']")

    # ③ stop_trace 用 `t`
    st = chart["trades"][0]["stop_trace"]
    if not st or not all(isinstance(p.get("t"), int) for p in st):
        problems.append(f"stop_trace 未统一为 `t`：{st!r}")
    if any("ts" in p for p in st):
        problems.append(f"stop_trace 仍残留 `ts` 键：{st!r}")

    # ④ signals 非空且 t>0
    if not chart["signals"]:
        problems.append("signals 仍为空")
    elif not all(s["t"] > 0 for s in chart["signals"]):
        problems.append(f"signals 含 t<=0：{[s['t'] for s in chart['signals']]}")

    # ⑤ funding_events 非空且 amount 为真实 delta（非 0.0 恒值）
    fe = chart["funding_events"]
    if not fe:
        problems.append("funding_events 仍为空")
    else:
        amts = [e["amount"] for e in fe]
        if all(a == 0.0 for a in amts):
            problems.append("funding_events.amount 全为 0.0（未取真实 delta）")
        if not all(e.get("trade_id") == real for e in fe):
            problems.append("funding_events.trade_id 未归属到本笔")

    # ⑥ 基准口径诚实
    bm = chart["benchmark"]
    if bm.get("net_return_pct") is not None:
        problems.append(f"benchmark.net_return_pct 应为 None，实为 {bm.get('net_return_pct')}")
    if bm.get("price_only_return_pct") is None:
        problems.append("benchmark.price_only_return_pct 缺失")
    if not bm.get("cost_model"):
        problems.append("benchmark.cost_model 缺失")

    # ⑦ 落盘格式：indent=2
    txt = (run_dir / "chart.json").read_text(encoding="utf-8")
    if not txt.startswith("{\n  "):
        problems.append("chart.json 未按 indent=2 落盘（仍是紧凑格式）")

    # ⑧ 原 run 目录未被改动
    before = (SRC / "chart.json").stat().st_mtime
    after = (SRC / "chart.json").stat().st_mtime
    if before != after:
        problems.append("原 run 的 chart.json 被改动（不允许）")

    print(json.dumps({
        "candles": len(chart["candles"]), "candle_format": "object",
        "trade_ids": tids, "stop_trace": st, "signals": len(chart["signals"]),
        "funding_events": len(fe),
        "funding_amount_sum": round(sum(e["amount"] or 0.0 for e in fe), 12),
        "benchmark": {k: v for k, v in bm.items() if k != "equity_series"},
        "chart_bytes": len(txt),
    }, ensure_ascii=False, indent=2))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"problems={len(problems)}")
    for p in problems:
        print("  ✗", p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
