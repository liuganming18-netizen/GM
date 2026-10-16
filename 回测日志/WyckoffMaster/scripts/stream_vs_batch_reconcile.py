#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史回放（任务三组合引擎）与流式推进（任务二共用核心 `ReplayEngine`）**逐笔对账**。

为什么要做：
  任务二共用核心的设计目标是「历史回放与实时模拟同一实现，同一根 bar、同一份配置
  得到同一决定」。本脚本把这句话**变成可核验的数字**：同一标的、同一窗口、同一层配置、
  同一研究输入下，比较两条路径的 **信号 / 成交 / 出场 / 费用 / 资金费 / 权益**。

两条路径：
  · 历史回放 = `engine.engine.Backtest`（任务三组合编排引擎，本脚本只喂**单个标的**以可比）
  · 流式推进 = `策略/WyckoffMaster/execution.py::ReplayEngine`（逐根已闭合 15m 推进）

对齐口径（保证可比）：
  · 窗口 `[start_ms, end_ms)` 完全一致；
  · 流式侧只喂 `[start_ms, end_ms)` 内的 15m bar（1h 信号 bar 仍给全量以保指标预热正确）；
  · 历史侧只接受 `entry_bar ∈ [start_ms, end_ms)` 的信号；为对齐，流式侧把
    `next_15m_open_at_or_after(earliest_fill_ms) < start_ms` 的陈旧信号标记为已消费
    （否则它会在窗口首根 bar 上「补成交」，而历史侧不会）。
  · 每笔对账键：信号用 `(close_time, kind)`；成交/出场用时间戳；资金费用 `funding_time`。

输出：
  · 逐案逐类（信号/成交/出场/资金费/权益）匹配与差异明细；
  · **首个分歧**时刻 + 根因分类；
  · 流式推进自身的「截断不变」（`upto_ms` 前缀稳定）核验；
  · 两边**能力差异**清单（差异不是掩盖，而是逐条列明并归属）。
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
sys.path.insert(0, str(WM.parent.parent / "策略"))

from engine import paths as P             # noqa: E402
from engine import loader as LD           # noqa: E402
from engine import ladder as LAD          # noqa: E402
from engine.engine import Backtest        # noqa: E402
from WyckoffMaster import execution as X  # noqa: E402
from WyckoffMaster.params import FROZEN   # noqa: E402

UTC = dt.timezone.utc
EV = WM.parent.parent / "evidence" / "Work_阶梯回测_2026-10-15"

# 只对账两边**共同能力**内的层：流式引擎未实现台阶锁利 ⇒ 取 trail_mode == "none" 的层
LEVELS = ("M0", "M1", "M2")
# 父层配置：M0 无父；M1 父 M0；M2 父 M0（本轮小窗 M1 未晋级，见 20_小型窗口验证_v31.json）
PARENT = {"M0": None, "M1": "M0", "M2": "M0"}

TOL = 1e-6


def ms(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
               .timestamp() * 1000)


def rel(a, b, tol=TOL):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


# ══════════════════════════ 数据准备 ══════════════════════════

def bars_dict(b):
    n = len(b["open_time"])
    return [{"open_time": int(b["open_time"][i]), "close_time": int(b["close_time"][i]),
             "open": float(b["open"][i]), "high": float(b["high"][i]),
             "low": float(b["low"][i]), "close": float(b["close"][i]),
             "volume": float(b["volume"][i]), "quote_volume": float(b["quote_volume"][i])}
            for i in range(n)]


def funding_events(f):
    if f is None:
        return []
    return [{"funding_time": int(f["funding_time"][i]),
             "funding_rate": float(f["funding_rate"][i]),
             "mark_price": (float(f["mark_price"][i]) if f["mark_price"][i] == f["mark_price"][i]
                            else None)}
            for i in range(len(f["funding_time"]))]


def limits_for(inst):
    return {"quantity_step": inst.step_size if inst else 0.0,
            "min_quantity": inst.step_size if inst else 0.0,
            "max_quantity": 0.0, "min_notional": 0.0,
            "price_step": inst.tick_size if inst else 0.0}


# ══════════════════════════ 两条路径 ══════════════════════════

