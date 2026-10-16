# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 共用执行核心的**验收套件**（纯合成，不读任何真实/Holdout 数据）。

覆盖任务二验收重点：
  E1  方向不被改写（short 显式拒单；非法方向/模式拒绝）
  E2  参数不被默认值覆盖（冻结键覆盖拒绝；视图缺失只降级不替换）
  E3  缺结构低点**不**退回固定止损（structural 模式必拒；uniform 才允许）
  E4  入场 15 分钟因果时钟（+15m、pending、穿底、bar 晚于尝试时钟）
  E5  退出事件顺序（同根 15m 固定 4 相位）
  E6  台阶止损生效时间（下一根 15m 生效、单调不降、不回填）
  E7  720 小时到期（= 入场+720h 后**首根 15m 开盘**，不是最后收盘）
  E8  回撤锁定（20% 四动作；缺权益停算）
  E9  资金费结算（窗口/正负/幂等/缺口）
  E10 确定性（同输入同输出）
  E11 追加未来数据不改变过去决定（前缀逐位一致）
  E12 旧仓维护与新开仓开关**分开**控制
  E13 共享 5000U / 最多 10 仓 / 只多 约束

运行：`python -m WyckoffMaster.test_execution [--out json]`（从 `策略/` 目录）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys

from . import execution as X
from . import core
from .params import FROZEN

HOUR = 3_600_000
M15 = 900_000
# 与整点对齐的基准时刻（1h 与 15m 网格同时对齐）
T0 = 1_699_999_200_000


# ══════════════════════════ 合成数据 ══════════════════════════

def hour_bars(n_box=40, n_after=6, box_hi=100.3, box_lo=99.7, brk_close=101.2):
    """箱体 → 突破 的 1h 序列（整点对齐）。返回 (bars, breakout_index)。"""
    bars = []
    for i in range(n_box):
        bars.append({"open_time": T0 + i * HOUR, "close_time": T0 + i * HOUR + HOUR - 1,
                     "open": 100.0, "high": box_hi, "low": box_lo, "close": 100.1,
                     "volume": 100.0, "quote_volume": 100.0 * 100.1})
    bi = n_box
    bars.append({"open_time": T0 + bi * HOUR, "close_time": T0 + bi * HOUR + HOUR - 1,
                 "open": 100.5, "high": brk_close + 0.3, "low": 100.4, "close": brk_close,
                 "volume": 500.0, "quote_volume": 500.0 * brk_close})
    for k in range(1, n_after + 1):
        i = bi + k
        o = brk_close + 0.05 * k
        bars.append({"open_time": T0 + i * HOUR, "close_time": T0 + i * HOUR + HOUR - 1,
                     "open": o, "high": o + 0.4, "low": o - 0.3, "close": o + 0.1,
                     "volume": 200.0, "quote_volume": 200.0 * o})
    return bars, bi


def bars_15m_from_hours(hours, *, quote=2_000_000.0, override=None):
    """把 1h bar 展开为 4 根 15m bar（OHLC 与小时一致，保证网格对齐）。"""
    out = []
    for h in hours:
        for k in range(4):
            ot = int(h["open_time"]) + k * M15
            row = {"open_time": ot, "close_time": ot + M15 - 1,
                   "open": float(h["open"]), "high": float(h["high"]),
                   "low": float(h["low"]), "close": float(h["close"]),
                   "volume": float(h["volume"]) / 4.0, "quote_volume": quote / 4.0}
            if override:
                override(row, h, k)
            out.append(row)
    return out


def flat_15m(start_ms, n, price, *, step=0.0):
    """从 start_ms 起 n 根价格恒定的 15m bar。"""
    out = []
    for i in range(n):
        ot = start_ms + i * M15
        p = price + i * step
        out.append({"open_time": ot, "close_time": ot + M15 - 1, "open": p, "high": p,
                    "low": p, "close": p, "volume": 100.0, "quote_volume": 2_000_000.0})
    return out


