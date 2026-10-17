#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-07 工具 30：解释测试结果，并把「三层」严格分开。

工单第 3 条要求：逐项解释 91 passed / 2 failed，说明 2 xfailed 未满足的条件，
且**禁止混称通过**。本工具因此把结论分三层，任何一层都不许借另一层的光：

  L1 当前引擎测试   —— 不加任何新闸门时的现状契约。93 passed 只说明「现在的代码
                       自洽」，不说明越界成交合格，也不说明 R7 已生效。
  L2 影子规则回放   —— 进程内子类 / 离线复算，验证「如果实现 R7 会怎样」。
                       它不是生产代码，跑完即失效。
  L3 端到端验收     —— 生产代码里真的有了闸门、真的跑一次回测。**现在没有**，
                       对应 2 个 xfailed。

数据来源（不重跑回测）：
  · `evidence/PRECISION-06/整改影响面.json`（加 R6 影子后的 91/2 明细）
  · `evidence/PRECISION-06/原始输出/pytest_既有93项_加R6影子.txt`
  · `evidence/PRECISION-06/隔离复现/test_p06_整改验收草案.py`（xfail marker）
  · 本窗口 `pytest --collect-only` 的用例构成（93 = 35+25+15+13+5）

用法：python3 工具_30_测试结果解释.py
输出：表3_测试结果与失败原因.csv / 测试结果解释.json / 测试解释原始输出.txt
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
P06 = os.path.join(ROOT, "evidence", "PRECISION-06")

TARGETS = ["策略/WyckoffMaster/event_trend_v0_1/tests/",
           "策略/WyckoffMaster/test_work02_core_contract.py"]

# 两条红：根因与改法（根因从 longrepr 读出，改法见报告 4.2）
FAILED = [
    {
        "层": "L1 当前引擎测试（加 R6 影子后）",
        "nodeid": "tests/test_fill_precision.py::test_price_below_one_cent_with_cent_step_must_be_rejected",
        "断言": 'ei.value.reason == "nonpositive_or_nonfinite_exit_fill"',
        "期望": "nonpositive_or_nonfinite_exit_fill（R5：取整后为 0）",
        "实际": "price_step_coarser_than_price（R6：step 0.01 > raw 0.004076）",
        "根因": ("该用例的价格 0.004076 本身小于步长 0.01，R6 在 R5 之前就拒了，"
                 "所以拿不到 R5 的 reason。这是**闸门顺序**问题，不是取整逻辑坏了。"),
        "改法": ("二选一，推荐前者：① 断言放宽为「属于这两类之一」并各自断言 detail；"
                 "② 把用例价格改成 ≥ 步长（如 0.015），只让 R5 触发。"),
        "是否表示修复错": "否 —— 用例测的是 R5 文案，被 R6 抢先；行为方向正确（都拒）",
    },
    {
        "层": "L1 当前引擎测试（加 R6 影子后）",
        "nodeid": "tests/test_fill_precision.py::test_exit_failure_keeps_position_open_and_marks_run_invalid",
        "断言": 'eng["open_positions_remaining"] 非空（出场失败 ⇒ 仓位仍开）',
        "期望": "非空列表（有未平仓位）",
        "实际": "[]（空）",
        "根因": ("该用例入场价 0.004 < 步长 0.01，**入场就被 R6 拒**，压根没建仓，"
                 "自然没有「未平仓位」。用例前提被 R6 改掉了。"),
        "改法": ("把入场价抬到 ≥ 步长（如 0.05 / 0.02），只让出场那腿触发 R5 的零价，"
                 "保住用例原意（出场失败不得当成已平仓）。"),
        "是否表示修复错": "否 —— 用例前提失效，不是被测行为错",
    },
]

XFAILED = [
    {
        "层": "L3 端到端验收（未实现）",
        "nodeid": "test_p06_整改验收草案.py::test_FIX_01_step_coarser_than_price_is_rejected",
        "xfail_reason": "整改未实施：fill.py 无 price_step 与价格的合理性闸门",
        "未满足条件": "`fill.py` 未实现 R6（step > raw ⇒ FillRejected('price_step_coarser_than_price')）",
        "转绿条件": "生产代码加 R6 后，删掉 xfail marker，应转绿并同时校验 detail 三件套",
    },
    {
        "层": "L3 端到端验收（未实现）",
        "nodeid": "test_p06_整改验收草案.py::test_FIX_02_out_of_range_fill_is_rejected_end_to_end",
        "xfail_reason": "整改未实施：引擎无「成交价必须落在该 bar 区间」校验",
        "未满足条件": ("`engine.py` 4 个成交点未做区间校验；且该用例的 marker 文案写的是"
                       "「超过滑点允许量」，与 P07 定稿的**零容差**不一致，需同步改"),
        "转绿条件": "生产代码加 R7（零容差）后转绿；断言以「入场被拒 + run_valid False + trades_count 0」为准，不绑 reason 字符串",
    },
]


