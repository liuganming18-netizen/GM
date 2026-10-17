#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-07 工具 10：订正 XMR / SOON 两条腿的归因。

要解决的两个问题（工单第 1 条）：
  A. **SOON 到底是入场还是出场** —— 不靠配对推断，直接读 `orders.jsonl` 的
     `action` / `side` / `reason` / `order_type` 实证，再用 `fills.jsonl` 交叉验证。
  B. **XMR / SOON 逐段复算** —— 原价 → 滑点后 → 取整后，三段全部用 Decimal
     按 `fill.py:124-130` 的同一套算法重算，并与记录成交价逐位比对。

判定口径（与 P06 的差别写在报告里）：
  · 参考价：入场 = bar.open；出场 `intrabar_stop` = order.stop_price；
    其余出场 = bar.open。
  · 滑点：`slippage_bps × 1e-4`，买 `raw×(1+slip)` 后 ceil、卖 `raw×(1-slip)` 后 floor。
  · 容差 = `raw × slip`（滑点允许量），仅用于与 P06 的分类判据对照，**不作为 R7 定稿**。

用法：python3 工具_10_订正两条腿.py
输出：表1_两腿订正.csv / 订正两条腿.json / 订正原始输出.txt
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

TARGETS = {
    "XMRUSDT": "run_WX_EVENT_TREND_V0_1_XMRUSDT_1792173989",
    "SOONUSDT": "run_WX_EVENT_TREND_V0_1_SOONUSDT_1792174449",
}

# 步长：这两个 run 都是修复前代际（无 instrument_spec.json、无 price_audit），
# 用 P06 的「逐 run 反推」结论 0.01；下面会用复算逐位验证，不是照抄。
STEP_DEFAULT = "0.01"


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
    """与 fill.py:124-130 完全同算法：先滑点、再按步长取整（买 ceil / 卖 floor）。"""
    slip = dec(slip_bps * 1e-4).quantize(Decimal("1e-12"))
    raw_d = dec(raw)
    st = dec(step)
    post_slip = raw_d * (Decimal(1) + slip) if is_buy else raw_d * (Decimal(1) - slip)
    post_round = ((post_slip / st).to_integral_value(rounding="ROUND_CEILING") * st
                  if is_buy else
                  (post_slip / st).to_integral_value(rounding="ROUND_FLOOR") * st)
    return {"slip": slip, "raw": raw_d, "post_slip": post_slip,
            "post_round": post_round, "slip_delta": post_slip - raw_d,
            "round_delta": post_round - post_slip}


def over_amount(price, *, is_buy, low, high):
    """越界金额（正数=越界），以及越界方向。"""
    if is_buy:
        return max(Decimal(0), price - dec(high)), "above_high"
    return max(Decimal(0), dec(low) - price), "below_low"


def classify(*, over, slip_over):
    """三段分类。**是否拒绝不看这个分类**，只看 over 是否 > 0（R7 零容差）。

    分类只回答「越界是怎么来的」：
      · inside         无越界
      · round_dominant 取整段占越界 ≥ 50%
      · slip_dominant  滑点段占越界 > 50%
    另外单独给一个布尔 `滑点段是否已越界` —— 它说明「这根 K 线本身容不下这个滑点」
    （典型是 O==L 的卖出、O==H 的买入），是模型与数据冲突的证据，不是豁免理由。
    """
    if over == 0:
        return "inside"
    return "slip_dominant" if slip_over * 2 > over else "round_dominant"