LIMITS = {"quantity_step": 0.0, "min_quantity": 0.0, "max_quantity": 0.0,
          "min_notional": 0.0, "price_step": 0.0}


def _mismatch(a, b):
    """比较两个决定列表（逐位 JSON 相等）。"""
    if len(a) != len(b):
        return f"len {len(a)} != {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        if json.dumps(x, sort_keys=True, default=str) != json.dumps(y, sort_keys=True, default=str):
            return f"index {i} differs: {x.get('kind')} vs {y.get('kind')}"
    return None


# ══════════════════════════ E1 方向不被改写 ══════════════════════════

def e1_direction():
    cases = []
    # short 必须显式拒单，绝不翻转成 long
    d = X.plan_entry(symbol="AAAUSDT", direction="short", signal_kind="base",
                     signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                     open_15m=100.0, atr_val=1.0, struct_low=99.0, stop_mode="structural",
                     limits=LIMITS, equity=5000.0)
    cases.append({"name": "short_not_rewritten_to_long",
                  "reject": d["reject_reason"], "direction": d["direction"],
                  "passed": d["reject_reason"] == "short_disabled_by_frozen_view_only_long"
                  and d["direction"] == "short"})
    # 非法方向
    d = X.plan_entry(symbol="AAAUSDT", direction="LONGISH", signal_kind="base",
                     signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                     open_15m=100.0, atr_val=1.0, limits=LIMITS, equity=5000.0)
    cases.append({"name": "illegal_direction_rejected", "reject": d["reject_reason"],
                  "passed": d["reject_reason"] == "illegal_direction:LONGISH"})
    # normalize 不默认 long
    cases.append({"name": "normalize_direction_no_default",
                  "passed": X.normalize_direction(None) is None
                  and X.normalize_direction("") is None
                  and X.normalize_direction("Long") == "long"
                  and X.normalize_direction("short") == "short"})
    # 非法模式
    errs = X.validate_modes(entry_mode="donchian_whatever", stop_mode="magic",
                            trail_mode="nope", rank_mode="bad", factor_filter="bad",
                            direction="long")
    cases.append({"name": "illegal_modes_rejected", "errors": errs,
                  "passed": len(errs) == 5})
    d = X.plan_entry(symbol="AAAUSDT", direction="long", signal_kind="base",
                     signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                     open_15m=100.0, atr_val=1.0, struct_low=99.0, stop_mode="structural_x",
                     limits=LIMITS, equity=5000.0)
    cases.append({"name": "illegal_stop_mode_rejected", "reject": d["reject_reason"],
                  "passed": d["reject_reason"] == "illegal_stop_mode:structural_x"})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


# ══════════════════════════ E2 参数不被默认值覆盖 ══════════════════════════

def e2_config():
    cases = []
    r = X.resolve_config({"max_positions_total": 20, "short_enabled": True})
    cases.append({"name": "frozen_override_rejected", "errors": r["errors"],
                  "passed": set(r["errors"]) == {"frozen_key_override_rejected:max_positions_total",
                                                 "frozen_key_override_rejected:short_enabled"}
                  and r["config"] is X.EXEC_FROZEN})
    r2 = X.resolve_config({"nope_key": 1})
    cases.append({"name": "unknown_key_rejected", "errors": r2["errors"],
                  "passed": r2["errors"] == ["unknown_config_key:nope_key"]})
    r3 = X.resolve_config({"max_positions_total": 10, "short_enabled": False})
    cases.append({"name": "same_value_ok", "passed": r3["errors"] == []})
    lc = X.load_exec_config("/definitely/not/here.json")
    cases.append({"name": "view_missing_degrades_not_replaces",
                  "config_source": lc["config_source"], "degraded": lc["degraded"],
                  "passed": lc["degraded"] is True and lc["config"] is X.EXEC_FROZEN
                  and "unreadable" in lc["config_source"]})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


