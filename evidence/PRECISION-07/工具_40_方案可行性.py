#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-07 工具 40：整改影响面的**方案对比**（R6 的插入位置）。

为什么值得单独测
────────────────
P06 把 R6 挂在 `_guard`（取整**之前**），结果抢在 R5 前面，把
`test_price_below_one_cent_with_cent_step_must_be_rejected` 的 reason 从
`nonpositive_or_nonfinite_exit_fill` 改成了 `price_step_coarser_than_price`，
多红了一条。

如果 R6 放在取整**之后**（R5 之后），那么「0.004076 用 0.01 步长卖出」这类用例
会先被 R5 拦住，reason 不变 ⇒ 该用例**保持绿色**。整改面因此从「改 2 条用例」
缩到「改 1 条用例」。这是实测，不是推断。

两个影子（都在进程内子类，生产文件一行未改）：
  S1  R6 挂在 `_guard`      —— P06 的做法，作为对照基线
  S2  R6 挂在取整之后        —— 本窗口建议

**R7 无法用这种方式测**：`FillAdapter.quote_entry/quote_exit` 协议里没有
`bar_low`/`bar_high`（`fill.py:179-183`），影子类拿不到区间，只能在 `engine.py`
里落地后实测。所以 R7 的既有测试影响面**本窗口给不出数字**，只给 L2 离线回放结论。
这一点在报告里单独标注，不许含糊。

用法：python3 工具_40_方案可行性.py
输出：原始输出/pytest_既有93项_R6前置.txt / pytest_既有93项_R6后置.txt / 方案对比.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "策略"))

import pytest                                                              # noqa: E402
from WyckoffMaster.trading_core.fill import (                              # noqa: E402
    FillRejected, SimulatedBarFillAdapter,
)

TARGETS = [os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/tests/"),
           os.path.join(ROOT, "策略/WyckoffMaster/test_work02_core_contract.py")]


class ShadowR6Before(SimulatedBarFillAdapter):
    """S1：R6 在 _guard（取整前）。与 P06 的 ShadowR6 等价。"""

    def _guard(self, *, raw_price, price_step, kind):
        super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
        raw, step = Decimal(str(raw_price)), Decimal(str(price_step))
        if step > raw:
            raise FillRejected("price_step_coarser_than_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "stage": kind})


class ShadowR6After(SimulatedBarFillAdapter):
    """S2：R6 在取整之后（R5 之后）。先让 R5 判零价，再判步长与价格的关系。"""

    def _r6(self, raw_price, price_step, kind):
        raw, step = Decimal(str(raw_price)), Decimal(str(price_step))
        if step > raw:
            raise FillRejected("price_step_coarser_than_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "stage": kind + "_post_round"})

    def quote_entry(self, *, raw_price, direction, price_step, stop_price=None):
        q = super().quote_entry(raw_price=raw_price, direction=direction,
                                price_step=price_step, stop_price=stop_price)
        self._r6(raw_price, price_step, "entry")
        return q

    def quote_exit(self, *, target_px, direction, price_step, reason):
        px = super().quote_exit(target_px=target_px, direction=direction,
                                price_step=price_step, reason=reason)
        self._r6(target_px, price_step, "exit")
        return px


def make_plugin(cls):
    class PatchPlugin:
        def pytest_sessionstart(self, session):
            import WyckoffMaster.event_trend_v0_1.engine_adapter as EA
            EA.SimulatedBarFillAdapter = cls

        def pytest_runtest_setup(self, item):
            m = getattr(item, "module", None)
            if m is not None and hasattr(m, "SimulatedBarFillAdapter"):
                m.SimulatedBarFillAdapter = cls
    return PatchPlugin()


class ResultCollector:
    def __init__(self):
        self.failed, self.passed = [], 0

    def pytest_runtest_logreport(self, report):
        if report.when != "call":
            return
        if report.passed:
            self.passed += 1
        elif report.failed:
            self.failed.append({"nodeid": report.nodeid,
                                "longrepr": str(report.longrepr)[-1500:]})


def run_shadow(cls, tag):
    outdir = os.path.join(HERE, "原始输出")
    os.makedirs(outdir, exist_ok=True)
    log = os.path.join(outdir, f"pytest_既有93项_{tag}.txt")
    col = ResultCollector()
    args = ["-q", "--basetemp=" + os.path.join(HERE, "tmp_pytest"),
            "-p", "no:cacheprovider"] + TARGETS
    with open(log, "w", encoding="utf-8") as f:
        old = sys.stdout
        sys.stdout = f
        try:
            code = pytest.main(args, plugins=[make_plugin(cls), col])
        finally:
            sys.stdout = old
    return {"tag": tag, "class": cls.__name__, "exit_code": int(code),
            "passed": col.passed, "failed_count": len(col.failed),
            "failed": col.failed, "log": os.path.relpath(log, ROOT)}


def main():
    s1 = run_shadow(ShadowR6Before, "R6前置")
    s2 = run_shadow(ShadowR6After, "R6后置")

    doc = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseline": {"passed": 93, "failed": 0, "note": "不加任何闸门（P06 实测）"},
        "S1_R6_before_round": s1,
        "S2_R6_after_round": s2,
        "R7_not_measurable_by_shadow": (
            "FillAdapter.quote_entry/quote_exit（fill.py:179-183）不接收 bar_low/bar_high，"
            "影子类拿不到区间 ⇒ R7 的既有测试影响面只能等 engine.py 落地后实测；"
            "本窗口只提供 L2 离线回放（62 腿：拒 27、误伤 0）。"),
    }
    with open(os.path.join(HERE, "方案对比.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    L = ["PRECISION-07 / 工具 40 · R6 插入位置的方案对比", "",
         f"基线（无闸门）：93 passed / 0 failed", "",
         f"S1 R6 在 _guard（取整前，P06 做法）：{s1['passed']} passed / {s1['failed_count']} failed"]
    for f_ in s1["failed"]:
        L.append(f"    ✗ {f_['nodeid'].split('::')[-1]}")
    L += ["", f"S2 R6 在取整之后（本窗口建议）：{s2['passed']} passed / {s2['failed_count']} failed"]
    for f_ in s2["failed"]:
        L.append(f"    ✗ {f_['nodeid'].split('::')[-1]}")
    L += ["", "R7 为何不给影响面数字：",
          "  FillAdapter 协议只收 raw_price / price_step（fill.py:179-183），",
          "  影子类拿不到 bar_low / bar_high ⇒ 无法在进程内注入 R7。",
          "  必须在 engine.py 落地后实测；本窗口只给 L2 离线回放（62 腿拒 27、误伤 0）。", ""]
    txt = "\n".join(L)
    with open(os.path.join(HERE, "方案对比原始输出.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)

    ok = (s1["passed"] == 91 and s1["failed_count"] == 2
          and s2["passed"] >= 91 and s2["failed_count"] <= 2)
    print(f"自检：S1=91/2（与 P06 一致）={s1['passed']==91 and s1['failed_count']==2}；"
          f"S2={s2['passed']}/{s2['failed_count']} → {'通过' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
