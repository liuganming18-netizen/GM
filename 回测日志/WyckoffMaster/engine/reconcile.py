# -*- coding: utf-8 -*-
"""账本逐笔对账：毛利/费用/滑点/资金费/净利四者互算 + 现金流 + 汇总一致性。

对账口径（四者分列，任一可由其余独立复算）：
  gross = (exit_price − entry_price) × qty
  fee   = entry_price×qty×fee_side + exit_price×qty×fee_side
  fund  = Σ 资金费行 amount
  net   = gross − entry_fee − exit_fee + fund
  cash_final = initial − Σ(entry_price×qty + entry_fee) + Σ(exit_price×qty − exit_fee) + Σ fund
"""
from __future__ import annotations

import sqlite3

FEE = 0.0005


def _rel(a, b, tol=1e-6):
    d = abs(a - b)
    return d <= tol * max(1.0, abs(a), abs(b))


def reconcile_ledger(path, fee_side=FEE):
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    positions = con.execute("SELECT * FROM positions ORDER BY position_id").fetchall()
    fund_rows = con.execute("SELECT * FROM funding").fetchall()
    review = con.execute("SELECT * FROM review LIMIT 1").fetchone()
    con.close()
    if review is None:
        return {"ok": False, "reason": "no_review"}
    fund_by_pos = {}
    fund_blocked = 0
    for r in fund_rows:
        if r["amount"] is None:
            fund_blocked += 1
            continue
        fund_by_pos[r["position_id"]] = fund_by_pos.get(r["position_id"], 0.0) + float(r["amount"])

    bad = []
    sum_net = sum_gross = sum_fee = sum_fund = 0.0
    cash = float(review["initial_equity"])
    for p in positions:
        qty = float(p["quantity"]); ep = float(p["entry_price"]); xp = float(p["exit_price"])
        ef = float(p["entry_fee"]); xf = float(p["exit_fee"])
        g = (xp - ep) * qty
        fee = ep * qty * fee_side + xp * qty * fee_side
        fu = fund_by_pos.get(p["position_id"], 0.0)
        net = g - ef - xf + fu
        for name, got, want in (("gross", float(p["gross_pnl"]), g),
                                ("fee", ef + xf, fee),
                                ("funding", float(p["funding"]), fu),
                                ("net", float(p["net_pnl"]), net)):
            if not _rel(got, want):
                bad.append({"position_id": p["position_id"], "field": name,
                            "ledger": got, "recomputed": want})
        sum_net += float(p["net_pnl"]); sum_gross += float(p["gross_pnl"])
        sum_fee += ef + xf; sum_fund += fu
        cash += -ep * qty - ef + xp * qty - xf + fu
    # 汇总一致性
    if not _rel(sum_net, float(review["net_pnl"])):
        bad.append({"field": "review.net_pnl", "ledger": float(review["net_pnl"]),
                    "recomputed": sum_net})
    if not _rel(sum_gross, float(review["gross_pnl"])):
        bad.append({"field": "review.gross_pnl", "ledger": float(review["gross_pnl"]),
                    "recomputed": sum_gross})
    if not _rel(sum_fee, float(review["fees"])):
        bad.append({"field": "review.fees", "ledger": float(review["fees"]),
                    "recomputed": sum_fee})
    if not _rel(sum_fund, float(review["funding_net"])):
        bad.append({"field": "review.funding_net", "ledger": float(review["funding_net"]),
                    "recomputed": sum_fund})
    if not _rel(cash, float(review["final_cash"])):
        bad.append({"field": "final_cash", "ledger": float(review["final_cash"]),
                    "recomputed": cash})
    return {"ok": not bad, "n_positions": len(positions), "n_funding_rows": len(fund_rows),
            "funding_blocked_rows": fund_blocked,
            "sum_net": sum_net, "sum_gross": sum_gross, "sum_fee": sum_fee, "sum_fund": sum_fund,
            "final_cash_recomputed": cash, "final_cash_ledger": float(review["final_cash"]),
            "mismatches": bad[:20], "n_mismatch": len(bad)}
