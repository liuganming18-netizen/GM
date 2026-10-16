#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Golden Reference 复跑 + 逐项 diff。

用途：任何 Trading Core / 策略 / 执行引擎重构之后，跑本脚本，逐项比对是否与
      `golden_reference.json` 一致。**只读行情库；只写一个新的 run 目录。**

用法：
    cd 框架/backend
    PYTHONDONTWRITEBYTECODE=1 \
    /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python \
      ../../docs/evidence/backtest_golden_2026-10-16/reproduce_golden_backtest.py

退出码：0 = 核心判据全部一致；1 = 存在实质差异。

────────────────────────────────────────────────────────────────────────────
判据分层（重要）
────────────────────────────────────────────────────────────────────────────
【核心判据】必须逐位/逐笔一致，任一不符即 FAIL：
  · 策略与执行核心身份：code_sha256 / signal_params_hash / exec_params_hash /
    signal_version / core_version / exec_core_version
  · 交易窗口：win_start_ms / win_end_ms / signal_series_start_ms / warmup_bars
  · 结果指标：initial_equity / final_equity / net_pnl / net_return_pct / gross_pnl /
    total_fees（及 entry/exit 分项）/ total_funding_fee / trades_count /
    wins / losses / flats / win_rate_pct / profit_factor / expectancy_usd /
    avg_win_loss_ratio / max_drawdown_pct
  · 计数：signals / orders / trades / funding / equity / rejected
  · **逐笔内容**：每笔的 entry_time_ms / exit_time_ms / entry_price / exit_price /
    qty / net_pnl / exit_reason（按时间排序后逐笔比对）

【已知口径差】记录但不判失败（下面每项都写明原因）：
  · trade_id / order_id
      生成式见 backtest_store.new_trade_id / new_order_id，**输入含 run_id**：
        trd_ = sha256(f"{run_id}|{symbol}|{entry_ms}|{seq}")[:16]
        ord_ = sha256(f"{run_id}|{seq}|{ts_ms}|{symbol}|{action}")[:16]
      ⇒ 每次运行 run_id 不同，ID 必然不同。这是设计（run-scoped 主键），
        不是结果差异。逐笔等价性由「核心判据」里的内容比对保证。
  · input_sha256 / signal_frame_sha256 / sig_end_ms / exec_end_ms / data_end_ms
      Golden 原始 run 用 `window.mode=auto`，其末端取「运行时刻最后已闭合 bar」；
      本复跑用**显式窗口**（否则窗口随日期漂移、根本不可复现）。
      两者**交易窗口完全相同**（win_start/win_end 一致），但「窗口末端到数据末端」
      的收尾段长度不同 ⇒ 输入快照哈希不同。交易结果不受影响（已由逐笔比对证明）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent
ROOT = GOLDEN_DIR.parents[2]                      # …/威科夫因子共振趋势
BE = ROOT / "框架" / "backend"
for p in (str(ROOT / "框架"), str(BE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("DATA_HUB_PROJECT", str(ROOT))
os.environ.setdefault("MARKET_DB", str(ROOT / "数据" / "市场数据" / "db" / "market_v2.sqlite3"))
os.environ.setdefault("MARKET_ARCHIVE", str(ROOT / "数据" / "市场数据" / "parquet"))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import backtest_api as BA  # noqa: E402

golden = json.loads((GOLDEN_DIR / "golden_reference.json").read_text(encoding="utf-8"))
led = json.loads((GOLDEN_DIR / "ledger_summary.json").read_text(encoding="utf-8"))
g_win, g_cfg = golden["market"], golden["cost_model"]
g_res, g_counts, g_strat = golden["result"], golden["counts"], golden["strategy"]

SPEC = {
    "strategy_id": "WX_OI_RESONANCE_V1",
    "exchange": g_win["exchange"],
    "market": g_win["market"],
    "symbols": [g_win["symbol"]],
    "signal_timeframe": g_win["signal_timeframe"],
    "exec_timeframe": g_win["exec_timeframe"],
    "window_start": g_win["win_start_ms"],      # 显式窗口（毫秒 epoch）
    "window_end": g_win["win_end_ms"],
    "params": {
        "maker_fee_rate": g_cfg["maker_fee_rate"],
        "taker_fee_rate": g_cfg["taker_fee_rate"],
        "fee_rate": g_cfg["fee_rate"],
        "slippage_bps": g_cfg["slippage_bps"],
        "risk_per_trade": g_cfg["risk_per_trade"],
        "max_concurrent_trades": g_cfg["max_concurrent_trades"],
        "max_portfolio_margin_pct": g_cfg["max_portfolio_margin_pct"],
        "max_leverage_per_symbol": g_cfg["max_leverage_per_symbol"],
    },
}

REL_TOL = 1e-9
diffs: list[str] = []
notes: list[str] = []


def cmp_num(label, got, want, tol=REL_TOL):
    if got is None or want is None:
        if got != want:
            diffs.append(f"{label}: got={got!r} want={want!r}")
        return
    try:
        g, w = float(got), float(want)
    except (TypeError, ValueError):
        if got != want:
            diffs.append(f"{label}: got={got!r} want={want!r}")
        return
    if w == 0:
        if abs(g) > tol:
            diffs.append(f"{label}: got={g} want={w}")
        return
    if abs(g - w) / abs(w) > tol:
        diffs.append(f"{label}: got={g} want={w} (rel={abs(g-w)/abs(w):.3e})")


def cmp_eq(label, got, want):
    if got != want:
        diffs.append(f"{label}: got={got!r} want={want!r}")


def note(label, got, want):
    if got != want:
        notes.append(f"{label}: golden={want!r} replay={got!r}")


def _sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              cwd=str(ROOT)).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    print("=" * 72)
    print("Golden Reference 复跑")
    print("  golden run_id :", golden["provenance"]["run_id"])
    print("  golden commit :", golden["repo"]["commit_short"], golden["repo"]["commit_subject"])
    print("  now    commit :", _sh("git rev-parse --short HEAD"), _sh("git log -1 --pretty=%s"))
    print("  窗口(显式)    :", g_win["win_start_ms"], "->", g_win["win_end_ms"])
    print("=" * 72)

    body, code = BA.create_run(SPEC)
    if code != 202:
        print("创建失败 http=%s body=%s" % (code, json.dumps(body, ensure_ascii=False)[:600]))
        return 1
    rid = body["run_id"]
    print("new run_id :", rid)

    status, t0 = None, time.time()
    for _ in range(450):
        time.sleep(2)
        doc, _c = BA.get_run(rid)
        status = doc.get("status")
        if status in ("succeeded", "failed", "interrupted"):
            break
    print("status=%s wall=%.1fs" % (status, time.time() - t0))
    if status != "succeeded":
        doc, _c = BA.get_run(rid)
        print("FAILED:", json.dumps(doc.get("error") or doc.get("missing_reason"),
                                    ensure_ascii=False)[:800])
        return 1

    doc, _c = BA.get_run(rid)
    met_body, _mc = BA.get_metrics(rid)
    m = (met_body.get("metrics") if isinstance(met_body, dict) else None) or {}
    if not m:
        m = doc.get("metrics") or {}
    new_win = doc.get("window") or {}
    eng = doc.get("engine") or {}

    # ── 1) 身份 / 版本 ────────────────────────────────────────────────
    cmp_eq("strategy.code_sha256", eng.get("code_sha256"), g_strat["code_sha256"])
    cmp_eq("strategy.signal_params_hash", eng.get("signal_params_hash"), g_strat["signal_params_hash"])
    cmp_eq("strategy.exec_params_hash", eng.get("exec_params_hash"), g_strat["exec_params_hash"])
    cmp_eq("strategy.signal_version", eng.get("signal_version"), g_strat["signal_version"])
    cmp_eq("strategy.exec_core_version", eng.get("exec_core_version"), g_strat["exec_core_version"])

    # ── 2) 窗口 ───────────────────────────────────────────────────────
    cmp_eq("window.win_start_ms", new_win.get("win_start_ms"), g_win["win_start_ms"])
    cmp_eq("window.win_end_ms", new_win.get("win_end_ms"), g_win["win_end_ms"])
    cmp_eq("window.signal_series_start_ms", new_win.get("signal_series_start_ms"),
           g_win["signal_series_start_ms"])
    cmp_eq("window.warmup_bars", new_win.get("warmup_bars"), g_win["warmup_bars"])
    note("window.window_source", new_win.get("window_source"), g_win["window_mode"])
    for k in ("data_end_ms", "sig_end_ms", "exec_end_ms"):
        note(f"window.{k}", new_win.get(k), g_win.get(k))

    # ── 3) 结果指标 ───────────────────────────────────────────────────
    for k in ("initial_equity", "final_equity", "net_pnl", "net_return_pct", "gross_pnl",
              "total_fees", "total_entry_fee", "total_exit_fee", "total_funding_fee",
              "trades_count", "wins", "losses", "flats", "win_rate_pct",
              "avg_win_loss_ratio", "profit_factor", "expectancy_usd", "max_drawdown_pct"):
        cmp_num(f"metrics.{k}", m.get(k), g_res["metrics_raw"].get(k))

    # ── 4) 计数 ───────────────────────────────────────────────────────
    run_dir = ROOT / "回测日志" / "oi_resonance_v1" / "runs" / rid
    con = sqlite3.connect(f"file:{run_dir/'ledger.sqlite3'}?mode=ro", uri=True)
    got_counts = {
        "signal_count": con.execute("select count(*) from signals").fetchone()[0],
        "order_count": con.execute("select count(*) from orders").fetchone()[0],
        "trade_count": con.execute("select count(*) from trades").fetchone()[0],
        "funding_event_count": con.execute("select count(*) from funding").fetchone()[0],
        "equity_points": con.execute("select count(*) from equity").fetchone()[0],
        "rejected_count": con.execute("select count(*) from rejected").fetchone()[0],
    }
    for k, v in got_counts.items():
        cmp_eq(f"counts.{k}", v, g_counts[k])

    # ── 5) 逐笔内容（核心判据；ID 不参与）──────────────────────────────
    got_trades = [dict(zip(("trade_id", "entry_time_ms", "exit_time_ms", "entry_price",
                            "exit_price", "qty", "gross_pnl", "entry_fee", "exit_fee",
                            "funding_fee", "net_pnl", "exit_reason"), r))
                  for r in con.execute(
        "select trade_id, entry_time_ms, exit_time_ms, entry_price, exit_price, qty, "
        "gross_pnl, entry_fee, exit_fee, funding_fee, net_pnl, exit_reason "
        "from trades order by entry_time_ms")]
    want_trades = led["trade_fingerprints"]
    cmp_eq("trades.length", len(got_trades), len(want_trades))
    for i, (g, w) in enumerate(zip(got_trades, want_trades)):
        for k in ("entry_time_ms", "exit_time_ms", "exit_reason"):
            cmp_eq(f"trades[{i}].{k}", g[k], w[k])
        for k in ("entry_price", "exit_price", "qty", "net_pnl"):
            cmp_num(f"trades[{i}].{k}", g[k], w[k])
        note(f"trades[{i}].trade_id", g["trade_id"], w["trade_id"])

    got_orders = [dict(zip(("order_id", "trade_id", "side", "phase", "ts_ms",
                            "action", "reason", "price", "qty", "fee"), r))
                  for r in con.execute(
        "select order_id, trade_id, side, phase, ts_ms, action, reason, price, qty, fee "
        "from orders order by ts_ms")]
    want_orders = led["order_fingerprints"]
    cmp_eq("orders.length", len(got_orders), len(want_orders))
    for i, (g, w) in enumerate(zip(got_orders, want_orders)):
        for k in ("side", "phase", "ts_ms", "action", "reason"):
            cmp_eq(f"orders[{i}].{k}", g[k], w[k])
        for k in ("price", "qty", "fee"):
            cmp_num(f"orders[{i}].{k}", g[k], w[k])
        note(f"orders[{i}].order_id", g["order_id"], w["order_id"])
    con.close()

    # ── 6) 输入快照哈希（已知口径差：auto vs explicit 的收尾段）─────────
    gh, nh = golden.get("hashes") or {}, doc.get("hashes") or {}
    for k in ("input_sha256", "signal_frame_sha256"):
        if k in gh:
            note(f"hashes.{k}", nh.get(k), gh.get(k))
    for k in ("code_sha256", "signal_params_hash", "exec_params_hash"):
        if k in gh:
            cmp_eq(f"hashes.{k}", nh.get(k), gh.get(k))

    # ── 输出 ──────────────────────────────────────────────────────────
    print()
    if notes:
        print("--- 已知口径差（不判失败；原因见文件头注释）---")
        for n in notes:
            print("  NOTE", n)
        print()
    if diffs:
        print("=== 核心判据差异 %d 项 ===" % len(diffs))
        for d in diffs[:80]:
            print("  DIFF", d)
        print("\n结果：FAIL（与 Golden Reference 核心判据不一致）")
        return 1
    print("=== 结果：PASS（核心判据与 Golden Reference 逐项一致）===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
