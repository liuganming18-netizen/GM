#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 准备件自核验：把「执行费用 / 三段独立账户 / 段末强平 / M2 拒单分母及 M2b 触发」
四件事变成**可运行的检查**，并产出证据 JSON。

不读 Holdout、不读真实行情、不启动回测；全部为配置一致性与合成单元测试。
退出码：0 = 全过；3 = 有未过项。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WM = HERE.parent
sys.path.insert(0, str(WM / "adapters"))
UTC = dt.timezone.utc

V31_SEG = {
    "IS": ("2022-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
    "Val": ("2025-01-01T00:00:00Z", "2026-04-15T00:00:00Z"),
    "Holdout": ("2026-04-15T00:00:00Z", "2026-10-15T00:00:00Z"),
}


def _ms(iso):
    return int(dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=UTC).timestamp() * 1000)


def check(name, ok, note=""):
    return {"item": name, "verdict": "OK" if ok else "FAIL", "note": note}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default=str(WM / "state" / "v31_prep_verify.json"))
    a = ap.parse_args()
    checks: list[dict] = []

    seg = json.loads((WM / "config" / "frozen_segments.json").read_text(encoding="utf-8"))
    lad = json.loads((WM / "config" / "ladder.json").read_text(encoding="utf-8"))
    m2 = json.loads((WM / "config" / "m2_rejection.json").read_text(encoding="utf-8"))

    # ── A. 三段独立账户 + v3.1 区间 ─────────────────────────────────────────
    by = {s["name"]: s for s in seg["segments"]}
    for n, (s0, e0) in V31_SEG.items():
        s = by.get(n, {})
        checks.append(check(f"A.segment.{n}.interval",
                            s.get("start_utc") == s0 and s.get("end_excl_utc") == e0,
                            f"{s.get('start_utc')} -> {s.get('end_excl_utc')}"))
        checks.append(check(f"A.segment.{n}.independent_account",
                            s.get("equity_start") == 5000.0
                            and s.get("positions_at_start") == 0
                            and s.get("pending_orders_at_start") == 0
                            and s.get("carry_positions_across") is False,
                            "5000U / 零仓 / 零挂单 / 不跨段继承"))
    checks.append(check("A.reset_fields",
                        set(seg["segments"][0]["reset_on_segment_boundary"])
                        == {"cash", "pending_orders", "positions", "execution_state", "equity_peak"},
                        str(seg["segments"][0]["reset_on_segment_boundary"])))
    checks.append(check("A.anchor_carry_policy",
                        "不得产生段前开仓" in json.dumps(seg["segment_boundary"],
                                                         ensure_ascii=False),
                        "仅预热形成的有效锚点可沿用且不得段前开仓"))
    checks.append(check("A.ladder.segments_independent",
                        lad["single_account"].get("segments_independent") is True))

    # ── B. 段末强平 ─────────────────────────────────────────────────────────
    sel = seg["segment_end_liquidation"]
    checks.append(check("B.spec.last_15m_close",
                        "最后 15m 收盘" in sel["price"] and "下一段开盘" in sel["forbidden"],
                        f"{sel['price']} / 禁止 {sel['forbidden']}"))
    checks.append(check("B.spec.costs",
                        set(sel["costs"]) >= {"卖出不利滑点 0.05%", "手续费 0.05%", "应结资金费"},
                        str(sel["costs"])))
    checks.append(check("B.adapter_present",
                        (WM / "adapters" / "segment_end_liquidation.py").exists()))

    # 合成单元测试：引擎「最后一根开盘」→ v3.1「最后 15m 收盘」
    import segment_end_liquidation as SEL
    class _X:
        @staticmethod
        def round_sell_price(px, step):
            if not step:
                return px
            return float(int(px / step) * step)
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "raw.sqlite3"
    con = sqlite3.connect(str(src))
    con.executescript("""
      CREATE TABLE research_position(run_id TEXT,position_id TEXT,exchange TEXT,symbol TEXT,
        entry_price REAL,quantity REAL,entry_fee REAL,funding REAL,exit_price REAL,
        exit_reason TEXT,exit_fee REAL,gross_pnl REAL,net_pnl REAL);
      CREATE TABLE research_order(intent_id TEXT,status TEXT,fill_time INTEGER,
        fill_price REAL,fee REAL,slippage REAL,notional REAL);
      CREATE TABLE research_equity(ts INTEGER,cash REAL,equity REAL);
      CREATE TABLE research_review(final_cash REAL,final_equity REAL);
      INSERT INTO research_position VALUES('r','p1','binance','BTCUSDT',100.0,1.0,0.05,0.01,
        101.0,'terminal_liquidation',0.0505,1.0,0.9595);
      INSERT INTO research_order VALUES('p1','fill',1000,101.0,0.0505,0.0,101.0);
      INSERT INTO research_equity VALUES(1000,5000.0,5000.0);
      INSERT INTO research_review VALUES(5000.0,5000.0);
    """)
    con.commit(); con.close()
    # 段内最后一根 15m：open=101, close=103（引擎用 open，v3.1 用 close）
    rep = SEL.apply_v31_segment_end(
        src_ledger=src, dst_ledger=tmp / "fin.sqlite3",
        bars=lambda ex, sym: (1000, 101.0, 104.0, 99.0, 103.0, 1e6),
        ex_module=_X, segment_end_ms=1000 + 900_000)
    d = rep["differs"][0] if rep["differs"] else {}
    checks.append(check("B.unit.engine_used_open", d.get("engine_exit_price") == 101.0,
                        f"engine_exit_price={d.get('engine_exit_price')}"))
    checks.append(check("B.unit.v31_used_close",
                        abs(d.get("v31_exit_price", 0) - 103.0 * (1 - 0.0005)) < 1e-6,
                        f"v31_exit_price={d.get('v31_exit_price')} (close 103 × (1-0.05%))"))
    checks.append(check("B.unit.reason_code", d.get("verdict") == "REWRITTEN"
                        and rep["n_terminal_liquidations"] == 1))

    # ── C. 执行费用 / 资金费模型 ─────────────────────────────────────────────
    ex = lad["execution_layer_shared"]
    checks.append(check("C.fee_slippage",
                        ex["fee_each_side"] == 0.0005 and ex["slippage_each_side"] == 0.0005,
                        f"fee={ex['fee_each_side']} slip={ex['slippage_each_side']}"))
    checks.append(check("C.funding_required_real",
                        ex["funding_model_required"] == "historical_events"
                        and set(ex["funding_model_forbidden"])
                        >= {"norm4h", "ext1h", "zero_fill", "ordinary_close_as_mark"}
                        and ex["funding_negative_income_kept"] is True))
    checks.append(check("C.risk_budgets",
                        ex["risk"] == {"per_trade_stop_budget_frac_of_equity": 0.005,
                                       "per_symbol_notional_frac": 0.10,
                                       "gross_long_notional_frac": 0.80,
                                       "cash_reserve_frac": 0.20,
                                       "portfolio_initial_stop_risk_frac": 0.03}))
    checks.append(check("C.stop_every_15m",
                        ex["stop_check_frequency"] == "every_15m_bar"
                        and "15m" in ex["expiry_check"]))

    # ── D. M2 拒单分母 + M2b 触发 ───────────────────────────────────────────
    den = m2["m2_overlimit_rejection"]["denominator"]
    checks.append(check("D.m2.denominator",
                        "到达结构止损超限判断" in den["definition"]
                        and den["zero_denominator"].startswith("数量为 0 记 N/A")))
    checks.append(check("D.m2.group_by_types",
                        m2["m2_overlimit_rejection"]["group_by_buy_type"]
                        == ["spring", "breakout", "retest"]))
    checks.append(check("D.m2.not_mixed",
                        set(m2["m2_overlimit_rejection"]["must_not_mix_with"])
                        == {"entry_below_struct_low", "precision_reject", "insufficient_funds"}))
    trig = m2["m2b_trigger"]
    checks.append(check("D.m2b.threshold",
                        "> 0.30" in trig["condition"] and trig["scope_of_rate"].startswith("IS+Val")))
    checks.append(check("D.m2b.formula",
                        m2["m2b_distance"]["formula"]
                        == "D = max(P_entry - (L_struct - 0.8*ATR), 1.5*ATR)"))
    checks.append(check("D.m2b.constraints_continue",
                        len(m2["m2b_distance"]["constraints_continue"]) == 5))
    checks.append(check("D.entry_below_struct_low",
                        m2["entry_below_struct_low"]["reject_reason"] == "entry_below_struct_low"
                        and set(m2["entry_below_struct_low"]["applies_to"]) == {"M2", "M2b"}))
    checks.append(check("D.ladder.scope_this_round",
                        lad["scope_this_round"]["historical_ladder"]
                        == ["M0", "M1", "M2", "M2b_conditional", "M3_BE", "M3_Step", "M4"]
                        and lad["scope_this_round"]["not_this_round"] == ["M5", "M6"]))
    checks.append(check("D.forward.m5m6",
                        "30" in lad["forward_validation"]["M5"]["rule"]
                        and "Z-score" in lad["forward_validation"]["M6"]["rule"]
                        and "90" in lad["forward_validation"]["M5"]["activation"]))

    # ── E. Holdout 红线（功能测试）─────────────────────────────────────────
    import holdout_guard as HG
    def _refused(fn, *args):
        try:
            fn(*args)
            return False
        except HG.HoldoutViolation:
            return True
    checks.append(check("E.guard.blocks_holdout_window",
                        _refused(HG.assert_window_allowed, _ms(V31_SEG["Holdout"][0]),
                                 _ms(V31_SEG["Holdout"][1]), "kline")))
    checks.append(check("E.guard.blocks_crossing_window",
                        _refused(HG.assert_window_allowed, _ms("2026-03-01T00:00:00Z"),
                                 _ms("2026-05-01T00:00:00Z"), "kline")))
    checks.append(check("E.guard.allows_is_val",
                        not _refused(HG.assert_window_allowed, _ms(V31_SEG["IS"][0]),
                                     _ms(V31_SEG["Val"][1]), "kline")))
    checks.append(check("E.guard.blocks_holdout_phase_without_lock",
                        _refused(HG.assert_phase_allowed, "holdout")))
    checks.append(check("E.guard.cutoff_not_reached",
                        _refused(HG.assert_holdout_cutoff_reached),
                        "Holdout 截止(北京时间 2026-10-15 08:00)未到"))
    iso = HG.assert_research_input_isolation(
        Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster"))
    checks.append(check("E.research_input_physical_isolation", iso.get("ok") is True,
                        json.dumps(iso, ensure_ascii=False)[:160]))

    # ── F. 门禁 v3.1 版本绑定要求 ──────────────────────────────────────────
    src = (HERE / "verify_gates.py").read_text(encoding="utf-8")
    checks.append(check("F.gate_requires_v31", "REQUIRED_VERSION = \"v3.1\"" in src))
    checks.append(check("F.gate_checks_backup_evidence",
                        "_check_backup_evidence" in src and "require_backup" in src))

    fails = [c for c in checks if c["verdict"] == "FAIL"]
    doc = {"schema": "wyckoffmaster_v31_prep_verify/v1",
           "generated_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "n_checks": len(checks), "n_fail": len(fails),
           "verdict": "PASS" if not fails else "FAIL", "checks": checks}
    out = Path(a.json_out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    for c in checks:
        mark = "OK  " if c["verdict"] == "OK" else "FAIL"
        print(f"[{mark}] {c['item']}  {c.get('note','')}")
    print(f"\n{len(checks)} 项，未过 {len(fails)} 项 → {doc['verdict']}")
    print("wrote", out)
    return 0 if not fails else 3


if __name__ == "__main__":
    raise SystemExit(main())
