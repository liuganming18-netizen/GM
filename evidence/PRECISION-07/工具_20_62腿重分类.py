#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-07 工具 20：62 条成交腿按「统一定义后的 R7」重算分类。

为什么要重算而不是照抄 P06：
  P06 用「越界量 vs 滑点允许量」做 A/B 二分，并声称 B 类（滑点导致）「不该拒」。
  但同一条判据会把 XMR 出场（越界 0.05 > 容差 0.0402）算进 A 类，而报告把它写在 B 类；
  同时 P06 的 R7 影子实现又把 XMR 拒了。三处互相矛盾（详见报告 2.3）。

本工具的定稿口径（无特例）：
  · **R7 零容差**：`fill ∉ [low, high]` 即拒。理由：low/high 是当日真实成交极值，
    低于 low 卖出在物理上不可能，滑点是成本模型、不是豁免券。
  · 分类只解释成因，**不改变是否拒绝**：
      inside / round_dominant（取整占越界 ≥50%）/ slip_dominant（滑点占越界 >50%）
  · 另外记录 `滑点段是否已越界`，用来说明「这根 K 线容不下这个滑点」。

步长：**不预设 0.01**。优先用 `summary.precision.price_steps` 记录值；没有记录的
run，用「能逐位复现记录成交价」的候选步长取交集（P06 已验证该方法在 4 个有真值的
run 上 4/4 自证）。