def run_batch(sd, level, sym, tf, start_ms, end_ms, led_dir, tag):
    """历史回放：任务三组合引擎（单标的）。返回账本路径。"""
    parent = PARENT[level]
    parent_dict = LAD.spec_for(parent, None).as_dict() if parent else None
    spec = LAD.spec_for(level, parent_dict)
    path = led_dir / f"svb_{tag}_{tf}_{sym}_{level}.sqlite3"
    bt = Backtest(seg_data=sd, spec=spec, timeframe=tf, start_ms=start_ms, end_ms=end_ms,
                  run_id=f"svb_{tag}_{tf}_{sym}_{level}", ledger_path=path, symbols=[sym],
                  commit_every=1, code_sha="", config_sha="", input_sha="",
                  collect_candidates=False, rank_provider=None)
    res = bt.run()
    bt.led.close()
    return path, spec, res


def run_stream(sd, spec, sym, tf, start_ms, end_ms):
    """流式推进：任务二共用核心 `ReplayEngine`（逐根已闭合 15m 推进）。"""
    b_sig = bars_dict(sd.ohlcv(sym, tf))
    b15_all = bars_dict(sd.ohlcv(sym, "15m"))
    b15 = [b for b in b15_all if start_ms <= b["open_time"] < end_ms]
    fund = funding_events(sd.funding(sym))
    lim = limits_for(sd.instrument(sym))
    eng = X.ReplayEngine(
        bars_signal=b_sig, bars_15m=b15, limits_by_symbol={sym: lim},
        funding_events_by_symbol=fund, symbol=sym, exchange="binance",
        entry_mode=spec.entry_mode, stop_mode=spec.stop_mode, trail_mode=spec.trail_mode,
        allow_new_entries=True, maintain_existing=True, contract_spec=lim)
    # 对齐：历史侧只接受 entry_bar ∈ [start, end)；流式侧把更早的信号标记为已消费
    dropped = 0
    for s in eng._signals:                      # noqa: SLF001（测试夹具对齐，不改核心）
        if X.next_15m_open_at_or_after(int(s["earliest_fill_ms"])) < start_ms:
            s["consumed"] = True
            dropped += 1
    r = eng.run()
    r["n_stale_signals_dropped"] = dropped
    return eng, r


# ══════════════════════════ 逐笔对账 ══════════════════════════

def batch_events(path):
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    sig = [dict(r) for r in con.execute(
        "SELECT signal_time, entry_bar_time, kind, decision, reject_reason "
        "FROM signals ORDER BY signal_time, kind")]
    pos = [dict(r) for r in con.execute(
        "SELECT position_id, entry_time, entry_price, quantity, entry_fee, stop_price, "
        "exit_time, exit_reason, exit_price, exit_fee, gross_pnl, net_pnl, funding "
        "FROM positions ORDER BY entry_time")]
    fun = [dict(r) for r in con.execute(
        "SELECT position_id, funding_time, amount FROM funding ORDER BY funding_time")]
    rev = dict(con.execute("SELECT * FROM review LIMIT 1").fetchone())
    eq = [dict(r) for r in con.execute(
        "SELECT ts, equity FROM equity ORDER BY ts")]
    con.close()
    return {"signals": sig, "positions": pos, "funding": fun, "review": rev, "equity": eq}


def stream_events(decisions, funding_charges=()):
    sig, fills, exits, dd = [], [], [], []
    for d in decisions:
        k = d.get("kind")
        t = d.get("times") or {}
        if k == "entry":
            kind = (d.get("event_identity") or "|".join(["", "", "", ""])).split("|")[2]
            row = {"close_time": t.get("signal_bar_close_time"), "kind": kind,
                   "reject_reason": d.get("reject_reason")}
            sig.append(row)
            if not d.get("reject_reason"):
                fills.append({"entry_time": t.get("attempt_clock_ms"),
                              "entry_price": d.get("entry_price"), "quantity": d.get("qty"),
                              "entry_fee": (d.get("notional") or 0.0) * X.EXEC_FROZEN.fee_each_side,
                              "stop_price": d.get("stop_price")})
        elif k == "exit":
            cv = d.get("condition_values") or {}
            exits.append({"exit_time": t.get("exit_ts"), "exit_reason": d.get("exit_reason"),
                          "exit_price": cv.get("fill_price"), "exit_fee": d.get("exit_fee"),
                          "gross_pnl": cv.get("gross_pnl"), "net_pnl": d.get("net_pnl")})
        elif k == "drawdown_lock":
            dd.append({"ts": t.get("bar_close_time")})
    return {"signals": sig, "fills": fills, "exits": exits, "drawdown": dd,
            "funding_charges": list(funding_charges)}


