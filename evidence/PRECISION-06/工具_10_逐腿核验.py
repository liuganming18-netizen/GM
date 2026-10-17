#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 10：逐腿核验 12 条成交腿 + 全量成交腿扫描。

做什么
──────
1. 对每个 run 读 `trades.jsonl` / `orders.jsonl` / `fills.jsonl` / `chart.json`，
   把每笔成交拆成「入场腿 / 出场腿」两条腿；
2. 逐腿还原 **运行时传给撮合器的输入**（raw_price、direction、price_step、reason），
   并用**当前** `SimulatedBarFillAdapter` 以 strict=True / strict=False 双模式重放；
3. 逐条列出 strict 的 8 条候选规则（5 条已实现 + 3 条未实现）是否触发；
4. 把「原价 → 滑点后 → 取整后」三段拆开，量化滑点与取整各自贡献；
5. 用 P04 时间线里**离成交时刻最近的历史 tick 观测**做反事实对照
   （只作灵敏度参考，不构成历史规格证据）。

边界
────
· `price_step` 在 run 产物里**没有记录**（`inputs.json → params` 49 个键里无 price_step /
  tick_size / instrument_spec）。本工具按 `0.01` 重放并在每条腿上标注
  `price_step_basis = "arithmetic_inferred"`，不冒充记录值。
· 只读 run 产物，不写回任何 run 目录。

用法
────
  python3 工具_10_逐腿核验.py                # 6 个 P05 run（12 腿）+ 全量扫描
  python3 工具_10_逐腿核验.py --no-scan      # 只做 6 个 run
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUNS_DIR = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")
P04 = os.path.join(ROOT, "evidence", "PRECISION-04")

sys.path.insert(0, os.path.join(ROOT, "策略"))
from WyckoffMaster.trading_core.fill import (  # noqa: E402
    FillRejected, SimulatedBarFillAdapter, price_breakdown,
    round_buy_price, round_sell_price,
)

# PRECISION-05 圈定的 6 个 run（12 条腿）
SCOPE6 = [
    "run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451",
    "run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703",
    "run_WX_EVENT_TREND_V0_1_TRUTHUSDT_1792174465",
    "run_WX_EVENT_TREND_V0_1_COTIUSDT_1792174145",
    "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973",
    "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568",
]

STEP_USED = "0.01"          # P05 假设值；逐腿再用算术反推，见 step_set
STEP_BASIS = "arithmetic_inferred"
# 逐腿反推候选步长（粗 → 细）。0.01 是「历史默认值」这一假设的待检项。
STEP_CANDIDATES = ["0.01", "0.001", "0.0001", "0.00001", "0.000001", "0.0000001"]

# strict 的规则清单：5 条已实现 + 3 条「看起来该有但没有」
RULES = [
    ("R1", "raw_price 有限", "fill.py::_guard → _dec() 抛 InvalidOperation ⇒ "
                             "FillRejected('non_finite_or_invalid_price')", True),
    ("R2", "raw_price > 0", "fill.py::_guard ⇒ FillRejected('nonpositive_raw_price')", True),
    ("R3", "price_step 有限", "fill.py::_guard ⇒ FillRejected('non_finite_or_invalid_price_step')", True),
    ("R4", "price_step > 0", "fill.py::_guard ⇒ FillRejected('invalid_price_step_nonpositive')", True),
    ("R5", "取整后价格有限且 > 0", "fill.py::quote_entry/quote_exit ⇒ "
                                  "FillRejected('nonpositive_or_nonfinite_*_fill')", True),
    ("R6", "price_step ≤ raw_price（步长不得粗于价格本身）", "**未实现**", False),
    ("R7", "成交价 ∈ 该 bar [low, high]", "**未实现**（fill.py 只拿到 raw_price，拿不到 bar）", False),
    ("R8", "price_step == instrument_spec.tick（规格交叉校验）", "**未实现**（只信调用方传值）", False),
]


# ══════════════════════════ 基础读取 ══════════════════════════

