#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 段末强平 · 局部适配与差异核验（**不改生产引擎**）。

背景（实测差异，不是猜测）
--------------------------
冻结执行核心 `wx_execution.run_arm` 的期末清算用的是
    px = float(b[1])          # 最后一根 15m 的**开盘**
    close_pos(p, t, px, "terminal_liquidation")
    price_rule = "last_executable_open"
而 v3.1 开工令要求：
    · 段末未平仓用**本段最后 15m 收盘**强平；
    · 计不利滑点、费用及应结资金费；
    · **不用下一段开盘**。

⇒ 二者不一致。按施工单「与已有引擎顺序不一致时写局部适配和差异核验，不改生产引擎」，
本模块在**独立账本副本**上做定点替换，并输出逐笔差异报告。

为什么安全
----------
期末清算发生在**整段最后一个时点**，其后没有任何新交易/新资金费，
因此只影响：该持仓的 exit_price/exit_fee/slip/gross/net、期末现金与期末权益。
不影响任何更早的成交、资金费与权益路径。

用法
----
    from segment_end_liquidation import apply_v31_segment_end
    rep = apply_v31_segment_end(src_ledger, dst_ledger, bars, ex_module, segment_end_ms)
其中 `bars(ex, sym) -> (ts, open, high, low, close, quote)` 取该币段内最后一根 15m bar。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

REASON_V31 = "segment_end_force_close_last_15m_close"


def _last_bar_in_segment(bars, ex, sym, segment_end_ms: int):
    """段内最后一根 15m bar（open_time < segment_end_ms）。"""
    b = bars(ex, sym)
    if b is None:
        return None
    ts = int(b[0])
    return b if ts < int(segment_end_ms) else None


def apply_v31_segment_end(*, src_ledger: Path, dst_ledger: Path, bars,
                          ex_module, segment_end_ms: int,
                          price_step_of=None) -> dict:
    """把引擎的 `terminal_liquidation`（按最后一根开盘）改写为 v3.1 段末强平（按最后 15m 收盘）。

    返回差异报告；无 `terminal_liquidation` 持仓时为空操作。
    """
    src_ledger, dst_ledger = Path(src_ledger), Path(dst_ledger)
    shutil.copy2(src_ledger, dst_ledger)
    con = sqlite3.connect(str(dst_ledger))
    con.row_factory = sqlite3.Row
    slip = 0.0005
    fee_side = 0.0005

    rows = con.execute(
        "SELECT * FROM research_position WHERE exit_reason='terminal_liquidation'"
    ).fetchall()
    diffs, cash_delta = [], 0.0
    for r in rows:
        ex, sym = r["exchange"], r["symbol"]
        b = _last_bar_in_segment(bars, ex, sym, segment_end_ms)
        if b is None:
            diffs.append({"position_id": r["position_id"], "verdict": "NO_BAR_SKIP"})
            continue
        ts_last, close_px = int(b[0]), float(b[4])
        step = float(price_step_of(ex, sym) or 0.0) if price_step_of else 0.0
        px_v31 = ex_module.round_sell_price(close_px * (1.0 - slip), step)
        qty = float(r["quantity"])
        entry = float(r["entry_price"])
        entry_fee = float(r["entry_fee"] or 0.0)
        funding = float(r["funding"] or 0.0)
        notional = px_v31 * qty
        exit_fee = notional * fee_side
        slip_cost = qty * (close_px - px_v31)
        gross = (px_v31 - entry) * qty
        net = gross - entry_fee - exit_fee + funding

        old_exit = float(r["exit_price"] or 0.0)
        old_fee = float(r["exit_fee"] or 0.0)
        old_gross = float(r["gross_pnl"] or 0.0)
        cash_delta += (gross - exit_fee) - (old_gross - old_fee)

        con.execute(
            "UPDATE research_position SET exit_price=?, exit_reason=?, exit_fee=?, "
            "gross_pnl=?, net_pnl=? WHERE run_id=? AND position_id=?",
            (px_v31, REASON_V31, exit_fee, gross, net, r["run_id"], r["position_id"]))
        con.execute(
            "UPDATE research_order SET fill_price=?, fee=?, slippage=?, notional=? "
            "WHERE intent_id=? AND status='fill' AND fill_time=?",
            (px_v31, exit_fee, slip_cost, notional, r["position_id"], ts_last))
        diffs.append({
            "position_id": r["position_id"], "exchange": ex, "symbol": sym,
            "quantity": qty,
            "engine_rule": "last_executable_open",
            "engine_exit_price": old_exit, "engine_exit_fee": old_fee,
            "engine_gross_pnl": old_gross,
            "v31_rule": "last_15m_close",
            "v31_bar_ts": ts_last, "v31_close": close_px, "v31_exit_price": px_v31,
            "v31_exit_fee": exit_fee, "v31_slip_cost": slip_cost, "v31_gross_pnl": gross,
            "price_delta": px_v31 - old_exit, "verdict": "REWRITTEN",
        })

    # 期末权益/现金同步
    if rows and abs(cash_delta) > 0:
        last_eq = con.execute(
            "SELECT rowid,* FROM research_equity ORDER BY ts DESC LIMIT 1").fetchone()
        if last_eq is not None:
            con.execute("UPDATE research_equity SET cash=cash+?, equity=equity+? WHERE rowid=?",
                        (cash_delta, cash_delta, last_eq["rowid"]))
        for col in ("final_cash", "final_equity"):
            try:
                con.execute(f"UPDATE research_review SET {col}={col}+? "
                            f"WHERE {col} IS NOT NULL", (cash_delta,))
            except sqlite3.OperationalError:
                pass
    con.commit()
    con.close()
    return {"schema": "wyckoffmaster_segment_end_v31/v1",
            "src_ledger": str(src_ledger), "dst_ledger": str(dst_ledger),
            "n_terminal_liquidations": len(rows), "cash_delta": cash_delta,
            "differs": [d for d in diffs if d.get("verdict") == "REWRITTEN"],
            "skipped": [d for d in diffs if d.get("verdict") != "REWRITTEN"],
            "note": ("期末清算是整段最后时点，其后无新交易/新资金费 ⇒ 改动不外溢到更早权益路径；"
                     "生产引擎未修改，差异在此单独留证。")}


def make_store_bars(store):
    """用 RA InputStore 造 bars(ex, sym) → 段内最后一根 15m bar 元组 (ts,o,h,l,c,q)。

    注意：`ColSeq.cols` 是**全量**列数组、`ts` 才是切片；必须用 `v[i]`（内部按 `_i0+i` 取值），
    不能用 `v.cols[...][len(v)-1]`，否则会取到窗口之外的错行。
    """
    def _bars(ex, sym):
        v = store.s15((ex, sym))
        if v is None or len(v) == 0:
            return None
        return tuple(v[len(v) - 1])
    return _bars