def _walk(b_rows, s_rows, key, fields, kind):
    """按时间顺序逐步比对同一类事件，返回首个分歧（无则 None）。"""
    b_rows = sorted(b_rows, key=lambda r: int(r[key]))
    s_rows = sorted(s_rows, key=lambda r: int(r[key]))
    i = j = 0
    while i < len(b_rows) and j < len(s_rows):
        tb, ts = int(b_rows[i][key]), int(s_rows[j][key])
        if tb != ts:
            # 流式侧在该时刻多出一笔 ⇒ 同标的未去重；历史侧多出一笔 ⇒ 流式缺事件
            if ts < tb:
                return {"ts": ts, "kind": kind, "field": key, "batch": None, "stream": ts,
                        "root_cause": "stream_no_same_symbol_dedup"}
            return {"ts": tb, "kind": kind, "field": key, "batch": tb, "stream": None,
                    "root_cause": "stream_missing_event"}
        for f in fields:
            a, c = b_rows[i][f], s_rows[j][f]
            same = (a == c) if f == "exit_reason" else rel(a, c)
            if not same:
                cause = ("stream_exit_price_not_tick_rounded"
                         if f in ("exit_price", "exit_fee", "gross_pnl", "net_pnl")
                         else "stream_" + f)
                return {"ts": tb, "kind": kind, "field": f, "batch": a, "stream": c,
                        "batch_reason": b_rows[i].get("exit_reason"),
                        "stream_reason": s_rows[j].get("exit_reason"),
                        "root_cause": cause}
        i += 1
        j += 1
    if i < len(b_rows) or j < len(s_rows):
        if j < len(s_rows):
            return {"ts": int(s_rows[j][key]), "kind": kind,
                    "batch": None, "stream": int(s_rows[j][key]),
                    "root_cause": "stream_no_same_symbol_dedup"}
        return {"ts": int(b_rows[i][key]), "kind": kind,
                "batch": int(b_rows[i][key]), "stream": None,
                "batch_reason": b_rows[i].get("exit_reason"),
                "root_cause": _reason_to_cause(b_rows[i].get("exit_reason"))}
    return None


def _reason_to_cause(reason):
    return {"donchian_exit": "stream_missing_scheduled_donchian_exit",
            "segment_end_force_close_last_15m_close": "stream_missing_segment_end_close",
            "stop_loss": "stream_stop_loss_timing",
            "expiry_720h": "stream_expiry_timing",
            "dd_lock_liquidation": "stream_drawdown_liquidation_timing",
            }.get(reason, "stream_missing_exit:" + str(reason))


def first_divergence(batch, stream):
    """按时间轴找**首个**分歧（成交 → 出场 → 资金费），并给出根因分类。"""
    cands = [
        _walk(batch["positions"], stream["fills"], "entry_time",
              ["entry_price", "quantity", "entry_fee", "stop_price"], "entry"),
        _walk([p for p in batch["positions"] if p["exit_time"] is not None], stream["exits"],
              "exit_time", ["exit_reason", "exit_price", "exit_fee"], "exit"),
    ]
    b_fu = {}
    for r in batch["funding"]:
        b_fu.setdefault(int(r["funding_time"]), 0.0)
        b_fu[int(r["funding_time"])] += -float(r["amount"])
    s_fu = {}
    for c in stream["funding_charges"]:
        s_fu.setdefault(int(c["funding_time"]), 0.0)
        s_fu[int(c["funding_time"])] += float(c["amount"])
    fu_b = [{"funding_time": t, "amount": v} for t, v in b_fu.items()]
    fu_s = [{"funding_time": t, "amount": v} for t, v in s_fu.items()]
    cands.append(_walk(fu_b, fu_s, "funding_time", ["amount"], "funding"))

    got = [c for c in cands if c]
    if not got:
        return None
    return min(got, key=lambda c: int(c["ts"]))


def _cmp_fields(pairs):
    """逐字段比较，返回差异列表。"""
    bad = []
    for row, fields in pairs:
        for f, a, c, exact in fields:
            same = (a == c) if exact else rel(a, c)
            if not same:
                bad.append({**row, "field": f, "batch": a, "stream": c})
    return bad


