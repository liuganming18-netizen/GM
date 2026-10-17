#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-05 工具：把核验结果落成两张交付表（CSV，UTF-8-SIG 便于 Excel 直接打开）。

输入（都是本窗口已生成/已复核过的）：
  四笔成交逐笔核验.json      ← 工具_逐笔核验.py
  ../PRECISION-04/六标的逐时间段证据.json  ← 已由 工具_复核PRECISION04.py 独立重算验证 0 差异

输出：
  表1_六标的证据对照.csv
  表2_四笔成交逐笔核验.csv
"""
from __future__ import annotations

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
P04 = os.path.join(ROOT, "evidence", "PRECISION-04")

LABEL = {"tick": "价格步长", "qty_step": "数量步长", "min_qty": "最小数量",
         "max_qty": "最大数量(LOT)", "mkt_max_qty": "最大数量(MARKET)",
         "min_notional": "最小名义额"}
GRADE = {"A": "A 可确认", "B": "B 部分覆盖", "C": "C 无法确认"}


def table1():
    d = json.load(open(os.path.join(P04, "六标的逐时间段证据.json"), encoding="utf-8"))
    out = os.path.join(HERE, "表1_六标的证据对照.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["标的", "窗口起(UTC)", "窗口止(UTC)", "时间段起(UTC)", "时间段止(UTC)",
                    "段名", "段内观测数", "字段", "等级", "值", "证据时间/来源",
                    "缺口与说明", "公告是否覆盖该字段"])
        for s in d["symbols"]:
            ws, we = s["window"]
            for seg in s["segments"]:
                t0, t1 = seg["segment"].split(" → ")
                n = len(seg["in_segment_observations"])
                for f, label in LABEL.items():
                    c = seg["fields"][f]
                    src = c.get("source") or "—"
                    note = c.get("note") or ""
                    covered = ("否：该字段从不进公告，只能靠快照"
                               if f in ("max_qty", "mkt_max_qty") else
                               "是（仅 tick 变更类公告）" if f == "tick" else "部分（上线公告）")
                    w.writerow([s["symbol"], ws, we, t0, t1, seg["name"], n, label,
                                GRADE[c["grade"]], "" if c["value"] is None else c["value"],
                                src, note, covered])
    return out


def table2():
    d = json.load(open(os.path.join(HERE, "四笔成交逐笔核验.json"), encoding="utf-8"))
    out = os.path.join(HERE, "表2_四笔成交逐笔核验.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["标的", "run_id", "腿", "order_id", "成交方向", "订单类型",
                    "成交时间(UTC)", "成交价", "K线周期", "K线起(UTC)", "K线止(UTC)",
                    "O", "H", "L", "C", "是否在[L,H]内", "越界方向",
                    "越界幅度(相对最近边界%)", "相对开盘偏离%",
                    "复现所用基准", "能复现该价的步长", "按公告tick应为(仅价格)",
                    "运行时代码指纹(combined)", "fill.py运行时sha256", "证据路径"])
        for r in d["runs"]:
            if "error" in r:
                continue
            fp = r["runtime_fingerprint"]
            for leg in r["legs"]:
                w.writerow([
                    r["symbol"], r["run_id"], leg["leg"], leg["order_id"],
                    leg["side"], leg["order_type"], leg["fill_time_utc"],
                    leg["fill_price"], r["chart_exec_timeframe"],
                    leg.get("bar_start_utc", ""), leg.get("bar_end_utc", ""),
                    leg.get("bar_open", ""), leg.get("bar_high", ""),
                    leg.get("bar_low", ""), leg.get("bar_close", ""),
                    leg.get("inside_low_high", ""), leg.get("overshoot_side", ""),
                    round(leg.get("overshoot_pct", 0.0), 4),
                    round(leg.get("dev_from_open_pct") or 0.0, 4),
                    leg.get("reproduce_base_used") or "（未能复现）",
                    "/".join(leg.get("steps_that_reproduce") or []) or "（无）",
                    leg.get("counterfactual_price_at_announced_tick", ""),
                    fp["code_combined_sha256"], fp["fill_py_sha256"],
                    os.path.join(r["run_dir"], "trades.jsonl"),
                ])
    return out


if __name__ == "__main__":
    for p in (table1(), table2()):
        print("写出:", p)