# ══════════════════════════ E3 缺结构低点不退回固定止损 ══════════════════════════

def e3_struct_low_required():
    cases = []
    for mode in ("structural", "structural_m2b"):
        d = X.plan_entry(symbol="AAAUSDT", direction="long", signal_kind="spring",
                         signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                         open_15m=100.0, atr_val=1.0, struct_low=None, stop_mode=mode,
                         limits=LIMITS, equity=5000.0)
        cases.append({"name": f"{mode}_missing_struct_low_rejected",
                      "reject": d["reject_reason"],
                      "passed": d["reject_reason"] == "missing_struct_low_for_structural_stop"})
    # uniform（M0/M1）允许无结构低点
    d = X.plan_entry(symbol="AAAUSDT", direction="long", signal_kind="donchian_breakout",
                     signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                     open_15m=100.0, atr_val=1.0, struct_low=None, stop_mode="uniform",
                     entry_mode="donchian_only", limits=LIMITS, equity=5000.0)
    cases.append({"name": "uniform_allows_no_struct_low", "reject": d["reject_reason"],
                  "stop": d.get("stop_price"),
                  "passed": d.get("ok") is True
                  and abs(d["stop_price"] - (X.round_buy_price(100.0 * 1.0005, 0.0) - 3.0)) < 1e-9})
    # 结构低点非有限（NaN）同样拒绝
    d = X.plan_entry(symbol="AAAUSDT", direction="long", signal_kind="spring",
                     signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                     open_15m=100.0, atr_val=1.0, struct_low=float("nan"), stop_mode="structural",
                     limits=LIMITS, equity=5000.0)
    cases.append({"name": "nan_struct_low_rejected",
                  "passed": d["reject_reason"] == "missing_struct_low_for_structural_stop"})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


# ══════════════════════════ E4 入场 15m 因果时钟 ══════════════════════════