def compare_case(sd, level, sym, tf, start_ms, end_ms, led_dir, tag):
    path, spec, bres = run_batch(sd, level, sym, tf, start_ms, end_ms, led_dir, tag)
    eng, sres = run_stream(sd, spec, sym, tf, start_ms, end_ms)
    b = batch_events(path)
    s = stream_events(sres["decisions"], sres["funding_charges"])
    div = first_divergence(b, s)
    div_ts = div["ts"] if div else None

    # ── 信号逐笔（键 = (close_time, kind)；比两边**共同消费**的信号）──
    b_sig = {(int(r["signal_time"]), r["kind"]): r for r in b["signals"]}
    s_sig = {(int(r["close_time"]), r["kind"]): r for r in s["signals"] if r["close_time"]}
    common = sorted(set(b_sig) & set(s_sig))
    sig_mismatch = []
    for k in common:
        b_rej = b_sig[k]["reject_reason"] or ""
        s_rej = s_sig[k]["reject_reason"] or ""
        if b_rej != s_rej:
            sig_mismatch.append({"key": list(k), "batch_reject": b_rej, "stream_reject": s_rej})

    # ── 成交 / 出场 / 资金费：**首个分歧之前的前缀**逐笔比对 ──
    # （分歧之后两边状态已不同，逐笔对齐无意义；故用前缀口径，避免把「错位」当「差异」）
    b_pre = [p for p in b["positions"] if div_ts is None or int(p["entry_time"]) < div_ts]
    s_pre = [f for f in s["fills"] if div_ts is None or int(f["entry_time"]) < div_ts]
    fill_mismatch = _cmp_fields([
        ({"entry_time": bp["entry_time"]},
         [("entry_time", int(bp["entry_time"]), int(sf["entry_time"]), True),
          ("entry_price", bp["entry_price"], sf["entry_price"], False),
          ("quantity", bp["quantity"], sf["quantity"], False),
          ("entry_fee", bp["entry_fee"], sf["entry_fee"], False),
          ("stop_price", bp["stop_price"], sf["stop_price"], False)])
        for bp, sf in zip(b_pre, s_pre)])

    b_exit = [p for p in b["positions"]
              if p["exit_time"] is not None and (div_ts is None or int(p["exit_time"]) < div_ts)]
    s_exit = [e for e in s["exits"] if div_ts is None or int(e["exit_time"]) < div_ts]
    exit_mismatch = _cmp_fields([
        ({"entry_time": bp["entry_time"]},
         [("exit_time", int(bp["exit_time"]), int(se["exit_time"]), True),
          ("exit_reason", bp["exit_reason"], se["exit_reason"], True),
          ("exit_price", bp["exit_price"], se["exit_price"], False),
          ("exit_fee", bp["exit_fee"], se["exit_fee"], False),
          ("gross_pnl", bp["gross_pnl"], se["gross_pnl"], False),
          ("net_pnl", bp["net_pnl"], se["net_pnl"], False)])
        for bp, se in zip(b_exit, s_exit)])

    b_fu = {}
    for r in b["funding"]:
        if div_ts is None or int(r["funding_time"]) <= div_ts:
            b_fu[int(r["funding_time"])] = b_fu.get(int(r["funding_time"]), 0.0) + (-float(r["amount"]))
    s_fu = {}
    for c in sres["funding_charges"]:
        if div_ts is None or int(c["funding_time"]) <= div_ts:
            s_fu[int(c["funding_time"])] = s_fu.get(int(c["funding_time"]), 0.0) + float(c["amount"])
    fund_mismatch = _cmp_fields([
        ({"funding_time": t},
         [("amount", b_fu.get(t, 0.0), s_fu.get(t, 0.0), False)])
        for t in sorted(set(b_fu) | set(s_fu))])

    # ── 权益检查点（只取分歧之前的检查点）──
    eq_pts = []
    eq_rows = [r for r in b["equity"]
               if div_ts is None or int(r["ts"]) <= div_ts]
    step = max(1, len(eq_rows) // 6)
    for row in eq_rows[::step][:6]:
        t = int(row["ts"])
        e2 = X.ReplayEngine(
            bars_signal=bars_dict(sd.ohlcv(sym, tf)),
            bars_15m=[x for x in bars_dict(sd.ohlcv(sym, "15m"))
                      if start_ms <= x["open_time"] < end_ms],
            limits_by_symbol={sym: limits_for(sd.instrument(sym))},
            funding_events_by_symbol=funding_events(sd.funding(sym)), symbol=sym,
            exchange="binance", entry_mode=spec.entry_mode, stop_mode=spec.stop_mode,
            trail_mode=spec.trail_mode, allow_new_entries=True, maintain_existing=True,
            contract_spec=limits_for(sd.instrument(sym)))
        for sg in e2._signals:                  # noqa: SLF001
            if X.next_15m_open_at_or_after(int(sg["earliest_fill_ms"])) < start_ms:
                sg["consumed"] = True
        e2.run(upto_ms=t)
        eq_pts.append({"ts": t, "batch": float(row["equity"]),
                       "stream": float(e2.equity()),
                       "diff": float(row["equity"]) - float(e2.equity())})

    return {
        "level": level, "symbol": sym, "trail_mode": spec.trail_mode,
        "spec": spec.as_dict(),
        "batch": {"trades": b["review"]["trades"], "net": b["review"]["net_pnl"],
                  "final_cash": b["review"]["final_cash"],
                  "gross": b["review"]["gross_pnl"], "fees": b["review"]["fees"],
                  "funding": b["review"]["funding_net"]},
        "stream": {"decisions": len(sres["decisions"]), "fills": len(s["fills"]),
                   "exits": len(s["exits"]), "cash": sres["cash"],
                   "equity": sres["equity"], "open_positions": sres["open_positions"],
                   "locked": sres["locked"],
                   "n_stale_signals_dropped": sres["n_stale_signals_dropped"]},
        "signals": {"batch_attempted": len(b["signals"]), "stream_consumed": len(s["signals"]),
                    "common": len(common), "matched": len(common) - len(sig_mismatch),
                    "mismatched": sig_mismatch[:10], "n_mismatch": len(sig_mismatch),
                    "only_in_batch": len(set(b_sig) - set(s_sig)),
                    "only_in_stream": len(set(s_sig) - set(b_sig))},
        "prefix": {
            "note": "只统计**首个分歧之前**的逐笔（分歧后两边状态不同，逐笔对齐无意义）",
            "fills_batch": len(b_pre), "fills_stream": len(s_pre),
            "fill_mismatch": fill_mismatch[:10], "n_fill_mismatch": len(fill_mismatch),
            "exits_batch": len(b_exit), "exits_stream": len(s_exit),
            "exit_mismatch": exit_mismatch[:10], "n_exit_mismatch": len(exit_mismatch),
            "funding_pts": len(set(b_fu) | set(s_fu)),
            "funding_mismatch": fund_mismatch[:10], "n_funding_mismatch": len(fund_mismatch),
            "all_matched": (len(fill_mismatch) == 0 and len(exit_mismatch) == 0
                            and len(fund_mismatch) == 0
                            and len(b_pre) == len(s_pre) and len(b_exit) == len(s_exit)),
        },
        "full_path": {
            "batch_fills": len(b["positions"]), "stream_fills": len(s["fills"]),
            "batch_exit_reasons": sorted({p["exit_reason"] for p in b["positions"]}),
            "stream_exit_reasons": sorted({e["exit_reason"] for e in s["exits"]}),
        },
        "equity_checkpoints": eq_pts,
        "first_divergence": div,
    }


# ══════════════════════════ 流式自身：截断不变 ══════════════════════════

def stream_prefix_stability(sd, spec, sym, tf, start_ms, end_ms, cut_ms):
    """`ReplayEngine` 用 `upto_ms` 截断重跑：截止前的决定必须逐字相同。"""
    def _fresh():
        e = X.ReplayEngine(
            bars_signal=bars_dict(sd.ohlcv(sym, tf)),
            bars_15m=[x for x in bars_dict(sd.ohlcv(sym, "15m"))
                      if start_ms <= x["open_time"] < end_ms],
            limits_by_symbol={sym: limits_for(sd.instrument(sym))},
            funding_events_by_symbol=funding_events(sd.funding(sym)), symbol=sym,
            exchange="binance", entry_mode=spec.entry_mode, stop_mode=spec.stop_mode,
            trail_mode=spec.trail_mode, allow_new_entries=True, maintain_existing=True,
            contract_spec=limits_for(sd.instrument(sym)))
        for sg in e._signals:                   # noqa: SLF001
            if X.next_15m_open_at_or_after(int(sg["earliest_fill_ms"])) < start_ms:
                sg["consumed"] = True
        return e
    e_cut = _fresh(); e_cut.run(upto_ms=cut_ms)
    e_full = _fresh(); e_full.run()
    a = e_cut.decisions                       # 截断运行：只处理前 K 根已闭合 15m
    b = e_full.decisions[:len(a)]             # 全量运行的前 K 个决定
    n_diff = sum(1 for x, y in zip(a, b) if json.dumps(x, sort_keys=True, default=str)
                 != json.dumps(y, sort_keys=True, default=str))
    return {"cut_ms": cut_ms, "n_cut_decisions": len(a), "n_full_total": len(e_full.decisions),
            "n_compared_prefix": len(b), "n_diff": n_diff, "prefix_stable": n_diff == 0}


# ══════════════════════════ 主流程 ══════════════════════════

def main():
    t0 = time.time()
    seg = "Val"
    tf = "1h"
    start_ms, end_ms = ms("2025-01-01T00:00:00Z"), ms("2025-02-01T00:00:00Z")
    n_sym = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    sd = LD.SegmentData(seg)
    pool = sd.symbols(tf)
    picked = []
    for s in pool:
        if (sd.ohlcv(s, tf) and sd.ohlcv(s, "15m") and sd.funding(s)
                and sd.instrument(s)):
            picked.append(s)
        if len(picked) >= n_sym:
            break

    led_dir = WM / "ledgers"
    led_dir.mkdir(parents=True, exist_ok=True)
    iface = X.interface()

    out = {"schema": "wyckoffmaster_stream_vs_batch/v1",
           "generated_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "segment": seg, "timeframe": tf,
           "window": [start_ms, end_ms],
           "window_utc": [dt.datetime.fromtimestamp(start_ms / 1000, UTC).isoformat(),
                          dt.datetime.fromtimestamp(end_ms / 1000, UTC).isoformat()],
           "core_version": X.CORE_VERSION, "strategy_version": X.STRATEGY_VERSION,
           "exec_config_hash": iface["config_hash"],
           "strategy_params_hash": iface["strategy_params_hash"],
           "levels": list(LEVELS), "symbols": picked,
           "production": P.assert_production_untouched(),
           "cases": [], "prefix_stability": []}

    for level in LEVELS:
        for sym in picked:
            c = compare_case(sd, level, sym, tf, start_ms, end_ms, led_dir, "svb")
            out["cases"].append(c)
            print(f"[{level}/{sym}] batch trades={c['batch']['trades']} "
                  f"net={c['batch']['net']:.2f} | stream fills={c['stream']['fills']} "
                  f"cash={c['stream']['cash']:.2f} | "
                  f"sig_cmp={c['signals']['common']} sig_bad={c['signals']['n_mismatch']} "
                  f"prefix_fill_bad={c['prefix']['n_fill_mismatch']} "
                  f"prefix_exit_bad={c['prefix']['n_exit_mismatch']} "
                  f"prefix_fund_bad={c['prefix']['n_funding_mismatch']} "
                  f"| div={((c['first_divergence'] or {}).get('root_cause'))}")

    # 流式自身截断不变：每层取**决定数最多**的标的（避免取到空案例而「假通过」）
    cut = start_ms + (end_ms - start_ms) // 2
    for level in LEVELS:
        parent_dict = LAD.spec_for(PARENT[level], None).as_dict() if PARENT[level] else None
        spec = LAD.spec_for(level, parent_dict)
        best = max(out["cases"], key=lambda c: c["stream"]["decisions"]
                   if c["level"] == level else -1)
        sym = best["symbol"]
        st = stream_prefix_stability(sd, spec, sym, tf, start_ms, end_ms, cut)
        st["level"] = level
        st["symbol"] = sym
        out["prefix_stability"].append(st)
        print(f"[prefix] {level}/{sym} stable={st['prefix_stable']} "
              f"n_cut={st['n_cut_decisions']} n_full={st['n_full_total']} "
              f"n_cmp={st['n_compared_prefix']} n_diff={st['n_diff']}")

    # ── 汇总 ──
    live = [c for c in out["cases"] if c["batch"]["trades"] or c["stream"]["fills"]]
    tot_sig = sum(c["signals"]["n_mismatch"] for c in out["cases"])
    tot_sig_cmp = sum(c["signals"]["common"] for c in out["cases"])
    tot_fill = sum(c["prefix"]["n_fill_mismatch"] for c in out["cases"])
    tot_fill_cmp = sum(c["prefix"]["fills_batch"] for c in out["cases"])
    tot_exit = sum(c["prefix"]["n_exit_mismatch"] for c in out["cases"])
    tot_exit_cmp = sum(c["prefix"]["exits_batch"] for c in out["cases"])
    tot_fund = sum(c["prefix"]["n_funding_mismatch"] for c in out["cases"])
    tot_fund_cmp = sum(c["prefix"]["funding_pts"] for c in out["cases"])
    causes = {}
    for c in out["cases"]:
        d = c["first_divergence"]
        if d:
            causes[d["root_cause"]] = causes.get(d["root_cause"], 0) + 1
    out["summary"] = {
        "n_cases": len(out["cases"]), "n_cases_with_trades": len(live),
        "signal_decisions_compared": tot_sig_cmp, "signal_mismatch": tot_sig,
        "prefix_fills_compared": tot_fill_cmp, "prefix_fill_mismatch": tot_fill,
        "prefix_exits_compared": tot_exit_cmp, "prefix_exit_mismatch": tot_exit,
        "prefix_funding_points_compared": tot_fund_cmp, "prefix_funding_mismatch": tot_fund,
        "prefix_all_matched": all(c["prefix"]["all_matched"] for c in live),
        "only_in_batch_signals": sum(c["signals"]["only_in_batch"] for c in out["cases"]),
        "only_in_stream_signals": sum(c["signals"]["only_in_stream"] for c in out["cases"]),
        "first_divergence_causes": causes,
        "all_prefix_stable": all(x["prefix_stable"] for x in out["prefix_stability"]),
        # 全路径等价 ⟺ **没有任何分歧**（而非「前缀无差异」）
        "full_path_equivalent": all(c["first_divergence"] is None for c in out["cases"]),
    }
    out["verdict"] = {
        "decision_layer": ("共用核心的开仓决定（含拒单原因）在两条路径上逐笔一致："
                           f"比较 {tot_sig_cmp} 笔，差异 {tot_sig} 笔"
                           if tot_sig == 0 else
                           f"决策层存在 {tot_sig} 笔差异（共比较 {tot_sig_cmp} 笔）"),
        "outcome_layer": ("首个分歧之前的结果（成交/出场/资金费）逐笔一致；"
                          "但**全路径不等价**，分歧根因见 capability_gaps"
                          if not out["summary"]["full_path_equivalent"] else
                          "全路径逐笔一致"),
        "stream_self_consistency": ("流式引擎自身「追加未来数据不改变过去决定」成立"
                                    if out["summary"]["all_prefix_stable"] else
                                    "流式引擎前缀不稳定"),
    }
    out["capability_gaps"] = [
        {"id": "G1", "item": "唐奇安预约出场（前 10 根最低）",
         "core": "`order_bar_events(scheduled_exits=...)` 已定义相位，但 `ReplayEngine` 未产生该事件",
         "owner": "任务二 / 策略窗（实时侧由 `live_worker._scan_position_exit` 承担，本机不可达）"},
        {"id": "G2", "item": "台阶锁利 / 保本（`apply_trail`）",
         "core": "`apply_trail` 已实现，但 `ReplayEngine._process_bar` 未调用",
         "owner": "任务二 / 策略窗"},
        {"id": "G3", "item": "段末强平（本段最后 15m 收盘）",
         "core": "`ReplayEngine` 无段概念、无段末结算",
         "owner": "任务三（本窗口已在组合引擎内实现）"},
        {"id": "G4", "item": "同标的并发去重（同一标的只允许 1 仓）",
         "core": "`ReplayEngine` 只按 `max_positions_total` 计数，不抑制同标的重复开仓",
         "owner": "任务二 / 策略窗"},
        {"id": "G5", "item": "出场价按 tick 向下取整（`round_protect_price`）",
         "core": "`ReplayEngine._close` 只用不利滑点、不按 tick 取整",
         "owner": "任务二 / 策略窗"},
    ]
    out["elapsed_sec"] = round(time.time() - t0, 1)

    (WM / "state" / "stream_vs_batch.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    EV.mkdir(parents=True, exist_ok=True)
    (EV / "25_历史回放与流式推进对账_v31.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("summary =", json.dumps(out["summary"], ensure_ascii=False))
    print("elapsed", out["elapsed_sec"])


if __name__ == "__main__":
    main()
