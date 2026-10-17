#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-05 工具：现有精度保护的**独立探针**（不依赖项目自带测试的断言）。

探针分五组：
  A 价格闸门        —— 非法/零/负/NaN/Inf 原价是否被拒
  B 步长处理        —— 步长非法是否被拒；步长「合法但不合理」是否被放行
  C 四笔异常复现    —— 用当前代码 + 当时的 bar open + 0.01 能否逐位重现记录成交价
  D 规格闸门        —— 无规格且无显式步长时，回测入口是否拒绝
  E 历史规格判定    —— 六标的按各自窗口解析，formal_pass_allowed 是否保持 False
  F 数量闸门        —— 步长/最小/最大/最小名义额逐条校验

只用公开入口，不改任何文件。
用法：python3 工具_保护机制探针.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "策略"))

import pandas as pd  # noqa: E402

from WyckoffMaster.trading_core.fill import (FillRejected,  # noqa: E402
                                             SimulatedBarFillAdapter)
from WyckoffMaster.event_trend_v0_1.engine_adapter import (  # noqa: E402
    run_event_trend_backtest)
from WyckoffMaster.event_trend_v0_1.instrument_spec import (  # noqa: E402
    APPLICABILITY_VERIFIED, resolve_instrument_spec)

OUT_JSON = os.path.join(HERE, "保护机制探针结果.json")

# 两个适配器必须分开测：
#   LEGACY = strict=False（当前代码的默认值，也是 2026-10-17 之前唯一的行为）
#   STRICT = strict=True（当前生产回测入口 engine_adapter.py:434-439 实际使用的）
LEGACY = SimulatedBarFillAdapter(slippage_bps=3.0, maker_fee_rate=2e-4,
                                 taker_fee_rate=5e-4)
STRICT = SimulatedBarFillAdapter(slippage_bps=3.0, maker_fee_rate=2e-4,
                                 taker_fee_rate=5e-4, strict=True)
MODES = [("legacy(strict=False)", LEGACY), ("strict(strict=True)", STRICT)]

# 六标的的 run 窗口（取自各 run 的 inputs.json → data_summary.time_range_1h）
WINDOWS = {
    "SPACEUSDT":    ("2026-01-31T20:00:00Z", "2026-07-30T19:00:00Z"),
    "AIGENSYNUSDT": ("2026-05-07T23:00:00Z", "2026-06-30T06:00:00Z"),
    "TRUTHUSDT":    ("2025-10-09T20:00:00Z", "2026-04-07T20:00:00Z"),
    "COTIUSDT":     ("2024-01-01T00:00:00Z", "2024-06-30T00:00:00Z"),
    "BNBUSDT":      ("2024-01-01T00:00:00Z", "2024-06-30T00:00:00Z"),
    "ORDIUSDT":     ("2024-01-01T00:00:00Z", "2024-06-30T00:00:00Z"),
}

# 四笔越界成交（来自 四笔成交逐笔核验.json 的 bar_open / 记录成交价）
ANOMALIES = [
    {"symbol": "SPACEUSDT", "leg": "entry", "side": "BUY", "bar_open": 0.009718, "recorded": 0.01},
    {"symbol": "SPACEUSDT", "leg": "exit", "side": "SELL", "bar_open": 0.007033, "recorded": 0.0},
    {"symbol": "AIGENSYNUSDT", "leg": "entry", "side": "BUY", "bar_open": 0.02125, "recorded": 0.03},
    {"symbol": "AIGENSYNUSDT", "leg": "exit", "side": "SELL", "bar_open": 0.01957, "recorded": 0.01},
    {"symbol": "TRUTHUSDT", "leg": "entry", "side": "BUY", "bar_open": 0.015693, "recorded": 0.02},
    {"symbol": "TRUTHUSDT", "leg": "exit", "side": "SELL", "bar_open": 0.013363, "recorded": 0.01},
    {"symbol": "COTIUSDT", "leg": "entry", "side": "BUY", "bar_open": 0.06228, "recorded": 0.07},
    {"symbol": "COTIUSDT", "leg": "exit", "side": "SELL", "bar_open": 0.08624, "recorded": 0.08},
]

results = {"generated_utc": None, "groups": {}}


def rec(group, name, **kw):
    results["groups"].setdefault(group, []).append({"probe": name, **kw})


def expect_reject(group, name, fn):
    try:
        out = fn()
        rec(group, name, expected="reject", got="accepted", result=_short(out),
            pass_=False)
        return False
    except FillRejected as e:
        rec(group, name, expected="reject", got="FillRejected",
            reason=getattr(e, "reason", str(e)), detail=_short(getattr(e, "detail", None)),
            pass_=True)
        return True
    except Exception as e:  # noqa: BLE001
        rec(group, name, expected="reject", got=type(e).__name__, reason=str(e),
            pass_=True, note="非 FillRejected 的异常也算拒绝，但类型不同，需人工确认")
        return True


