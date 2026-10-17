#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-05 工具：逐笔核验 6 个 run 的成交（重点 4 笔越界）。

只做核验，不改任何东西：
  1. 从 run 目录读出 trades / orders / chart / inputs；
  2. 把每条腿的成交价与**该腿时间戳对应的那根 15m K 线**对齐（先断言时间戳
     确实等于该 K 线的起始时刻，再取 OHLC）；
  3. 计算越界幅度（绝对 / 相对）；
  4. 用候选步长做**算术复现**：`买=ceil`、`卖=floor` 的 0.01 网格能否逐位复现
     记录的成交价 —— 能复现只说明"算术上等价于用了这个步长"，不等于拿到了
     运行时的调用参数（那要靠代码指纹，见 fingerprint 段）。
  5. 输出 JSON，不推断盈亏、不估算收益。

用法：python3 工具_逐笔核验.py
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUNS_DIR = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")
OUT = os.path.join(HERE, "四笔成交逐笔核验.json")

RUN_IDS = [
    "run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451",
    "run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703",
    "run_WX_EVENT_TREND_V0_1_TRUTHUSDT_1792174465",
    "run_WX_EVENT_TREND_V0_1_COTIUSDT_1792174145",
    "run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973",
    "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568",
]

BAR_MS_15M = 15 * 60 * 1000

# 候选步长（从粗到细）。只用来做算术复现，不是"猜交易所规格"。
CANDIDATE_STEPS = ["0.1", "0.05", "0.01", "0.005", "0.001", "0.0001",
                   "0.00001", "0.000001", "0.0000001"]

# PRECISION-04 第 4 节公告给出的上线/变更后 tick（仅作"参考价"用，不计算盈亏）
ANNOUNCED_TICK = {
    "SPACEUSDT": "0.000001",
    "AIGENSYNUSDT": "0.00001",
    "TRUTHUSDT": "0.000001",
    "COTIUSDT": "0.00001",      # 2024H1 窗口内应为 0.00001（2026-07-21 才降到 0.000001）
    "BNBUSDT": "0.01",
    "ORDIUSDT": "0.001",
}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(p):
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def round_buy(px, step):
    """与运行时代码 fill.py 同式：`step * ceil(px/step - 1e-9)`。"""
    return float(step * math.ceil(px / step - 1e-9))


def round_sell(px, step):
    return float(step * math.floor(px / step + 1e-9))


def same(a, b):
    if a is None or b is None:
        return False
    return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))


def iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def verify_run(run_id):
    d = os.path.join(RUNS_DIR, run_id)
    trades = load_jsonl(os.path.join(d, "trades.jsonl"))
    orders = {o["order_id"]: o for o in load_jsonl(os.path.join(d, "orders.jsonl"))}
    chart = json.load(open(os.path.join(d, "chart.json"), encoding="utf-8"))
    inputs = json.load(open(os.path.join(d, "inputs.json"), encoding="utf-8"))
    manifest = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))

    candles = chart["candles"]
    by_t = {int(c[0]): c for c in candles}
    gaps = {int(b[0]) - int(a[0]) for a, b in zip(candles, candles[1:])}

    code_fp = inputs["fingerprints"]["code"]
    params = inputs["fingerprints"]["config"]["params"]

    # run 时代码指纹 vs 当前磁盘代码
    cur_match = {}
    for rel, h in code_fp["files"].items():
        p = os.path.join(ROOT, rel)
        cur_match[rel] = (sha256_file(p) if os.path.exists(p) else None) == h

    legs = []
    align = []
    for tr in trades:
        # 数据对齐：signal_id 末段是「确认 bar 的时刻」，按 entry_delay_15m_bars=1
        # 应当在下一根 15m bar 的开盘成交 ⇒ delta 应恰为 900000ms
        sid = tr.get("signal_id") or ""
        m = re.findall(r"_(\d{13})_(\d{13})$", sid)
        if m:
            confirm_ms = int(m[0][1])
            align.append({
                "signal_id": sid,
                "confirm_bar_ms": confirm_ms,
                "confirm_bar_utc": iso(confirm_ms),
                "entry_time_ms": int(tr["entry_time"]),
                "delta_ms": int(tr["entry_time"]) - confirm_ms,
                "delta_bars_15m": (int(tr["entry_time"]) - confirm_ms) / BAR_MS_15M,
                "matches_entry_delay_1_bar": (int(tr["entry_time"]) - confirm_ms) == BAR_MS_15M,
            })
        for leg, t_key, p_key, oid_key in (
                ("entry", "entry_time", "entry_price", "order_id"),
                ("exit", "exit_time", "exit_price", "exit_order_id")):
            t = int(tr[t_key])
            px = float(tr[p_key])
            oid = tr.get(oid_key)
            o = orders.get(oid) or {}
            candle = by_t.get(t)
            rec = {
                "leg": leg,
                "order_id": oid,
                "side": o.get("side"),
                "order_type": o.get("order_type"),
                "position_side": o.get("position_side"),
                "action": o.get("action"),
                "reason": o.get("reason") or o.get("reject_reason") or "",
                "stop_price": o.get("stop_price"),
                "fill_price": px,
                "fill_time_ms": t,
                "fill_time_utc": iso(t),
                "bar_ms_aligned": (t % BAR_MS_15M == 0),
                "bar_found_exact": candle is not None,
            }
            if candle is not None:
                o_, h_, l_, c_ = (float(candle[1]), float(candle[2]),
                                  float(candle[3]), float(candle[4]))
                rec.update({
                    "bar_open": o_, "bar_high": h_, "bar_low": l_, "bar_close": c_,
                    "bar_start_utc": iso(int(candle[0])),
                    "bar_end_utc": iso(int(candle[0]) + BAR_MS_15M),
                    "inside_low_high": (l_ <= px <= h_),
                    "dev_from_open": px - o_,
                    "dev_from_open_pct": ((px - o_) / o_ * 100.0) if o_ else None,
                })
                if l_ <= px <= h_:
                    rec["overshoot_abs"] = 0.0
                    rec["overshoot_pct"] = 0.0
                    rec["overshoot_side"] = "inside"
                else:
                    edge = h_ if px > h_ else l_
                    rec["overshoot_abs"] = px - edge
                    rec["overshoot_pct"] = (px - edge) / edge * 100.0
                    rec["overshoot_side"] = "above_high" if px > h_ else "below_low"
                # 算术复现：买 ceil / 卖 floor。
                # 基准价按成交模型取：
                #   开仓 MARKET  → 该 bar 的 open
                #   止损 intrabar_stop → 该单的 stop_price（bar 内触发，成交在止损价）
                #   其它离场（max_holding 等）→ 该 bar 的 open
                slip = float(params.get("slippage_bps", 0.0)) * 1e-4
                is_buy = (o.get("side") == "BUY")
                bases = []
                if leg == "entry":
                    bases.append(("bar_open", o_))
                else:
                    if str(o.get("reason")) == "intrabar_stop" and o.get("stop_price"):
                        bases.append(("stop_price", float(o["stop_price"])))
                    bases.append(("bar_open", o_))
                rec["reproduce_side_rule"] = "buy_ceil" if is_buy else "sell_floor"
                rec["reproduce_candidates"] = []
                for bname, braw in bases:
                    base = braw * (1.0 + slip) if is_buy else braw * (1.0 - slip)
                    hits = []
                    for s in CANDIDATE_STEPS:
                        sv = float(s)
                        got = round_buy(base, sv) if is_buy else round_sell(base, sv)
                        if same(got, px):
                            hits.append(s)
                    rec["reproduce_candidates"].append(
                        {"base": bname, "base_raw": braw, "base_after_slip": base,
                         "steps_that_reproduce": hits})
                    if hits and "reproduce_base_used" not in rec:
                        # 优先取第一个能复现的基准（离场优先 stop_price，其次 bar open）
                        rec["reproduce_base_used"] = bname
                        rec["steps_that_reproduce"] = hits
                if "steps_that_reproduce" not in rec:
                    rec["steps_that_reproduce"] = []
                    rec["reproduce_base_used"] = None
                # 参考价：若按公告 tick 取整（只给价格，不算盈亏）
                tk = ANNOUNCED_TICK.get(tr["symbol"])
                if tk:
                    tv = float(tk)
                    used = rec.get("reproduce_base_used")
                    pick = None
                    for c in rec["reproduce_candidates"]:
                        if c["base"] == used:
                            pick = c
                            break
                    if pick is None:
                        pick = rec["reproduce_candidates"][0]
                    rec["counterfactual_price_at_announced_tick"] = (
                        round_buy(pick["base_after_slip"], tv) if is_buy
                        else round_sell(pick["base_after_slip"], tv))
                    rec["counterfactual_tick"] = tk
            legs.append(rec)

    return {
        "symbol": trades[0]["symbol"] if trades else None,
        "run_id": run_id,
        "run_dir": os.path.relpath(d, ROOT),
        "trades_count": len(trades),
        "chart_exec_timeframe": chart.get("exec_timeframe"),
        "chart_candle_count": len(candles),
        "chart_candle_step_ms_set": sorted(gaps),
        "chart_window": chart.get("window"),
        "manifest_files_count": manifest.get("files_count"),
        "runtime_fingerprint": {
            "code_combined_sha256": code_fp["combined_sha256"],
            "fill_py_sha256": code_fp["files"].get("策略/WyckoffMaster/trading_core/fill.py"),
            "engine_py_sha256": code_fp["files"].get("策略/WyckoffMaster/trading_core/engine.py"),
            "position_py_sha256": code_fp["files"].get("策略/WyckoffMaster/trading_core/position.py"),
            "engine_adapter_py_sha256": code_fp["files"].get(
                "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py"),
            "runner_py_sha256": code_fp["files"].get(
                "策略/WyckoffMaster/event_trend_v0_1/runner.py"),
            "current_disk_identical": cur_match,
        },
        "run_config_has_price_step": any(
            k in params for k in ("price_step", "tick_size", "instrument_spec")),
        "run_config_params_keys_sample": sorted(params.keys())[:5],
        "slippage_bps": params.get("slippage_bps"),
        "taker_fee_bps": params.get("taker_fee_bps"),
        "alignment": align,
        "legs": legs,
    }


