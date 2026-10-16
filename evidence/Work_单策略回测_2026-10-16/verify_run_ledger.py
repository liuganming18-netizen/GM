#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读回账本**独立重算**验收（不复用回测运行器的任何指标/资金费函数）。

判据（全部来自账本行 + **原始毫秒快照**，纯算术）：
  1. 逐笔：net = gross − entry_fee − exit_fee − funding
  2. 逐笔：funding_fee == Σ(该笔逐事件 amount)，且每条事件时间 ∈ **(entry_ms, exit_ms]**
     （**毫秒**比较，不做 //1000 截秒）
  3. **独立**从 `inputs/funding_events.json`（原始毫秒）重算逐笔资金费，
     与账本 funding 行、与 trades.funding_fee 三方一致 —— **不引用运行器的取整判断**
  3b. 结算覆盖：从 `inputs/funding_events_lookback.json`（回看窗原始毫秒）**独立重推**
     「应有结算网格」（半开 [start,end)、**含窗口左端点**），与 run.json 的
     expected_settlements 对账，并逐网格点核验确有真实事件命中（非仅比次数）
  4. 现金恒等式：final_cash == initial + Σgross − Σentry_fee − Σexit_fee − Σfunding
  5. 权益恒等式：末点 total_equity == initial + Σnet（无未平仓时）
  6. 权益曲线：等间隔（15m）、首点 = 初始权益、末点 = final_equity
  7. 指标重算并逐项比对 run.json（净收益 / 最大回撤 / 胜率 / 平均盈亏比 / profit_factor / 交易数）
  8. 窗口纪律：所有成交 entry_ms >= win_start（预热不交易）；窗口外信号未被交易
  9. 输入快照可复核：`inputs/*` 文件 SHA256 == inputs_manifest 记录；code_sha256 == 账本 meta
 10. 质量门：若 quality.status == ok，则资金费覆盖、对账、指标一致必须全部为真
 11. 基准：同成本买入持有序列存在、长度与权益曲线一致，且**末点 == final_equity**、
     net_return_pct 与末点自洽、回撤在**含末点**的同一序列上算得

用法：
  python3 verify_run_ledger.py <run_id> [runs_root]
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")

DAY_MS = 86_400_000
Q_MS = 900_000

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name} :: {detail}")