用法：python3 工具_20_62腿重分类.py
输出：表2_62条腿分类.csv / 重分类.json / 重分类原始输出.txt
"""
from __future__ import annotations

import csv
import json
import os
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUNS = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")
P06 = os.path.join(ROOT, "evidence", "PRECISION-06")

STEP_CANDIDATES = ["0.01", "0.001", "0.0001", "0.00001", "0.000001", "0.0000001"]


def jl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def load_bars(chart_path):
    with open(chart_path, encoding="utf-8") as f:
        doc = json.load(f)
    bars = {}
    for c in doc.get("candles") or []:
        if isinstance(c, dict):
            g = lambda *ks: next(c[k] for k in ks if k in c)
            bars[int(g("time", "timestamp", "t"))] = {
                "open": float(g("open", "o")), "high": float(g("high", "h")),
                "low": float(g("low", "l")), "close": float(g("close", "c"))}
        else:
            bars[int(c[0])] = {"open": float(c[1]), "high": float(c[2]),
                               "low": float(c[3]), "close": float(c[4])}
    return bars


def utc(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000.0))


def dec(x):
    return Decimal(str(x))


def three_stage(raw, *, is_buy, step, slip_bps):
    slip = dec(slip_bps * 1e-4).quantize(Decimal("1e-12"))
    raw_d, st = dec(raw), dec(step)
    post_slip = raw_d * (Decimal(1) + slip) if is_buy else raw_d * (Decimal(1) - slip)
    post_round = ((post_slip / st).to_integral_value(rounding="ROUND_CEILING") * st
                  if is_buy else
                  (post_slip / st).to_integral_value(rounding="ROUND_FLOOR") * st)
    return slip, raw_d, post_slip, post_round


def over_amount(price, *, is_buy, low, high):
    if is_buy:
        return max(Decimal(0), price - dec(high))
    return max(Decimal(0), dec(low) - price)


def run_legs(run_id):
    """把 run 的成交拆成腿（用 orders.jsonl 的 order_id 实证，不靠顺序猜）。"""
    d = os.path.join(RUNS, run_id)
    trades = jl(os.path.join(d, "trades.jsonl"))
    orders = {o["order_id"]: o for o in jl(os.path.join(d, "orders.jsonl"))}
    bars = load_bars(os.path.join(d, "chart.json"))
    inputs = json.load(open(os.path.join(d, "inputs.json"), encoding="utf-8"))
    params = inputs.get("params") or inputs.get("config") or {}
    slip_bps = float(params.get("slippage_bps", 3.0))
    summ = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
    prec = (summ.get("precision") or {})
    steps_rec = prec.get("price_steps")

    legs = []
    for t in trades:
        for leg, oid in (("入场", t["order_id"]), ("出场", t.get("exit_order_id"))):
            o = orders.get(oid)
            if not o:
                continue
            is_entry = (o.get("action") == "open")
            is_buy = (o.get("side") == "BUY")
            ts = int(o["filled_at"])
            bar = bars.get(ts)
            if bar is None:
                continue
            reason = o.get("reason") or ""
            if is_entry:
                raw, raw_src = bar["open"], "bar.open"
            elif reason == "intrabar_stop":
                raw, raw_src = float(o.get("stop_price") or 0.0), "order.stop_price"
            else:
                raw, raw_src = bar["open"], f"bar.open({reason})"
            legs.append({"leg": leg, "order": o, "bar": bar, "is_buy": is_buy,
                         "is_entry": is_entry, "raw": raw, "raw_src": raw_src,
                         "reason": reason, "recorded": float(o["fill_price"]),
                         "ts": ts, "slip_bps": slip_bps})
    return legs, steps_rec


def infer_steps(legs):
    """逐腿找能复现记录价的步长集合，取交集。返回 (交集, 每腿命中情况)。"""
    per_leg = []
    for L in legs:
        hits = []
        for cand in STEP_CANDIDATES:
            _, _, _, post_round = three_stage(L["raw"], is_buy=L["is_buy"],
                                              step=cand, slip_bps=L["slip_bps"])
            if abs(float(post_round) - L["recorded"]) <= 1e-9:
                hits.append(cand)
        per_leg.append(hits)
    inter = set(per_leg[0]) if per_leg else set()
    for h in per_leg[1:]:
        inter &= set(h)
    return sorted(inter), per_leg


def classify(over, slip_over):
    if over == 0:
        return "inside"
    return "slip_dominant" if slip_over * 2 > over else "round_dominant"


def main():
    with open(os.path.join(P06, "表3b_全量成交腿扫描.csv"), encoding="utf-8-sig") as f:
        scan = list(csv.DictReader(f))
    run_ids = sorted({r["run_id"] for r in scan})

    rows, runs_meta = [], []
    for run_id in run_ids:
        legs, steps_rec = run_legs(run_id)
        if not legs:
            continue
        inter, per_leg = infer_steps(legs)
        if steps_rec:
            # 落盘形态实测为 dict：{"ZKUSDT": "0.000001"}
            if isinstance(steps_rec, dict):
                step = str(list(steps_rec.values())[0])
            elif isinstance(steps_rec, list):
                step = str(steps_rec[0])
            else:
                step = str(steps_rec)
            step_src = "summary.precision.price_steps（记录值）"
        elif len(inter) == 1:
            step, step_src = inter[0], "逐腿反推（两腿交集单元素）"
        elif inter:
            step, step_src = inter[0], f"逐腿反推（交集 {inter}，取最粗）"
        else:
            step, step_src = None, "**反推无解**"

        symbol = legs[0]["order"]["symbol"]
        runs_meta.append({"run_id": run_id, "symbol": symbol, "legs": len(legs),
                          "step": step, "step_source": step_src,
                          "step_candidates_intersection": inter,
                          "per_leg_hits": per_leg})

        for L in legs:
            if step is None:
                continue
            slip, raw_d, post_slip, post_round = three_stage(
                L["raw"], is_buy=L["is_buy"], step=step, slip_bps=L["slip_bps"])
            low, high = L["bar"]["low"], L["bar"]["high"]
            over = over_amount(post_round, is_buy=L["is_buy"], low=low, high=high)
            slip_over = over_amount(post_slip, is_buy=L["is_buy"], low=low, high=high)
            round_over = over - slip_over
            tol = raw_d * slip
            cls = classify(over, slip_over)

            r7_zero = over > 0
            r7_slip = over > tol
            r7_tick = over > tol + dec(step)

            if cls == "inside":
                why = f"fill {float(post_round)} ∈ [{low}, {high}]，越界 0 → 允许"
            elif cls == "round_dominant":
                why = (f"fill {float(post_round)} 越出 [{low}, {high}] {float(over)}"
                       f"（取整占 {float(round_over/over*100):.2f}%）> 0 → 拒（R7 零容差）")
            else:
                why = (f"fill {float(post_round)} 越出 [{low}, {high}] {float(over)}"
                       f"（滑点段已越界 {float(slip_over)}，占 {float(slip_over/over*100):.2f}%）"
                       f" → 拒（R7 零容差）")

            rows.append({
                "标的": symbol, "run_id": run_id, "order_id": L["order"]["order_id"],
                "腿": L["leg"], "reason": L["reason"],
                "成交时间UTC": utc(L["ts"]), "记录成交价": L["recorded"],
                "步长": step, "步长来源": step_src,
                "参考价来源": L["raw_src"], "参考价raw": L["raw"],
                "滑点后价格": float(post_slip), "取整后价格": float(post_round),
                "复算==记录": abs(float(post_round) - L["recorded"]) <= 1e-9,
                "K线O": L["bar"]["open"], "K线H": high, "K线L": low, "K线C": L["bar"]["close"],
                "在区间内": over == 0,
                "越界金额": float(over), "越界%": float(over / raw_d * 100) if over else 0.0,
                "滑点段越界": float(slip_over),
                "滑点段占越界%": float(slip_over / over * 100) if over else 0.0,
                "取整段越界": float(round_over),
                "取整段占越界%": float(round_over / over * 100) if over else 0.0,
                "滑点段是否已越界": slip_over > 0,
                "容差(raw×slip)": float(tol),
                "三段分类": cls,
                "R7-零容差": "拒绝" if r7_zero else "允许",
                "R7-容差=滑点": "拒绝" if r7_slip else "允许",
                "R7-容差=滑点+1档": "拒绝" if r7_tick else "允许",
                "判定依据": why,
            })

    # ---------- 统计 ----------
    n = len(rows)
    inside = [r for r in rows if r["在区间内"]]
    outside = [r for r in rows if not r["在区间内"]]
    stat = {
        "runs": len(runs_meta), "legs": n,
        "inside": len(inside), "outside": len(outside),
        "reproduced": sum(1 for r in rows if r["复算==记录"]),
        "cls": {k: sum(1 for r in rows if r["三段分类"] == k)
                for k in ("inside", "round_dominant", "slip_dominant")},
        "R7-零容差": {"拒绝": sum(1 for r in rows if r["R7-零容差"] == "拒绝"),
                      "误伤区间内": sum(1 for r in inside if r["R7-零容差"] == "拒绝")},
        "R7-容差=滑点": {"拒绝": sum(1 for r in rows if r["R7-容差=滑点"] == "拒绝"),
                         "误伤区间内": sum(1 for r in inside if r["R7-容差=滑点"] == "拒绝")},
        "R7-容差=滑点+1档": {"拒绝": sum(1 for r in rows if r["R7-容差=滑点+1档"] == "拒绝"),
                             "误伤区间内": sum(1 for r in inside if r["R7-容差=滑点+1档"] == "拒绝")},
        "滑点段已越界的腿": sum(1 for r in rows if r["滑点段是否已越界"]),
        "步长分布": {},
    }
    for r in rows:
        stat["步长分布"][str(r["步长"])] = stat["步长分布"].get(str(r["步长"]), 0) + 1

    # 三种定义的差异腿
    diff_slip = [r for r in rows if r["R7-零容差"] != r["R7-容差=滑点"]]
    diff_tick = [r for r in rows if r["R7-容差=滑点"] != r["R7-容差=滑点+1档"]]
    stat["零容差与滑点容差的差异腿"] = len(diff_slip)
    stat["滑点容差与+1档的差异腿"] = len(diff_tick)

    # ---------- 输出 ----------
    cp = os.path.join(HERE, "表2_62条腿分类.csv")
    with open(cp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)

    with open(os.path.join(HERE, "重分类.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "rule": {"R7-零容差": "fill ∉ [low,high] ⇒ 拒（定稿）",
                            "分类": "inside / round_dominant(取整≥50%) / slip_dominant(滑点>50%)",
                            "分类不改变是否拒绝": True},
                   "stat": stat, "runs": runs_meta, "legs": rows},
                  f, ensure_ascii=False, indent=1)

    L = ["PRECISION-07 / 工具 20 · 62 条成交腿按统一定义重分类", "",
         f"run 数 {stat['runs']}，腿数 {stat['legs']}；复算逐位一致 {stat['reproduced']}/{stat['legs']}",
         f"区间内 {stat['inside']}，越界 {stat['outside']}",
         f"分类：{stat['cls']}",
         f"滑点段已越界的腿：{stat['滑点段已越界的腿']}",
         f"步长分布（按腿）：{stat['步长分布']}", "",
         "R7 三种定义对比："]
    for k in ("R7-零容差", "R7-容差=滑点", "R7-容差=滑点+1档"):
        v = stat[k]
        L.append(f"  {k:22s} 拒绝 {v['拒绝']:2d} 条，误伤区间内 {v['误伤区间内']} 条")
    L += [f"  · 零容差 vs 滑点容差：结果不同的腿 {stat['零容差与滑点容差的差异腿']} 条",
          f"  · 滑点容差 vs +1档：结果不同的腿 {stat['滑点容差与+1档的差异腿']} 条", ""]
    if diff_tick:
        L.append("被「+1档」放过的腿（即滑点容差会拒、+1档会允许）：")
        for r in diff_tick:
            L.append(f"  {r['标的']} {r['腿']} {r['成交时间UTC']} 越界 {r['越界金额']}"
                     f" 容差 {r['容差(raw×slip)']:.10f} 步长 {r['步长']} 分类 {r['三段分类']}")
        L.append("")
    L.append("按标的汇总（越界腿数 / 腿数）：")
    by = {}
    for r in rows:
        b = by.setdefault(r["标的"], [0, 0])
        b[1] += 1
        if not r["在区间内"]:
            b[0] += 1
    for sym in sorted(by):
        L.append(f"  {sym:14s} {by[sym][0]}/{by[sym][1]}")
    txt = "\n".join(L)
    with open(os.path.join(HERE, "重分类原始输出.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    ok = (stat["reproduced"] == stat["legs"]) and stat["R7-零容差"]["误伤区间内"] == 0
    print(f"\n自检：复算全一致={stat['reproduced']==stat['legs']}，"
          f"零容差误伤={stat['R7-零容差']['误伤区间内']} → {'通过' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