def e4_causal_clock():
    cases = []
    SIG_CLOSE = T0 + HOUR - 1                 # 真实 1h 信号收盘时刻
    EARLIEST = SIG_CLOSE + 1 + M15            # = T0 + HOUR + M15（对齐到 15m 网格）
    base = dict(symbol="AAAUSDT", direction="long", signal_kind="breakout", atr_val=1.0,
                struct_low=99.0, stop_mode="structural", limits=LIMITS, equity=5000.0)
    # 正常：earliest_fill = 收盘+1ms + 15m，且成交 bar 开盘恰为该时刻
    d = X.plan_entry(signal_bar_close_time=SIG_CLOSE, available_at=None, request_clock_ms=SIG_CLOSE,
                     open_15m=100.0, attempt_clock_ms=EARLIEST, **base)
    cases.append({"name": "earliest_fill_is_close_plus_15m",
                  "earliest_fill_ms": d["times"]["earliest_fill_ms"], "expected": EARLIEST,
                  "passed": d.get("ok") is True and d["times"]["earliest_fill_ms"] == EARLIEST})
    # 尝试时钟早于最早成交 ⇒ 挂单
    d = X.plan_entry(signal_bar_close_time=SIG_CLOSE, available_at=None, request_clock_ms=SIG_CLOSE,
                     open_15m=100.0, attempt_clock_ms=SIG_CLOSE + 1000, **base)
    cases.append({"name": "pending_when_attempt_before_earliest",
                  "reject": d["reject_reason"],
                  "passed": d["reject_reason"] == "pending_future_fill"})
    # available_at 晚于收盘 ⇒ order_request 取 available_at
    d = X.plan_entry(signal_bar_close_time=SIG_CLOSE, available_at=SIG_CLOSE + 10 * HOUR,
                     request_clock_ms=SIG_CLOSE, open_15m=100.0,
                     attempt_clock_ms=SIG_CLOSE + 11 * HOUR, **base)
    cases.append({"name": "order_request_uses_available_at",
                  "passed": d["times"]["order_request_ms"] == SIG_CLOSE + 10 * HOUR
                  and d["times"]["earliest_fill_ms"] == SIG_CLOSE + 10 * HOUR + M15})
    # 真实首次请求墙钟晚于信号 ⇒ 取请求墙钟
    d = X.plan_entry(signal_bar_close_time=SIG_CLOSE, available_at=None,
                     request_clock_ms=SIG_CLOSE + 5 * HOUR, open_15m=100.0,
                     attempt_clock_ms=SIG_CLOSE + 6 * HOUR, **base)
    cases.append({"name": "order_request_uses_request_clock",
                  "passed": d["times"]["order_request_ms"] == SIG_CLOSE + 5 * HOUR})
    # 穿底拒单（P_entry <= L_struct）
    d = X.plan_entry(signal_bar_close_time=SIG_CLOSE, available_at=None, request_clock_ms=SIG_CLOSE,
                     open_15m=98.0, attempt_clock_ms=EARLIEST, atr_val=1.0,
                     struct_low=99.0, stop_mode="structural", limits=LIMITS, equity=5000.0,
                     symbol="AAAUSDT", direction="long", signal_kind="spring")
    cases.append({"name": "entry_below_struct_low_rejected",
                  "reject": d["reject_reason"],
                  "passed": d["reject_reason"] == "entry_below_struct_low"})
    # 只用 open：同一 open、不同 low/close 不影响决定（接口只吃 open）
    import inspect
    sig = inspect.signature(X.plan_entry).parameters
    cases.append({"name": "entry_decision_uses_open_only",
                  "has_low_param": "low_15m" in sig or "bar_low" in sig,
                  "has_close_param": "close_15m" in sig,
                  "passed": "low_15m" not in sig and "bar_low" not in sig
                  and "close_15m" not in sig})
    # 成交价含不利滑点并向上取整
    d = X.plan_entry(signal_bar_close_time=SIG_CLOSE, available_at=None, request_clock_ms=SIG_CLOSE,
                     open_15m=100.0, attempt_clock_ms=EARLIEST, price_step=0.05, **base)
    cases.append({"name": "buy_price_adverse_slippage_and_ceil",
                  "entry_price": d.get("entry_price"),
                  "passed": d.get("ok") is True and d["entry_price"] >= 100.0 * 1.0005
                  and abs(d["entry_price"] % 0.05) < 1e-9})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


# ══════════════════════════ E5 退出事件顺序 ══════════════════════════

def e5_exit_order():
    ev = X.order_bar_events(
        funding=[{"position_id": 1}], expiries=[{"position_id": 2}],
        drawdown_liquidations=[{"position_id": 3}], scheduled_exits=[{"position_id": 4}],
        new_entries=[{"symbol": "A"}], intrabar_stops=[{"position_id": 5}])
    seq = [e["event"] for e in ev]
    expect = ["funding_settlement", "expiry", "drawdown_liquidation", "scheduled_exit",
              "new_entry", "intrabar_stop"]
    phases = [e["phase"] for e in ev]
    return {"passed": seq == expect and phases == [1, 2, 2, 2, 3, 4],
            "sequence": seq, "phases": phases,
            "declared_order": list(X.EXIT_EVENT_ORDER),
            "note": "①资金费 → ②到期/回撤清仓/预约出场 → ③新入场 → ④bar内止损"}


# ══════════════════════════ E6 台阶止损生效时间 ══════════════════════════