def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _series_mdd(series, init):
    """在给定净值序列（峰含 init）上算最大回撤比例。"""
    peak, mdd = float(init), 0.0
    for p in series:
        v = float(p["equity"] if isinstance(p, dict) else p)
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def main(run_id: str, runs_root: Path):
    d = runs_root / run_id
    doc = json.loads((d / "run.json").read_text(encoding="utf-8"))
    man = json.loads((d / "inputs_manifest.json").read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{d/'ledger.sqlite3'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY entry_time_ms")]
    funding = [dict(r) for r in con.execute("SELECT * FROM funding ORDER BY funding_time_ms")]
    equity = [dict(r) for r in con.execute("SELECT * FROM equity ORDER BY ts_ms")]
    signals = [dict(r) for r in con.execute("SELECT * FROM signals ORDER BY signal_bar_open_ms")]
    orders = [dict(r) for r in con.execute("SELECT * FROM orders ORDER BY ts_ms")]
    meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}

    m = doc["metrics"]
    win = doc["window"]
    dq = doc.get("data_quality") or {}
    init = float(m["initial_equity"])
    TOL = 1e-9

    print(f"=== run_id {run_id} · status {doc['status']} ===")
    print(f"  窗口 {iso(win['win_start_ms'])} .. {iso(win['win_end_ms'])}（{win.get('window_source')}）"
          f"  预热 {win['warmup_bars']} 根  交易 {len(trades)} 笔")

    # 0) 原始毫秒资金费快照（独立来源）
    snap_path = d / man["series"]["funding_events"]["file"]
    raw_events = json.loads(snap_path.read_text(encoding="utf-8"))
    raw_ms = sorted(int(e["time_ms"]) for e in raw_events)
    print(f"  资金费快照 {len(raw_events)} 条（毫秒）："
          f"{iso(raw_ms[0]) if raw_ms else '-'} .. {iso(raw_ms[-1]) if raw_ms else '-'}")

    # 1) 逐笔净利恒等式
    bad = [t["trade_id"] for t in trades
           if abs(float(t["net_pnl"]) - (float(t["gross_pnl"]) - float(t["entry_fee"])
                                         - float(t["exit_fee"]) - float(t["funding_fee"]))) > TOL]
    check("① 逐笔 net = gross − entry_fee − exit_fee − funding", not bad, str(bad[:3]))

    # 2) 逐事件资金费（账本内部一致性；**毫秒**区间）
    by_trade: dict = {}
    for f in funding:
        by_trade.setdefault(f["trade_id"], []).append(f)
    bad2, bad2b = [], []
    for t in trades:
        rows = by_trade.get(t["trade_id"], [])
        s = sum(float(r["amount"]) for r in rows)
        if abs(s - float(t["funding_fee"])) > TOL:
            bad2.append((t["trade_id"], s, float(t["funding_fee"])))
        e_ms, x_ms = int(t["entry_time_ms"]), int(t["exit_time_ms"])
        for r in rows:
            fs = int(r["funding_time_ms"])
            if not (e_ms < fs <= x_ms):
                bad2b.append((t["trade_id"], fs, e_ms, x_ms))
        if int(t["funding_events"]) != len(rows):
            bad2b.append((t["trade_id"], "count", t["funding_events"], len(rows)))
    check("② funding_fee == Σ逐事件 amount（毫秒区间）", not bad2, str(bad2[:3]))
    check("②b 逐事件时间 ∈ (entry_ms, exit_ms] 且计数一致", not bad2b, str(bad2b[:3]))

    # 3) **独立**从原始毫秒快照重算（不复用运行器）
    mism = []
    for t in trades:
        e_ms, x_ms = int(t["entry_time_ms"]), int(t["exit_time_ms"])
        qty, dirn = float(t["qty"]), int(1 if t["direction"] == "long" else -1)
        tot, cnt = 0.0, 0
        for ev in raw_events:
            tms = int(ev["time_ms"])
            if not (e_ms < tms <= x_ms):
                continue
            r, mk = ev.get("rate"), ev.get("mark_price")
            if r is None or mk is None:
                continue
            tot += (float(mk) * qty) * float(r) * dirn
            cnt += 1
        if abs(tot - float(t["funding_fee"])) > TOL or cnt != int(t["funding_events"]):
            mism.append((t["trade_id"], tot, float(t["funding_fee"]), cnt,
                         int(t["funding_events"])))
    check("③ 独立毫秒重算 == 账本 funding_fee（含计数）", not mism, str(mism[:3]))

    # 3b) 结算覆盖：从**回看快照**独立重推「应有结算网格」（**含窗口左端点**）并与 run.json 对账
    lb_ent = (man["series"].get("funding_events_lookback") or {})
    lb_file = lb_ent.get("file")
    if lb_file:
        look = json.loads((d / lb_file).read_text(encoding="utf-8"))
        look_ms = sorted(int(e["time_ms"]) for e in look)
        ivs = [look_ms[i + 1] - look_ms[i] for i in range(len(look_ms) - 1)]
        iv = max(set(ivs), key=ivs.count) if ivs else None
        ws, we = int(win["win_start_ms"]), int(win["win_end_ms"])
        anchor = None
        for t in look_ms:
            if t <= ws:
                anchor = t
            else:
                break
        grid = []
        if iv and anchor is not None:
            t = anchor
            while t < we:
                if ws <= t < we:
                    grid.append(t)
                t += iv
        exp_indep = len(grid) if iv else None
        # 逐网格命中（容差 60s，与运行器同口径但**独立**算）
        tol = 60_000
        miss = [g for g in grid
                if not any(abs(a - g) <= tol for a in raw_ms)]
        check("③b 独立重推应有结算数 == run.json expected",
              exp_indep == dq.get("funding_expected_settlements"),
              f"独立={exp_indep} run.json={dq.get('funding_expected_settlements')}")
        check("③c 每个应有网格时刻都有真实 settled 事件命中（非仅比次数）",
              not miss, f"缺格 {[iso(x) for x in miss[:5]]}")
        # 左端点恰在网格上时，锚点即窗口起点，必须被计入网格（旧实现从 anchor+iv 起数 ⇒ 漏 1）
        if iv:
            left_on_grid = (ws % iv == 0)
            check("③d 窗口左端点恰在结算网格上时**不得漏算**该笔",
                  (not left_on_grid) or (anchor == ws and ws in grid),
                  f"ws%iv={ws % iv} anchor={iso(anchor) if anchor else None} ws_in_grid={ws in grid}")
        else:
            check("③d 频率未验证 ⇒ 不做网格锚点核对（如实标注，不冒充通过）",
                  exp_indep is None, "interval 未观测到")
    else:
        check("③b 回看快照存在（供独立重推网格）", False, "inputs_manifest 缺 funding_events_lookback")

    # 4) 现金恒等式
    sum_gross = sum(float(t["gross_pnl"]) for t in trades)
    sum_ef = sum(float(t["entry_fee"]) for t in trades)
    sum_xf = sum(float(t["exit_fee"]) for t in trades)
    sum_fu = sum(float(t["funding_fee"]) for t in trades)
    exp_cash = init + sum_gross - sum_ef - sum_xf - sum_fu
    final_cash = float(equity[-1]["cash_equity"])
    check("④ 现金恒等式", abs(final_cash - exp_cash) < 1e-6, f"cash={final_cash} exp={exp_cash}")

    # 5) 权益恒等式
    net_sum = sum(float(t["net_pnl"]) for t in trades)
    final_eq = float(equity[-1]["total_equity"])
    open_rem = m.get("open_positions_remaining") or []
    check("⑤ 权益恒等式 final = init + Σnet（无未平仓）",
          abs(final_eq - (init + net_sum)) < 1e-6 and not open_rem,
          f"final={final_eq} exp={init + net_sum} open={open_rem}")

    # 6) 权益曲线等间隔
    ts = [int(e["ts_ms"]) for e in equity]
    gaps = {ts[i + 1] - ts[i] for i in range(len(ts) - 1)}
    check("⑥ 权益曲线等间隔 15m", gaps == {Q_MS}, str(sorted(gaps)[:5]))
    check("⑥b 首点=初始权益 & 末点=final_equity",
          abs(float(equity[0]["total_equity"]) - init) < TOL
          and abs(float(equity[-1]["total_equity"]) - final_eq) < TOL, "")

    # 7) 指标重算（**峰含初始权益**）
    nets = [float(t["net_pnl"]) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gp, gl = sum(wins), -sum(losses)
    peak, mdd = float(init), 0.0
    for e in equity:
        v = float(e["total_equity"])
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    check("⑦ 净收益 %", abs(m["net_return_pct"] - (final_eq - init) / init * 100) < 1e-6, "")
    check("⑦b 最大回撤 %（峰含初始权益）", abs(m["max_drawdown_pct"] - mdd * 100) < 1e-6,
          f"{m['max_drawdown_pct']} vs {mdd*100}")
    check("⑦c 交易数", m["trades_count"] == len(trades), f"{m['trades_count']} vs {len(trades)}")
    check("⑦d 胜率", (abs(m["win_rate_pct"] - len(wins) / len(nets) * 100) < 1e-9
                      if nets else m["win_rate_pct"] is None), "")
    pf = (gp / gl) if gl > 0 else None
    check("⑦e profit_factor", (abs(m["profit_factor"] - pf) < 1e-9 if pf is not None
                               else m["profit_factor"] is None), f"{m['profit_factor']} vs {pf}")
    aw = (gp / len(wins)) if wins else None
    al = (gl / len(losses)) if losses else None
    wl = (aw / al) if (aw and al) else None
    check("⑦f 平均盈亏比（与 profit_factor 分列）",
          (abs(m["avg_win_loss_ratio"] - wl) < 1e-9 if wl is not None
           else m["avg_win_loss_ratio"] is None), f"{m['avg_win_loss_ratio']} vs {wl}")
    led = m.get("metrics_from_ledger") or {}
    check("⑦g run.json 自带「账本重算指标」且标记一致",
          bool(led.get("available")) and bool(m.get("metrics_match"))
          and not (m.get("metrics_match_diffs") or []), str(m.get("metrics_match_diffs")))
    # ⑦h 末行持仓数（**不是**整段持仓过的行数——旧实现语义错）
    last_open = int(equity[-1]["open_positions_count"])
    rows_open = sum(1 for e in equity if int(e["open_positions_count"]) > 0)
    check("⑦h open_positions_last == 末行持仓数（≠ 整段持仓行数）",
          led.get("open_positions_last") == last_open,
          f"重算={led.get('open_positions_last')} 末行={last_open} 整段行数={rows_open}")

    # 8) 窗口纪律
    early = [t["trade_id"] for t in trades if int(t["entry_time_ms"]) < win["win_start_ms"]]
    check("⑧ 预热不交易（全部成交在窗口内）", not early, str(early[:3]))
    sig_out = [s for s in signals if not int(s["in_window"])]
    traded_sig_ms = {int(o["ts_ms"]) for o in orders if o["action"] == "entry"}
    leaked = [s["signal_bar_open_ms"] for s in sig_out
              if int(s["signal_bar_open_ms"]) + 3_600_000 in traded_sig_ms]
    check("⑧b 窗口外信号未被交易", not leaked, str(leaked[:3]))
    # 账本 trade.signal_id 必须能在 signals 表里找到（可追溯原始信号）
    sig_ids = {s["signal_id"] for s in signals}
    missing_sig = [t["trade_id"] for t in trades
                   if t.get("signal_id") and t["signal_id"] not in sig_ids]
    check("⑧c 每笔 trade.signal_id 可联结回 signals 表", not missing_sig, str(missing_sig[:3]))

    # 9) 输入快照可复核（文件 SHA == manifest 记录）
    snap_bad = []
    for name, ent in (man.get("series") or {}).items():
        p = d / ent["file"]
        if not p.exists():
            snap_bad.append((name, "missing"))
            continue
        if sha_bytes(p.read_bytes()) != ent["sha256"]:
            snap_bad.append((name, "sha_mismatch"))
    check("⑨ 输入快照文件 SHA == manifest", not snap_bad, str(snap_bad[:3]))
    check("⑨b 代码 SHA == 账本 meta == manifest",
          meta.get("code_sha256") == man["code_sha256"] == doc["hashes"]["code_sha256"], "")
    check("⑨c 代码 hash 覆盖后端文件",
          any(k.startswith("backend/") for k in (man.get("code_files") or {})), "")

    # 10) 质量门
    if dq.get("status") == "ok":
        check("⑩ quality=ok ⇒ 覆盖/对账/指标一致全部为真",
              bool(dq.get("funding_coverage_ok")) and bool(dq.get("reconcile_ok"))
              and bool(dq.get("metrics_match"))
              and bool(dq.get("signal_full_window_ok")) and bool(dq.get("exec_full_window_ok")),
              json.dumps({k: dq.get(k) for k in ("funding_coverage_ok", "reconcile_ok",
                                                 "metrics_match", "signal_full_window_ok",
                                                 "exec_full_window_ok")}, ensure_ascii=False))
    else:
        check("⑩ quality != ok ⇒ 必须给出原因", bool(dq.get("reasons")), str(dq.get("status")))

    # 11) 基准
    bench = doc.get("benchmark_buy_hold") or {}
    chart = json.loads((d / "chart.json").read_text(encoding="utf-8"))
    bs = (chart.get("benchmark") or {}).get("equity_series") or []
    check("⑪ 同成本基准存在且逐点长度 == 权益曲线",
          bench.get("benchmark_kind") == "perp_buy_and_hold_same_cost"
          and len(bs) == len(equity), f"{len(bs)} vs {len(equity)}")
    # ⑪d 基准曲线**末点**必须等于 final_equity（含出场滑点/费）——否则曲线尾端与净收益打架
    bench_init = float(m["initial_equity"])
    check("⑪d 基准末点 == final_equity（含出场滑点/费）",
          abs(float(bs[-1]["equity"]) - float(bench["final_equity"])) < 1e-9,
          f"末点={bs[-1]['equity']} final={bench['final_equity']} "
          f"Δ={float(bs[-1]['equity']) - float(bench['final_equity'])}")
    check("⑪e 基准 net_return_pct 与末点自洽",
          abs(bench["net_return_pct"]
              - (float(bs[-1]["equity"]) - bench_init) / bench_init * 100) < 1e-9,
          f"{bench['net_return_pct']} vs "
          f"{(float(bs[-1]['equity']) - bench_init) / bench_init * 100}")
    check("⑪f 基准回撤在**含末点**的同一序列上算得",
          abs(bench["max_drawdown_pct"]
              - _series_mdd(bs, bench_init) * 100) < 1e-9,
          f"{bench['max_drawdown_pct']} vs {_series_mdd(bs, bench_init) * 100}")
    check("⑪b 图表信号 confirm ∈ [win_start, win_end)",
          all(win["win_start_ms"] <= int(s["t"]) < win["win_end_ms"] for s in chart["signals"]), "")
    check("⑪c 图表无止盈规则时显式声明", chart["take_profit"]["exists"] is False, "")

    print(f"\n=== 结果：{ok} PASS / {fail} FAIL ===")
    print(json.dumps({
        "run_id": run_id, "status": doc["status"], "trades": len(trades),
        "net_return_pct": m["net_return_pct"], "final_equity": final_eq,
        "total_fees": m["total_fees"], "total_funding_fee": m["total_funding_fee"],
        "max_drawdown_pct": m["max_drawdown_pct"],
        "quality": dq.get("status"),
        "signal_frame_sha256": man["series"]["signal_frame"]["sha256"],
        "input_sha256": doc["hashes"]["input_sha256"],
        "code_sha256": doc["hashes"]["code_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    rid = sys.argv[1]
    rr = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        ROOT / "回测日志" / "oi_resonance_v1" / "runs")
    raise SystemExit(main(rid, rr))
