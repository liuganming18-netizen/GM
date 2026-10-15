#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段A·共享预算/单账户契约与**隔离验证**（只读；不合并、不重置权益）。

合同 A：**不得**擅自把四份旧账户合成一个新 5000U 账户，**不得**悄悄重置权益；
先实现共享预算/单一账户契约及隔离验证；正式新 epoch 必须记录为受控启用时刻，
通过 Codex 运行验收后才生效，**绝不补开历史信号**。
"""
import json
import sqlite3
import sys
from pathlib import Path

PAPER_DIR = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
                 "最新唐奇安威科夫接通与收尾_2026-10-11/量价信号接入与模拟历史并行43/纸面模拟43")
BACKEND = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现")
OUT = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_闭环整改_2026-10-15/"
           "shared_account_contract_and_isolation.json")
sys.path.insert(0, str(BACKEND))
import paper_engine as PE  # noqa: E402

ARMS = ("3a", "3b", "3c", "3d")


def ro(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main():
    legacy = {}
    for arm in ARMS:
        p = PAPER_DIR / f"paper_{arm}.sqlite3"
        if not p.exists():
            continue
        con = ro(p)
        try:
            acc = [dict(r) for r in con.execute("SELECT * FROM paper_account")]
            n_open = con.execute("SELECT COUNT(*) FROM paper_position WHERE status='open' "
                                 "AND COALESCE(voided,0)=0").fetchone()[0]
            n_pos = con.execute("SELECT COUNT(*) FROM paper_position").fetchone()[0]
            n_fill = con.execute("SELECT COUNT(*) FROM paper_fill").fetchone()[0]
            eq = con.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM paper_equity").fetchone()
            cash = [dict(r) for r in con.execute("SELECT * FROM paper_account")]
            legacy[arm] = {
                "db": str(p), "db_sha256_size": p.stat().st_size,
                "account_rows": acc, "cash_rows": cash,
                "n_positions_total": n_pos, "n_open": n_open, "n_fills": n_fill,
                "equity_ts_min": eq["a"], "equity_ts_max": eq["b"], "equity_rows": eq["n"],
            }
        finally:
            con.close()

    params = PE.approved_params()
    contract = {
        "spec_version": params.get("view_spec_version"),
        "view_sha256": params.get("view_sha256"),
        "model": "one_shared_virtual_account_5000USDT",
        "initial_equity_USDT": params["initial_equity_USDT"],
        "max_positions_total": params["max_positions"],
        "leverage": params["leverage"],
        "direction": "long_only" if not params["short_enabled"] else "long_short",
        "short_enabled": params["short_enabled"],
        "risk_per_trade_equity_fraction": params["risk_per_trade_equity_fraction"],
        "single_name_notional_equity_cap": params["single_name_notional_equity_cap"],
        "gross_long_notional_equity_cap": params["gross_long_notional_equity_cap"],
        "cash_or_free_margin_reserve_fraction": params["cash_or_free_margin_reserve_fraction"],
        "portfolio_initial_stop_risk_cap": params["portfolio_initial_stop_risk_cap"],
        "entry_participation_previous_15m_quote_cap": params[
            "entry_participation_previous_15m_quote_cap"],
        "fee_each_side": params["fee_each_side"],
        "slippage_each_side": params["slippage_each_side"],
        "entry_delay_minutes": params["signal_execution_delay_minutes"],
        "max_holding_hours": params["max_holding_hours"],
    }

    out = {
        "mode": "read_only_isolation_validation",
        "legacy_accounts": legacy,
        "legacy_account_ids": sorted({a for v in legacy.values() for r in v["account_rows"]
                                      for a in [r.get("account_id")]}),
        "n_legacy_dbs": len(legacy),
        "legacy_merged": False,
        "equity_reset": False,
        "prospective_shared_account": {
            "status": "NOT_CREATED_NOT_ACTIVATED",
            "planned_ledger": str(BACKEND / "live" / "shared5000" / "paper_shared.sqlite3"),
            "requires": "Codex 运行验收 + 受控启用时刻（epoch）后才可创建/启用",
            "never_backfill_history": True,
            "does_not_touch_existing_ledgers": True,
        },
        "frozen_shared_contract": contract,
        "contract_matches_frozen_view": (
            contract["initial_equity_USDT"] == 5000
            and contract["max_positions_total"] == 10
            and contract["direction"] == "long_only"
            and contract["risk_per_trade_equity_fraction"] == 0.005
            and contract["single_name_notional_equity_cap"] == 0.1
            and contract["gross_long_notional_equity_cap"] == 0.8
            and contract["cash_or_free_margin_reserve_fraction"] == 0.2
            and contract["portfolio_initial_stop_risk_cap"] == 0.03
            and contract["entry_participation_previous_15m_quote_cap"] == 0.01
            and contract["fee_each_side"] == 0.0005
            and contract["slippage_each_side"] == 0.0005
            and contract["entry_delay_minutes"] == 15
        ),
        "notes": [
            "四份旧账户保持**各自独立**（独立 DB 文件 + 独立 account_id）；本阶段**未**合并。",
            "旧账本金额守恒证据见 old_ledger_violation_audit.json（原样保留，未改时间/价格/费用）。",
            "共享 5000U 单账户为**契约**，尚未创建、未启用；不得补开历史信号。",
        ],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("n_legacy_dbs", "legacy_account_ids", "legacy_merged",
                                          "equity_reset", "contract_matches_frozen_view")},
                     ensure_ascii=False, indent=1))
    for arm, v in legacy.items():
        print(arm, "open=", v["n_open"], "pos_total=", v["n_positions_total"],
              "fills=", v["n_fills"], "equity_rows=", v["equity_rows"])


if __name__ == "__main__":
    main()