def e6_trail_effective_time():
    closes = [100.0 + i for i in range(11)]
    lows = [c - 0.5 for c in closes]
    close_t = [T0 + i * HOUR for i in range(len(closes))]
    steps, stop = [], 94.0
    for k in range(1, len(closes) + 1):
        eff = X.next_15m_open_at_or_after(close_t[k - 1] + 1)
        res = X.apply_trail(closes=closes[:k], lows=lows[:k], current_stop=stop,
                            entry_price=100.0, entry_atr=2.0,
                            evaluate_close_time=close_t[k - 1], next_15m_open_time=eff)
        steps.append({"k": k, "stop_after": res["stop"], "updated": res["updated"],
                      "effective_from_ms": res["effective_from_ms"], "close_time": close_t[k - 1]})
        stop = res["stop"]
    monotone = all(s["stop_after"] >= (steps[i - 1]["stop_after"] if i else 94.0) - 1e-12
                   for i, s in enumerate(steps))
    next_15m = all(s["effective_from_ms"] > s["close_time"] for s in steps)
    return {"passed": monotone and next_15m,
            "monotone_non_decreasing": monotone, "effective_next_15m": next_15m,
            "steps": steps}


# ══════════════════════════ E7 720 小时到期 ══════════════════════════

def e7_expiry():
    entry_ts = T0
    due = X.expiry_exit_ms(entry_ts, 720)
    # 到期 = 入场 + 720h 之后首根 15m 开盘（对齐后即 T0+720h）
    aligned = (T0 + 720 * HOUR) % M15 == 0
    bars = flat_15m(T0 + 719 * HOUR, 8, 100.0)      # 覆盖到期前后
    r = X.expiry_exit_from_bars(entry_ts, bars, 720)
    first_open_ge_due = None
    for b in bars:
        if int(b["open_time"]) >= due:
            first_open_ge_due = int(b["open_time"])
            break
    # 反例：若按「最后一根已闭合 bar 的收盘」会得到不同时刻
    last_close = int(bars[-1]["close_time"])
    return {"passed": aligned and r["ok"] and r["bar_open_time"] == due
            and r["bar_open_time"] == first_open_ge_due and r["bar_open_time"] != last_close,
            "due_ms": due, "exit_bar_open_time": r.get("bar_open_time"),
            "last_bar_close_time": last_close, "aligned_to_15m": aligned,
            "note": "到期取『入场+720h 后首根 15m 开盘』；非『最后一根已闭合 bar 收盘』"}


# ══════════════════════════ E8 回撤锁定 ══════════════════════════

def e8_drawdown():
    cases = []
    st = X.drawdown_state(equity=7900.0, peak_equity=10000.0)
    cases.append({"name": "trip_at_21pct", "drawdown": st["drawdown"], "actions": st["actions"],
                  "passed": st["tripped_now"] and st["locked"]
                  and st["actions"] == ["disable_new_entries", "cancel_pending_orders",
                                        "liquidate_all_next_15m_open", "lock_for_run_no_resume"]})
    st = X.drawdown_state(equity=8100.0, peak_equity=10000.0)
    cases.append({"name": "no_trip_at_19pct",
                  "passed": (not st["tripped_now"]) and (not st["locked"]) and st["actions"] == []})
    st = X.drawdown_state(equity=7900.0, peak_equity=10000.0, locked_before=True)
    cases.append({"name": "already_locked_no_repeat_actions",
                  "passed": st["locked"] and st["actions"] == [] and st["tripped_now"]})
    st = X.drawdown_state(equity=None, peak_equity=10000.0)
    cases.append({"name": "uncomputable_stops_calc",
                  "passed": st["computable"] is False and st["actions"] == []
                  and st["drawdown"] is None})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


# ══════════════════════════ E9 资金费结算 ══════════════════════════

