# -*- coding: utf-8 -*-
"""资金费审计 · **生产代码反例复测**（常规回归测试；离线、零网络、零库写入）。

来源与提升理由
--------------
原一次性脚本：`审计报告对账_2026-10-13/C1_生产代码反例复测.py`（21/21 PASS）。
提升依据（`审计报告对账_2026-10-13/C0_审计报告对账与处置建议.md` §8「真正待办清单」P2：
「把 `C1_生产代码反例复测.py` 纳入常规测试集（建议落到 `后端实现/tests/`，并在 CI/门禁里跑）」
+ §9 第 2 条「自验 PASS ≠ 生产已修」）。

为什么必须打在生产函数上
------------------------
原 `Gemini协作_费用闭环_2026-10-12/test_7_counterexamples.py` 第 3 行
`from reconciliation_fix import (...)` 测的是**独立模块**，不是生产代码
`strategies/wx_execution.py`；那份「10/10 PASS」**不构成**对生产代码的验收证据。
本测试**测谁就 import 谁**：直接打到 `wx_execution.audit_funding_usage`。

两处适配（口径差异，非放水）
----------------------------
1. **凭证（voucher）**：生产函数在无 `con`/`run_id` 时会追加 `voucher_unavailable:*`
   使 `ok` 恒 False；该条属账本上下文脚手架，与本批反例要考的「逐行审查逻辑」无关，
   故断言落在 `forward.ok` / `forward.fail[].reasons` / `reverse.*` 上并显式剔除该前缀。
2. **相邻不利费率来源**：独立模块放在 `sources[].adverse_rates`；生产函数用独立形参
   `rate_series_by_sym={(exchange, symbol): [(ts, rate), ...]}`（升序真实费率序列）。

覆盖边界（如实声明）
--------------------
反例 6（多币同刻缺标价）与反例 7（未来周期元数据）在生产代码里是 `run_arm` 内部**嵌套函数**，
无法在不重构的前提下单独调用 ⇒ 只做**静态断言**（源码存在性），**明确不冒充行为证据**。

运行：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_funding_audit_counterexamples -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent           # 后端实现/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies import wx_execution as X                # noqa: E402

VOUCHER_PREFIX = "voucher_unavailable"


def problems_without_voucher(res):
    return [p for p in (res.get("problems") or []) if not p.startswith(VOUCHER_PREFIX)]


def reasons_of(res):
    fails = (res.get("forward") or {}).get("fail") or []
    return list(fails[0].get("reasons") or []) if fails else []


# ── 夹具（与 Gemini 版逐字一致，仅传参口径改为生产）─────────────────────────
POS = {"position_id": "pos_btc_01", "exchange": "binance", "symbol": "BTCUSDT",
       "contract_generation": "GEN1", "entry_time": 1000, "exit_time": 5000,
       "quantity": 10.0, "status": "open"}
ORD = {"order_id": "ord_buy_01", "intent_id": "pos_btc_01", "position_id": "pos_btc_01",
       "exchange": "binance", "symbol": "BTCUSDT", "contract_generation": "GEN1",
       "side": "BUY", "status": "fill", "quantity": 10.0, "effective_fill_time": 950}
MAT = {"sources": [{"exchange": "binance", "symbol": "BTCUSDT",
                    "events": [[2000, 0.0001]], "marks": {"2000": 20000.0}}]}
# 生产口径：相邻不利费率走独立形参，值为升序 [(ts, rate)]
SERIES = {("binance", "BTCUSDT"): [(1000, 0.0002), (2000, 0.0005), (3000, 0.0003)]}

GOOD_LEDGER = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 10.0,
                "mark": 20000.0, "rate": 0.0001, "amount": -20.0}]


class FundingAuditBase(unittest.TestCase):
    def call(self, materials, ledger, *, orders=None, series=None,
             model="historical_events"):
        return X.audit_funding_usage(
            materials, ledger_rows=ledger, position_rows=[POS],
            order_rows=[ORD] if orders is None else orders,
            rate_series_by_sym=series, model=model)


class BaselineTest(FundingAuditBase):
    """合法样本必须通过 —— 防「盲目全拒」。"""

    def test_baseline_forward_ok(self):
        r = self.call(MAT, GOOD_LEDGER)
        self.assertTrue(r["forward"]["ok"], f"reasons={reasons_of(r)}")

    def test_baseline_reverse_ok(self):
        r = self.call(MAT, GOOD_LEDGER)
        self.assertTrue(r["reverse"]["ok"],
                        f"missing={r['reverse']['missing']} extra={r['reverse']['extra']}")

    def test_baseline_no_non_voucher_problems(self):
        r = self.call(MAT, GOOD_LEDGER)
        self.assertEqual(problems_without_voucher(r), [])


class BlindspotTest(FundingAuditBase):
    """2 个盲区（原 7 反例之外的补充）。"""

    def test_blindspot_a_missing_adverse_rate_source_is_blocked(self):
        mat = {"sources": [{"exchange": "binance", "symbol": "BTCUSDT",
                            "events": [[2000, None]], "marks": {"2000": 20000.0}}]}
        led = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 10.0, "mark": 20000.0,
                "rate": 0.0005, "amount": -100.0, "rate_substituted": 1,
                "rate_source": "adjacent_adverse_estimate"}]
        r = self.call(mat, led, series=None, model="norm4h")
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("no_adjacent_adverse_rate_source", reasons_of(r))

    def test_blindspot_b_nan_amount_is_blocked(self):
        led = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 10.0,
                "mark": 20000.0, "rate": 0.0001, "amount": float("nan")}]
        r = self.call(MAT, led)
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("invalid_amount_not_finite", reasons_of(r))
        self.assertEqual(r["total_abs_error"], 0.0, "NaN 污染了累计误差")


class CounterexampleTest(FundingAuditBase):
    """原 7 反例中的 1–5（行为证据，打在生产函数上）。"""

    def test_ce1_missing_rate_on_charged_row(self):
        led = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 10.0,
                "mark": 20000.0, "rate": None, "amount": -20.0}]
        r = self.call(MAT, led)
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("missing_rate_on_charged_row", reasons_of(r))

    def test_ce2_unauthorized_estimate_tag(self):
        """真率存在却贴估计标签 ⇒ 必须拒。"""
        led = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 10.0, "mark": 20000.0,
                "rate": 0.0009, "amount": -180.0, "rate_substituted": 1,
                "rate_source": "adjacent_adverse_estimate"}]
        r = self.call(MAT, led, series=SERIES, model="norm4h")
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("unauthorized_estimate_tag", reasons_of(r))

    def test_ce3_quantity_mismatch_with_position(self):
        """qty 与 amount 同乘翻倍：公式自洽但与持仓不符 ⇒ 必须拒。"""
        led = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 20.0,
                "mark": 20000.0, "rate": 0.0001, "amount": -40.0}]
        r = self.call(MAT, led)
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("quantity_mismatch_with_position", reasons_of(r))

    def test_ce4_order_fill_scope_mismatch(self):
        """借用异币订单凑数量 ⇒ 必须拒。"""
        borrowed = dict(ORD, order_id="ord_borrowed_01", symbol="ETHUSDT")
        r = self.call(MAT, GOOD_LEDGER, orders=[borrowed])
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("order_fill_scope_mismatch", reasons_of(r))

    def test_ce5_future_exit_not_deducted_before_T(self):
        """未来平仓单被提前扣减导致 T 时持仓算成 0 ⇒ 必须拒。"""
        future_exit = dict(ORD, order_id="ord_exit_01", side="SELL",
                           effective_fill_time=3000)
        led = [{"run_id": "run_01", "position_id": "pos_btc_01", "exchange": "binance",
                "symbol": "BTCUSDT", "funding_time": 2000, "qty": 0.0,
                "mark": 20000.0, "rate": 0.0001, "amount": 0.0}]
        r = self.call(MAT, led, orders=[ORD, future_exit])
        self.assertFalse(r["forward"]["ok"], f"reasons={reasons_of(r)}")
        self.assertIn("quantity_mismatch_with_order_fills_before_T", reasons_of(r))


class StaticEvidenceTest(unittest.TestCase):
    """反例 6 / 7：生产代码为 `run_arm` 内部嵌套实现 ⇒ **仅静态证据**，非行为证据。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "strategies" / "wx_execution.py").read_text(encoding="utf-8")

    def test_ce6_static_full_scan_marker_present(self):
        self.assertIn("不再「遇首币 break」", self.src)
        self.assertIn("hits = []", self.src)

    def test_ce6_static_iterates_all_open_positions(self):
        self.assertIn("for p in list(positions.values()):", self.src)

    def test_ce7_static_unknown_interval_entry_present(self):
        self.assertIn("UNKNOWN", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