def main():
    out = {"generated_utc": None, "method": (
        "成交价 vs 该腿时间戳对应的那根 15m K 线；先断言时间戳==K线起始时刻再取 OHLC；"
        "越界幅度按 (成交价-最近边界)/边界 计；算术复现只证明『等价于某步长』，"
        "不证明运行时真的传了该步长。"), "runs": []}
    import time
    out["generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for rid in RUN_IDS:
        if not os.path.isdir(os.path.join(RUNS_DIR, rid)):
            out["runs"].append({"run_id": rid, "error": "run_dir_not_found"})
            continue
        out["runs"].append(verify_run(rid))

    # —— 用全部腿求「能同时复现所有成交」的步长交集 ——
    # 单腿往往有多个步长都能复现（粗步长在小价格上会被 ceil 抬到同一格），
    # 交集才能把步长钉死。
    all_sets = []
    for r in out["runs"]:
        if "error" in r:
            continue
        for leg in r["legs"]:
            if leg.get("steps_that_reproduce"):
                all_sets.append(set(leg["steps_that_reproduce"]))
    inter = set(CANDIDATE_STEPS)
    for s in all_sets:
        inter &= s
    out["step_intersection_all_legs"] = sorted(
        inter, key=lambda x: float(x), reverse=True)
    out["step_intersection_note"] = (
        "交集 = 能同时复现全部已定位成交的步长集合；只证明『算术上等价』，"
        "不证明运行时调用参数。")
    out["legs_used_in_intersection"] = len(all_sets)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for r in out["runs"]:
        if "error" in r:
            print("MISSING", r["run_id"])
            continue
        print("=" * 96)
        print(f'{r["symbol"]}  {r["run_id"]}')
        print(f'   timeframe={r["chart_exec_timeframe"]} candles={r["chart_candle_count"]} '
              f'step_ms={r["chart_candle_step_ms_set"]} config_has_price_step={r["run_config_has_price_step"]}')
        for leg in r["legs"]:
            if not leg["bar_found_exact"]:
                print("   !! 该腿时间戳找不到对应 K 线:", leg["fill_time_utc"])
                continue
            print(f'   {leg["leg"]:<6} {leg["side"]:<4} {leg["order_type"]:<12} '
                  f'{leg["fill_time_utc"]}  fill={leg["fill_price"]!r:<22} '
                  f'bar O={leg["bar_open"]} H={leg["bar_high"]} L={leg["bar_low"]}')
            print(f'          inside={leg["inside_low_high"]} '
                  f'overshoot={leg["overshoot_pct"]:.2f}% ({leg["overshoot_side"]}) '
                  f'steps_reproducing={leg["steps_that_reproduce"]}')
            if "counterfactual_price_at_announced_tick" in leg:
                print(f'          参考：按公告 tick={leg["counterfactual_tick"]} 取整 → '
                      f'{leg["counterfactual_price_at_announced_tick"]!r}（仅价格，不算盈亏）')
    print("\n写出:", OUT)
    print("能同时复现全部 %d 条腿的步长交集: %s" % (
        len(all_sets), out["step_intersection_all_legs"]))


if __name__ == "__main__":
    main()