def jl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def load_bars(chart_path):
    with open(chart_path, encoding="utf-8") as f:
        doc = json.load(f)
    bars = {}
    for c in doc.get("candles") or []:
        if isinstance(c, dict):          # 少数 run 落盘为对象形式（t/o/h/l/c/v 或 time/open/...）
            g = lambda *ks: next(c[k] for k in ks if k in c)
            ts = int(g("time", "timestamp", "t"))
            bars[ts] = {"open": float(g("open", "o")), "high": float(g("high", "h")),
                        "low": float(g("low", "l")), "close": float(g("close", "c")),
                        "volume": float(g("volume", "v")) if ("volume" in c or "v" in c) else None}
            continue
        bars[int(c[0])] = {"open": float(c[1]), "high": float(c[2]),
                           "low": float(c[3]), "close": float(c[4]),
                           "volume": float(c[5]) if len(c) > 5 else None}
    return doc, bars


def utc(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000.0))


def load_tick_observations():
    """从 P04 时间线读每个标的的 (server_ms, tick) 观测，用于反事实对照。"""
    p = os.path.join(P04, "规格时间线.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    obs = {}
    for rec in doc.get("timeline") or []:
        ms = rec.get("server_ms")
        for sym, fields in (rec.get("present") or {}).items():
            tick = fields.get("tick")
            if ms is None or not tick:
                continue
            obs.setdefault(sym, []).append({
                "server_ms": int(ms), "server_utc": rec.get("server_utc"),
                "tick": str(tick), "kind": rec.get("kind"),
            })
    for sym in obs:
        obs[sym].sort(key=lambda x: x["server_ms"])
    return obs


def tick_near(obs, sym, ts_ms):
    """取离成交时刻最近的 tick 观测（优先成交之前最近的一条）。"""
    lst = obs.get(sym) or []
    if not lst:
        return None
    before = [o for o in lst if o["server_ms"] <= ts_ms]
    pick = before[-1] if before else lst[0]
    return dict(pick, delta_ms=abs(int(ts_ms) - pick["server_ms"]),
                side=("before" if pick["server_ms"] <= ts_ms else "after"))


# ══════════════════════════ 单腿核验 ══════════════════════════

def make_adapters(slip_bps, taker_bps):
    common = dict(slippage_bps=float(slip_bps), maker_fee_rate=2e-4,
                  taker_fee_rate=float(taker_bps) * 1e-4)
    return (SimulatedBarFillAdapter(strict=False, **common),
            SimulatedBarFillAdapter(strict=True, **common))


def replay(adapter, *, is_entry, raw_price, direction, step, stop_price=None, reason=""):
    """按腿类型重放一次报价；返回 (outcome, price, reject_reason, reject_detail, audit)。"""
    try:
        if is_entry:
            q = adapter.quote_entry(raw_price=raw_price, direction=direction,
                                    price_step=step, stop_price=stop_price)
            return "FILL", q.entry_fill, "", {}, dict(q.audit or {})
        px = adapter.quote_exit(target_px=raw_price, direction=direction,
                                price_step=step, reason=reason)
        return "FILL", px, "", {}, dict(getattr(adapter, "last_exit_audit", None) or {})
    except FillRejected as rej:
        return "REJECT", None, rej.reason, dict(rej.detail or {}), {}


def resolve_leg_inputs(*, trade, order, bar, is_entry):
    """还原这一腿「运行时传给撮合器的原价」，返回 dict。"""
    direction = int(trade.get("direction", 1))
    reason = "entry" if is_entry else str(trade.get("exit_reason") or order.get("reason") or "")
    if is_entry:
        raw_price = bar["open"]
        raw_src = "bar.open（engine.py:441 `raw_open = float(arr['open'][i])`）"
        stop_price = float(order.get("stop_price") or 0.0)
    else:
        stop_price = float(order.get("stop_price") or 0.0)
        if reason == "intrabar_stop":
            raw_price = stop_price
            raw_src = ("order.stop_price（policy.py:166 `target_px=float(pos.current_stop)`，"
                       "engine.py:658 传入）")
        elif reason == "max_holding_expired":
            raw_price = bar["open"]
            raw_src = "bar.open（engine.py:377 到期退出 target_px=op）"
        else:
            raw_price = bar["open"]
            raw_src = "bar.open（默认按 B_gap 开盘退出）"
    return {"raw_price": raw_price, "raw_price_source": raw_src,
            "direction": direction, "reason": reason, "stop_price": stop_price,
            "is_buy_effective": (direction == 1) if is_entry else (direction == -1)}


def infer_step_set(*, inputs, is_entry, slip_bps, taker_bps, recorded_fill):
    """用「能复现记录成交价」的步长集合反推该腿用过的步长（不预设 0.01）。"""
    legacy, _ = make_adapters(slip_bps, taker_bps)
    hits = []
    for cand in STEP_CANDIDATES:
        out, px, _, _, _ = replay(legacy, is_entry=is_entry,
                                  raw_price=inputs["raw_price"],
                                  direction=inputs["direction"], step=cand,
                                  stop_price=inputs["stop_price"],
                                  reason=inputs["reason"])
        if out == "FILL" and abs(px - recorded_fill) < 1e-12:
            hits.append(cand)
    return hits


def build_leg(*, symbol, run_id, trade, order, bar, is_entry, slip_bps, taker_bps,
              tick_obs, run_meta, step, step_basis, step_set):
    fill_price = float(trade["entry_price"] if is_entry else trade["exit_price"])
    fill_ms = int(trade["entry_time"] if is_entry else trade["exit_time"])
    order_id = trade["order_id"] if is_entry else trade.get("exit_order_id")
    inputs = resolve_leg_inputs(trade=trade, order=order, bar=bar, is_entry=is_entry)
    direction = inputs["direction"]
    reason = inputs["reason"]
    raw_price = inputs["raw_price"]
    raw_src = inputs["raw_price_source"]
    stop_price = inputs["stop_price"]
    buy = inputs["is_buy_effective"]

    legacy, strict = make_adapters(slip_bps, taker_bps)
    l_out, l_px, l_rej, l_det, _ = replay(
        legacy, is_entry=is_entry, raw_price=raw_price, direction=direction,
        step=step, stop_price=stop_price, reason=reason)
    s_out, s_px, s_rej, s_det, s_audit = replay(
        strict, is_entry=is_entry, raw_price=raw_price, direction=direction,
        step=step, stop_price=stop_price, reason=reason)

    # —— 三段拆解（与原价同源、与 strict 同算法）——
    bd = price_breakdown(raw_price=raw_price, is_buy=buy,
                         price_step=step, slippage_rate=float(slip_bps) * 1e-4)
    raw_d = Decimal(bd["raw_price"])
    slip_d = Decimal(bd["post_slip_price"])
    round_d = Decimal(bd["post_round_price"])
    step_d = Decimal(step)

    # —— 越界判定 ——
    lo, hi = bar["low"], bar["high"]
    inside = (lo - 1e-15) <= fill_price <= (hi + 1e-15)
    if fill_price > hi:
        over_abs, over_side = fill_price - hi, "above_high"
        over_pct = (fill_price - hi) / hi * 100.0
    elif fill_price < lo:
        over_abs, over_side = lo - fill_price, "below_low"
        over_pct = (lo - fill_price) / lo * 100.0
    else:
        over_abs, over_side, over_pct = 0.0, "inside", 0.0

    # —— 越界归因：滑点 vs 取整（把越界量按两段拆开，可加总）——
    obreak = None
    if over_side in ("above_high", "below_low"):
        boundary = hi if over_side == "above_high" else lo
        sign = 1.0 if over_side == "above_high" else -1.0
        slip_part = max(0.0, sign * (float(slip_d) - boundary))
        round_part = max(0.0, over_abs - slip_part)
        obreak = {
            "boundary": boundary,
            "total": over_abs,
            "from_slippage": slip_part,
            "from_rounding": round_part,
            "from_slippage_pct": round(slip_part / over_abs * 100, 3) if over_abs else None,
            "from_rounding_pct": round(round_part / over_abs * 100, 3) if over_abs else None,
            "slip_alone_crosses_boundary": slip_part > 0,
            "note": "滑点段 = max(0, 滑点后价 − 边界)；取整段 = 总越界 − 滑点段；两段之和恒等于总越界。",
        }

    # —— 备选基准（同一笔换个基准是否也能复现）——
    alt = None
    if not is_entry and reason == "intrabar_stop" and bar["open"] != raw_price:
        a_out, a_px, _, _, _ = replay(
            legacy, is_entry=False, raw_price=bar["open"], direction=direction,
            step=step, reason=reason)
        alt = {"base": "bar.open", "value": bar["open"], "outcome": a_out,
               "price": a_px, "reproduces": (a_out == "FILL" and
                                             abs(a_px - fill_price) < 1e-12)}
    elif not is_entry and reason != "intrabar_stop" and stop_price:
        a_out, a_px, _, _, _ = replay(
            legacy, is_entry=False, raw_price=stop_price, direction=direction,
            step=step, reason=reason)
        alt = {"base": "order.stop_price", "value": stop_price, "outcome": a_out,
               "price": a_px, "reproduces": (a_out == "FILL" and
                                             abs(a_px - fill_price) < 1e-12)}

    # —— 反事实：换成离成交时刻最近的历史 tick 会落在哪 ——
    cf = None
    tn = tick_near(tick_obs, symbol, fill_ms)
    if tn:
        c_out, c_px, _, _, _ = replay(
            legacy, is_entry=is_entry, raw_price=raw_price, direction=direction,
            step=tn["tick"], stop_price=stop_price, reason=reason)
        cf = {"tick": tn["tick"], "tick_server_utc": tn["server_utc"],
              "tick_delta_hours": round(tn["delta_ms"] / 3600000.0, 2),
              "tick_side": tn["side"], "tick_kind": tn["kind"],
              "price_at_tick": c_px,
              "inside_bar_at_tick": (c_px is not None and lo <= c_px <= hi),
              "outcome": c_out}

    # —— 规则逐条判定 ——
    rule_rows = []
    for rid, name, where, implemented in RULES:
        row = {"id": rid, "rule": name, "where": where, "implemented": implemented}
        if rid == "R1":
            row.update(evaluated=repr(raw_price), threshold="有限", triggered=False)
        elif rid == "R2":
            row.update(evaluated=repr(raw_price), threshold="> 0",
                       triggered=not (raw_price > 0))
        elif rid == "R3":
            row.update(evaluated=repr(step), threshold="有限", triggered=False)
        elif rid == "R4":
            row.update(evaluated=repr(step), threshold="> 0",
                       triggered=not (float(step) > 0))
        elif rid == "R5":
            row.update(evaluated=bd["post_round_price"], threshold="> 0",
                       triggered=not (round_d > 0))
        elif rid == "R6":
            row.update(evaluated=f"step={step} vs raw={bd['raw_price']}",
                       threshold="step ≤ raw",
                       triggered=(step_d > raw_d))     # 仅「若实现则会触发」
        elif rid == "R7":
            row.update(evaluated=f"fill={fill_price} vs [{lo}, {hi}]",
                       threshold="low ≤ fill ≤ high",
                       triggered=(not inside))         # 仅「若实现则会触发」
        else:
            row.update(evaluated=f"step={step}", threshold="== spec.tick",
                       triggered=None)                 # 无历史规格 ⇒ 无法判定
        row["would_trigger_if_implemented"] = row["triggered"] if not implemented else None
        if implemented:
            row["would_trigger_if_implemented"] = None
        rule_rows.append(row)

    return {
        "leg_id": f"{run_id}::{'entry' if is_entry else 'exit'}",
        "symbol": symbol,
        "run_id": run_id,
        "order_id": order_id,
        "leg": "entry" if is_entry else "exit",
        "leg_label": "入场" if is_entry else "出场",
        "order_type": order.get("order_type"),
        "side": order.get("side"),
        "position_side": order.get("position_side"),
        "action": order.get("action"),
        "phase": order.get("phase"),
        "reason": reason,
        "direction": direction,
        "fill_time_ms": fill_ms,
        "fill_time_utc": utc(fill_ms),
        "fill_price_recorded": fill_price,
        "bar": {"start_ms": int(bar.get("_ts", fill_ms)), "start_utc": utc(int(bar.get("_ts", fill_ms))),
                "end_utc": utc(int(bar.get("_ts", fill_ms)) + 900000 - 1),
                "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"]},
        "bar_ts_equals_fill_ms": int(bar.get("_ts", -1)) == fill_ms,
        "inside_bar": inside,
        "overshoot_abs": over_abs,
        "overshoot_pct": round(over_pct, 6),
        "overshoot_side": over_side,
        "overshoot_breakdown": obreak,
        "step_over_price": round(float(step_d) / float(raw_d), 6),
        "strict_input": {
            "fn": "quote_entry" if is_entry else "quote_exit",
            "raw_price": raw_price,
            "raw_price_source": raw_src,
            "direction": direction,
            "is_buy_effective": buy,
            "price_step": step,
            "price_step_basis": step_basis,
            "price_step_candidates_that_reproduce": step_set,
            "stop_price": stop_price,
            "reason": reason,
        },
        "strict_result": {
            "outcome": s_out,
            "price": s_px,
            "reject_reason": s_rej,
            "reject_detail": s_det,
            "matches_recorded": (s_out == "FILL" and abs(s_px - fill_price) < 1e-12),
        },
        "legacy_result": {
            "outcome": l_out, "price": l_px, "reject_reason": l_rej,
            "matches_recorded": (l_out == "FILL" and abs(l_px - fill_price) < 1e-12),
        },
        "verdict": "拦截" if s_out == "REJECT" else "放行",
        "three_stage": {
            "raw": bd["raw_price"],
            "post_slip": bd["post_slip_price"],
            "post_round": bd["post_round_price"],
            "slip_delta": bd["slip_delta"],
            "slip_delta_pct_of_raw": round(float(bd["slip_delta"]) / float(raw_d) * 100, 6),
            "round_delta": bd["round_delta"],
            "round_delta_in_steps": str((round_d - slip_d) / step_d),
            "round_delta_pct_of_raw": round(float(bd["round_delta"]) / float(raw_d) * 100, 6),
            "round_delta_pct_of_slip": round(float(bd["round_delta"]) / float(bd["slip_delta"]) * 100, 3)
            if float(bd["slip_delta"]) else None,
        },
        "alt_base": alt,
        "counterfactual_tick": cf,
        "rules": rule_rows,
        "evidence_paths": [
            f"回测日志/event_trend_v0_1/runs/{run_id}/trades.jsonl",
            f"回测日志/event_trend_v0_1/runs/{run_id}/orders.jsonl",
            f"回测日志/event_trend_v0_1/runs/{run_id}/fills.jsonl",
            f"回测日志/event_trend_v0_1/runs/{run_id}/chart.json",
            f"回测日志/event_trend_v0_1/runs/{run_id}/inputs.json",
        ],
        "run_meta": run_meta,
    }