def e9_funding():
    events = [
        {"funding_time": T0 + 8 * HOUR, "funding_rate": 0.0001, "mark_price": 100.0},
        {"funding_time": T0 + 16 * HOUR, "funding_rate": -0.0002, "mark_price": 101.0},
        {"funding_time": T0 + 24 * HOUR, "funding_rate": None, "mark_price": 102.0},   # 缺 rate
        {"funding_time": T0 + 32 * HOUR, "funding_rate": 0.0003, "mark_price": 103.0},  # 窗外
    ]
    r = X.settle_funding_events(position_id=7, qty=10.0, entry_ts=T0, exit_ts=T0 + 20 * HOUR,
                                events=events)
    amts = [c["amount"] for c in r["charges"]]
    ok = (len(r["charges"]) == 2
          and abs(amts[0] - 10.0 * 100.0 * 0.0001) < 1e-12          # 支出 +
          and abs(amts[1] - 10.0 * 101.0 * -0.0002) < 1e-12         # 收入 −（负费率保留）
          and r["n_gaps"] == 0
          and abs(r["total_expense"] - (amts[0] + amts[1])) < 1e-12)
    r2 = X.settle_funding_events(position_id=7, qty=10.0, entry_ts=T0, exit_ts=T0 + 30 * HOUR,
                                 events=events)
    ok2 = (len(r2["charges"]) == 2 and r2["n_gaps"] == 1
           and r2["gaps"][0]["reason"] == "missing_real_rate_or_mark"
           and r2["gaps"][0]["funding_time"] == T0 + 24 * HOUR)
    # 幂等：事件身份唯一
    ident = {c["event_identity"] for c in r["charges"]}
    ok3 = len(ident) == len(r["charges"]) and all(i.startswith("funding|7|") for i in ident)
    return {"passed": ok and ok2 and ok3, "charges": r["charges"], "gaps": r2["gaps"],
            "total_expense": r["total_expense"], "forbidden_substitutes": r["forbidden_substitutes"],
            "note": "窗口 (entry,exit]；正负原样（负费率收入保留）；缺 rate/mark 记缺口不补 0"}


# ══════════════════════════ E10–E12 引擎：确定性/前缀/开关分离 ══════════════════════════

def _engine(*, allow=True, maintain=True, bars15=None, bars1h=None, funding=None,
            seed=None, limits=None):
    hours, bi = hour_bars()
    b15 = bars15 if bars15 is not None else bars_15m_from_hours(hours)
    eng = X.ReplayEngine(bars_signal=bars1h if bars1h is not None else hours,
                         bars_15m=b15, entry_mode="donchian_only", stop_mode="structural",
                         allow_new_entries=allow, maintain_existing=maintain,
                         funding_events_by_symbol=funding or [],
                         contract_spec=limits or LIMITS)
    if seed:
        eng.seed_position(**seed)
    return eng


def e10_determinism():
    r1 = _engine().run()
    r2 = _engine().run()
    diff = _mismatch(r1["decisions"], r2["decisions"])
    return {"passed": diff is None and r1["cash"] == r2["cash"],
            "n_decisions": len(r1["decisions"]), "diff": diff,
            "cash": r1["cash"], "note": "同输入同配置 ⇒ 逐位相同决定与相同现金"}


def e11_append_safety():
    hours, bi = hour_bars(n_after=8)
    b15 = bars_15m_from_hours(hours)
    cut = int(b15[int(len(b15) * 0.9)]["close_time"])   # 截止点放在突破入场之后
    full = _engine(bars15=b15, bars1h=hours).run()
    prefix = _engine(bars15=[b for b in b15 if int(b["close_time"]) <= cut],
                     bars1h=[h for h in hours if int(h["close_time"]) <= cut]).run()
    past_full = [d for d in full["decisions"]
                 if int((d.get("times") or {}).get("bar_close_time")
                        or (d.get("times") or {}).get("signal_bar_close_time") or 0) <= cut]
    diff = _mismatch(past_full, prefix["decisions"])
    return {"passed": diff is None and len(past_full) > 0, "cut_ms": cut,
            "n_past_full": len(past_full), "n_prefix": len(prefix["decisions"]),
            "diff": diff,
            "note": "追加未来数据后，截止时刻之前的决定逐位不变"}