def analyze(symbol, run_id):
    d = os.path.join(RUNS, run_id)
    trades = jl(os.path.join(d, "trades.jsonl"))
    orders = {o["order_id"]: o for o in jl(os.path.join(d, "orders.jsonl"))}
    fills = jl(os.path.join(d, "fills.jsonl"))
    bars = load_bars(os.path.join(d, "chart.json"))
    inputs = json.load(open(os.path.join(d, "inputs.json"), encoding="utf-8"))
    params = inputs.get("params") or inputs.get("config") or {}
    slip_bps = float(params.get("slippage_bps", 3.0))

    fill_by_order = {f["payload"]["order_id"]: f for f in fills}

    out = []
    for t in trades:
        for leg, oid in (("入场", t["order_id"]), ("出场", t.get("exit_order_id"))):
            o = orders.get(oid)
            if not o:
                continue
            is_entry = (o.get("action") == "open")
            is_buy = (o.get("side") == "BUY")
            ts = int(o["filled_at"])
            bar = bars.get(ts)
            reason = o.get("reason") or ""

            # 参考价来源（与 engine 的真实取价一致）
            if is_entry:
                raw = bar["open"]
                raw_src = "bar.open（engine.py:441 raw_open）"
            elif reason == "intrabar_stop":
                raw = float(o.get("stop_price") or 0.0)
                raw_src = "order.stop_price（policy.py:166）"
            else:
                raw = bar["open"]
                raw_src = f"bar.open（reason={reason}，到期/缺口按开盘价）"

            st = three_stage(raw, is_buy=is_buy, step=STEP_DEFAULT, slip_bps=slip_bps)
            recorded = float(o["fill_price"])
            # 复算判定分两级：
            #   严格 = 十进制逐位一致；宽松 = 差 ≤ 1e-9（记录值常带 float 尾巴，
            #   如 132.92000000000002，那是 JSON 序列化产物，不是成交价不同）
            exact = (st["post_round"] == dec(recorded))
            near = abs(float(st["post_round"]) - recorded) <= 1e-9
            if exact:
                repro, repro_kind = True, "逐位一致"
            elif near:
                repro, repro_kind = True, "float 尾巴（值等价，差≤1e-9）"
            else:
                repro, repro_kind = False, f"不一致（差 {float(st['post_round'])-recorded:+.12g}）"

            low, high = bar["low"], bar["high"]
            over, direction = over_amount(st["post_round"], is_buy=is_buy, low=low, high=high)
            slip_over, _ = over_amount(st["post_slip"], is_buy=is_buy, low=low, high=high)
            round_over = over - slip_over
            tolerance = st["raw"] * st["slip"]

            cls = classify(over=over, slip_over=slip_over)
            # 三种 R7 变体
            r7_zero = over > 0
            r7_slip = over > tolerance
            r7_tick = over > tolerance + dec(STEP_DEFAULT)

            out.append({
                "标的": symbol, "run_id": run_id, "order_id": oid, "腿类型": leg,
                "实证依据": f"action={o.get('action')} side={o.get('side')} "
                            f"reason={reason} type={o.get('order_type')} "
                            f"status={o.get('status')}",
                "fills交叉验证": (f"fill_price={fill_by_order[oid]['payload']['fill_price']} "
                                 f"action={fill_by_order[oid]['payload']['action']}"
                                 if oid in fill_by_order else "**fills.jsonl 无此单**"),
                "成交时间UTC": utc(ts), "记录成交价": recorded,
                "参考价来源": raw_src, "参考价raw": raw,
                "滑点bps": slip_bps, "滑点率": str(st["slip"]),
                "滑点后价格": float(st["post_slip"]),
                "步长": STEP_DEFAULT, "取整后价格": float(st["post_round"]),
                "复算==记录": repro, "复算差异类型": repro_kind,
                "K线O": bar["open"], "K线H": high, "K线L": low, "K线C": bar["close"],
                "区间下界": low, "区间上界": high,
                "是否在区间内": (over == 0), "越界方向": direction if over else "inside",
                "越界金额": float(over),
                "越界%": float(over / dec(raw) * 100) if over else 0.0,
                "滑点段越界金额": float(slip_over),
                "滑点段占越界%": float(slip_over / over * 100) if over else 0.0,
                "取整段越界金额": float(round_over),
                "取整段占越界%": float(round_over / over * 100) if over else 0.0,
                "容差(raw×slip)": float(tolerance),
                "越界>容差": bool(over > tolerance),
                "R7-零容差": "拒绝" if r7_zero else "允许",
                "R7-容差=滑点": "拒绝" if r7_slip else "允许",
                "R7-容差=滑点+1档": "拒绝" if r7_tick else "允许",
                "滑点段是否已越界": bool(slip_over > 0),
                "三段分类": cls,
            })
    return out, {"slippage_bps": slip_bps, "step": STEP_DEFAULT}