# ══════════════════════════ 单 run 处理 ══════════════════════════

def process_run(run_id, tick_obs):
    run_dir = os.path.join(RUNS_DIR, run_id)
    inputs_p = os.path.join(run_dir, "inputs.json")
    if not os.path.exists(inputs_p):
        return None, []
    with open(inputs_p, encoding="utf-8") as f:
        inputs = json.load(f)
    params = ((inputs.get("fingerprints") or {}).get("config") or {}).get("params") or {}
    slip_bps = params.get("slippage_bps", 3.0)
    taker_bps = params.get("taker_fee_bps", 5.0)
    symbol = inputs.get("symbol")
    code = ((inputs.get("fingerprints") or {}).get("code") or {})
    run_meta = {
        "symbol": symbol,
        "variant": inputs.get("variant"),
        "strategy_version": inputs.get("strategy_version"),
        "code_combined_sha256": code.get("combined_sha256"),
        "params_has_price_step": ("price_step" in params) or ("tick_size" in params),
        "params_has_instrument_spec": "instrument_spec" in params,
        "params_n": len(params),
        "slippage_bps": slip_bps,
        "taker_fee_bps": taker_bps,
        "window": ((inputs.get("data_summary") or {}).get("time_range_1h") or {}),
        "instrument_spec_file": os.path.exists(os.path.join(run_dir, "instrument_spec.json")),
    }

    trades = jl(os.path.join(run_dir, "trades.jsonl"))
    orders = jl(os.path.join(run_dir, "orders.jsonl"))
    if not trades:
        return run_meta, []

    _, bars = load_bars(os.path.join(run_dir, "chart.json"))
    by_id = {o.get("order_id"): o for o in orders}
    legs = []
    for t in trades:
        ent_o = by_id.get(t.get("order_id"), {})
        ex_o = by_id.get(t.get("exit_order_id"), {})
        # ── 第一遍：只还原输入 + 反推「能复现记录价的步长集合」──
        pending = []
        for is_entry, order, ts in ((True, ent_o, int(t["entry_time"])),
                                    (False, ex_o, int(t["exit_time"]))):
            bar = bars.get(ts)
            if bar is None:
                legs.append({"leg_id": f"{run_id}::{'entry' if is_entry else 'exit'}",
                             "symbol": symbol, "run_id": run_id, "blocked": "bar_not_found",
                             "fill_time_ms": ts, "fill_time_utc": utc(ts)})
                continue
            bar = dict(bar)
            bar["_ts"] = ts
            inp = resolve_leg_inputs(trade=t, order=order, bar=bar, is_entry=is_entry)
            rec = float(t["entry_price"] if is_entry else t["exit_price"])
            cands = infer_step_set(inputs=inp, is_entry=is_entry, slip_bps=slip_bps,
                                   taker_bps=taker_bps, recorded_fill=rec)
            pending.append((is_entry, order, bar, inp, rec, cands))

        # ── 该 run 的步长集合 = 两条腿的交集 ──
        sets = [set(p[5]) for p in pending if p[5]]
        run_set = sorted(set.intersection(*sets), key=lambda s: float(s)) if sets else []
        if STEP_USED in run_set:
            step, basis = STEP_USED, "arithmetic_inferred_0.01"
        elif run_set:
            step, basis = run_set[0], "arithmetic_inferred_other"
        else:
            step, basis = STEP_USED, "not_reproducible"
        run_meta["run_step_set"] = run_set
        run_meta["run_step_used"] = step
        run_meta["run_step_basis"] = basis

        # ── 第二遍：用该 run 的步长正式重放 ──
        for is_entry, order, bar, inp, rec, cands in pending:
            legs.append(build_leg(symbol=symbol, run_id=run_id, trade=t, order=order,
                                  bar=bar, is_entry=is_entry, slip_bps=slip_bps,
                                  taker_bps=taker_bps, tick_obs=tick_obs,
                                  run_meta=run_meta, step=step, step_basis=basis,
                                  step_set=cands))
    return run_meta, legs