def collect_structure():
    """只收集不执行，拿到 93 项的构成（不跑任何用例）。"""
    p = subprocess.run([sys.executable, "-m", "pytest", *TARGETS, "--collect-only", "-q",
                        "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True)
    counts = {}
    total = 0
    for ln in p.stdout.splitlines():
        ln = ln.strip()
        if "::" not in ln or ln.startswith("="):
            continue
        f = ln.split("::")[0].split("/")[-1]
        counts[f] = counts.get(f, 0) + 1
        total += 1
    return counts, total, p.returncode


def main():
    impact = json.load(open(os.path.join(P06, "整改影响面.json"), encoding="utf-8"))
    counts, total, rc = collect_structure()

    rows = []
    for f in FAILED:
        rows.append({"层": f["层"], "类型": "failed", "用例": f["nodeid"],
                     "断言/条件": f["断言"], "期望": f["期望"], "实际": f["实际"],
                     "根因": f["根因"], "改法": f["改法"],
                     "是否表示修复错": f["是否表示修复错"]})
    for x in XFAILED:
        rows.append({"层": x["层"], "类型": "xfailed", "用例": x["nodeid"],
                     "断言/条件": x["xfail_reason"], "期望": x["未满足条件"],
                     "实际": "未实现（xfail 生效）", "根因": x["未满足条件"],
                     "改法": x["转绿条件"], "是否表示修复错": "不适用（这是待落地契约）"})

    cp = os.path.join(HERE, "表3_测试结果与失败原因.csv")
    with open(cp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)

    doc = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "L1_current_engine_tests": {
            "collected": total, "by_file": counts,
            "baseline_no_gate": {"passed": 93, "failed": 0,
                                 "note": "P06 实测：不加任何闸门时 93 passed"},
            "with_shadow_R6": {"passed": impact["passed"], "failed": impact["failed_count"],
                               "note": "P06 实测：进程内子类加 R6 后 91/2"},
        },
        "L2_shadow_replay": {
            "what": "进程内子类 / 离线复算，不是生产代码",
            "R7_zero_tolerance": {"reject": 27, "legs": 62, "false_positive": 0,
                                  "source": "evidence/PRECISION-07/重分类.json"},
            "note": "L2 结果不能当作 L3 通过",
        },
        "L3_end_to_end": {"implemented": False, "xfailed": len(XFAILED),
                          "note": "生产代码无 R6/R7，端到端跑不出「越界被拒」"},
        "failed": FAILED, "xfailed": XFAILED,
    }
    with open(os.path.join(HERE, "测试结果解释.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    L = ["PRECISION-07 / 工具 30 · 测试结果解释（三层严格分开）", "",
         "【L1 当前引擎测试】不加任何新闸门",
         f"  收集到 {total} 项，按文件：{json.dumps(counts, ensure_ascii=False)}",
         "  现状：93 passed / 0 failed  —— 只证明现有代码自洽，不证明越界成交合格",
         f"  加 R6 影子后：{impact['passed']} passed / {impact['failed_count']} failed", ""]
    for i, f_ in enumerate(FAILED, 1):
        L += [f"  [F{i}] {f_['nodeid']}",
              f"      断言：{f_['断言']}",
              f"      期望：{f_['期望']}",
              f"      实际：{f_['实际']}",
              f"      根因：{f_['根因']}",
              f"      改法：{f_['改法']}",
              f"      是否修复错了：{f_['是否表示修复错']}", ""]
    L += ["【L2 影子规则回放】不是生产代码，跑完即失效",
          "  R7 零容差：62 腿中拒 27 条、误伤 0 条（见 重分类.json）",
          "  ⚠ 这一层再漂亮也不能当 L3 通过", "",
          "【L3 端到端验收】生产代码真的有了闸门 —— 现在没有"]
    for i, x in enumerate(XFAILED, 1):
        L += [f"  [X{i}] {x['nodeid']}",
              f"      xfail 原因：{x['xfail_reason']}",
              f"      未满足条件：{x['未满足条件']}",
              f"      转绿条件：{x['转绿条件']}", ""]
    L += ["【三层对照】",
          "  L1 93 passed  → 现状契约，不代表合格",
          "  L2 27/27 拦截 → 规则效果，不代表已上线",
          "  L3 2 xfailed  → 端到端未实现，禁止写成「已通过」", ""]
    txt = "\n".join(L)
    with open(os.path.join(HERE, "测试解释原始输出.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)

    ok = (total == 93 and impact["passed"] == 91 and impact["failed_count"] == 2
          and len(XFAILED) == 2)
    print(f"自检：收集 {total}（应 93）、91/2、2 xfailed → {'通过' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
