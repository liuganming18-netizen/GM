# -*- coding: utf-8 -*-
"""WORK-07 第四次独立复核「整改轮」逐字段对账验收（2026-10-17）。

本脚本只做两件事，且都要求**可复算、可对账**：

  对账①  Run 内全链路勾稽
          同一个 Run 的 summary ↔ orders ↔ fills ↔ positions ↔ trades ↔
          ledger ↔ equity_curve 逐字段等值，身份链唯一命中，且引擎自证恒等式成立。
  对账②  两次独立 Run 逐字段一致
          同一输入跑两次，全链路逐笔证据文件 sha256 逐字节相同；
          四类指纹（规则/代码/配置/输入）相同；
          落盘 manifest 中每个文件的 sha256 与磁盘实际内容一致。

样本分三层，**严格区分、不混报**：
  · 真实行情：BTC/ETH/SOL 2024H1（A/B/C 三版本），如实交付「0 候选」；
  · 合成单笔成交：验证成交链路（Order/Fill/Position/Ledger/Equity）确有逐笔记录；
  · 合成两笔顺序成交：验证多笔累积恒等式（net_pnl 求和、权益递增、10 日到期退出）。

产物：evidence/Work_事件趋势第四次复核整改_逐字段对账_2026-10-17.json
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

base_dir = "/Users/boos/Desktop/威科夫因子共振趋势"
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from 策略.WyckoffMaster.event_trend_v0_1.config import EventTrendConfig
from 策略.WyckoffMaster.event_trend_v0_1.contracts import CandidateSnapshot
from 策略.WyckoffMaster.event_trend_v0_1.engine_adapter import run_event_trend_backtest
from 策略.WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner

HOUR_MS = 3600 * 1000
MIN15_MS = 15 * 60 * 1000
DAY_MS = 24 * HOUR_MS
TOL = 1e-6

FROZEN_RULES_SHA = "1f1167de35412cb7d1d3608d8069c4108324f7641de7c369bf94882f1679a8ab"

# Run 产物落盘位置：**放在本轮整改证据目录内**，不放 `回测日志/event_trend_v0_1/runs/`。
# 原因：该默认目录会被并行工作流的运行产物清理动作整目录删除（实测 2026-10-17
# 00:48 / 00:51 / 01:00 三次），导致「新 Run 的完整逐笔证据」无法长期留存。
RUNS_BASE = os.path.join(
    base_dir, "evidence/Work_事件趋势第四次复核整改_2026-10-17/runs")

# 全链路逐笔证据文件（对账②的比对范围；inputs/summary 含 run_id 与时间戳，天然不同）
CHAIN_FILES = (
    "orders.jsonl", "fills.jsonl", "positions.jsonl", "trades.jsonl",
    "rejects.jsonl", "funding.jsonl", "ledger.jsonl", "equity_curve.jsonl",
    "decisions.jsonl", "validation_results.jsonl",
)
ALL_FILES = CHAIN_FILES + ("inputs.json", "summary.json")


# ══════════════════════════ 合成夹具 ══════════════════════════

def _candidate(cid: str, ev: str, snapshot_close_ms: int, exec_ms: int,
               symbol: str = "SOLUSDT") -> CandidateSnapshot:
    return CandidateSnapshot.create(
        candidate_id=cid, event_id=ev, strategy_version="v0.1.0",
        symbol=symbol, venue="binance",
        snapshot_time_ms=snapshot_close_ms, available_at_ms=snapshot_close_ms,
        box_high_h=110.0, box_low_l=100.0, box_atr_a=2.0, box_er=0.15,
        sweep_low_s=99.2, retest_low_lr=100.3, breakout_close=111.0,
        breakout_volume=2500.0, v20_median_volume=1000.0, volume_ratio=2.5,
        oi_breakout=103500.0, oi_6h_ago=100000.0, oi_growth_6h=0.035,
        initial_stop_price=100.1, initial_risk_r=10.9,
        max_entry_price=112.0, min_entry_price=110.0,
        execution_earliest_ms=exec_ms)


def _bars_15m(start_ms: int, n: int, *, open_px: float,
              quote_volume: float = 5_500_000.0) -> pd.DataFrame:
    rows = []
    for k in range(n):
        ts = start_ms + k * MIN15_MS
        rows.append({"timestamp": ts, "open": open_px, "high": open_px,
                     "low": open_px, "close": open_px,
                     "volume": 50000.0, "quote_volume": quote_volume})
    return pd.DataFrame(rows)


def scenario_single_trade() -> Tuple[pd.DataFrame, pd.DataFrame, List[CandidateSnapshot], List[dict]]:
    """合成单笔成交：入场后一路上行，期末平仓。"""
    t0 = 1788652800000
    t_1h_open = t0 + DAY_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS
    cand = _candidate("cand_single_001", "ev_single_001", t_1h_close, t_exec)

    df_1h = pd.DataFrame([{
        "timestamp": t_1h_open, "open": 105.0, "high": 111.5, "low": 104.8,
        "close": 111.0, "volume": 2500.0, "quote_volume": 277_500.0}])

    df_15m = _bars_15m(t_exec - MIN15_MS, 21, open_px=111.2)
    df_15m.loc[0, ["open", "high", "low", "close"]] = (111.0, 111.3, 110.9, 111.1)
    df_15m.loc[1, ["open", "high", "low", "close"]] = (111.2, 111.8, 111.0, 111.5)
    for k in range(1, 20):
        df_15m.loc[1 + k, ["open", "high", "low", "close"]] = (
            111.5 + k * 0.5, 111.8 + k * 0.5, 111.3 + k * 0.5, 111.6 + k * 0.5)
    funding = [{"time_ms": t_exec + 4 * HOUR_MS, "rate": 0.0001,
                "mark_price": 115.0, "rate_type": "settled"}]
    return df_1h, df_15m, [cand], funding


def scenario_two_sequential_trades() -> Tuple[pd.DataFrame, pd.DataFrame, List[CandidateSnapshot], List[dict]]:
    """合成两笔顺序成交：两笔都由冻结规则「最长持仓 10 个自然日」到期退出。

    第 1 笔 t_exec 入场 → t_exec+10d 到期平仓；
    第 2 笔 t_exec+11d 入场 → t_exec+21d 到期平仓（行情覆盖 24 天）。
    """
    t0 = 1788652800000
    t1_open = t0 + DAY_MS
    t1_close = t1_open + HOUR_MS
    t1_exec = t1_close + MIN15_MS

    t2_open = t1_open + 11 * DAY_MS
    t2_close = t2_open + HOUR_MS
    t2_exec = t2_close + MIN15_MS

    c1 = _candidate("cand_seq_001", "ev_seq_001", t1_close, t1_exec)
    c2 = _candidate("cand_seq_002", "ev_seq_002", t2_close, t2_exec)

    df_1h = pd.DataFrame([
        {"timestamp": t1_open, "open": 105.0, "high": 111.5, "low": 104.8,
         "close": 111.0, "volume": 2500.0, "quote_volume": 277_500.0},
        {"timestamp": t2_open, "open": 111.0, "high": 111.5, "low": 110.5,
         "close": 111.0, "volume": 2500.0, "quote_volume": 277_500.0},
    ])
    # 横盘 111.2，始终远离止损 100.1 ⇒ 只能由 10 日到期规则退出
    df_15m = _bars_15m(t1_exec - MIN15_MS, 24 * 96 + 2, open_px=111.2)
    return df_1h, df_15m, [c1, c2], []


# ══════════════════════════ 对账① ══════════════════════════

def reconcile_within_run(label: str, res: Dict[str, Any]) -> Dict[str, Any]:
    """单个 Run 内部全链路逐字段勾稽。"""
    trades = res["engine_result"]["trades"]
    eq = res["engine_result"]["equity_curve"]
    orders = res["orders"]
    ledger = res["ledger_events"]
    summary = res["summary"]
    recon = res["engine_result"].get("reconcile", {})
    vals = res["validation_results"]

    def s(k: str) -> float:
        return float(sum(float(t.get(k, 0.0) or 0.0) for t in trades))

    checks: Dict[str, Dict[str, Any]] = {}

    def add(name: str, ok: bool, detail: Any) -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    add("C01_trade_count", summary["trade_count"] == len(trades),
        {"summary": summary["trade_count"], "actual": len(trades)})
    add("C02_order_count", summary["order_count"] == len(orders),
        {"summary": summary["order_count"], "actual": len(orders)})
    add("C03_net_pnl_sum", abs(summary["net_pnl"] - s("net_pnl")) < TOL,
        {"summary": summary["net_pnl"], "sum_trades": s("net_pnl")})
    add("C04_equity_delta",
        abs((summary["final_equity"] - summary["initial_equity"]) - s("net_pnl")) < TOL,
        {"equity_delta": summary["final_equity"] - summary["initial_equity"],
         "sum_net_pnl": s("net_pnl")})
    add("C05_fee_sum", abs(summary["fee_total"] - s("fees")) < TOL,
        {"summary": summary["fee_total"], "sum_trades": s("fees")})
    add("C06_funding_sum", abs(summary["funding_total"] - s("funding_fee")) < TOL,
        {"summary": summary["funding_total"], "sum_trades": s("funding_fee")})
    add("C07_slippage_sum", abs(summary["slippage_total"] - s("slippage_loss")) < TOL,
        {"summary": summary["slippage_total"], "sum_trades": s("slippage_loss")})

    # 权益曲线首末点
    if eq:
        add("C08_equity_curve_ends",
            abs(float(eq[0]["total_equity"]) - summary["initial_equity"]) < TOL
            and abs(float(eq[-1]["total_equity"]) - summary["final_equity"]) < 1e-9,
            {"head": eq[0]["total_equity"], "tail": eq[-1]["total_equity"],
             "initial_equity": summary["initial_equity"],
             "final_equity": summary["final_equity"]})
    else:
        add("C08_equity_curve_ends", False, "equity_curve 为空")

    # 身份链唯一命中：每笔成交的 signal_id 在 orders 中恰有一条，且两处 signal_id 相同
    by_oid = {o["order_id"]: o for o in orders}
    unmatched = []
    dup = []
    for t in trades:
        o = by_oid.get(t.get("order_id"))
        if o is None or o.get("signal_id") != t.get("signal_id"):
            unmatched.append({"trade_signal_id": t.get("signal_id"),
                              "trade_order_id": t.get("order_id")})
        if t.get("signal_id") is None or t.get("order_id") is None \
                or t.get("exit_order_id") is None:
            dup.append(t.get("signal_id"))
    add("C09_identity_unique", not unmatched and not dup,
        {"unmatched": unmatched, "missing_identity_fields": dup,
         "trades": len(trades), "orders": len(orders)})

    # 候选 ↔ 成交：已执行候选数 == 成交笔数；未执行候选不得带 order_id
    executed = [v for v in vals if v.executed]
    not_exec_with_order = [v.candidate_id for v in vals
                           if (not v.executed) and v.order_id is not None]
    add("C10_candidate_trade_map",
        len(executed) == len(trades) and not not_exec_with_order,
        {"executed_candidates": len(executed), "trades": len(trades),
         "not_executed_with_order": not_exec_with_order})

    # 账本事件配平
    def cnt(et: str) -> int:
        return sum(1 for e in ledger if e.get("event_type") == et)

    add("C11_position_pairs",
        cnt("POSITION_OPENED") == cnt("POSITION_CLOSED") == len(trades),
        {"POSITION_OPENED": cnt("POSITION_OPENED"),
         "POSITION_CLOSED": cnt("POSITION_CLOSED"), "trades": len(trades)})
    add("C12_fill_events", cnt("ORDER_FILLED") == 2 * len(trades),
        {"ORDER_FILLED": cnt("ORDER_FILLED"), "expected": 2 * len(trades)})
    add("C13_fee_events", cnt("FEE_CHARGED") == 2 * len(trades),
        {"FEE_CHARGED": cnt("FEE_CHARGED"), "expected": 2 * len(trades)})

    # 账本幂等：event_id 唯一
    ids = [e.get("event_id") for e in ledger]
    add("C14_ledger_idempotent", len(ids) == len(set(ids)),
        {"events": len(ids), "unique": len(set(ids))})

    # 引擎自证恒等式
    add("C15_engine_identities",
        bool(recon.get("cash_identity_ok")) and bool(recon.get("equity_identity_ok"))
        and bool(recon.get("equity_curve_tail_matches_final")),
        {k: recon.get(k) for k in ("cash_identity_ok", "equity_identity_ok",
                                   "equity_curve_tail_matches_final")})

    # 未成交时不得出现非零盈亏（禁止用 0 冒充已有结果）
    if not trades:
        add("C16_no_phantom_pnl",
            summary["net_pnl"] == 0.0 and summary["final_equity"] == summary["initial_equity"]
            and summary["fee_total"] == 0.0,
            {"net_pnl": summary["net_pnl"], "final_equity": summary["final_equity"]})
    else:
        add("C16_no_phantom_pnl", True, "有成交，跳过零值检查")

    return {
        "label": label,
        "trades": len(trades),
        "orders": len(orders),
        "ledger_events": len(ledger),
        "equity_points": len(eq),
        "all_pass": all(c["pass"] for c in checks.values()),
        "checks": checks,
    }


# ══════════════════════════ 对账② ══════════════════════════

def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reconcile_two_runs(label: str, runner: EventTrendRunner,
                       df_1h, df_15m, candidates, funding,
                       fingerprints: Dict[str, Any]) -> Dict[str, Any]:
    """同一输入两次独立 Run：全链路证据逐字节一致 + 指纹一致 + manifest 可复算。"""
    res1 = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, candidates,
                                    funding, variant="A")
    res2 = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, candidates,
                                    funding, variant="A")

    f1 = runner._build_run_files(run_id="run_rep_1", symbol="SOLUSDT", variant="A",
                                 sm_inst=None, res_inst=res1, funnel={},
                                 run_timestamp=1, data_summary={},
                                 fingerprints=fingerprints)
    f2 = runner._build_run_files(run_id="run_rep_2", symbol="SOLUSDT", variant="A",
                                 sm_inst=None, res_inst=res2, funnel={},
                                 run_timestamp=2, data_summary={},
                                 fingerprints=fingerprints)

    per_file: Dict[str, Dict[str, Any]] = {}
    for name in CHAIN_FILES:
        h1, h2 = _sha_text(f1[name]), _sha_text(f2[name])
        per_file[name] = {"sha256_run1": h1, "sha256_run2": h2,
                          "identical": h1 == h2, "bytes": len(f1[name].encode())}
    chain_ok = all(v["identical"] for v in per_file.values())

    # 落盘：真实写入两个目录，再按磁盘内容复算 manifest
    disk_checks: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        dirs = {}
        for tag, files in (("run_rep_1", f1), ("run_rep_2", f2)):
            d = os.path.join(tmp, tag)
            dirs[tag] = d
            runner._commit_run_dir(d, files)
        for tag in dirs:
            d = dirs[tag]
            with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
                man = json.load(fh)
            bad = []
            for name, meta in man["files"].items():
                with open(os.path.join(d, name), encoding="utf-8") as fh:
                    content = fh.read()
                if _sha_text(content) != meta["sha256"]:
                    bad.append(name)
            disk_checks[tag] = {"files": man["files_count"],
                                "manifest_mismatch": bad,
                                "commit_mode": man["commit_mode"]}
        # 磁盘上两份全链路证据逐字节一致
        disk_identical = {}
        for name in CHAIN_FILES:
            with open(os.path.join(dirs["run_rep_1"], name), encoding="utf-8") as fh:
                a = fh.read()
            with open(os.path.join(dirs["run_rep_2"], name), encoding="utf-8") as fh:
                b = fh.read()
            disk_identical[name] = (a == b)

    # 指纹：两次调用必须给出相同四类指纹
    code1 = runner.code_fingerprint()
    code2 = runner.code_fingerprint()
    cfg = EventTrendConfig()
    fp_stable = {
        "rules": runner.rules_fingerprint()["sha256"] == FROZEN_RULES_SHA,
        "code": code1["combined_sha256"] == code2["combined_sha256"],
        "config": runner.config_fingerprint(cfg)["sha256"]
        == runner.config_fingerprint(EventTrendConfig())["sha256"],
        "input": (runner.input_fingerprint(df_1h, df_1h, df_15m,
                                           np.array([], dtype=np.int64),
                                           np.array([], dtype=np.float64),
                                           funding)["bar_15m"]["sha256"]
                  == runner.input_fingerprint(df_1h, df_1h, df_15m,
                                              np.array([], dtype=np.int64),
                                              np.array([], dtype=np.float64),
                                              funding)["bar_15m"]["sha256"]),
    }

    return {
        "label": label,
        "chain_files_identical": chain_ok,
        "disk_files_identical": all(disk_identical.values()),
        "fingerprints_stable": fp_stable,
        "disk_checks": disk_checks,
        "per_file": per_file,
        "all_pass": chain_ok and all(disk_identical.values()) and all(fp_stable.values()),
    }


# ══════════════════════════ 主流程 ══════════════════════════

def main() -> int:
    runner = EventTrendRunner(base_dir=base_dir)
    os.makedirs(RUNS_BASE, exist_ok=True)
    runner.runs_base = RUNS_BASE
    out: Dict[str, Any] = {
        "meta": {
            "title": "WORK-07 第四次复核整改轮 · 逐字段对账（对账①Run内勾稽 / 对账②两次独立Run一致）",
            "date": "2026-10-17",
            "timestamp": int(time.time()),
            "strategy": "WX_EVENT_TREND_V0_1",
            "strategy_version": "v0.1.0",
            "frozen_rules_sha256": FROZEN_RULES_SHA,
            "runs_base": RUNS_BASE,
            "governance_status": {
                "work_06_gate": "FAIL（未解除）",
                "work_07_engineering_gate": "NOT_ASSERTED（本脚本不自行宣告 PASS）",
                "work_07_research_gate": "FAIL（真实 2024H1 三币 0 候选，样本不足）",
                "pit": "UNPROVEN（pit_proven=False，未伪造发布/到达时间）",
            },
        },
        "reconciliation_1_within_run": {},
        "reconciliation_2_two_runs": {},
        "real_market_playback": {},
    }

    # ── 真实行情：BTC/ETH/SOL 2024H1 ──
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        df_4h, df_1h, df_15m, oi_ts, oi_vals, funding = runner.load_symbol_data(
            sym, "2024-01-01", "2024-06-30")
        res_abc = runner.run_variant_abc(sym, df_4h, df_1h, df_15m,
                                         oi_ts, oi_vals, funding)
        entry = {"run_ids": res_abc["run_ids"], "funnel_a": res_abc["funnel_a"]}
        for vname, rkey in (("A", "res_a"), ("B", "res_b"), ("C", "res_c")):
            r = res_abc[rkey]
            entry[f"variant_{vname}"] = reconcile_within_run(
                f"{sym}/variant_{vname}", r)
        out["real_market_playback"][sym] = entry

    # ── 合成单笔成交 ──
    df_1h, df_15m, cands, funding = scenario_single_trade()
    res_single = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, cands,
                                          funding, variant="A")
    out["reconciliation_1_within_run"]["synthetic_single_trade"] = \
        reconcile_within_run("synthetic_single_trade", res_single)

    # ── 合成两笔顺序成交 ──
    df_1h2, df_15m2, cands2, funding2 = scenario_two_sequential_trades()
    res_two = run_event_trend_backtest("SOLUSDT", df_1h2, df_15m2, cands2,
                                       funding2, variant="A")
    r_two = reconcile_within_run("synthetic_two_sequential_trades", res_two)
    # 追加多笔累积断言
    tr2 = res_two["engine_result"]["trades"]
    reasons = sorted(t["exit_reason"] for t in tr2)
    r_two["checks"]["C17_two_trades_and_expiry"] = {
        "pass": (len(tr2) == 2 and reasons == ["max_holding_expired"] * 2),
        "detail": {"trades": len(tr2), "exit_reasons": reasons,
                   "holding_hours": [(t["exit_time"] - t["entry_time"]) / HOUR_MS
                                     for t in tr2]},
    }
    r_two["all_pass"] = all(c["pass"] for c in r_two["checks"].values())
    out["reconciliation_1_within_run"]["synthetic_two_sequential_trades"] = r_two

    # ── 对账②：两次独立 Run 逐字段一致（用两笔场景，样本最重） ──
    fp_bundle = {
        "rules": runner.rules_fingerprint(),
        "code": runner.code_fingerprint(),
        "config": runner.config_fingerprint(EventTrendConfig()),
    }
    out["reconciliation_2_two_runs"]["synthetic_two_sequential_trades"] = \
        reconcile_two_runs("synthetic_two_sequential_trades", runner,
                           df_1h2, df_15m2, cands2, funding2, fp_bundle)
    out["reconciliation_2_two_runs"]["synthetic_single_trade"] = \
        reconcile_two_runs("synthetic_single_trade", runner,
                           df_1h, df_15m, cands, funding, fp_bundle)

    # ── 汇总 ──
    r1 = out["reconciliation_1_within_run"]
    r2 = out["reconciliation_2_two_runs"]
    real_runs = [v for sym in out["real_market_playback"].values()
                 for k, v in sym.items() if k.startswith("variant_")]
    out["summary"] = {
        "reconciliation_1_runs_total": len(r1) + len(real_runs),
        "reconciliation_1_all_pass": all(v["all_pass"] for v in r1.values())
        and all(v["all_pass"] for v in real_runs),
        "reconciliation_2_all_pass": all(v["all_pass"] for v in r2.values()),
        "synthetic_trades_single": len(res_single["engine_result"]["trades"]),
        "synthetic_trades_two": len(tr2),
        "real_market_candidates": {s: out["real_market_playback"][s]["funnel_a"]["candidates"]
                                   for s in out["real_market_playback"]},
    }

    outp = os.path.join(base_dir,
                        "evidence/Work_事件趋势第四次复核整改_逐字段对账_2026-10-17.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print(f"\n已写出：{outp}")
    ok = out["summary"]["reconciliation_1_all_pass"] and \
        out["summary"]["reconciliation_2_all_pass"]
    print("逐字段对账总判定：", "全部通过" if ok else "存在未通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
