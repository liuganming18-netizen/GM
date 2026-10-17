#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-06 工具 50：整改影响面 —— 加了 R6 之后，既有 93 项会红几个？

做法
────
用 pytest 插件在 session 开始时把 `SimulatedBarFillAdapter` 换成带 R6 闸门的子类
（同时替换 `engine_adapter` 模块引用与测试模块的直接引用），然后**原样**跑既有 93 项。
生产文件一行未改，进程退出即还原。

为什么必须做这一步
──────────────────
最小整改方案的「影响面」不能靠推演。R6（`step > raw` ⇒ 拒绝）会改变两类既有用例的
执行路径，必须实测出来，才能在整改 PR 里一并调整。

用法：python3 工具_50_既有测试影响面.py
输出：原始输出/pytest_既有93项_加R6影子.txt / 整改影响面.json
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

TARGETS = [
    os.path.join(ROOT, "策略/WyckoffMaster/event_trend_v0_1/tests/"),
    os.path.join(ROOT, "策略/WyckoffMaster/test_work02_core_contract.py"),
]


class ShadowR6(SimulatedBarFillAdapter):
    def _guard(self, *, raw_price, price_step, kind):
        super()._guard(raw_price=raw_price, price_step=price_step, kind=kind)
        raw, step = Decimal(str(raw_price)), Decimal(str(price_step))
        if step > raw:
            raise FillRejected("price_step_coarser_than_price",
                               {"raw_price": str(raw), "price_step": str(step),
                                "stage": kind})


class PatchPlugin:
    """把闸门注入到「引擎实际使用的类」与「测试模块自己的类引用」两处。

    注意：`策略/.../tests/` 下没有 `__init__.py`，pytest 会以 rootdir 相对名
    （如 `test_fill_precision`）再导入一次，与 `WyckoffMaster...tests.test_fill_precision`
    是两个不同的模块对象。所以必须在 `pytest_runtest_setup` 里按 `item.module` 打补丁，
    否则补丁打在了没人用的那份模块上（第一次跑就是这么漏掉的）。
    """

    def pytest_sessionstart(self, session):
        import WyckoffMaster.event_trend_v0_1.engine_adapter as EA
        EA.SimulatedBarFillAdapter = ShadowR6

    def pytest_runtest_setup(self, item):
        m = getattr(item, "module", None)
        if m is not None and hasattr(m, "SimulatedBarFillAdapter"):
            m.SimulatedBarFillAdapter = ShadowR6


class ResultCollector:
    def __init__(self):
        self.failed = []
        self.passed = 0
        self.other = []

    def pytest_runtest_logreport(self, report):
        if report.when != "call":
            return
        if report.passed:
            self.passed += 1
        elif report.failed:
            self.failed.append({"nodeid": report.nodeid,
                                "longrepr": str(report.longrepr)[-1200:]})
        else:
            self.other.append(report.nodeid)


def main():
    outdir = os.path.join(HERE, "原始输出")
    os.makedirs(outdir, exist_ok=True)
    log = os.path.join(outdir, "pytest_既有93项_加R6影子.txt")

    collector = ResultCollector()
    args = ["-q", "--basetemp=" + os.path.join(HERE, "tmp_pytest"),
            "-p", "no:cacheprovider"] + TARGETS
    with open(log, "w", encoding="utf-8") as f:
        old = sys.stdout
        sys.stdout = f
        try:
            code = pytest.main(args, plugins=[PatchPlugin(), collector])
        finally:
            sys.stdout = old

    doc = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shadow_gate": "R6: price_step > raw_price ⇒ FillRejected",
        "exit_code": int(code),
        "passed": collector.passed,
        "failed": collector.failed,
        "failed_count": len(collector.failed),
        "log": os.path.relpath(log, ROOT),
    }
    with open(os.path.join(HERE, "整改影响面.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    print("=" * 96)
    print("整改影响面：既有 93 项在「加 R6」影子下的结果")
    print("=" * 96)
    print(f"pytest 退出码：{doc['exit_code']}   通过：{doc['passed']}   "
          f"失败：{doc['failed_count']}")
    for f_ in collector.failed:
        print(f"\n✗ {f_['nodeid']}")
        tail = [ln for ln in f_["longrepr"].splitlines()
                if ln.strip().startswith("E ") or "assert" in ln][-4:]
        for ln in tail:
            print("   ", ln.strip()[:160])
    print(f"\n→ {doc['log']}")
    print("→ evidence/PRECISION-06/整改影响面.json")


if __name__ == "__main__":
    main()