def _short(x):
    try:
        s = repr(x)
    except Exception:  # noqa: BLE001
        s = "<unrepr>"
    return s[:300]


def probe_price_guards():
    for mode, ad in MODES:
        G = "A_价格闸门_" + mode
        for label, raw in [("raw_price=0", 0.0), ("raw_price=-1", -1.0),
                           ("raw_price=nan", float("nan")),
                           ("raw_price=inf", float("inf")),
                           ("raw_price=None", None), ("raw_price='abc'", "abc")]:
            expect_reject(G, label, lambda raw=raw, ad=ad: ad.quote_entry(
                raw_price=raw, direction=1, price_step=0.01, stop_price=99.0))
        for label, raw in [("raw_price=0", 0.0), ("raw_price=nan", float("nan"))]:
            expect_reject(G, "exit " + label, lambda raw=raw, ad=ad: ad.quote_exit(
                target_px=raw, direction=1, price_step=0.01, reason="x"))


def probe_step_guards():
    for mode, ad in MODES:
        G = "B_步长处理_" + mode
        for label, step in [("price_step=0", 0.0), ("price_step=-0.01", -0.01),
                            ("price_step=nan", float("nan")),
                            ("price_step=inf", float("inf")),
                            ("price_step='abc'", "abc"), ("price_step=None", None)]:
            expect_reject(G, label, lambda step=step, ad=ad: ad.quote_entry(
                raw_price=100.0, direction=1, price_step=step, stop_price=99.0))

        # 「合法但不合理」的步长：这是本次要如实记录的点
        for label, px, step in [("0.004076 用 step=0.000001 买", 0.004076, "0.000001"),
                                ("0.004076 用 step=0.01 买", 0.004076, "0.01"),
                                ("0.004076 用 step=0.01 卖", 0.004076, "0.01"),
                                ("0.000001 用 step=0.01 卖", 0.000001, "0.01")]:
            is_buy = "买" in label
            try:
                if is_buy:
                    got = ad.quote_entry(raw_price=px, direction=1, price_step=step,
                                         stop_price=px * 0.9).entry_fill
                else:
                    got = ad.quote_exit(target_px=px, direction=1,
                                        price_step=step, reason="x")
                rec(G, label, expected="观察", got="accepted", fill_price=got,
                    step_used=str(step), pass_=None,
                    note="步长合法即放行；是否合理由调用方/规格闸门负责")
            except Exception as e:  # noqa: BLE001
                rec(G, label, expected="观察", got=type(e).__name__,
                    reason=getattr(e, "reason", str(e)), pass_=None)


def probe_anomaly_reproduction():
    G = "C_四笔异常复现"
    slip = 3.0 * 1e-4
    for a in ANOMALIES:
        is_buy = a["side"] == "BUY"
        tk = {"SPACEUSDT": "0.000001", "AIGENSYNUSDT": "0.00001",
              "TRUTHUSDT": "0.000001", "COTIUSDT": "0.00001"}[a["symbol"]]

        def run(ad, step):
            if is_buy:
                return ad.quote_entry(raw_price=a["bar_open"], direction=1,
                                      price_step=step,
                                      stop_price=a["bar_open"] * 0.9).entry_fill
            return ad.quote_exit(target_px=a["bar_open"], direction=1,
                                 price_step=step, reason="x")

        out = {"bar_open": a["bar_open"], "recorded": a["recorded"],
               "announced_tick": tk}
        for mode, ad in MODES:
            try:
                got = run(ad, "0.01")
                out[f"{mode}_step_0.01"] = got
            except Exception as e:  # noqa: BLE001
                out[f"{mode}_step_0.01"] = "REJECT:" + getattr(e, "reason", type(e).__name__)
            try:
                out[f"{mode}_tick"] = run(ad, tk)
            except Exception as e:  # noqa: BLE001
                out[f"{mode}_tick"] = "REJECT:" + getattr(e, "reason", type(e).__name__)
        out["reproduce_exact_legacy"] = (
            out.get("legacy(strict=False)_step_0.01") == a["recorded"])
        out["note"] = ("legacy 复现=当时的成交模型仍能算出同一价格；"
                       "strict 若报 REJECT 说明该笔会被新闸门拦下")
        rec(G, f'{a["symbol"]} {a["leg"]}', **out)

    # 零价能否被 strict 拦住
    for mode, ad in MODES:
        try:
            zero = ad.quote_exit(target_px=0.0068, direction=1,
                                 price_step="0.01", reason="stop")
            rec(G, f"{mode}：0.0068 按 0.01 卖出", fill_price=zero,
                produced_zero=(zero == 0.0), pass_=None,
                note="能产出 0 价 ⇒ 该模式下没有零价防线")
        except Exception as e:  # noqa: BLE001
            rec(G, f"{mode}：0.0068 按 0.01 卖出", got="REJECT",
                reason=getattr(e, "reason", type(e).__name__), pass_=None,
                note="被拒 ⇒ 该模式下零价有防线")


