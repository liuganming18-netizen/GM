# -*- coding: utf-8 -*-
"""PRECISION-02 · 精度执行路径**独立反例**审核（只读，不修改任何生产源码/测试）。

原则
────
1. 预期值**手算**给出（注释里写清算式），不调用被测函数反推；
   只在需要「用另一种方式独立复算」时使用 `decimal.Decimal` 手写算式。
2. 每个用例都留**最小复现**片段，可单独复制运行。
3. 结论只分 通过 / 失败 / 未知；未知 = 现象已观测但无法判定是否属缺陷（含可达性未证）。

运行
────
  /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python \
      evidence/PRECISION-02/审核_精度执行路径反例.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if os.path.join(ROOT, "策略") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "策略"))

from WyckoffMaster.trading_core.contracts import OrderType  # noqa: E402
from WyckoffMaster.trading_core.engine import TradingCore  # noqa: E402
from WyckoffMaster.trading_core.fill import (  # noqa: E402
    FillRejected, SimulatedBarFillAdapter, price_breakdown, round_buy_price,
    round_qty_down, round_sell_price,
)
from WyckoffMaster.trading_core.policy import ExecutionPolicy  # noqa: E402
from WyckoffMaster.oi_resonance_v1.config import ExecParams, SignalParams  # noqa: E402
from WyckoffMaster.event_trend_v0_1.instrument_spec import InstrumentSpec  # noqa: E402
from WyckoffMaster.event_trend_v0_1.engine_adapter import (  # noqa: E402
    run_event_trend_backtest,
)
from WyckoffMaster.event_trend_v0_1.config import EventTrendConfig  # noqa: E402
from WyckoffMaster.event_trend_v0_1.tests.test_event_trend_closure import (  # noqa: E402
    HOUR_MS, MIN15_MS, _bars_15m, _candidate,
)

RESULTS: List[Dict[str, Any]] = []


def record(cid: str, item: int, title: str, status: str, expected: Any,
           actual: Any, repro: str, source: str, impact: str,
           note: str = "") -> None:
    RESULTS.append({
        "id": cid, "item": item, "title": title, "status": status,
        "expected": expected, "actual": actual, "repro": repro,
        "source": source, "impact": impact, "note": note,
    })


# ══════════════════════════ 夹具（自建，不复用被测逻辑）══════════════════════════

T0 = 1788652800000            # 2026-09-05 00:00:00Z，仅作时间轴原点
T_1H = T0
T_EXEC = T0 + HOUR_MS         # 确认时刻 = 1h 收盘 ⇒ 首根 15m = 同一时刻


def _bars15(opens, *, closes=None, highs=None, lows=None, start=T0):
    rows = []
    for k, op in enumerate(opens):
        cl = op if closes is None else closes[k]
        hi = max(op, cl) if highs is None else highs[k]
        lo = min(op, cl) if lows is None else lows[k]
        rows.append({"timestamp": start + k * MIN15_MS,
                     "open": float(op), "high": float(hi), "low": float(lo),
                     "close": float(cl), "volume": 50000.0,
                     "quote_volume": 5_000_000.0})
    return pd.DataFrame(rows)


def _sig1h(stop, *, signal=1, direction=1, t=T_1H):
    return pd.DataFrame([{
        "timestamp": t, "signal": signal, "direction": direction,
        "stop_price": float(stop), "confirm_time": (t + HOUR_MS) // 1000,
        "oi_resonance_score": 1.0,
        "exit_low_curr": float("nan"), "exit_high_curr": float("nan"),
    }])


def _spec(**over) -> InstrumentSpec:
    base = dict(
        symbol="TESTUSDT", venue="binance", market="um",
        price_step="0.01", qty_step="0.01", min_qty="0.01", max_qty="0",
        min_notional="5", price_precision=2, quantity_precision=2,
        order_type="MARKET", source_url="test", source_kind="test",
        fetched_at_ms=None, evidence_path=None, evidence_sha256=None,
        onboard_ms=None, snapshot_server_ms=None,
        applicability="unverified_historical", applicability_note="PRECISION-02 夹具",
        formal_pass_allowed=False,
    )
    base.update(over)
    return InstrumentSpec(**base)


@dataclass(frozen=True)
class _ExecP(ExecParams):
    """OI 的 `ExecParams` 不含 `participation_rate`（引擎用 getattr 读取），
    这里派生一个子类把该可选约束显式打开，不改动生产类。"""
    participation_rate: Optional[float] = None


def _core(*, strict: bool, price_step=0.0, slippage_bps=3.0,
          specs: Optional[Dict[str, Any]] = None, stop: float = 99.0,
          policy_cls=ExecutionPolicy, exec_over: Optional[dict] = None):
    """装配一个**最小**共享核心（单标的、单信号）。"""
    ep = dict(initial_equity=10000.0, risk_per_trade=0.005,
              max_portfolio_risk=0.015, max_portfolio_margin_pct=0.90,
              max_concurrent_trades=3, max_leverage_per_symbol=1.0,
              fee_rate=0.0005, taker_fee_rate=0.0005, maker_fee_rate=0.0002,
              slippage_bps=slippage_bps, force_close_at_end=True,
              enable_trailing_exit=False, funding_model="settlement_events")
    ep.update(exec_over or {})
    sp = SignalParams(price_step=price_step)
    pol = policy_cls(signal_params=sp, exec_params=_ExecP(**ep),
                     strategy_key="PRECISION02")
    ad = SimulatedBarFillAdapter(slippage_bps=slippage_bps,
                                 maker_fee_rate=0.0002, taker_fee_rate=0.0005,
                                 strict=strict)
    return TradingCore(policy=pol, fill_adapter=ad,
                       signal_fn=lambda *a, **k: _sig1h(stop),
                       to_epoch_seconds=lambda arr, unit: (arr // 1000).astype("int64"),
                       strategy_key="PRECISION02", strategy_id="PRECISION02",
                       strategy_version="audit", instrument_specs=specs)


def _run_core(core, opens, *, closes=None, stop=99.0, sym="TESTUSDT",
              sig=None):
    df15 = _bars15(opens, closes=closes)
    md = {sym: {"signal_bars": (sig if sig is not None else _sig1h(stop)),
                "intrabar_bars": df15, "time_unit": "ms"}}
    return core.run(md, time_unit="ms")


# ══════════════════════════ 1. 方向 / 滑点 / 取整 / 边界 / 极低价 / 数量 ══════════════════════════

def item1() -> None:
    a = SimulatedBarFillAdapter(slippage_bps=3.0, maker_fee_rate=0.0002,
                                taker_fee_rate=0.0005, strict=True)

    # A1 多头入场 = 买 ⇒ 向更贵取整。
    # 手算：100.001 × 1.0003 = 100.0310003 → ceil 到 0.01 ⇒ 100.04
    q = a.quote_entry(raw_price=100.001, direction=1, price_step=0.01, stop_price=99.0)
    record("A1", 1, "多头入场：滑点后向更贵取整（ceil）", 
           "PASS" if abs(q.entry_fill - 100.04) < 1e-12 else "FAIL",
           "100.04", q.entry_fill,
           "SimulatedBarFillAdapter(slippage_bps=3,strict=True)"
           ".quote_entry(raw_price=100.001,direction=1,price_step=0.01,stop_price=99)",
           "trading_core/fill.py:125-127", "所有 strict 回测入场价")

    # A2 空头入场 = 卖 ⇒ 向更便宜取整。
    # 手算：100.001 × 0.9997 = 99.9709997 → floor 到 0.01 ⇒ 99.97
    q = a.quote_entry(raw_price=100.001, direction=-1, price_step=0.01, stop_price=101.0)
    record("A2", 1, "空头入场：滑点后向更便宜取整（floor）",
           "PASS" if abs(q.entry_fill - 99.97) < 1e-12 else "FAIL",
           "99.97", q.entry_fill,
           "同上，direction=-1",
           "trading_core/fill.py:127-130", "空头开仓（当前生产策略只多，暂不可达）")

    # A3 多头出场 = 卖 ⇒ floor。手算同上 ⇒ 99.97
    px = a.quote_exit(target_px=100.001, direction=1, price_step=0.01, reason="x")
    record("A3", 1, "多头出场：向更便宜取整（floor）",
           "PASS" if abs(px - 99.97) < 1e-12 else "FAIL", "99.97", px,
           "quote_exit(target_px=100.001,direction=1,price_step=0.01)",
           "trading_core/fill.py:278-281", "所有 strict 回测出场价")

    # A4 空头出场 = 买 ⇒ ceil。手算：100.001×1.0003=100.0310003 → 100.04
    px = a.quote_exit(target_px=100.001, direction=-1, price_step=0.01, reason="x")
    record("A4", 1, "空头出场：向更贵取整（ceil）",
           "PASS" if abs(px - 100.04) < 1e-12 else "FAIL", "100.04", px,
           "quote_exit(...,direction=-1)", "trading_core/fill.py:278-281",
           "空头平仓（当前生产策略只多）")

    # A5 精确边界（滑点=0）：100.03 本身就在 0.01 网格上 ⇒ 两方向都不动
    a0 = SimulatedBarFillAdapter(slippage_bps=0.0, maker_fee_rate=0.0002,
                                 taker_fee_rate=0.0005, strict=True)
    b = a0.quote_entry(raw_price=100.03, direction=1, price_step=0.01, stop_price=99.0)
    s = a0.quote_exit(target_px=100.03, direction=1, price_step=0.01, reason="x")
    ok = abs(b.entry_fill - 100.03) < 1e-12 and abs(s - 100.03) < 1e-12
    record("A5", 1, "精确步长边界不多跳一档（100.03/0.01）",
           "PASS" if ok else "FAIL", "100.03 / 100.03",
           f"{b.entry_fill} / {s}",
           "SimulatedBarFillAdapter(slippage_bps=0,strict=True) 上 100.03 双向报价",
           "trading_core/fill.py:87-98", "边界价（Decimal 精确除法修复点）")

    # A6 极低价格 + 粗 tick：tick 成本被放大。手算 0.07,3bps：
    #   买 0.07×1.0003=0.070021 → 0.08（+14.29%）；卖 0.07×0.9997=0.069979 → 0.06（−14.29%）
    b = a.quote_entry(raw_price=0.07, direction=1, price_step=0.01, stop_price=0.06)
    s = a.quote_exit(target_px=0.07, direction=1, price_step=0.01, reason="x")
    amp = (b.entry_fill - 0.07) / 0.07 * 100
    record("A6", 1, "极低价格：tick 相对价格过粗时成本被放大（观测项）",
           "UNKNOWN", "买 0.08 / 卖 0.06（各偏离 14.29%，为 3bps 的 47.6 倍）",
           f"买 {b.entry_fill}（+{amp:.2f}%）/ 卖 {s}（{(s-0.07)/0.07*100:.2f}%）",
           "quote_entry(raw_price=0.07,direction=1,price_step=0.01) + quote_exit(同价)",
           "trading_core/fill.py:108-139",
           "价格 0.07 用 0.01 tick 时，单边 tick 成本≈14.3%；无任何「tick/价格」上限校验",
           note="属规格选择问题而非算术错误，故记 UNKNOWN；但见 A7 —— 同一机制会把"
                "「失真」一路带进盈亏而不被标记。")

    # A7 **入场取整把价格抬到原价 1.25 倍仍被判有效**（SPACE 类标的的真实形态）
    core = _core(strict=True, price_step=0.01, stop=0.01)
    opens = [0.024] * 6
    res = _run_core(core, opens)
    ent = [d for d in res["decisions"] if d["action"] == "entry"]
    entry_px = ent[0]["price"] if ent else None
    # 手算：ceil(0.024×1.0003 / 0.01) × 0.01 = ceil(2.40072)=3 ⇒ 0.03
    ratio = (entry_px / 0.024) if entry_px else float("nan")
    trade = res["trades"][0] if res["trades"] else None
    exit_px = trade["exit_price"] if trade else None
    status = "FAIL" if (entry_px is not None and abs(entry_px - 0.03) < 1e-12
                        and res["precision"]["run_valid"]) else "PASS"
    record("A7", 1, "极低价格：取整后价格偏离原价 25%（入场）/17%（出场）仍判 run_valid",
           status,
           "应在「取整后价格相对原价偏差过大」时拒绝或标记无效",
           f"entry_fill={entry_px}（= 原价 ×{ratio:.3f}）, exit_fill={exit_px}"
           f"（= 原价 ×{(exit_px/0.024 if exit_px else float('nan')):.3f}）, "
           f"run_valid={res['precision']['run_valid']}",
           "_core(strict=True,price_step=0.01,stop=0.01) + 6 根 15m open=close=0.024",
           "trading_core/fill.py:252-253（只校验 >0）；engine.py:886（run_valid 只由出场拒绝触发）",
           "价格 < 10×tick 的标的：往返价差 −33.3%，全部计入 gross_pnl，"
           "run 仍标 valid；SPACE/AIGENSYN/TRUTH 类标的正是此形态",
           note="strict 只挡「取整后 = 0」，不挡「取整后 = 2.5 倍原价」。")

    # A8 数量向下取整（多买不可）。手算 43.139 → 43.13
    record("A8", 1, "数量向**下**取整到 stepSize",
           "PASS" if abs(round_qty_down(43.139, 0.01) - 43.13) < 1e-12 else "FAIL",
           "43.13", round_qty_down(43.139, 0.01),
           "round_qty_down(43.139, 0.01)", "trading_core/fill.py:101-105",
           "所有带规格的入场数量")

    # A9 数量精确边界：0.29 是 0.01 整数倍
    record("A9", 1, "数量精确边界不丢一档（0.29/0.01）",
           "PASS" if abs(round_qty_down(0.29, 0.01) - 0.29) < 1e-12 else "FAIL",
           "0.29", round_qty_down(0.29, 0.01),
           "round_qty_down(0.29, 0.01)", "trading_core/fill.py:104-105",
           "数量边界")

    # A10 非有限步长：抛出**非契约异常类型**
    try:
        round_buy_price(100.0, float("nan"))
        got = "no exception"
        status = "FAIL"
    except FillRejected as e:  # 契约期望
        got = f"FillRejected:{e.reason}"
        status = "PASS"
    except Exception as e:  # 非契约
        got = f"{type(e).__module__}.{type(e).__name__}: {e}"
        status = "FAIL"
    record("A10", 1, "非法步长（NaN）抛出的异常类型是否符合契约",
           status, "FillRejected 或 ExecutionInputError", got,
           "round_buy_price(100.0, float('nan'))", "trading_core/fill.py:87-90",
           "非 strict 路径若传入 NaN 步长，会在引擎深处抛 decimal.InvalidOperation，"
           "不是 FillRejected ⇒ 上层 except FillRejected 捕不到")

    # A11 数量 NaN：同样抛非契约异常（引擎的 isfinite 检查在函数之后）
    try:
        round_qty_down(float("nan"), 0.01)
        got, status = "no exception", "FAIL"
    except FillRejected as e:
        got, status = f"FillRejected:{e.reason}", "PASS"
    except Exception as e:
        got, status = f"{type(e).__module__}.{type(e).__name__}", "FAIL"
    record("A11", 1, "数量 NaN 的异常类型是否符合契约",
           status, "FillRejected 或返回 NaN 由调用方判", got,
           "round_qty_down(float('nan'), 0.01)", "trading_core/fill.py:104-105",
           "engine.py:527 先取整后判 isfinite ⇒ 若 qty 为 NaN 会先炸；"
           "当前 policy 已在上游拦 NaN 数量，故实际不可达（UNKNOWN 可达性）",
           note="engine 的顺序是「先 round_qty_down 再 isfinite」，"
                "所以这条防线只是纸面防线。")

    # A12 负滑点 = 有利成交（strict 不拦）
    an = SimulatedBarFillAdapter(slippage_bps=-30.0, maker_fee_rate=0.0002,
                                 taker_fee_rate=0.0005, strict=True)
    q = an.quote_entry(raw_price=100.0, direction=1, price_step=0.01, stop_price=99.0)
    better = q.entry_fill < 100.0
    record("A12", 1, "负滑点（slippage_bps<0）被当作有利成交且不拒绝",
           "FAIL" if better else "PASS",
           "应拒绝（滑点必须非负、方向必须不利）",
           f"entry_fill={q.entry_fill} < raw 100.0 ⇒ 有利成交",
           "SimulatedBarFillAdapter(slippage_bps=-30,strict=True)"
           ".quote_entry(raw_price=100,direction=1,price_step=0.01)",
           "trading_core/fill.py:211（无校验）；engine.py:261（无校验）",
           "OI 路径由 validate_exec_params 兜住（>=0）；**事件趋势路径 EventTrendConfig "
           "无任何校验** ⇒ 配置写错即静默产生有利成交")


# ══════════════════════════ 2. 最小量 / 最大量 / 最小名义额 / 订单类型 ══════════════════════════

def item2() -> None:
    # B1 **maxQty 完全未执行**：手算 qty
    #   unit_risk = |100.03 − 99×0.9997| + (100.03+98.9703)×0.0005 = 1.0597+0.09950=1.15920
    #   qty_risk = 0.005×10000/1.15920 = 43.13；名义上限 10000×0.9×1.0/100.03 = 89.97
    #   ⇒ qty = 43.13，远超 maxQty=1
    spec = _spec(symbol="TESTUSDT", max_qty="1", qty_step="0.01",
                 min_qty="0.01", min_notional="5")
    core = _core(strict=True, price_step=0.01, specs={"TESTUSDT": spec})
    res = _run_core(core, [100.0] * 6, stop=99.0)
    tr = res["trades"][0] if res["trades"] else None
    qty = tr["qty"] if tr else None
    status = "FAIL" if (qty is not None and qty > 1.0) else "PASS"
    record("B1", 2, "maxQty（最大下单量）是否实际执行",
           status, "qty ≤ 1.0，否则拒绝（qty_above_max）",
           f"qty={qty}（maxQty=1），trades_count={res['trades_count']}，"
           f"rejected={[r['reason'] for r in res['rejected_orders']]}",
           "_spec(max_qty='1') + _core(strict=True,price_step=0.01) + open=100,stop=99",
           "trading_core/engine.py:517-570（只有 min_qty / min_notional 两道）；"
           "event_trend_v0_1/instrument_spec.py:120（max_qty 只解析不使用）",
           "所有带规格的入场：超大单不会被拦。旧实现 "
           "`_legacy_archive_v31/execution.py:391` 有 max_quantity 校验 ⇒ 本次收敛**丢失**该约束")

    # B2 minQty 执行（手算：qty 43.13 < minQty 1e6 ⇒ 拒绝）
    spec = _spec(symbol="TESTUSDT", max_qty="0", qty_step="0.01",
                 min_qty="1000000", min_notional="5")
    core = _core(strict=True, price_step=0.01, specs={"TESTUSDT": spec})
    res = _run_core(core, [100.0] * 6, stop=99.0)
    reasons = [r["reason"] for r in res["rejected_orders"]]
    record("B2", 2, "minQty 是否实际执行",
           "PASS" if "qty_below_min_step" in reasons else "FAIL",
           "拒绝，reason=qty_below_min_step",
           f"trades_count={res['trades_count']}, reasons={reasons}",
           "_spec(min_qty='1000000') + open=100,stop=99",
           "trading_core/engine.py:535-550", "带规格的入场")

    # B3 minNotional 执行（手算：43.13×100.03=4314 < 1e6 ⇒ 拒绝）
    spec = _spec(symbol="TESTUSDT", max_qty="0", qty_step="0.01",
                 min_qty="0.01", min_notional="1000000")
    core = _core(strict=True, price_step=0.01, specs={"TESTUSDT": spec})
    res = _run_core(core, [100.0] * 6, stop=99.0)
    reasons = [r["reason"] for r in res["rejected_orders"]]
    record("B3", 2, "minNotional 是否实际执行",
           "PASS" if "notional_below_min" in reasons else "FAIL",
           "拒绝，reason=notional_below_min",
           f"trades_count={res['trades_count']}, reasons={reasons}",
           "_spec(min_notional='1000000') + open=100,stop=99",
           "trading_core/engine.py:551-567", "带规格的入场")

    # B4 minNotional 边界：恰好等于下限 ⇒ 交易所侧合法，引擎不得误拒
    #   滑点置 0 使入场价恰为 5.00。手算 unit_risk = |5.00−4.90| + (5.00+4.90)×0.0005
    #   = 0.10 + 0.00495 = 0.10495；qty = (0.005×1000)/0.10495 = 47.64 → floor 到 1 ⇒ 47
    #   名义 = 47×5.00 = 235 ⇒ minNotional=235 必须通过；=236 必须拒绝
    for mn, want_trade in (("235", True), ("236", False)):
        spec = _spec(symbol="TESTUSDT", price_step="0.01", qty_step="1",
                     min_qty="1", min_notional=mn, max_qty="0")
        core = _core(strict=True, price_step=0.01, slippage_bps=0.0,
                     specs={"TESTUSDT": spec}, stop=4.9,
                     exec_over={"initial_equity": 1000.0, "risk_per_trade": 0.005})
        res = _run_core(core, [5.0] * 6)
        tr = res["trades"][0] if res["trades"] else None
        notional = (tr["entry_price"] * tr["qty"]) if tr else None
        got_trade = tr is not None
        record(f"B4-{mn}", 2, f"minNotional 边界（={mn}）：notional=235 时的判定",
               "PASS" if got_trade == want_trade else "FAIL",
               f"{'成交' if want_trade else '拒绝'}",
               f"trades_count={res['trades_count']}, notional={notional}, "
               f"reasons={[r['reason'] for r in res['rejected_orders']]}",
               f"_spec(price_step='0.01',qty_step='1',min_qty='1',min_notional='{mn}') "
               "+ open=5.0, stop=4.9, slippage=0",
               "trading_core/engine.py:553", "带规格的入场",
               note="确认 minNotional 用「<」而非「<=」，边界值不被误拒。")

    # B5 订单类型约束未实现：LIMIT 单没有 limit_price 仍然成交
    class _LimitPolicy(ExecutionPolicy):
        def entry_order_type(self):
            return OrderType.LIMIT

        def exit_order_type(self):
            return OrderType.LIMIT

    core = _core(strict=True, price_step=0.01, policy_cls=_LimitPolicy)
    res = _run_core(core, [100.0] * 6)
    orders = res["trading_core"]["orders"]
    ent = [o for o in orders if o["action"] == "open"]
    bad = [o for o in orders if str(o["order_type"]).upper() == "LIMIT"
           and o["limit_price"] is None]
    filled = [o for o in bad if str(o["status"]).lower() in ("filled", "closed")]
    status = "FAIL" if filled else "PASS"
    record("B5", 2, "订单类型约束：LIMIT 单缺 limit_price 仍被按市价成交",
           status, "拒绝（limit_price_required）或按 limit_price 撮合",
           f"orders={[(o['action'], o['order_type'], o['limit_price'], o['status']) for o in orders]}",
           "子类 policy 令 entry/exit_order_type() 返回 OrderType.LIMIT，其余不变",
           "trading_core/engine.py:232-242（_fill_order 不看 order_type/limit_price）；"
           "trading_core/order.py:93（limit_price 允许 None）",
           "当前生产只有 MARKET / STOP_MARKET，故为**潜在**缺陷："
           "任何人加限价单都会静默退化为「开盘价+滑点」成交",
           note="同一缺陷使「STOP_MARKET 的触发价」也只作为审计字段存在，"
                "引擎不会校验成交价与 stop_price 的关系。")

    # B6 spec.order_type 从未被引擎读取（静态证据）
    src = open(os.path.join(ROOT, "策略/WyckoffMaster/trading_core/engine.py"),
               encoding="utf-8").read()
    used = "order_type" in src and "spec.order_type" in src
    record("B6", 2, "合约规格的 order_type 是否参与撮合约束选择",
           "UNKNOWN" if not used else "PASS",
           "应按实际订单类型（MARKET vs STOP_MARKET）选择 LOT_SIZE / MARKET_LOT_SIZE",
           f"engine.py 中是否出现 `spec.order_type`：{used}",
           "grep 'spec.order_type' 策略/WyckoffMaster/trading_core/engine.py",
           "trading_core/engine.py（全文）；instrument_spec.py:125,233-249",
           "解析侧按 order_type='MARKET' 取「两侧更保守」的步长，"
           "对 STOP_MARKET 出场不一定是交易所真值；因取 max 保守，"
           "只会更小不会非法 ⇒ 暂列未知")

    # B7 参与率上限执行（手算：prev_qv=5e6 × 0.001 = 5000 USDT 名义 ⇒ qty ≤ 49.98）
    core = _core(strict=True, price_step=0.01,
                 exec_over={"participation_rate": 0.001})
    res = _run_core(core, [100.0] * 6, stop=99.0)
    tr = res["trades"][0] if res["trades"] else None
    notional = tr["entry_price"] * tr["qty"] if tr else None
    record("B7", 2, "参与率上限（名义 ≤ 前一根 15m 成交额 × 0.1%）是否执行",
           "PASS" if (notional is not None and notional <= 5000.0 + 1e-6) else "FAIL",
           "名义 ≤ 5000（=5e6×0.001）",
           f"notional={notional}, qty={tr['qty'] if tr else None}",
           "_core(participation_rate=0.001) + quote_volume=5e6, open=100, stop=99",
           "trading_core/engine.py:487-515", "事件趋势回测（participation_rate=0.001 恒开）")


# ══════════════════════════ 3. 非正/非有限价格、出场失败语义、账本 ══════════════════════════

def item3() -> None:
    a = SimulatedBarFillAdapter(slippage_bps=3.0, maker_fee_rate=0.0002,
                                taker_fee_rate=0.0005, strict=True)
    bad_ok = True
    detail = []
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        for fn in ("entry", "exit"):
            try:
                if fn == "entry":
                    a.quote_entry(raw_price=bad, direction=1, price_step=0.01,
                                  stop_price=99.0)
                else:
                    a.quote_exit(target_px=bad, direction=1, price_step=0.01, reason="x")
                bad_ok = False
                detail.append(f"{fn}({bad}) 未拒绝")
            except FillRejected:
                pass
    record("C1", 3, "strict=True：非正/非有限成交价必须拒绝",
           "PASS" if bad_ok else "FAIL", "全部 FillRejected",
           "全部 FillRejected" if bad_ok else "; ".join(detail),
           "strict adapter 上 quote_entry/quote_exit 传 0/-1/NaN/inf",
           "trading_core/fill.py:220-241", "事件趋势回测（strict=True）")

    # C2 strict=False：0 价静默通过；NaN 抛**非契约**异常（不是 FillRejected）
    a2 = SimulatedBarFillAdapter(slippage_bps=3.0, maker_fee_rate=0.0002,
                                 taker_fee_rate=0.0005, strict=False)
    q0 = a2.quote_entry(raw_price=0.0, direction=1, price_step=0.01, stop_price=1.0)
    zero_silent = q0.entry_fill == 0.0
    try:
        a2.quote_entry(raw_price=float("nan"), direction=1, price_step=0.01,
                       stop_price=99.0)
        nan_kind = "no exception"
    except FillRejected:
        nan_kind = "FillRejected"
    except Exception as e:
        nan_kind = type(e).__module__ + "." + type(e).__name__
    record("C2", 3, "strict=False：0 价静默通过（返回 0 成交价），NaN 抛非契约异常",
           "FAIL" if (zero_silent and nan_kind != "FillRejected") else "PASS",
           "0 价应拒绝；NaN 应以 FillRejected 拒绝",
           f"raw=0 ⇒ entry_fill={q0.entry_fill}（无异常）；"
           f"raw=NaN ⇒ 抛 {nan_kind}",
           "SimulatedBarFillAdapter(strict=False).quote_entry(raw_price=0.0/NaN,...)",
           "trading_core/fill.py:261-270（非 strict 分支无闸门）；fill.py:58-76（_dec 抛 InvalidOperation）",
           "OI 共振生产回测 `oi_resonance_v1/execution.py:105` 正是 strict 缺省 False。"
           "引擎级 0 价目前被 `check_entry` 的 gap_beyond_stop 偶然拦住，"
           "但 NaN 会以 decimal.InvalidOperation 穿透 `except FillRejected`",
           note="这就是 SPACE「零元卖出」在非 strict 路径下的原始形态：算术不报错，"
                "只是把成交价算成 0。")

    # C3 出场失败：仓位保留 + run_valid=False + 账本一致 + 无平仓事件
    #   手算：0.004 用 0.01 tick ⇒ 入场 ceil 0.01，出场 floor 0.00 ⇒ 拒绝
    core = _core(strict=True, price_step=0.01, stop=0.003)
    res = _run_core(core, [0.004] * 6)
    eng = res
    counts = eng["trading_core"]["ledger_event_counts"]
    ok = (eng["trades_count"] == 0
          and eng["open_positions_remaining"] == ["TESTUSDT"]
          and eng["precision"]["run_valid"] is False
          and any("exit" in r for r in eng["precision"]["invalid_reasons"])
          and eng["reconcile"]["cash_identity_ok"] is True
          and counts.get("position_closed", 0) == 0)
    record("C3", 3, "出场失败：仓位保留 / 运行标记无效 / 账本仍一致 / 不写平仓事件",
           "PASS" if ok else "FAIL",
           "trades_count=0，open_positions_remaining=['TESTUSDT']，"
           "run_valid=False，cash_identity_ok=True，position_closed=0",
           f"trades_count={eng['trades_count']}, "
           f"open={eng['open_positions_remaining']}, "
           f"run_valid={eng['precision']['run_valid']}, "
           f"cash_identity_ok={eng['reconcile']['cash_identity_ok']}, "
           f"position_closed={counts.get('position_closed', 0)}, "
           f"ledger={counts}",
           "_core(strict=True,price_step=0.01,stop=0.003) + 6 根 15m open=close=0.004",
           "trading_core/engine.py:384-395, 719-729, 782-816",
           "所有出场被拒的场景（SPACE 类）")

    # C4 出场先失败、同 run 后续成功平仓 ⇒ unclosed_positions 仍误报
    #   时间轴：confirm_time = 1h 收盘 ⇒ 入场在**第 5 根** 15m（index 4）
    #   手算：入场 0.02 → ceil 0.03；current_stop=0.01
    #   第 6 根（index 5）open=0.004 ⇒ gap 出场 floor(0.0039988/0.01)=0 拒绝；
    #   同根 low=0.004 ⇒ intrabar floor(0.009997/0.01)=0 也拒绝；
    #   其后 open/close=0.02 ⇒ EOD floor(0.019994/0.01)=0.01 成功平仓
    opens = [0.02] * 5 + [0.004] + [0.02] * 3
    closes = [0.02] * 5 + [0.02] + [0.02] * 3
    lows = [0.02] * 5 + [0.004] + [0.02] * 3
    df15 = _bars15(opens, closes=closes, lows=lows)
    core = _core(strict=True, price_step=0.01, stop=0.01)
    md = {"TESTUSDT": {"signal_bars": _sig1h(0.01), "intrabar_bars": df15,
                       "time_unit": "ms"}}
    res = core.run(md, time_unit="ms")
    eng = res
    closed_ok = eng["trades_count"] == 1 and eng["open_positions_remaining"] == []
    misreport = bool(eng["precision"]["unclosed_positions"]) and closed_ok
    record("C4", 3, "出场先被拒、同 run 后段成功平仓 ⇒ `unclosed_positions` 误报",
           "FAIL" if misreport else "PASS",
           "仓位最终已平 ⇒ unclosed_positions 应为空（run_valid 可保守为 False）",
           f"trades_count={eng['trades_count']}, "
           f"open_positions_remaining={eng['open_positions_remaining']}, "
           f"precision.unclosed_positions={eng['precision']['unclosed_positions']}, "
           f"invalid_reasons={eng['precision']['invalid_reasons']}",
           "_core(strict=True,price_step=0.01,stop=0.01) + "
           "opens=[0.02,0.02,0.004,0.02,0.02,0.02]，第 3 根 low=0.004，其余 close=0.02",
           "trading_core/engine.py:394（append 后无回撤）；engine.py:888（直接 sorted(set(...))）",
           "任何「跳空出场被拒 → 后续正常平仓」的 run：报告字段自相矛盾，"
           "下游若据 unclosed_positions 判「仓位未平」会误判")

    # C5 出场失败后权益曲线末点仍 = cash + 未实现（不得当成已回笼现金）
    core = _core(strict=True, price_step=0.01, stop=0.003)
    res = _run_core(core, [0.004] * 6)
    last = res["equity_curve"][-1]
    ok = (abs(last["total_equity"] - res["final_equity"]) < 1e-9
          and last["open_positions_count"] == 1)
    record("C5", 3, "出场失败后权益曲线末点与 final_equity 一致且仍计未平仓",
           "PASS" if ok else "FAIL",
           "末点 total_equity == final_equity，open_positions_count=1",
           f"last.total_equity={last['total_equity']}, "
           f"final_equity={res['final_equity']}, "
           f"open_positions_count={last['open_positions_count']}",
           "同 C3 场景", "trading_core/engine.py:756-767", "出场失败场景")

    # C7 空输入返回体缺 `precision` 键（与主路径形状不一致）
    core = _core(strict=True, price_step=0.01)
    out = core.run({})
    record("C7", 3, "空 market_data 的返回体缺少 `precision` 键（形状不一致）",
           "FAIL" if "precision" not in out else "PASS",
           "主路径与空路径返回同一组键（含 precision），且缺结果用 None 而非 0",
           f"keys={sorted(out.keys())}；precision 存在={'precision' in out}；"
           f"max_drawdown_pct={out.get('max_drawdown_pct')!r}",
           "_core(strict=True,price_step=0.01).run({})",
           "trading_core/engine.py:906-933（_empty_result 无 precision，mdd 写死 0.0）",
           "任何以 `res['precision']` 读运行有效性的下游：空输入直接 KeyError；"
           "且 mdd 用 0.0 冒充「无回撤」，与「缺结果一律 None」的项目约定相反")

    # C6 非 strict + price_step=0（OI 生产回测缺省）：完全不取整，run 仍 valid
    core = _core(strict=False, price_step=0.0)
    res = _run_core(core, [100.0] * 6, stop=99.0)
    tr = res["trades"][0]
    no_round = abs(tr["entry_price"] - 100.0 * 1.0003) < 1e-9
    record("C6", 3, "price_step=0（OI 生产缺省）+ strict=False ⇒ 无任何价格取整且 run_valid=True",
           "FAIL" if (no_round and res["precision"]["run_valid"]) else "PASS",
           "应显式标记「未施加 tick 约束」（run 不应与「已取整」同权）",
           f"entry_price={tr['entry_price']}（= 100×1.0003，未取整）, "
           f"price_steps={res['precision']['price_steps']}, "
           f"spec_status={res['precision']['spec_status']}, "
           f"run_valid={res['precision']['run_valid']}",
           "_core(strict=False,price_step=0.0) + open=100,stop=99",
           "oi_resonance_v1/config.py:139（price_step 默认 0.0）；"
           "config.py:228（校验下限 0.0 ⇒ 0 合法）；fill.py:87（step<=0 不取整）",
           "8787 OI 共振回测默认即此形态：`backtest_runner.py:948` → "
           "`run_portfolio_execution`，不传 instrument_specs；"
           "结果里 price_steps 显示 0.0，但 run_valid 仍 True、无 invalid_reasons",
           note="`precision.spec_status='declared_no_spec'` 已如实标注，"
                "但没有「无 tick 约束 ⇒ 不得正式通过」的阻断位（formal_pass_allowed 未出现在该路径）。")


# ══════════════════════════ 4. 纯滑点 / 取整损失分账 ══════════════════════════

def item4() -> None:
    # D1 strict：分账恒等式 + 独立复算（原始价 100.005 不在 0.01 网格上 ⇒ 两侧都有取整损失）
    #   手算（Decimal，独立于被测函数）：
    #     入场：100.005×1.0003 = 100.0350015 → ceil 0.01 ⇒ 100.04
    #           pure = (100.0350015−100.005) = 0.0300015；round = (100.04−100.0350015)=0.0049985
    #     出场：100.005×0.9997 =  99.9749985 → floor 0.01 ⇒  99.97
    #           pure = (100.005−99.9749985) = 0.0300015；round = (99.9749985−99.97)=0.0049985
    #     ⇒ 单边合计 pure=0.0600030、round=0.0099970、total=0.07（= 100.04−99.97）
    core = _core(strict=True, price_step=0.01)
    res = _run_core(core, [100.005] * 6)
    t = res["trades"][0]
    qty = Decimal(str(t["qty"]))
    exp_pure = Decimal("0.0600030") * qty
    exp_round = Decimal("0.0099970") * qty
    exp_total = Decimal("0.07") * qty
    ok = (abs(Decimal(str(t["slippage_loss"])) - exp_total) < Decimal("1e-9")
          and abs(Decimal(str(t["slippage_loss_pure"])) - exp_pure) < Decimal("1e-8")
          and abs(Decimal(str(t["rounding_loss"])) - exp_round) < Decimal("1e-8")
          and abs(Decimal(str(t["exit_price"] - t["entry_price"]))
                  - Decimal("-0.07")) < Decimal("1e-9"))
    record("D1", 4, "strict：纯滑点与取整损失分别记录且合计等于总口径",
           "PASS" if ok else "FAIL",
           f"pure≈{exp_pure:.6f}, round≈{exp_round:.6f}, total={exp_total:.6f}",
           f"pure={t['slippage_loss_pure']}, round={t['rounding_loss']}, "
           f"total={t['slippage_loss']}, qty={t['qty']}, "
           f"entry={t['entry_price']}, exit={t['exit_price']}",
           "_core(strict=True,price_step=0.01) + open=close=100.005, stop=99（EOD 平于 100.005）",
           "trading_core/engine.py:577-591（入场）、996-1010（出场）、790-793（汇总）",
           "事件趋势回测的成本分账字段")

    # D2 不重复扣费：net_pnl 必须只由「实际成交价」推出，不得再减一次 slippage_loss
    fees = t["entry_fee"] + t["exit_fee"]
    gross = (t["exit_price"] - t["entry_price"]) * t["qty"] * t["direction"]
    net_from_fills = gross - fees - t["funding_fee"]
    net_minus_slip = net_from_fills - t["slippage_loss"]
    no_double = (abs(t["net_pnl"] - net_from_fills) < 1e-9
                 and abs(t["net_pnl"] - net_minus_slip) > 1e-9)
    record("D2", 4, "不重复扣费：net_pnl 只由实际成交价推出，slippage_loss 不再二次扣减",
           "PASS" if no_double else "FAIL",
           "net_pnl == gross(实际成交价) − fees − funding，且 ≠ 再减 slippage_loss",
           f"net_pnl={t['net_pnl']}, 由成交价复算={net_from_fills}, "
           f"若再减滑点={net_minus_slip}",
           "读取 trades[0] 的 entry_price/exit_price/qty/fees/funding_fee 手工复算",
           "trading_core/engine.py:1026", "所有平仓记录与净值")

    # D3 非 strict：分账是**自证**（pure=total、round=0），恒等式恒真
    core = _core(strict=False, price_step=0.0)
    res = _run_core(core, [100.0] * 6, stop=99.0)
    t = res["trades"][0]
    taut = (abs(t["rounding_loss"]) < 1e-15
            and t["price_audit"]["entry"] == {}
            and t["price_audit"]["exit"] == {}
            and res["reconcile"]["slippage_split_ok"] is True)
    record("D3", 4, "非 strict：`slippage_split_ok` 为自证恒真，不能证明取整损失已计量",
           "FAIL" if taut else "PASS",
           "分账字段应能区分「未计量」与「计量为 0」",
           f"rounding_loss={t['rounding_loss']}, price_audit={t['price_audit']}, "
           f"slippage_split_ok={res['reconcile']['slippage_split_ok']}, "
           f"slippage_loss={t['slippage_loss']}",
           "_core(strict=False,price_step=0.0) + open=100,stop=99",
           "trading_core/engine.py:582-591, 1000-1010（缺 audit 时 pure=total, round=0）",
           "OI 共振回测（strict=False）的 slippage_split_ok 恒为 True，"
           "若被当作「成本已正确分账」的证据即为**假阳性**")

    # D4 汇总口径：reconcile 汇总 == Σ 明细
    s = sum(x["slippage_loss"] for x in res["trades"])
    ok = abs(res["reconcile"]["sum_slippage_loss"] - s) < 1e-9
    record("D4", 4, "汇总口径：reconcile.sum_slippage_loss == Σ 明细",
           "PASS" if ok else "FAIL", f"{s}", res["reconcile"]["sum_slippage_loss"],
           "对比 reconcile.sum_slippage_loss 与 sum(trades.slippage_loss)",
           "trading_core/engine.py:777-793", "所有 run 的成本汇总")

    # D5 未平仓仓位的入场滑点不进汇总（口径说明，非缺陷）
    core = _core(strict=True, price_step=0.01, stop=0.003)
    res = _run_core(core, [0.004] * 6)
    record("D5", 4, "未平仓仓位的入场执行成本是否进入 sum_slippage_loss",
           "UNKNOWN",
           "应明确：汇总只含已平仓笔（当前实现如此）",
           f"sum_slippage_loss={res['reconcile']['sum_slippage_loss']}，"
           f"trades_count={res['trades_count']}，未平仓=1 笔（其入场成本未计入）",
           "同 C3 场景（出场被拒 ⇒ 无平仓记录）",
           "trading_core/engine.py:777（只遍历 closed）",
           "出场被拒的 run：成本汇总偏低（漏计该仓入场成本），与 run_valid=False 一致，"
           "但若下游只看 sum_slippage_loss 会低估")


# ══════════════════════════ 5. strict 开关的入口与影响面 ══════════════════════════

def _grep(pattern: str, files: List[str]) -> List[Dict[str, Any]]:
    out = []
    for rel in files:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if pattern in line:
                    out.append({"file": rel, "line": i, "text": line.strip()[:160]})
    return out


def item5() -> None:
    files = [
        "策略/WyckoffMaster/trading_core/fill.py",
        "策略/WyckoffMaster/trading_core/engine.py",
        "策略/WyckoffMaster/oi_resonance_v1/execution.py",
        "策略/WyckoffMaster/oi_resonance_v1/config.py",
        "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py",
        "策略/WyckoffMaster/event_trend_v0_1/config.py",
        "策略/WyckoffMaster/event_trend_v0_1/instrument_spec.py",
        "框架/backend/backtest_runner.py",
        "框架/backend/strategies/wx_execution.py",
        "框架/backend/paper_engine.py",
    ]
    strict_true = _grep("strict=True,", files)
    strict_default = _grep("strict: bool = False", files)
    adapters = _grep("SimulatedBarFillAdapter(", files)
    oi_entry = [x for x in _grep("run_portfolio_execution", files) if "(" in x["text"]]
    et_entry = [x for x in _grep("run_event_trend_backtest", files) if "(" in x["text"]]
    wx_round = [x for x in (_grep("def round_buy_price", files)
                            + _grep("def round_sell_price", files))
                if "trading_core/fill.py" not in x["file"]]
    record("E1", 5, "strict=True 的实际使用点",
           "PASS" if len(strict_true) == 1 else "UNKNOWN",
           "仅事件趋势适配器 1 处",
           [f"{x['file']}:{x['line']}" for x in strict_true],
           "grep 'strict=True' 于策略/与框架/backend/",
           "event_trend_v0_1/engine_adapter.py:438", "事件趋势回测（唯一 strict 路径）")
    record("E2", 5, "strict 缺省值 = False（构造点不传即为非严格）",
           "PASS" if strict_default else "FAIL",
           "fill.py 中 `strict: bool = False`",
           [f"{x['file']}:{x['line']}" for x in strict_default],
           "grep 'strict: bool = False'", "trading_core/fill.py:210",
           "所有未显式传 strict 的构造点")
    record("E3", 5, "SimulatedBarFillAdapter 构造点清单（谁在用哪一端）",
           "UNKNOWN", "应逐点标注 strict 取值",
           [f"{x['file']}:{x['line']} → {x['text']}" for x in adapters],
           "grep 'SimulatedBarFillAdapter('", "fill.py / execution.py / engine_adapter.py",
           "OI 回测（strict=False 缺省）、事件趋势回测（strict=True）、测试")
    record("E4", 5, "OI 共振生产回测入口（不受 strict / 规格闸门保护）",
           "UNKNOWN", "应知该路径无 tick 约束、无数量闸门",
           [f"{x['file']}:{x['line']}" for x in oi_entry],
           "grep 'run_portfolio_execution'", "oi_resonance_v1/execution.py:127；"
           "框架/backend/backtest_runner.py:948",
           "8787 的 OI 共振回测：price_step 缺省 0.0 ⇒ 无取整；"
           "instrument_specs 未传 ⇒ minQty/minNotional/maxQty 全部不生效")
    record("E5", 5, "事件趋势回测入口（strict=True + 规格闸门）",
           "PASS" if et_entry else "UNKNOWN",
           "唯一注入 instrument_spec 与 strict=True 的路径",
           [f"{x['file']}:{x['line']}" for x in et_entry],
           "grep 'run_event_trend_backtest'",
           "event_trend_v0_1/engine_adapter.py:341（定义）、runner.py:551-563（调用）",
           "事件趋势回测 / 6 个历史标的的机制验证")
    record("E6", 5, "第二套取整实现（float + 1e-9 偏置）仍在生产代码中",
           "FAIL" if wx_round else "PASS",
           "取整只应有 fill.py 一处实现",
           [f"{x['file']}:{x['line']}" for x in wx_round],
           "grep 'def round_buy_price' / 'def round_sell_price'",
           "框架/backend/strategies/wx_execution.py:1671,1678",
           "375 标的臂的实盘/纸面信号执行链（不经 trading_core）；"
           "其 `math.ceil(px/step - 1e-9)` 正是 fill.py 注释里指出的、"
           "在 100.03/0.01 这类边界上会多跳一档的旧写法",
           note="该实现 `price_step` 取自标的池字段，非写死 0.01；"
                "`_round_price_step` 还无条件 floor（多空不分）")

    # E7 纸面撮合是否经过 trading_core
    pe = open(os.path.join(ROOT, "框架/backend/paper_engine.py"), encoding="utf-8").read()
    has_tc = "trading_core" in pe
    record("E7", 5, "Paper 撮合是否经过 trading_core（strict 是否影响纸面）",
           "PASS" if not has_tc else "UNKNOWN",
           "Paper 撮合不经 trading_core ⇒ strict 对其无影响",
           f"paper_engine.py 含 'trading_core'：{has_tc}；"
           f"含 'price_step'：{'price_step' in pe}；含 'slippage'：{'slippage' in pe}",
           "grep trading_core / price_step / slippage 于 框架/backend/paper_engine.py",
           "框架/backend/paper_engine.py（全 1782 行）",
           "纸面成交 = 已闭合 15m open ± 0.0005 滑点，**无任何 tick 取整**；"
           "与 trading_core.fill 的取整口径不可比")

    # E8 六笔历史样本：读取侧不重算成交价
    six = os.path.join(ROOT, "evidence/Work_六笔账本接口页面一致性整改_2026-10-17")
    files6 = sorted(os.listdir(six)) if os.path.isdir(six) else []
    read_side = ["框架/backend/ledger_links.py", "框架/backend/backtest_metrics.py"]
    recompute_hits = []
    for rel in read_side:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        txt = open(p, encoding="utf-8", errors="replace").read()
        for kw in ("price_step", "round_buy_price", "round_sell_price",
                   "quote_exit", "quote_entry", "tickSize"):
            if kw in txt:
                recompute_hits.append(f"{rel}:{kw}")
    record("E8", 5, "六笔修复是否重算成交价（strict 是否影响历史六笔数值）",
           "PASS" if (files6 and not recompute_hits) else "UNKNOWN",
           "读取侧只做联结/口径修正，不重算 fill ⇒ 历史成交价保持旧 tick 结果",
           {"six_dir_entries": files6[:12],
            "read_side_recompute_hits": recompute_hits},
           "ls evidence/Work_六笔账本接口页面一致性整改_2026-10-17；"
           "grep price_step/round_*/quote_* 于 ledger_links.py、backtest_metrics.py",
           "框架/backend/ledger_links.py、backtest_metrics.py（读取侧）",
           "六笔的成交价仍是旧 price_step=0.01 的产物；本次精度修复**不会**"
           "回溯修正其数值 ⇒ 任何「修复后历史收益」结论都不成立")


# ══════════════════════════ 6. 六个历史标的：规格适用性（机制验证前置条件）══════════════════════════

SIX_SYMBOLS = ["LPTUSDT", "OPUSDT", "ORDIUSDT", "ACEUSDT", "BNBUSDT", "MOVRUSDT"]


def item6() -> None:
    """六个历史样本标的只做**机制**验证。

    ⚠ 结论限定：这些标的的**历史规格未验证**（applicability != verified，
    formal_pass_allowed=False）。本节只证明「给定该 tick 时取整方向正确、成交价落在 tick 网格上」，
    **不输出任何修复后的历史收益结论**。
    """
    from WyckoffMaster.event_trend_v0_1.instrument_spec import resolve_instrument_spec
    ws, we = 1767225600000, 1782864000000        # 2026-01-01 ~ 2026-07-01（仅作窗口声明）
    rows = []
    all_on_grid = True
    all_unverified = True
    for sym in SIX_SYMBOLS:
        spec = resolve_instrument_spec(sym, window_start_ms=ws, window_end_ms=we)
        tick = Decimal(spec.price_step)
        core = _core(strict=True, price_step=spec.price_step, stop=99.0,
                     specs={sym: spec}, exec_over={"initial_equity": 10000.0})
        res = _run_core(core, [100.0] * 6, sym=sym)
        tr = res["trades"][0] if res["trades"] else None
        entry = tr["entry_price"] if tr else None
        exit_ = tr["exit_price"] if tr else None
        on_grid = True
        for v in (entry, exit_):
            if v is None:
                on_grid = False
                continue
            if (Decimal(str(v)) % tick) != 0:
                on_grid = False
        all_on_grid = all_on_grid and on_grid
        all_unverified = all_unverified and (spec.formal_pass_allowed is False)
        rows.append({
            "symbol": sym, "price_step": spec.price_step, "qty_step": spec.qty_step,
            "min_qty": spec.min_qty, "max_qty": spec.max_qty,
            "min_notional": spec.min_notional,
            "applicability": spec.applicability,
            "formal_pass_allowed": spec.formal_pass_allowed,
            "evidence_sha256_8": (spec.evidence_sha256 or "")[:8],
            "mechanism_entry": entry, "mechanism_exit": exit_,
            "on_tick_grid": on_grid, "run_valid": res["precision"]["run_valid"],
            "qty": tr["qty"] if tr else None,
        })
    record("F1", 6, "六个历史标的：历史规格一律 unverified，formal_pass_allowed 恒 False",
           "PASS" if all_unverified else "FAIL",
           "全部 applicability != 'verified' 且 formal_pass_allowed=False",
           rows, "resolve_instrument_spec(每个标的, window 2026-01-01~2026-07-01)",
           "event_trend_v0_1/instrument_spec.py:381-404",
           "六笔修复 / 6 个历史标的的任何结论：只能报「机制可复现」，"
           "**不得**报「修复后历史收益已修正」")
    record("F2", 6, "六个历史标的：给定真实 tick 时成交价落在 tick 网格上（机制验证）",
           "PASS" if all_on_grid else "FAIL",
           "entry/exit 均为该标的 tick 的整数倍",
           [{r["symbol"]: (r["mechanism_entry"], r["mechanism_exit"],
                           r["price_step"], r["on_tick_grid"])} for r in rows],
           "同上（价格固定 100.0，仅验机制，不代表历史价位）",
           "trading_core/fill.py:125-130",
           "只证明取整方向/网格正确；**不是**历史收益结论")
    record("F3", 6, "六个历史标的的历史收益是否可下结论",
           "UNKNOWN", "规格未验证 ⇒ 不可下结论",
           "历史规格未验证（unverified_historical），本次仅机制验证；"
           "未输出任何修复后历史收益数值",
           "本脚本不计算历史收益", "—",
           "交付口径约束")


# ══════════════════════════ 汇总 ══════════════════════════

def summarize() -> Dict[str, Any]:
    items = {}
    for it in (1, 2, 3, 4, 5, 6):
        rs = [r for r in RESULTS if r["item"] == it]
        st = [r["status"] for r in rs]
        if "FAIL" in st:
            verdict = "失败"
        elif st and all(s == "PASS" for s in st):
            verdict = "通过"
        else:
            verdict = "未知"
        items[it] = {
            "verdict": verdict,
            "counts": {k: st.count(k) for k in ("PASS", "FAIL", "UNKNOWN")},
            "cases": [r["id"] for r in rs],
        }
    return {
        "task": "PRECISION-02",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "items": items,
        "results": RESULTS,
    }


def main() -> int:
    for fn in (item1, item2, item3, item4, item5, item6):
        try:
            fn()
        except Exception:
            record(f"X-{fn.__name__}", 0, f"{fn.__name__} 执行异常", "FAIL",
                   "用例应可独立执行", traceback.format_exc()[-800:],
                   fn.__name__, "-", "审核脚本自身")
    out = summarize()
    path = os.path.join(os.path.dirname(__file__), "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print("=" * 78)
    for it, v in out["items"].items():
        print(f"项 {it}: {v['verdict']}  {v['counts']}")
    print("=" * 78)
    for r in out["results"]:
        mark = {"PASS": "通过", "FAIL": "失败", "UNKNOWN": "未知"}[r["status"]]
        print(f"[{mark}] {r['id']:>4} (项{r['item']}) {r['title']}")
        if r["status"] != "PASS":
            print(f"        期望: {r['expected']}")
            print(f"        实际: {r['actual']}")
    print("=" * 78)
    print("written:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