def e12_switch_separation():
    # ① 新开仓关 + 旧仓维护开：既有持仓仍被维护（到期出场）
    entry_ts = T0
    bars = flat_15m(entry_ts + 719 * HOUR, 6, 100.0)
    eng = _engine(allow=False, maintain=True, bars15=bars, bars1h=[],
                  seed=dict(qty=5.0, entry_price=100.0, entry_ts=entry_ts, stop_price=94.0))
    r = eng.run()
    expired = [d for d in r["decisions"] if d["kind"] == "exit"
               and d["exit_reason"] == "max_holding_720h_at_first_15m_open"]
    no_entry = not any(d["kind"] == "entry" and d.get("ok") for d in r["decisions"])
    ok1 = len(expired) == 1 and no_entry

    # ② 新开仓关 + 旧仓维护**关**：持仓不动（不维护），也不开新仓
    eng2 = _engine(allow=False, maintain=False, bars15=bars, bars1h=[],
                   seed=dict(qty=5.0, entry_price=100.0, entry_ts=entry_ts, stop_price=94.0))
    r2 = eng2.run()
    ok2 = len(r2["decisions"]) == 0 and r2["open_positions"] == 1

    # ③ 新开仓开 + 旧仓维护关：可开新仓，但不维护旧仓
    hours, _ = hour_bars()
    b15 = bars_15m_from_hours(hours)
    eng3 = _engine(allow=True, maintain=False, bars15=b15, bars1h=hours)
    r3 = eng3.run()
    opened = [d for d in r3["decisions"] if d["kind"] == "entry" and d.get("ok")]
    ok3 = len(opened) >= 1

    # ④ 新开仓关 + 旧仓维护开：不开新仓（正式候选未验收 ⇒ 继续阻断）
    eng4 = _engine(allow=False, maintain=True, bars15=b15, bars1h=hours)
    r4 = eng4.run()
    ok4 = not any(d["kind"] == "entry" and d.get("ok") for d in r4["decisions"])

    return {"passed": ok1 and ok2 and ok3 and ok4,
            "case_maintain_on_entry_off": {"expired": len(expired), "no_new_entry": no_entry},
            "case_maintain_off_entry_off": {"decisions": len(r2["decisions"]),
                                            "open_positions": r2["open_positions"]},
            "case_entry_on_maintain_off": {"opened": len(opened)},
            "case_entry_off_maintain_on": {"opened": 0},
            "note": "两个开关独立：维护旧仓不受新开仓闸影响；默认新开仓仍阻断"}


# ══════════════════════════ E13 冻结约束 ══════════════════════════

def e13_frozen_constraints():
    c = X.EXEC_FROZEN
    checks = {
        "shared_5000": c.initial_equity == 5000.0 and c.shared_account is True,
        "max_positions_10": c.max_positions_total == 10,
        "only_long": c.short_enabled is False,
        "risk_0.5pct": c.risk_per_trade_fraction == 0.005,
        "single_name_10pct": c.single_name_notional_cap == 0.10,
        "gross_80pct": c.gross_long_notional_cap == 0.80,
        "cash_reserve_20pct": c.cash_reserve_fraction == 0.20,
        "portfolio_risk_3pct": c.portfolio_stop_risk_cap == 0.03,
        "participation_1pct": c.entry_participation_prev15m_cap == 0.01,
        "fee_0.05pct": c.fee_each_side == 0.0005,
        "slippage_0.05pct": c.slippage_each_side == 0.0005,
        "holding_720h": c.max_holding_hours == 720,
        "dd_20pct": c.max_drawdown_fraction == 0.20,
    }
    # 11 仓时拒绝
    d = X.plan_entry(symbol="AAAUSDT", direction="long", signal_kind="breakout",
                     signal_bar_close_time=T0, available_at=T0, request_clock_ms=T0,
                     open_15m=100.0, atr_val=1.0, struct_low=99.0, limits=LIMITS,
                     equity=5000.0, open_positions=10, attempt_clock_ms=T0 + M15)
    checks["max_positions_total_reached"] = (
        d["reject_reason"] == "max_positions_total_reached")
    # 接口含约束声明
    iface = X.interface()
    fc = iface["frozen_constraints"]
    checks["interface_declares_constraints"] = (
        fc["shared_equity_usdt"] == 5000.0 and fc["max_positions_total"] == 10
        and fc["short_enabled"] is False and fc["legacy_four_accounts_preserved"] is True
        and fc["new_entry_default_blocked"] is True
        and fc["maintenance_independent_of_entry_switch"] is True)
    return {"passed": all(checks.values()), "checks": checks,
            "interface_versions": {"core": iface["core_version"],
                                   "strategy": iface["strategy_version"],
                                   "ledger": iface["ledger_model_version"],
                                   "config_hash": iface["config_hash"]}}


