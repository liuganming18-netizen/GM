# -*- coding: utf-8 -*-
"""在线 API 验收取证（HTTP 实测，非进程内直调）。

对 6 笔样本逐个打真实 HTTP 端点，把**原始响应**落盘到本目录 `在线API响应/`：
  · metrics.json / trades.json / orders.json / equity_meta.json   —— 完整响应
  · trade_<tid>.json                                              —— 单笔明细（关联订单/资金费/原始信号）
  · chart_摘要.json                                               —— 图表载荷摘要 + 全量 body 的 sha256/字节数
  · 在线核验_2026-10-17.json                                      —— 逐项断言结果

纪律：只 GET，不改任何数据。
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "在线API响应"
BASE = "http://127.0.0.1:8787/api/v1/backtests"

RUNS = [
    ("LPTUSDT", "run_WX_EVENT_TREND_V0_1_LPTUSDT_1792173591",
     "trd_eb941d2defdde27285ef06e2"),
    ("OPUSDT", "run_WX_EVENT_TREND_V0_1_OPUSDT_1792172950",
     "trd_a01814754027b85386a054ca"),
    ("ORDIUSDT", "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568",
     "trd_4b7e88ebe45d27376fb18ca0"),
    ("ACEUSDT", "run_WX_EVENT_TREND_V0_1_ACEUSDT_1792171577",
     "trd_d574e8cd27bd34df692444e9"),
    ("BNBUSDT", "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973",
     "trd_ae5861c5781bd75236471b3e"),
    ("MOVRUSDT", "run_WX_EVENT_TREND_V0_1_MOVRUSDT_1792171582",
     "trd_6d6be42bdeed0ceb56044e2c"),
]

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str):
    with _opener.open(f"{BASE}{path}", timeout=120) as r:
        body = r.read()
        return r.status, body, json.loads(body.decode("utf-8"))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    summary: list[dict] = []

    for sym, rid, tid in RUNS:
        rec: dict = {"symbol": sym, "run_id": rid, "trade_id": tid}
        st, body, m = get(f"/{rid}/metrics")
        (OUT / f"metrics_{sym}.json").write_bytes(body)
        mm = m.get("metrics") or {}
        rec["metrics"] = {k: mm.get(k) for k in (
            "trades_count", "wins", "losses", "flats", "net_pnl", "net_return_pct",
            "profit_factor", "avg_win_loss_ratio", "max_drawdown_pct",
            "max_drawdown_basis", "win_loss_basis", "equity_points")}
        rec["metrics_http"] = st
        rec["benchmark_buy_hold"] = m.get("benchmark_buy_hold")
        rec["monthly_returns_n"] = len(((m.get("monthly_returns") or {}).get("months")) or [])
        rec["metrics_stored_summary"] = m.get("metrics_stored_summary")

        st, body, t = get(f"/{rid}/trades")
        (OUT / f"trades_{sym}.json").write_bytes(body)
        row = (t.get("items") or [{}])[0]
        rec["trades"] = {k: row.get(k) for k in (
            "trade_id", "trade_id_source", "entry_order_id", "exit_order_id",
            "linked_order_count", "holding_hours", "funding_events", "stop_trace_points",
            "stop_trace_json")}
        rec["trades_http"] = st

        st, body, d = get(f"/{rid}/trades/{tid}")
        (OUT / f"trade_{sym}.json").write_bytes(body)
        rec["trade_detail"] = {
            "http": st,
            "trade_id": (d.get("trade") or {}).get("trade_id"),
            "orders_count": len(d.get("orders") or []),
            "order_ids": [o.get("order_id") for o in (d.get("orders") or [])],
            "order_ts_ms": [o.get("ts_ms") for o in (d.get("orders") or [])],
            "funding_count": len(d.get("funding") or []),
            "funding_cross_check": d.get("funding_cross_check"),
            "original_signal_id": (d.get("original_signal") or {}).get("candidate_id"),
            "missing": d.get("missing"),
        }

        st, body, o = get(f"/{rid}/orders")
        (OUT / f"orders_{sym}.json").write_bytes(body)
        rec["orders"] = {"http": st, "total": o.get("total"),
                         "unlinked": o.get("unlinked_orders"),
                         "ts_ms_ok": all(x.get("ts_ms") for x in (o.get("items") or []))}

        st, body, c = get(f"/{rid}/chart")
        (OUT / f"chart_{sym}.json").write_bytes(body)
        ch = c.get("chart") or {}
        candles = ch.get("candles") or []
        c0 = candles[0] if candles else None
        ctr = (ch.get("trades") or [{}])[0]
        rec["chart"] = {
            "http": st, "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "candles": len(candles),
            "candle_format": ("tuple" if isinstance(c0, (list, tuple)) else "object"),
            "candle0": c0,
            "signals": len(ch.get("signals") or []),
            "signal0": (ch.get("signals") or [None])[0],
            "trades": len(ch.get("trades") or []),
            "chart_trade_id": ctr.get("trade_id"),
            "stop_trace": ctr.get("stop_trace"),
            "stop_trace_source": ch.get("stop_trace_source"),
            "funding_events": len(ch.get("funding_events") or []),
            "funding_event0": (ch.get("funding_events") or [None])[0],
            "benchmark_return_pct": (ch.get("benchmark") or {}).get("return_pct"),
        }

        st, body, e = get(f"/{rid}/equity")
        em = {"http": st, "points": e.get("total"), "benchmark": e.get("benchmark"),
              "benchmark_series_points": len(e.get("benchmark_equity") or [])}
        (OUT / f"equity_meta_{sym}.json").write_text(
            json.dumps(em, ensure_ascii=False, indent=2), encoding="utf-8")
        rec["equity"] = {k: (em["benchmark"] or {}).get(k) for k in (
            "benchmark_kind", "cost_model", "price_only_return_pct", "net_return_pct",
            "max_drawdown_pct", "final_equity")}
        rec["equity"]["points"] = em["points"]
        rec["equity"]["benchmark_series_points"] = em["benchmark_series_points"]

        # ── 断言 ──
        s = sym
        if rec["trades"]["trade_id"] != tid:
            problems.append(f"{s}: /trades.trade_id={rec['trades']['trade_id']} != {tid}")
        if rec["trades"]["trade_id_source"] != "ledger:positions.POSITION_CLOSED":
            problems.append(f"{s}: trade_id 来源 {rec['trades']['trade_id_source']}")
        if rec["trades"]["linked_order_count"] != 2:
            problems.append(f"{s}: linked_order_count={rec['trades']['linked_order_count']}")
        if rec["trade_detail"]["orders_count"] != 2:
            problems.append(f"{s}: 单笔订单 {rec['trade_detail']['orders_count']} 条")
        if not (rec["trade_detail"]["funding_cross_check"] or {}).get("match"):
            problems.append(f"{s}: 资金费交叉校验未过 {rec['trade_detail']['funding_cross_check']}")
        if not rec["trade_detail"]["original_signal_id"]:
            problems.append(f"{s}: 原始候选信号未命中")
        if rec["chart"]["signals"] <= 0:
            problems.append(f"{s}: 在线 chart.signals 为空")
        if rec["chart"]["funding_events"] <= 0:
            problems.append(f"{s}: 在线 chart.funding_events 为空")
        if rec["chart"]["chart_trade_id"] != tid:
            problems.append(f"{s}: 在线 chart.trades[].trade_id={rec['chart']['chart_trade_id']}")
        if not rec["chart"]["stop_trace"] or any(
                "t" not in p for p in rec["chart"]["stop_trace"]):
            problems.append(f"{s}: 在线 stop_trace 缺 `t`：{rec['chart']['stop_trace']}")
        if rec["metrics"]["max_drawdown_pct"] is None:
            problems.append(f"{s}: 在线 max_drawdown_pct 缺失")
        if rec["metrics"]["wins"] + rec["metrics"]["losses"] != 1:
            problems.append(f"{s}: 在线胜负 {rec['metrics']['wins']}/{rec['metrics']['losses']}")
        if (rec["equity"].get("net_return_pct")) is not None:
            problems.append(f"{s}: 在线基准 net_return_pct 应为 None")
        if rec["orders"]["unlinked"] not in (0, None):
            problems.append(f"{s}: 在线订单有 {rec['orders']['unlinked']} 条未关联")

        summary.append(rec)
        print(f"  {s}: mdd={rec['metrics']['max_drawdown_pct']:.6f}% "
              f"signals={rec['chart']['signals']} fe={rec['chart']['funding_events']} "
              f"tid={rec['trades']['trade_id']} orders={rec['trade_detail']['orders_count']}")

    (OUT / "在线核验_2026-10-17.json").write_text(
        json.dumps({"runs": summary, "problems": problems}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"problems={len(problems)}")
    for p in problems:
        print("  ✗", p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