def main():
    rows, meta = [], {}
    for sym, run_id in TARGETS.items():
        legs, m = analyze(sym, run_id)
        rows += legs
        meta[sym] = m

    # ---------- 与 P06 报告原分类对照 ----------
    P06_CLAIM = {
        ("XMRUSDT", "出场"): "B 类「滑点导致」→ 不该拒",
        ("SOONUSDT", "出场"): "B 类「滑点导致」→ 不该拒（越界 2.8%）",
    }
    for r in rows:
        claim = P06_CLAIM.get((r["标的"], r["腿类型"]))
        r["P06报告原分类"] = claim or "未在报告正文分类（仅在表3b 出现）"
        if r["三段分类"] == "inside":
            r["订正后分类"] = "区间内（无越界，不涉及拒绝）"
        elif r["三段分类"] == "round_dominant":
            r["订正后分类"] = ("取整主导 → 越界即拒（R7 零容差）"
                               + ("；且滑点后已越界，该 K 线容不下 3bps"
                                  if r["滑点段是否已越界"] else ""))
        else:
            r["订正后分类"] = "滑点主导（该 K 线容不下 3bps）→ 越界即拒，标记 slippage_exceeds_bar_range"

    # ---------- 输出 CSV ----------
    cols = list(rows[0].keys())
    cp = os.path.join(HERE, "表1_两腿订正.csv")
    with open(cp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    with open(os.path.join(HERE, "订正两条腿.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "params": meta, "legs": rows}, f, ensure_ascii=False, indent=1)

    # ---------- 控制台原文 ----------
    L = []
    L.append("PRECISION-07 / 工具 10 · XMR 与 SOON 逐段订正")
    L.append(f"滑点参数：{meta}（来源 inputs.json params，缺省 3.0）")
    L.append("")
    for r in rows:
        L.append(f"[{r['标的']} · {r['腿类型']}] {r['成交时间UTC']}")
        L.append(f"  实证：{r['实证依据']}")
        L.append(f"  交叉：{r['fills交叉验证']}")
        L.append(f"  参考价 {r['参考价raw']} ← {r['参考价来源']}")
        L.append(f"  滑点后 {r['滑点后价格']}（{r['滑点bps']}bps，Δ={r['滑点后价格']-r['参考价raw']:+.10f}）")
        L.append(f"  取整后 {r['取整后价格']}（step={r['步长']}）  记录 {r['记录成交价']}  "
                 f"复算：{r['复算==记录']}（{r['复算差异类型']}）")
        L.append(f"  K线 O={r['K线O']} H={r['K线H']} L={r['K线L']} C={r['K线C']}  → [{r['区间下界']}, {r['区间上界']}]")
        L.append(f"  越界 {r['越界金额']}（{r['越界%']:.6f}%）方向={r['越界方向']}")
        L.append(f"  拆解：滑点段 {r['滑点段越界金额']}（{r['滑点段占越界%']:.2f}%）+ "
                 f"取整段 {r['取整段越界金额']}（{r['取整段占越界%']:.2f}%）")
        L.append(f"  容差 raw×slip = {r['容差(raw×slip)']:.10f}；越界>容差：{r['越界>容差']}")
        L.append(f"  R7-零容差={r['R7-零容差']}  R7-容差=滑点={r['R7-容差=滑点']}  "
                 f"R7-容差=滑点+1档={r['R7-容差=滑点+1档']}")
        L.append(f"  三段分类：{r['三段分类']}（滑点段已越界={r['滑点段是否已越界']}）")
        L.append(f"  → {r['订正后分类']}")
        L.append(f"  P06 原分类：{r['P06报告原分类']}")
        L.append("")

    # ---------- 关键断言 ----------
    soon = [r for r in rows if r["标的"] == "SOONUSDT"]
    xmr = [r for r in rows if r["标的"] == "XMRUSDT"]
    checks = []
    for r in rows:
        checks.append((f"{r['标的']}-{r['腿类型']} 复算逐位一致", r["复算==记录"]))
    soon_entry = [r for r in soon if r["腿类型"] == "入场"][0]
    soon_exit = [r for r in soon if r["腿类型"] == "出场"][0]
    checks.append(("SOON 入场越界 2.85% 左右（P06 误记为出场）",
                   abs(soon_entry["越界%"] - 2.847593) < 0.01))
    checks.append(("SOON 出场在区间内（P06 记为越界 2.8%）", soon_exit["是否在区间内"]))
    xmr_exit = [r for r in xmr if r["腿类型"] == "出场"][0]
    checks.append(("XMR 出场越界 > 容差（P06 归为不该拒）", xmr_exit["越界>容差"]))
    checks.append(("XMR 出场滑点段已越界（非纯取整）", xmr_exit["滑点段越界金额"] > 0))
    checks.append(("XMR 出场滑点主导（滑点占越界 > 50%）", xmr_exit["三段分类"] == "slip_dominant"))
    checks.append(("SOON 入场取整主导（取整占越界 > 50%）", soon_entry["三段分类"] == "round_dominant"))
    checks.append(("SOON 入场取整占越界 > 98%", soon_entry["取整段占越界%"] > 98))
    checks.append(("XMR 出场滑点占越界 > 80%", xmr_exit["滑点段占越界%"] > 80))
    checks.append(("R7 零容差下：区间内的腿全部允许",
                   all(r["R7-零容差"] == "允许" for r in rows if r["三段分类"] == "inside")))
    checks.append(("R7 零容差下：越界的腿全部拒绝",
                   all(r["R7-零容差"] == "拒绝" for r in rows if r["三段分类"] != "inside")))
    L.append("关键断言：")
    for name, ok in checks:
        L.append(f"  [{'OK' if ok else 'FAIL'}] {name}")
    allok = all(ok for _, ok in checks)
    L.append("")
    L.append(f"断言汇总：{'全部通过' if allok else '存在失败'}（{sum(1 for _,ok in checks if ok)}/{len(checks)}）")

    txt = "\n".join(L)
    with open(os.path.join(HERE, "订正原始输出.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
