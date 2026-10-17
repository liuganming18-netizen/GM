#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 30：影子整改（只在内存里加闸门，**不动生产代码**）。

做什么
──────
定义三个「影子适配器」子类，各自加一条候选闸门，然后把 12 条腿（及全池 62 条腿）
重新过一遍，量化每条闸门：

  · 能拦住多少条越界腿（覆盖率）
  · 会误伤多少条**区间内**的正常成交（误杀率）

候选闸门
────────
  R6   `price_step > raw_price` ⇒ 拒绝（最粗的合理性检查）
  R6'  `price_step > ratio × raw_price` ⇒ 拒绝（ratio 可调，默认 1%）
  R7   `fill` 越出该 bar `[low, high]` 超过「滑点允许量」⇒ 拒绝
       （滑点允许量 = slip_rate × raw_price；不含整档，避免把粗步长洗成合法）

⚠️ 影子子类只存在于本进程内存里：`SimulatedBarFillAdapter` 本身、`engine.py`、
   `engine_adapter.py` **一行未改**。跑完即失效。

用法：python3 工具_30_影子整改.py
输出：影子整改.json / 表5_影子整改覆盖.csv
"""
from __future__ import annotations

import csv
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

sys.path.insert(0, os.path.join(ROOT, "策略"))
from WyckoffMaster.trading_core.fill import (  # noqa: E402
    FillRejected, SimulatedBarFillAdapter,
)

STEP = "0.01"
RATIO_SWEEP = [0.001, 0.005, 0.01, 0.02, 0.05]


# ══════════════════════════ 影子闸门 ══════════════════════════

class ShadowR6(SimulatedBarFillAdapter):
    """step > raw_price ⇒ 拒绝。"""

    def _guard(self, *, raw_price, price_step, kind):
        super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
        raw = Decimal(str(raw_price))
        step = Decimal(str(price_step))
        if step > raw:
            raise FillRejected("price_step_coarser_than_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "stage": kind})


class ShadowR6Ratio(SimulatedBarFillAdapter):
    """step > ratio × raw_price ⇒ 拒绝（ratio 可调）。"""

    ratio = Decimal("0.01")

    def _guard(self, *, raw_price, price_step, kind):
        super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
        raw = Decimal(str(raw_price))
        step = Decimal(str(price_step))
        if step > raw * self.ratio:
            raise FillRejected("price_step_too_coarse_for_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "ratio": str(self.ratio), "stage": kind})


class ShadowR7(SimulatedBarFillAdapter):
    """成交价越出 bar 区间超过「滑点允许量」⇒ 拒绝。

    滑点允许量 = slippage_rate × raw_price（不含整档）。
    理由：滑点模型本身就会把成交价推到边界外一点点，这部分是**模型假设**；
    超出滑点允许量的部分只可能来自取整，属结构性错误。
    """

    def _check(self, *, fill, raw, bar_low, bar_high, kind):
        if bar_low is None or bar_high is None:
            return
        allow = abs(float(raw)) * self.slippage_rate
        low_lim = float(bar_low) - allow
        high_lim = float(bar_high) + allow
        if not (low_lim <= float(fill) <= high_lim):
            raise FillRejected("fill_outside_bar_range",
                               {"fill": repr(float(fill)), "bar_low": float(bar_low),
                                "bar_high": float(bar_high),
                                "allowance": allow, "stage": kind})

    def quote_entry(self, *, raw_price, direction, price_step, stop_price,
                    bar_low=None, bar_high=None):
        q = super().quote_entry(raw_price=raw_price, direction=direction,
                                price_step=price_step, stop_price=stop_price)
        self._check(fill=q.entry_fill, raw=raw_price, bar_low=bar_low,
                    bar_high=bar_high, kind="entry")
        return q

    def quote_exit(self, *, target_px, direction, price_step, reason,
                   bar_low=None, bar_high=None):
        px = super().quote_exit(target_px=target_px, direction=direction,
                                price_step=price_step, reason=reason)
        self._check(fill=px, raw=target_px, bar_low=bar_low, bar_high=bar_high,
                    kind="exit")
        return px


# ══════════════════════════ 重放 ══════════════════════════

def adapter(cls, **kw):
    return cls(slippage_bps=3.0, maker_fee_rate=2e-4, taker_fee_rate=5e-4,
               strict=True, **kw)


def try_entry(a, *, raw, step, stop, bar):
    try:
        q = a.quote_entry(raw_price=raw, direction=1, price_step=step, stop_price=stop,
                          bar_low=bar["low"], bar_high=bar["high"]) \
            if isinstance(a, ShadowR7) else \
            a.quote_entry(raw_price=raw, direction=1, price_step=step, stop_price=stop)
        return "FILL", q.entry_fill, ""
    except FillRejected as e:
        return "REJECT", None, e.reason


def try_exit(a, *, raw, step, reason, bar):
    try:
        px = a.quote_exit(target_px=raw, direction=1, price_step=step, reason=reason,
                          bar_low=bar["low"], bar_high=bar["high"]) \
            if isinstance(a, ShadowR7) else \
            a.quote_exit(target_px=raw, direction=1, price_step=step, reason=reason)
        return "FILL", px, ""
    except FillRejected as e:
        return "REJECT", None, e.reason


def replay_legs(legs, make_adapter):
    out = []
    for L in legs:
        if L.get("blocked"):
            continue
        a = make_adapter()
        inp = L["strict_input"]
        if L["leg"] == "entry":
            res, px, rej = try_entry(a, raw=inp["raw_price"], step=inp["price_step"],
                                     stop=inp["stop_price"], bar=L["bar"])
        else:
            res, px, rej = try_exit(a, raw=inp["raw_price"], step=inp["price_step"],
                                    reason=inp["reason"], bar=L["bar"])
        out.append({"leg_id": L["leg_id"], "symbol": L["symbol"], "leg": L["leg_label"],
                    "inside": L["inside_bar"], "outcome": res, "reason": rej,
                    "baseline_outcome": L["strict_result"]["outcome"]})
    return out


def coverage(rows):
    outside = [r for r in rows if not r["inside"]]
    inside = [r for r in rows if r["inside"]]
    return {
        "legs": len(rows),
        "outside_total": len(outside),
        "outside_rejected": sum(1 for r in outside if r["outcome"] == "REJECT"),
        "inside_total": len(inside),
        "inside_rejected": sum(1 for r in inside if r["outcome"] == "REJECT"),
        "baseline_rejected": sum(1 for r in rows if r["baseline_outcome"] == "REJECT"),
        "rejected_ids": [r["leg_id"] for r in rows if r["outcome"] == "REJECT"],
        "false_positive_ids": [r["leg_id"] for r in inside if r["outcome"] == "REJECT"],
    }


def main():
    with open(os.path.join(HERE, "逐腿核验.json"), encoding="utf-8") as f:
        doc = json.load(f)
    scope = doc["scope6_legs"]
    alllegs = doc["scan"]["legs"]

    variants = {}
    variants["baseline_strict_only"] = {
        "desc": "现状：strict=True，无任何区间/步长合理性检查",
        "make": lambda: adapter(SimulatedBarFillAdapter),
    }
    variants["R6_step_gt_raw"] = {
        "desc": "step > raw_price ⇒ 拒绝",
        "make": lambda: adapter(ShadowR6),
    }
    for ratio in RATIO_SWEEP:
        def mk(r=ratio):
            class _V(ShadowR6Ratio):
                pass
            _V.ratio = Decimal(str(r))
            return adapter(_V)
        variants[f"R6ratio_{ratio:g}"] = {
            "desc": f"step > {ratio:g} × raw_price ⇒ 拒绝", "make": mk}
    variants["R7_bar_range"] = {
        "desc": "fill 越出 bar[low,high] 超过 滑点允许量 ⇒ 拒绝",
        "make": lambda: adapter(ShadowR7),
    }

    def mk_combo(ratio):
        class _V(ShadowR7):
            _ratio = Decimal(str(ratio))

            def _guard(self, *, raw_price, price_step, kind):
                super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
                raw = Decimal(str(raw_price))
                step = Decimal(str(price_step))
                if step > raw * self._ratio:
                    raise FillRejected("price_step_too_coarse_for_price",
                                       {"raw_price": str(raw), "price_step": str(step),
                                        "ratio": str(self._ratio), "stage": kind})
        return adapter(_V)

    variants["R6ratio_0.01_plus_R7"] = {
        "desc": "step > 1% × raw ⇒ 拒绝；且 fill 越出区间超过滑点允许量 ⇒ 拒绝",
        "make": lambda: mk_combo(0.01),
    }

    result = {"step": STEP, "scope6": {}, "all": {}}
    for name, v in variants.items():
        result["scope6"][name] = dict(coverage(replay_legs(scope, v["make"])), desc=v["desc"])
        result["all"][name] = dict(coverage(replay_legs(alllegs, v["make"])), desc=v["desc"])

    with open(os.path.join(HERE, "影子整改.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    with open(os.path.join(HERE, "表5_影子整改覆盖.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["方案", "说明", "范围", "腿数", "越界腿", "拦下越界腿",
                    "漏掉越界腿", "区间内腿", "误伤区间内腿", "拦截率(越界)", "误杀率(区间内)"])
        for name in variants:
            for scope_name, tag in (("scope6", "6个P05 run"), ("all", "全池")):
                c = result[scope_name][name]
                w.writerow([
                    name, c["desc"], tag, c["legs"], c["outside_total"],
                    c["outside_rejected"], c["outside_total"] - c["outside_rejected"],
                    c["inside_total"], c["inside_rejected"],
                    round(c["outside_rejected"] / c["outside_total"] * 100, 2)
                    if c["outside_total"] else None,
                    round(c["inside_rejected"] / c["inside_total"] * 100, 2)
                    if c["inside_total"] else None,
                ])

    print("=" * 104)
    print("影子整改：各候选闸门的覆盖率 / 误杀率（生产代码未改动，仅进程内子类）")
    print("=" * 104)
    for scope_name, tag in (("scope6", "6 个 P05 run（12 腿）"), ("all", "全池（62 腿）")):
        print(f"\n—— {tag} ——")
        print(f"{'方案':<28}{'越界腿':>7}{'拦下':>6}{'漏掉':>6}{'区间内':>7}{'误伤':>6}"
              f"{'拦截率':>9}{'误杀率':>9}")
        for name in variants:
            c = result[scope_name][name]
            ir = (c["outside_rejected"] / c["outside_total"] * 100) if c["outside_total"] else 0
            fr = (c["inside_rejected"] / c["inside_total"] * 100) if c["inside_total"] else 0
            print(f"{name:<28}{c['outside_total']:>7}{c['outside_rejected']:>6}"
                  f"{c['outside_total']-c['outside_rejected']:>6}{c['inside_total']:>7}"
                  f"{c['inside_rejected']:>6}{ir:>8.1f}%{fr:>8.1f}%")
    print("\n—— 全池：各方案拦下的越界腿（按标的） ——")
    for name in variants:
        ids = result["all"][name]["rejected_ids"]
        syms = sorted({i.split("::")[0].split("_")[4] for i in ids})
        print(f"{name:<28} n={len(ids):<3} {','.join(syms[:12])}{' …' if len(syms) > 12 else ''}")
    print("\n→ evidence/PRECISION-06/影子整改.json")
    print("→ evidence/PRECISION-06/表5_影子整改覆盖.csv")


if __name__ == "__main__":
    main()