# ══════════════════════════ 输出 ══════════════════════════

def write_scope_csv(legs, path):
    cols = ["标的", "run_id", "order_id", "成交腿类型", "成交时间(UTC)", "原始成交价",
            "K线起(UTC)", "O", "H", "L", "C", "是否在[L,H]内", "越界方向", "越界幅度(绝对)",
            "越界幅度(%)", "越界中滑点占(%)", "越界中取整占(%)", "step/raw",
            "strict输入(raw_price)", "strict输入(step)", "strict输入(方向)",
            "strict结果", "strict产出价", "拒绝原因", "触发规则", "未触发规则",
            "若实现则会触发的规则", "三段:原价", "三段:滑点后", "三段:取整后",
            "滑点贡献(% of raw)", "取整贡献(% of raw)",
            "反事实tick价(仅参考)", "证据路径"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for L in legs:
            if L.get("blocked"):
                row = [""] * len(cols)
                row[0] = L.get("symbol") or ""
                row[1] = L.get("run_id") or ""
                row[4] = L.get("fill_time_utc") or ""
                row[12] = "BLOCKED"
                row[23] = L["blocked"]
                w.writerow(row)
                continue
            trig = [r["id"] for r in L["rules"] if r["implemented"] and r["triggered"]]
            notrig = [r["id"] for r in L["rules"] if r["implemented"] and not r["triggered"]]
            would = [r["id"] for r in L["rules"] if not r["implemented"]
                     and r["would_trigger_if_implemented"]]
            cf = L.get("counterfactual_tick") or {}
            ob = L.get("overshoot_breakdown") or {}
            w.writerow([
                L["symbol"], L["run_id"], L["order_id"], L["leg_label"], L["fill_time_utc"],
                L["fill_price_recorded"],
                L["bar"]["start_utc"], L["bar"]["open"], L["bar"]["high"],
                L["bar"]["low"], L["bar"]["close"],
                L["inside_bar"], L["overshoot_side"], L["overshoot_abs"], L["overshoot_pct"],
                ob.get("from_slippage_pct"), ob.get("from_rounding_pct"), L["step_over_price"],
                L["strict_input"]["raw_price"], L["strict_input"]["price_step"],
                "买" if L["strict_input"]["is_buy_effective"] else "卖",
                L["strict_result"]["outcome"], L["strict_result"]["price"],
                L["strict_result"]["reject_reason"],
                "+".join(trig) or "无", "+".join(notrig),
                "+".join(would) or "无",
                L["three_stage"]["raw"], L["three_stage"]["post_slip"],
                L["three_stage"]["post_round"],
                L["three_stage"]["slip_delta_pct_of_raw"],
                L["three_stage"]["round_delta_pct_of_raw"],
                cf.get("price_at_tick"), L["evidence_paths"][0],
            ])


def write_scan_csv(rows, path):
    cols = ["标的", "run_id", "腿", "reason", "成交时间(UTC)", "成交价",
            "O", "H", "L", "C", "在区间内", "越界方向", "越界幅度(%)",
            "strict(step=0.01)", "strict产出", "若实现R7是否会拦"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r["symbol"], r["run_id"], r["leg_label"], r["reason"],
                        r["fill_time_utc"], r["fill_price_recorded"],
                        r["bar"]["open"], r["bar"]["high"], r["bar"]["low"], r["bar"]["close"],
                        r["inside_bar"], r["overshoot_side"], r["overshoot_pct"],
                        r["strict_result"]["outcome"], r["strict_result"]["price"],
                        "是" if not r["inside_bar"] else "否"])


