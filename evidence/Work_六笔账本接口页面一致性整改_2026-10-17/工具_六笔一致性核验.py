# -*- coding: utf-8 -*-
"""六笔回测交易「账本 — 接口」一致性核验（只读；不写任何 run 目录）。

用途
────
对本轮任务书指定的 6 笔样本，逐笔核验：
  ① 逐笔明细（`/trades`）的 `trade_id` 是否为**账本真实标识**（而不是 `trd_1`）；
  ② `/trades/{id}` 的关联订单 / 资金费 / 原始信号是否**只用真实标识**命中；
  ③ 图表载荷（`/chart`）的 K 线 / 买卖点 / 止损轨迹 / 资金费是否真的可画；
  ④ `/metrics` 的胜负（扣费后净盈亏）、最大回撤（真实权益曲线）、
     profit factor 与平均盈亏比是否**分别**给出且口径写明；
  ⑤ `/equity` 的基准成本口径是否如实标注（纯价格口径不给 net_return_pct）。

输出
────
  · 六笔对照表 `六笔对照表_2026-10-17.md`
  · 原始核验结果 `六笔核验_2026-10-17.json`

纪律：本脚本**只读**，绝不修改任何 run 目录、账本、策略参数或成交结果。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
BACKEND = PROJECT / "框架" / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import backtest_api as API            # noqa: E402
import backtest_store as S            # noqa: E402
import ledger_links as LL             # noqa: E402

RUNS = [
    ("LPTUSDT", "run_WX_EVENT_TREND_V0_1_LPTUSDT_1792173591"),
    ("OPUSDT", "run_WX_EVENT_TREND_V0_1_OPUSDT_1792172950"),
    ("ORDIUSDT", "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568"),
    ("ACEUSDT", "run_WX_EVENT_TREND_V0_1_ACEUSDT_1792171577"),
    ("BNBUSDT", "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973"),
    ("MOVRUSDT", "run_WX_EVENT_TREND_V0_1_MOVRUSDT_1792171582"),
]

NUM_KEYS = ("entry_price", "exit_price", "qty", "gross_pnl", "entry_fee", "exit_fee",
            "fees", "funding_fee", "net_pnl")


def _ledger_truth(run_id: str) -> dict:
    """账本**原始**事实（直接读 trades.jsonl，不经任何兜底）。"""
    d = S.run_dir(run_id)
    rows = LL.read_jsonl(d / "trades.jsonl")
    assert len(rows) == 1, f"{run_id}: 期望 1 笔，实得 {len(rows)}"
    t = rows[0]
    pos = LL.read_jsonl(d / "positions.jsonl")
    closed = [p for p in pos if p.get("event_type") == "POSITION_CLOSED"]
    return {
        "trades_jsonl": t,
        "real_trade_id": (closed[0].get("payload") or {}).get("trade_id") if closed else None,
        "trades_jsonl_has_trade_id": ("trade_id" in t),
        "orders_jsonl_count": len(LL.read_jsonl(d / "orders.jsonl")),
        "funding_jsonl_count": len(LL.read_jsonl(d / "funding.jsonl")),
        "candidates_jsonl_count": len(LL.read_jsonl(d / "candidates.jsonl")),
        "equity_jsonl_count": len(LL.read_jsonl(d / "equity_curve.jsonl")),
    }


def _check_run(symbol: str, run_id: str) -> dict:
    truth = _ledger_truth(run_id)
    t = truth["trades_jsonl"]

    tr_env, _ = API.get_trades(run_id)
    row = (tr_env.get("items") or [{}])[0]

    # 账本 vs /trades：数值逐项比对
    diffs = []
    for k in NUM_KEYS:
        a, b = t.get(k), row.get(k)
        if a is None or b is None:
            if a != b:
                diffs.append(f"{k}: ledger={a} api={b}")
            continue
        if abs(float(a) - float(b)) > 1e-12:
            diffs.append(f"{k}: ledger={a} api={b}")

    tid = row.get("trade_id")
    det_env, det_code = API.get_trade(run_id, tid) if tid else ({}, 0)
    orders_env, _ = API.get_orders(run_id)
    met_env, _ = API.get_metrics(run_id)
    chart_env, _ = API.get_chart(run_id)
    eq_env, _ = API.get_equity(run_id)

    chart = chart_env.get("chart") or {}
    ct = next((x for x in (chart.get("trades") or [])
               if isinstance(x, dict) and x.get("trade_id") == tid), None)
    stop = (ct or {}).get("stop_trace") or []
    stop_all_have_t = bool(stop) and all(isinstance(p.get("t"), (int, float)) for p in stop)
    sig = chart.get("signals") or []
    fe = chart.get("funding_events") or []
    candles = chart.get("candles") or []
    c0 = candles[0] if candles else None
    candle_fmt = ("tuple" if isinstance(c0, (list, tuple))
                  else ("object" if isinstance(c0, dict) else "none"))

    m = met_env.get("metrics") or {}
    linked_orders = det_env.get("orders") or []
    funding_rows = det_env.get("funding") or []

    return {
        "symbol": symbol,
        "run_id": run_id,
        "ledger": {
            "trade_id": truth["real_trade_id"],
            "trades_jsonl_has_trade_id": truth["trades_jsonl_has_trade_id"],
            "entry_time_ms": t.get("entry_time"),
            "entry_price": t.get("entry_price"),
            "exit_time_ms": t.get("exit_time"),
            "exit_price": t.get("exit_price"),
            "exit_reason": t.get("exit_reason"),
            "qty": t.get("qty"),
            "gross_pnl": t.get("gross_pnl"),
            "entry_fee": t.get("entry_fee"),
            "exit_fee": t.get("exit_fee"),
            "fees": t.get("fees"),
            "funding_fee": t.get("funding_fee"),
            "net_pnl": t.get("net_pnl"),
            "order_id": t.get("order_id"),
            "exit_order_id": t.get("exit_order_id"),
            "signal_id": t.get("signal_id"),
            "stop_trace_points_ledger": len(t.get("stop_trace") or []),
            "orders_jsonl_count": truth["orders_jsonl_count"],
            "funding_jsonl_count": truth["funding_jsonl_count"],
            "candidates_jsonl_count": truth["candidates_jsonl_count"],
            "equity_jsonl_count": truth["equity_jsonl_count"],
        },
        "api_trades": {
            "trade_id": row.get("trade_id"),
            "trade_id_source": row.get("trade_id_source"),
            "entry_order_id": row.get("entry_order_id"),
            "exit_order_id": row.get("exit_order_id"),
            "entry_order_found": row.get("entry_order_found"),
            "exit_order_found": row.get("exit_order_found"),
            "linked_order_count": row.get("linked_order_count"),
            "holding_hours": row.get("holding_hours"),
            "funding_events": row.get("funding_events"),
            "stop_trace_points": row.get("stop_trace_points"),
            "stop_trace_json": row.get("stop_trace_json"),
            "total": tr_env.get("total"),
            "unresolved_trade_ids": tr_env.get("unresolved_trade_ids"),
            "link_notes": tr_env.get("link_notes"),
            "ledger_vs_api_diffs": diffs,
        },
        "api_trade_detail": {
            "http_code": det_code,
            "trade_id": (det_env.get("trade") or {}).get("trade_id"),
            "orders_count": len(linked_orders),
            "order_ids": [o.get("order_id") for o in linked_orders],
            "order_ts_ms": [o.get("ts_ms") for o in linked_orders],
            "funding_count": len(funding_rows),
            "funding_trade_link": sorted({f.get("trade_link") for f in funding_rows}),
            "funding_sum_amount": (sum(f.get("amount") or 0.0 for f in funding_rows)
                                   if funding_rows else None),
            "original_signal_present": det_env.get("original_signal") is not None,
            "original_signal_id": (det_env.get("original_signal") or {}).get("candidate_id"),
            "funding_cross_check": det_env.get("funding_cross_check"),
            "missing": det_env.get("missing"),
        },
        "api_orders_global": {
            "total": orders_env.get("total"),
            "unlinked": orders_env.get("unlinked_orders"),
            "linked_to_this_trade": sum(1 for o in (orders_env.get("items") or [])
                                        if o.get("trade_id") == tid),
        },
        "chart": {
            "candles": len(candles),
            "candle_format": candle_fmt,
            "signals": len(sig),
            "signal_times_ok": all(isinstance(s.get("t"), (int, float)) and s.get("t") > 0
                                   for s in sig) if sig else None,
            "trades": len(chart.get("trades") or []),
            "chart_trade_trade_id": (ct or {}).get("trade_id"),
            "stop_trace_points_chart": len(stop),
            "stop_trace_all_have_t": stop_all_have_t,
            "stop_trace_raw_keys": (sorted(stop[0].keys()) if stop else None),
            "funding_events": len(fe),
            "funding_events_with_trade_id": sum(1 for e in fe if e.get("trade_id")),
            "benchmark_return_pct": (chart.get("benchmark") or {}).get("return_pct"),
            "stop_trace_source": chart.get("stop_trace_source"),
        },
        "metrics": {
            "trades_count": m.get("trades_count"),
            "wins": m.get("wins"),
            "losses": m.get("losses"),
            "flats": m.get("flats"),
            "win_rate_pct": m.get("win_rate_pct"),
            "net_pnl": m.get("net_pnl"),
            "net_return_pct": m.get("net_return_pct"),
            "gross_pnl": m.get("gross_pnl"),
            "total_fees": m.get("total_fees"),
            "total_funding_fee": m.get("total_funding_fee"),
            "profit_factor": m.get("profit_factor"),
            "profit_factor_basis": m.get("profit_factor_basis"),
            "avg_win_loss_ratio": m.get("avg_win_loss_ratio"),
            "avg_win_loss_ratio_basis": m.get("avg_win_loss_ratio_basis"),
            "max_drawdown_pct": m.get("max_drawdown_pct"),
            "max_drawdown_basis": m.get("max_drawdown_basis"),
            "max_drawdown_detail": m.get("max_drawdown_detail"),
            "equity_points": m.get("equity_points"),
            "win_loss_basis": m.get("win_loss_basis"),
            "stored_summary": met_env.get("metrics_stored_summary"),
            "notes": met_env.get("metrics_notes"),
        },
        "equity": {
            "points": eq_env.get("total"),
            "benchmark_kind": (eq_env.get("benchmark") or {}).get("benchmark_kind"),
            "benchmark_cost_model": (eq_env.get("benchmark") or {}).get("cost_model"),
            "benchmark_net_return_pct": (eq_env.get("benchmark") or {}).get("net_return_pct"),
            "benchmark_price_only_return_pct": (eq_env.get("benchmark") or {}).get(
                "price_only_return_pct"),
            "benchmark_max_drawdown_pct": (eq_env.get("benchmark") or {}).get("max_drawdown_pct"),
            "benchmark_final_equity": (eq_env.get("benchmark") or {}).get("final_equity"),
            "benchmark_series_points": len(eq_env.get("benchmark_equity") or []),
        },
    }


def main() -> int:
    out = [_check_run(sym, rid) for sym, rid in RUNS]

    # ── 硬断言（任一不成立 ⇒ 退出码非 0，不得当成通过）──
    problems = []
    for r in out:
        s = r["symbol"]
        lg, at = r["ledger"], r["api_trades"]
        if not lg["trade_id"]:
            problems.append(f"{s}: 账本没有真实 trade_id")
        if at["trade_id"] != lg["trade_id"]:
            problems.append(f"{s}: /trades.trade_id={at['trade_id']} != 账本 {lg['trade_id']}")
        if at["trade_id_source"] != "ledger:positions.POSITION_CLOSED":
            problems.append(f"{s}: trade_id 来源不是账本真值：{at['trade_id_source']}")
        if at["ledger_vs_api_diffs"]:
            problems.append(f"{s}: 账本与 /trades 数值不一致 {at['ledger_vs_api_diffs']}")
        if at["linked_order_count"] != 2:
            problems.append(f"{s}: 关联订单数 {at['linked_order_count']} != 2")
        d = r["api_trade_detail"]
        if d["orders_count"] != 2:
            problems.append(f"{s}: /trades/{{id}} 订单 {d['orders_count']} 条")
        if d["funding_count"] != lg["funding_jsonl_count"]:
            problems.append(f"{s}: 资金费归属 {d['funding_count']} != 账本 {lg['funding_jsonl_count']}")
        cc = d.get("funding_cross_check") or {}
        if not cc.get("match"):
            problems.append(f"{s}: 资金费交叉校验未通过 {cc}")
        if not d["original_signal_present"]:
            problems.append(f"{s}: /trades/{{id}} 未命中原始候选信号")
        c = r["chart"]
        if c["candles"] <= 0:
            problems.append(f"{s}: chart.candles 为空")
        if c["signals"] <= 0:
            problems.append(f"{s}: chart.signals 为空")
        if c["chart_trade_trade_id"] != lg["trade_id"]:
            problems.append(f"{s}: chart.trades[].trade_id={c['chart_trade_trade_id']} != 账本")
        if c["stop_trace_points_chart"] != lg["stop_trace_points_ledger"]:
            problems.append(f"{s}: chart 止损点数 {c['stop_trace_points_chart']} "
                            f"!= 账本 {lg['stop_trace_points_ledger']}")
        if not c["stop_trace_all_have_t"]:
            problems.append(f"{s}: chart 止损点缺 `t` 字段")
        if c["funding_events"] <= 0:
            problems.append(f"{s}: chart.funding_events 为空")
        m = r["metrics"]
        if m["trades_count"] != 1:
            problems.append(f"{s}: metrics.trades_count={m['trades_count']}")
        exp_win = 1 if (lg["net_pnl"] or 0) > 0 else 0
        exp_loss = 1 if (lg["net_pnl"] or 0) < 0 else 0
        if (m["wins"], m["losses"]) != (exp_win, exp_loss):
            problems.append(f"{s}: 胜负与净盈亏矛盾 wins={m['wins']} losses={m['losses']} "
                            f"net_pnl={lg['net_pnl']}")
        if m["max_drawdown_pct"] is None:
            problems.append(f"{s}: max_drawdown_pct 缺失")
        if abs((m["net_pnl"] or 0) - (lg["net_pnl"] or 0)) > 1e-9:
            problems.append(f"{s}: metrics.net_pnl != 账本 net_pnl")

    (HERE / "六笔核验_2026-10-17.json").write_text(
        json.dumps({"runs": out, "problems": problems}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ── 对照表 ──
    lines = [
        "# 六笔回测交易「账本 — 接口」对照表（2026-10-17）",
        "",
        "> 全部数字直接读自各 run 目录的账本 JSONL（`trades.jsonl` / `orders.jsonl` / "
        "`funding.jsonl` / `positions.jsonl` / `equity_curve.jsonl`），未经任何兜底或推算。",
        "> `API` 列取自进程内直调 `backtest_api` 的同一批函数（与 HTTP 端点同一实现）。",
        "",
        "## 1 逐笔（账本 vs 接口）",
        "",
        "| 标的 | run_id 尾号 | 入场时间(UTC) | 入场价 | 出场时间(UTC) | 出场价 | 退出原因 | qty |",
        "|---|---|---|---|---|---|---|---|",
    ]
    import datetime as dt

    def utc(ms):
        return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

    for r in out:
        lg = r["ledger"]
        lines.append(
            f"| {r['symbol']} | {r['run_id'].rsplit('_', 1)[-1]} | {utc(lg['entry_time_ms'])} | "
            f"{lg['entry_price']} | {utc(lg['exit_time_ms'])} | {lg['exit_price']} | "
            f"{lg['exit_reason']} | {lg['qty']:.8f} |")
    lines += [
        "",
        "| 标的 | 毛盈亏 | 手续费(入+出) | 资金费 | **净盈亏** | 关联订单数 | 真实 trade_id | 关联来源 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in out:
        lg, at = r["ledger"], r["api_trades"]
        lines.append(
            f"| {r['symbol']} | {lg['gross_pnl']:+.6f} | {lg['fees']:.6f} | "
            f"{lg['funding_fee']:+.6f} | **{lg['net_pnl']:+.6f}** | "
            f"{at['linked_order_count']}/2 | `{at['trade_id']}` | {at['trade_id_source']} |")
    lines += [
        "",
        "## 2 指标（`/metrics`，全部由账本现算）",
        "",
        "| 标的 | 笔数 | 胜 | 负 | 净盈亏 | 净收益率 | 最大回撤 | PF(聚合) | 平均盈亏比(均值) | 权益点数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in out:
        m = r["metrics"]
        pf = "—" if m["profit_factor"] is None else f"{m['profit_factor']:.6f}"
        awlr = "—" if m["avg_win_loss_ratio"] is None else f"{m['avg_win_loss_ratio']:.6f}"
        lines.append(
            f"| {r['symbol']} | {m['trades_count']} | {m['wins']} | {m['losses']} | "
            f"{m['net_pnl']:+.6f} | {m['net_return_pct']:.6f}% | "
            f"{m['max_drawdown_pct']:.6f}% | {pf} | {awlr} | {m['equity_points']} |")
    lines += [
        "",
        "## 3 图表载荷（`/chart`）",
        "",
        "| 标的 | K线根数 | 格式 | 信号点 | 逐笔 | chart.trade_id | 止损点 | 含`t` | 资金费事件 | 含trade_id |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in out:
        c = r["chart"]
        lines.append(
            f"| {r['symbol']} | {c['candles']} | {c['candle_format']} | {c['signals']} | "
            f"{c['trades']} | `{c['chart_trade_trade_id']}` | {c['stop_trace_points_chart']} | "
            f"{c['stop_trace_all_have_t']} | {c['funding_events']} | "
            f"{c['funding_events_with_trade_id']} |")
    lines += [
        "",
        "## 4 基准与权益（`/equity`）",
        "",
        "| 标的 | 权益点 | 基准口径 | 成本模型 | 纯价格收益 | 净收益(基准) | 基准最大回撤 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in out:
        e = r["equity"]
        net = "—" if e["benchmark_net_return_pct"] is None else f"{e['benchmark_net_return_pct']:.4f}%"
        lines.append(
            f"| {r['symbol']} | {e['points']} | {e['benchmark_kind']} | {e['benchmark_cost_model']} | "
            f"{e['benchmark_price_only_return_pct']:.4f}% | {net} | "
            f"{e['benchmark_max_drawdown_pct']:.4f}% |")
    lines += [
        "",
        "## 5 硬断言结果",
        "",
    ]
    if problems:
        lines.append(f"**{len(problems)} 项不通过**：")
        lines += [f"- {p}" for p in problems]
    else:
        lines.append("**全部通过**（逐笔数值一致 / trade_id 为账本真值 / 关联订单 2 条 / "
                     "资金费归属与交叉校验一致 / K线与信号与止损轨迹可画 / 胜负与净盈亏一致）。")
    lines.append("")
    (HERE / "六笔对照表_2026-10-17.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"runs={len(out)} problems={len(problems)}")
    for p in problems:
        print("  ✗", p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
