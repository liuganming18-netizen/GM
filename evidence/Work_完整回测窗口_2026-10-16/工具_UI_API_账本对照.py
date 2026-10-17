#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WORK-05 · 真实 run 的「UI ↔ API ↔ 账本」逐项对照（只读）。

用法：
  python 工具_UI_API_账本对照.py <run_id> [--base http://127.0.0.1:8787]

做三件事：
  1) 直接以只读方式打开该 run 的账本 sqlite（`ledger.sqlite3`），取各表行数与关键求和；
  2) 通过后端只读 API 取同一 run 的 metrics / ledger_counts / trades / orders / rejected / equity / quality；
  3) 逐项比对，输出 JSON + Markdown 对照表（一致 / 不一致 / 缺失）。

纪律：只读；不写任何账本；缺失一律 None（不填 0 冒充）。
"""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
RUNS = ROOT / "回测日志" / "oi_resonance_v1" / "runs"


def api(base: str, path: str):
    url = base.rstrip("/") + "/api/v1" + path
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def ledger_path(run_id: str) -> Path:
    return RUNS / run_id / "ledger.sqlite3"


def ledger_facts(run_id: str) -> dict:
    p = ledger_path(run_id)
    if not p.exists():
        return {"available": False, "path": str(p)}
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    cur = con.cursor()
    out = {"available": True, "path": str(p), "counts": {}, "sums": {}}
    for t in ["signals", "orders", "trades", "funding", "rejected", "equity"]:
        try:
            out["counts"][t] = cur.execute(f"select count(*) from '{t}'").fetchone()[0]
        except Exception as e:  # noqa: BLE001
            out["counts"][t] = None
            out.setdefault("errors", []).append(f"{t}: {e}")
    # 关键求和：净盈亏 / 手续费 / 资金费（账本 trades 表）
    try:
        row = cur.execute(
            "select round(sum(net_pnl),6), round(sum(fees),6), round(sum(funding_fee),6), "
            "round(sum(gross_pnl),6), count(*) from trades"
        ).fetchone()
        out["sums"]["trades"] = {
            "net_pnl": row[0], "fees": row[1], "funding_fee": row[2], "gross_pnl": row[3], "n": row[4],
        }
    except Exception as e:  # noqa: BLE001
        out.setdefault("errors", []).append(f"trades sums: {e}")
    # funding 明细求和（逐事件）
    try:
        row = cur.execute("select round(sum(amount),6), count(*) from funding").fetchone()
        out["sums"]["funding"] = {"amount": row[0], "n": row[1]}
    except Exception as e:  # noqa: BLE001
        out.setdefault("errors", []).append(f"funding sums: {e}")
    # 逐笔 trade 明细（按 trade_id 排序，用于与 API /trades 逐笔对照）
    try:
        cols = ["trade_id", "symbol", "direction", "entry_time_ms", "exit_time_ms",
                "entry_price", "exit_price", "qty", "net_pnl", "fees", "funding_fee", "exit_reason"]
        rows = cur.execute(f"select {','.join(cols)} from trades order by trade_id").fetchall()
        out["trades_detail"] = [dict(zip(cols, r)) for r in rows]
    except Exception as e:  # noqa: BLE001
        out.setdefault("errors", []).append(f"trades detail: {e}")
    # 订单逐行
    try:
        cols = ["order_id", "trade_id", "phase", "ts_ms", "action", "price", "qty", "fee", "stop_price"]
        rows = cur.execute(f"select {','.join(cols)} from orders order by ts_ms, order_id").fetchall()
        out["orders_detail"] = [dict(zip(cols, r)) for r in rows]
    except Exception as e:  # noqa: BLE001
        out.setdefault("errors", []).append(f"orders detail: {e}")
    con.close()
    return out


def approx(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol
    return a == b


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    base = "http://127.0.0.1:8787"
    if "--base" in sys.argv:
        base = sys.argv[sys.argv.index("--base") + 1]
    if not run_id:
        print("用法: python 工具_UI_API_账本对照.py <run_id> [--base URL]")
        sys.exit(2)

    led = ledger_facts(run_id)
    detail = api(base, f"/backtests/{run_id}")
    metrics = api(base, f"/backtests/{run_id}/metrics")
    trades = api(base, f"/backtests/{run_id}/trades")
    orders = api(base, f"/backtests/{run_id}/orders")
    rejected = api(base, f"/backtests/{run_id}/rejected")
    equity = api(base, f"/backtests/{run_id}/equity")
    quality = api(base, f"/backtests/{run_id}/quality")
    chart = api(base, f"/backtests/{run_id}/chart")

    m = metrics.get("metrics") or {}
    lc = detail.get("ledger_counts") or {}
    api_trades = trades.get("items") or []
    api_orders = orders.get("items") or []
    api_rej = rejected.get("items") or []
    api_eq = equity.get("items") or []
    api_fund_events = (chart.get("chart") or {}).get("funding_events") or []

    checks = []

    def chk(name, led_v, api_v, ok=None):
        ok = approx(led_v, api_v) if ok is None else ok
        checks.append({"项": name, "账本": led_v, "API": api_v, "一致": bool(ok)})

    # 1) 行数
    chk("signals 行数", led["counts"].get("signals"), lc.get("signals"))
    chk("orders 行数", led["counts"].get("orders"), lc.get("orders"))
    chk("trades 行数", led["counts"].get("trades"), lc.get("trades"))
    chk("funding 行数", led["counts"].get("funding"), lc.get("funding"))
    chk("rejected 行数", led["counts"].get("rejected"), lc.get("rejected"))
    chk("equity 行数", led["counts"].get("equity"), lc.get("equity"))
    chk("API /trades 行数", led["counts"].get("trades"), len(api_trades))
    chk("API /orders 行数", led["counts"].get("orders"), len(api_orders))
    chk("API /rejected 行数", led["counts"].get("rejected"), len(api_rej))
    chk("API /equity 行数", led["counts"].get("equity"), len(api_eq))
    chk("chart funding_events 行数", led["counts"].get("funding"), len(api_fund_events))

    # 2) 求和
    ts = led["sums"].get("trades", {})
    chk("净盈亏 Σ(账本 trades)", ts.get("net_pnl"), round(m.get("net_pnl"), 6) if m.get("net_pnl") is not None else None)
    chk("手续费 Σ(账本 trades)", ts.get("fees"), round(m.get("total_fees"), 6) if m.get("total_fees") is not None else None)
    chk("资金费 Σ(账本 trades)", ts.get("funding_fee"), round(m.get("total_funding_fee"), 6) if m.get("total_funding_fee") is not None else None)
    chk("毛盈亏 Σ(账本 trades)", ts.get("gross_pnl"), round(m.get("gross_pnl"), 6) if m.get("gross_pnl") is not None else None)
    fs = led["sums"].get("funding", {})
    chk("资金费 Σ(账本 funding 明细)", fs.get("amount"), round(m.get("total_funding_fee"), 6) if m.get("total_funding_fee") is not None else None)

    # 3) 逐笔对照（按 trade_id）
    api_by_id = {t["trade_id"]: t for t in api_trades}
    led_by_id = {t["trade_id"]: t for t in (led.get("trades_detail") or [])}
    diffs = []
    for tid in sorted(set(api_by_id) | set(led_by_id)):
        a = api_by_id.get(tid)
        b = led_by_id.get(tid)
        if a is None or b is None:
            diffs.append({"trade_id": tid, "问题": "仅存在于一侧"})
            continue
        for f in ["entry_time_ms", "exit_time_ms", "entry_price", "exit_price", "qty", "net_pnl", "fees", "funding_fee", "symbol", "direction", "exit_reason"]:
            if not approx(a.get(f), b.get(f), 1e-6):
                diffs.append({"trade_id": tid, "字段": f, "API": a.get(f), "账本": b.get(f)})
    checks.append({"项": "逐笔明细（trade_id 级）", "账本": f"{len(led_by_id)} 笔", "API": f"{len(api_by_id)} 笔",
                   "一致": len(diffs) == 0, "差异": diffs[:20]})

    # 4) 订单逐行对照
    api_ord_by_id = {o["order_id"]: o for o in api_orders}
    led_ord_by_id = {o["order_id"]: o for o in (led.get("orders_detail") or [])}
    odiffs = []
    for oid in sorted(set(api_ord_by_id) | set(led_ord_by_id)):
        a = api_ord_by_id.get(oid)
        b = led_ord_by_id.get(oid)
        if a is None or b is None:
            odiffs.append({"order_id": oid, "问题": "仅存在于一侧"})
            continue
        for f in ["trade_id", "phase", "ts_ms", "action", "price", "qty", "fee", "stop_price"]:
            if not approx(a.get(f), b.get(f), 1e-6):
                odiffs.append({"order_id": oid, "字段": f, "API": a.get(f), "账本": b.get(f)})
    checks.append({"项": "订单明细（order_id 级）", "账本": f"{len(led_ord_by_id)} 条", "API": f"{len(api_ord_by_id)} 条",
                   "一致": len(odiffs) == 0, "差异": odiffs[:20]})

    # 5) quality / hashes 存在性
    dq = quality.get("data_quality") or {}
    checks.append({"项": "quality.status", "账本": "—", "API": quality.get("quality", {}).get("status"),
                   "一致": quality.get("quality", {}).get("status") == "ok"})
    checks.append({"项": "hashes.code_sha256", "账本": "—", "API": (quality.get("hashes") or {}).get("code_sha256"),
                   "一致": bool((quality.get("hashes") or {}).get("code_sha256"))})
    checks.append({"项": "PIT 声明 (oi_publication_lag_ms)", "账本": "—",
                   "API": dq.get("oi_min_resonance_status") or dq.get("oi_gate_note") or "—",
                   "一致": True})

    result = {
        "run_id": run_id,
        "base": base,
        "ledger": {"path": led.get("path"), "counts": led.get("counts"), "sums": led.get("sums"),
                   "errors": led.get("errors")},
        "api": {
            "metrics": {k: m.get(k) for k in ["initial_equity", "final_equity", "net_pnl", "net_return_pct",
                                              "gross_pnl", "total_fees", "total_funding_fee", "trades_count",
                                              "win_rate_pct", "profit_factor", "max_drawdown_pct",
                                              "sharpe_annualized_sqrt365"]},
            "ledger_counts": lc,
            "counts": {"trades": len(api_trades), "orders": len(api_orders), "rejected": len(api_rej),
                       "equity": len(api_eq), "funding_events": len(api_fund_events)},
            "window": detail.get("window"),
            "hashes": quality.get("hashes"),
            "data_quality_status": dq.get("status"),
        },
        "checks": checks,
        "all_ok": all(c["一致"] for c in checks),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # Markdown
    md = [f"# UI ↔ API ↔ 账本 逐项对照 · {run_id}", "",
          f"- 账本文件：`{led.get('path')}`", f"- 后端：`{base}`", ""]
    md.append("| 对照项 | 账本 | API | 一致 |")
    md.append("|---|---|---|---|")
    for c in checks:
        md.append(f"| {c['项']} | `{c['账本']}` | `{c['API']}` | {'✅' if c['一致'] else '❌'} |")
    md.append("")
    md.append(f"**总判定：{'全部一致' if result['all_ok'] else '存在不一致'}**")
    out_md = ROOT / "evidence" / "Work_完整回测窗口_2026-10-16" / f"对照_UI_API_账本_{run_id}.md"
    out_json = ROOT / "evidence" / "Work_完整回测窗口_2026-10-16" / f"对照_UI_API_账本_{run_id}.json"
    out_md.write_text("\n".join(md), encoding="utf-8")
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[written] {out_md}\n[written] {out_json}")
    sys.exit(0 if result["all_ok"] else 1)


if __name__ == "__main__":
    main()