def probe_spec_gate():
    G = "D_规格闸门"
    df = pd.DataFrame()
    for label, kw in [("无 instrument_spec 且无 price_step", {}),
                      ("price_step='0'", {"price_step": "0"}),
                      ("price_step='-1'", {"price_step": "-1"}),
                      ("price_step='abc'", {"price_step": "abc"})]:
        try:
            run_event_trend_backtest(symbol="SPACEUSDT", df_1h=df, df_15m=df,
                                     candidates=[], funding_events=[], **kw)
            rec(G, label, expected="raise", got="returned", pass_=False)
        except Exception as e:  # noqa: BLE001
            rec(G, label, expected="raise", got=type(e).__name__, reason=str(e),
                pass_="instrument_spec_required" in str(e) or "price_step" in str(e))

    # 显式给 0.01 会怎样：应放行但标 declared_no_spec
    try:
        run_event_trend_backtest(symbol="SPACEUSDT", df_1h=pd.DataFrame(),
                                 df_15m=pd.DataFrame(), candidates=[],
                                 funding_events=[], price_step="0.01")
        rec(G, "显式 price_step='0.01'（无规格）", expected="放行但降级",
            got="returned", pass_=None)
    except Exception as e:  # noqa: BLE001
        rec(G, "显式 price_step='0.01'（无规格）", expected="放行但降级",
            got=type(e).__name__, reason=str(e),
            pass_=None, note="若在此处就报错，说明还有更前置的校验")


def probe_history_verdict():
    G = "E_历史规格判定"
    for sym, (ws, we) in WINDOWS.items():
        ws_ms = _ms(ws)
        we_ms = _ms(we)
        sp = resolve_instrument_spec(sym, window_start_ms=ws_ms, window_end_ms=we_ms,
                                     order_type="MARKET")
        rec(G, sym,
            window=[ws, we],
            applicability=sp.applicability,
            formal_pass_allowed=bool(sp.formal_pass_allowed),
            blocking=bool(sp.blocking),
            price_step=sp.price_step, qty_step=sp.qty_step,
            applicability_note=(sp.applicability_note or "")[:220],
            verdict_ok=(sp.applicability != APPLICABILITY_VERIFIED
                        and sp.formal_pass_allowed is False))


def probe_qty_gate():
    G = "F_数量闸门"
    sp = resolve_instrument_spec("SPACEUSDT",
                                 window_start_ms=_ms("2026-01-31T20:00:00Z"),
                                 window_end_ms=_ms("2026-07-30T19:00:00Z"),
                                 order_type="MARKET")
    for label, qty, price in [("qty=1.5（非整步长，price=100）", "1.5", "100"),
                              ("qty=0（price=100）", "0", "100"),
                              ("qty=-1（price=100）", "-1", "100"),
                              ("qty=999999999999（超 max，price=100）", "999999999999", "100"),
                              ("qty=1（整步长，price=100 ⇒ 名义额 100）", "1", "100"),
                              ("qty=1（整步长，price=0.01 ⇒ 名义额 0.01）", "1", "0.01")]:
        try:
            r = sp.check_quantity(qty, price=price)
            rec(G, label, ok=bool(r.get("ok")),
                violations=[v.get("filter") for v in r.get("violations") or []],
                pass_=None)
        except Exception as e:  # noqa: BLE001
            rec(G, label, ok=None, got=type(e).__name__, reason=str(e), pass_=None)


def _ms(iso):
    import datetime
    return int(datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=datetime.UTC).timestamp() * 1000)


def main():
    import time
    results["generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for fn in (probe_price_guards, probe_step_guards, probe_anomaly_reproduction,
               probe_spec_gate, probe_history_verdict, probe_qty_gate):
        try:
            fn()
        except Exception:  # noqa: BLE001
            rec(fn.__name__, "PROBE_CRASHED", got="exception",
                traceback=traceback.format_exc()[-800:], pass_=False)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    for g, items in results["groups"].items():
        print("=" * 90)
        print(g)
        for it in items:
            flag = {True: "PASS", False: "FAIL", None: "INFO"}[it.get("pass_")]
            print(f'  [{flag}] {it["probe"]}')
            for k, v in it.items():
                if k in ("probe", "pass_"):
                    continue
                print(f'        {k} = {_short(v)}')
    print("\n写出:", OUT_JSON)


if __name__ == "__main__":
    main()