def e14_signal_core_stop_modes_fail_closed():
    """信号核心缺结构/非法模式拒绝；只有 uniform 可无结构低点。"""
    cases = []
    for mode in ("structural", "structural_m2b"):
        r = core.compute_stop(100.0, None, 1.0, stop_mode=mode)
        cases.append({"name": f"compute_stop_{mode}_missing_low",
                      "reject": r["reject"],
                      "passed": r["reject"] == "missing_struct_low_for_structural_stop"})
        r = core.evaluate_entry_fill(open_15m=100.0, struct_low=None, atr_val=1.0,
                                     stop_mode=mode)
        cases.append({"name": f"evaluate_fill_{mode}_missing_low",
                      "reject": r["reject"],
                      "passed": r["reject"] == "missing_struct_low_for_structural_stop"})

    for fn, args in (
        (core.compute_stop, (100.0, 99.0, 1.0)),
        (core.evaluate_entry_fill, ()),
    ):
        if fn is core.compute_stop:
            r = fn(*args, stop_mode="unapproved_mode")
        else:
            r = fn(open_15m=100.0, struct_low=99.0, atr_val=1.0,
                   stop_mode="unapproved_mode")
        cases.append({"name": f"{fn.__name__}_illegal_mode",
                      "reject": r["reject"],
                      "passed": r["reject"] == "illegal_stop_mode:unapproved_mode"})

    r = core.evaluate_entry_fill(open_15m=100.0, struct_low=None, atr_val=1.0,
                                 stop_mode="uniform")
    cases.append({"name": "uniform_mode_allows_missing_low",
                  "reject": r["reject"], "stop_dist": r["stop_dist"],
                  "passed": r["reject"] is None and r["stop_dist"] is not None})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


# ══════════════════════════ 主流程 ══════════════════════════

CHECKS = {
    "E1_direction_not_rewritten": e1_direction,
    "E2_config_not_overridden_by_defaults": e2_config,
    "E3_struct_low_required_not_fallback": e3_struct_low_required,
    "E4_entry_15m_causal_clock": e4_causal_clock,
    "E5_exit_event_order": e5_exit_order,
    "E6_trail_effective_next_15m": e6_trail_effective_time,
    "E7_expiry_720h_first_15m_open": e7_expiry,
    "E8_drawdown_lock": e8_drawdown,
    "E9_funding_settlement": e9_funding,
    "E10_determinism": e10_determinism,
    "E11_append_future_data_keeps_past": e11_append_safety,
    "E12_maintenance_vs_entry_switch": e12_switch_separation,
    "E13_frozen_constraints": e13_frozen_constraints,
    "E14_signal_core_stop_modes_fail_closed": e14_signal_core_stop_modes_fail_closed,
}


def run_all() -> dict:
    out = {}
    for name, fn in CHECKS.items():
        try:
            out[name] = fn()
        except Exception as exc:                                  # noqa: BLE001
            out[name] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"core_version": X.CORE_VERSION, "strategy_version": X.STRATEGY_VERSION,
            "all_passed": all(v.get("passed") for v in out.values()),
            "checks": out, "interface": X.interface(),
            "data_read_disclosure": "纯合成样本；未打开任何真实行情/Holdout 文件。"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    res = run_all()
    text = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if res["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