def main():
    do_scan = "--no-scan" not in sys.argv
    tick_obs = load_tick_observations()

    scope_runs, scope_legs = [], []
    for r in SCOPE6:
        meta, legs = process_run(r, tick_obs)
        if meta is None:
            print(f"[MISS] {r}")
            continue
        scope_runs.append(meta)
        scope_legs.extend(legs)

    scan_legs, scan_runs = [], 0
    if do_scan:
        for d in sorted(os.listdir(RUNS_DIR)):
            if not d.startswith("run_WX_EVENT_TREND_V0_1_"):
                continue
            p = os.path.join(RUNS_DIR, d, "trades.jsonl")
            if not os.path.isfile(p) or os.path.getsize(p) == 0:
                continue
            meta, legs = process_run(d, tick_obs)
            if legs:
                scan_runs += 1
                scan_legs.extend(legs)

    # ── 汇总 ──
    def tally(legs):
        out = {"legs": len(legs), "inside": 0, "outside": 0, "rejected": 0, "passed": 0}
        for L in legs:
            if L.get("blocked"):
                out.setdefault("blocked", 0)
                out["blocked"] += 1
                continue
            if L["inside_bar"]:
                out["inside"] += 1
            else:
                out["outside"] += 1
            if L["strict_result"]["outcome"] == "REJECT":
                out["rejected"] += 1
            else:
                out["passed"] += 1
        return out

    scope_sum = tally(scope_legs)
    outside_scope = [L for L in scope_legs if not L.get("blocked") and not L["inside_bar"]]
    scope_sum["outside_rejected"] = sum(
        1 for L in outside_scope if L["strict_result"]["outcome"] == "REJECT")
    scope_sum["outside_passed"] = sum(
        1 for L in outside_scope if L["strict_result"]["outcome"] == "FILL")
    scope_sum["reproduced_by_legacy"] = sum(
        1 for L in scope_legs if not L.get("blocked") and L["legacy_result"]["matches_recorded"])

    doc = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step_candidates": STEP_CANDIDATES,
        "step_basis_note": (
            "run 产物未记录 price_step（inputs.json → fingerprints.config.params 49 个键里无该键，"
            "也无 tick_size / instrument_spec）。本工具**不预设** 0.01：先对每条腿枚举 "
            f"{STEP_CANDIDATES} 中能逐位复现记录成交价的步长，取同一 run 两条腿的**交集**作为该 run 的"
            "步长集合；0.01 在交集内则用 0.01，否则用交集里最粗的一个，并标注 "
            "`arithmetic_inferred_other`。交集为空 ⇒ `not_reproducible`，不补造输入。"),
        "scope6_runs": scope_runs,
        "scope6_summary": scope_sum,
        "scope6_legs": scope_legs,
        "scan": {"runs": scan_runs, "summary": tally(scan_legs),
                 "legs": scan_legs} if do_scan else None,
    }
    if do_scan:
        from collections import Counter
        doc["scan"]["step_distribution"] = dict(Counter(
            L["strict_input"]["price_step"] for L in scan_legs if not L.get("blocked")))
        runs_001 = {L["run_id"] for L in scan_legs
                    if not L.get("blocked")
                    and L["strict_input"]["price_step"] == STEP_USED}
        doc["scan"]["runs_using_0_01"] = len(runs_001)
        doc["scan"]["runs_not_using_0_01"] = sorted(
            {L["run_id"] for L in scan_legs if not L.get("blocked")} - runs_001)
    jp = os.path.join(HERE, "逐腿核验.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    write_scope_csv(scope_legs, os.path.join(HERE, "表3_12条成交腿核验明细.csv"))
    if do_scan:
        write_scan_csv(scan_legs, os.path.join(HERE, "表3b_全量成交腿扫描.csv"))

    # ── 控制台 ──
    print("=" * 100)
    print("PRECISION-06 逐腿核验（步长逐 run 反推，不预设 0.01）")
    print("=" * 100)
    print(f"{'标的':<14}{'腿':<5}{'成交价':>11}{'L':>11}{'H':>11}  {'内':<3}"
          f"{'step':>9}{'strict':>8}{'产出':>11}{'越界%':>9}  触发规则")
    for L in scope_legs:
        if L.get("blocked"):
            print(f"{L['symbol']:<14}{'?':<5}  BLOCKED {L['blocked']}")
            continue
        trig = ",".join(r["id"] for r in L["rules"] if r["implemented"] and r["triggered"]) or "-"
        px = L["strict_result"]["price"]
        print(f"{L['symbol']:<14}{L['leg_label']:<5}{L['fill_price_recorded']:>11.6g}"
              f"{L['bar']['low']:>11.6g}{L['bar']['high']:>11.6g}  "
              f"{'是' if L['inside_bar'] else '否':<3}"
              f"{L['strict_input']['price_step']:>9} "
              f"{L['strict_result']['outcome']:<7}"
              f"{(px if px is not None else float('nan')):>11.6g}"
              f"{L['overshoot_pct']:>9.4f}  {trig}")
    print("-" * 100)
    print(f"范围6：{scope_sum}")
    if do_scan:
        print(f"全量扫描：runs={scan_runs} {doc['scan']['summary']}")
        print(f"步长分布（按腿）：{doc['scan']['step_distribution']}")
        print(f"用 0.01 的 run 数：{doc['scan']['runs_using_0_01']} / {scan_runs}")
    print(f"→ {os.path.relpath(jp, ROOT)}")
    print(f"→ evidence/PRECISION-06/表3_12条成交腿核验明细.csv")
    if do_scan:
        print("→ evidence/PRECISION-06/表3b_全量成交腿扫描.csv")


if __name__ == "__main__":
    main()
