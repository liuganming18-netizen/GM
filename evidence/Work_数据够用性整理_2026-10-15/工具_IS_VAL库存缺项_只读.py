#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 v3.1 固定时间段（IS/Val）与预热，汇总「本窗已交付材料」中的库存/缺项。

严格边界（本窗为既有盘点窗，不新增触及 Holdout 的扫描）：
  · 本脚本 **不读任何行情/因子数值**，只读「已计算完成」的聚合产物：
      - Codex  数据完整度验收_2026-10-14/逐币逐周期冷热覆盖.csv
      - Codex  数据完整度验收_2026-10-14/冷热合并内部缺槽.csv
      - 本窗   metrics原件逐币连续性.csv
      - 本窗   metrics多空比逐币可用性.csv
  · 窗口切分只用时间戳过滤，窗口上界 < 2026-04-15T00:00:00Z，**不触达 Holdout**。

固定窗口（半开 UTC）：
  IS   = [2022-01-01, 2025-01-01)
  Val  = [2025-01-01, 2026-04-15)
  IS+V = [2022-01-01, 2026-04-15)
  预热90 = [2021-10-03, 2022-01-01)   # OI 需 90 日 + 20 根预热

输出：IS_VAL与预热_库存缺项.csv / IS_VAL与预热_库存缺项.json / IS_VAL逐币覆盖_主周期.csv
"""
import csv
import datetime as dt
import json
import os
from collections import defaultdict

BASE = "/Users/boos/Desktop/威科夫因子共振趋势"
CODEX = f"{BASE}/evidence/数据完整度验收_2026-10-14"
OUT = f"{BASE}/evidence/Work_数据够用性整理_2026-10-15"

def ms(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)

W = {
    "IS": (ms("2022-01-01T00:00:00Z"), ms("2025-01-01T00:00:00Z")),
    "Val": (ms("2025-01-01T00:00:00Z"), ms("2026-04-15T00:00:00Z")),
    "IS+Val": (ms("2022-01-01T00:00:00Z"), ms("2026-04-15T00:00:00Z")),
    "预热90(IS前)": (ms("2021-10-03T00:00:00Z"), ms("2022-01-01T00:00:00Z")),
}
MAINLINE = ["1d", "12h", "8h", "4h", "1h", "30m", "15m"]

def iso2ms(s):
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def main():
    # ---------- 1. 冷热覆盖（逐币逐周期） ----------
    cov = defaultdict(list)  # (metric,ex,mkt,period) -> [{sym,first,last,gaps,slots}]
    with open(os.path.join(CODEX, "逐币逐周期冷热覆盖.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = (r["metric"], r["exchange"], r["market"], r["period"])
            cov[k].append({
                "symbol": r["symbol"],
                "first": iso2ms(r["first_utc"]), "last": iso2ms(r["last_utc"]),
                "gaps": int(r["internal_gap_segments"] or 0),
                "slots": int(r["missing_grid_slots_candidate"] or 0),
            })

    # ---------- 2. 内部缺槽（逐段，带时间戳） ----------
    gaps = defaultdict(list)  # (metric,ex,mkt,period) -> [(prev,next,slots)]
    with open(os.path.join(CODEX, "冷热合并内部缺槽.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = (r["metric"], r["exchange"], r["market"], r["period"])
            try:
                prev = int(r["previous_ts"]); nxt = int(r["next_ts"])
            except (ValueError, KeyError):
                prev = iso2ms(r.get("from_utc", "")); nxt = iso2ms(r.get("to_utc", ""))
            if prev is None or nxt is None:
                continue
            gaps[k].append((prev, nxt, int(r["missing_slots"] or 0)))

    def gap_in(k, w0, w1):
        seg = 0; slots = 0
        for prev, nxt, s in gaps.get(k, []):
            # 缺槽区间 [prev,nxt] 与窗口 [w0,w1) 有交叠即计入
            if prev < w1 and nxt >= w0:
                seg += 1; slots += s
        return seg, slots

    # ---------- 3. 汇总表 ----------
    rows = []
    for (metric, ex, mkt, period), items in sorted(cov.items()):
        n = len(items)
        for wname, (w0, w1) in W.items():
            bracket = sum(1 for it in items if it["first"] is not None and it["last"] is not None
                          and it["first"] <= w0 and it["last"] >= w1)
            no_before = sum(1 for it in items if it["first"] is None or it["first"] > w0)
            seg, slots = gap_in((metric, ex, mkt, period), w0, w1)
            rows.append({
                "指标": metric, "交易所": ex, "市场": mkt, "周期": period, "窗口": wname,
                "币数": n, "覆盖该窗口币数": bracket, "窗口起点前无数据币数": no_before,
                "窗口内缺口段": seg, "窗口内缺槽": slots,
                "全期缺口段": sum(it["gaps"] for it in items),
                "全期缺槽": sum(it["slots"] for it in items),
            })

    with open(os.path.join(OUT, "IS_VAL与预热_库存缺项.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    # ---------- 4. 逐币主周期覆盖（candle_heads） ----------
    per_sym = []
    for (metric, ex, mkt, period), items in sorted(cov.items()):
        if metric != "candle_heads" or period not in MAINLINE:
            continue
        for it in items:
            f_, l_ = it["first"], it["last"]
            seg_is, slot_is = gap_in((metric, ex, mkt, period), *W["IS"])
            seg_v, slot_v = gap_in((metric, ex, mkt, period), *W["Val"])
            per_sym.append({
                "交易所": ex, "市场": mkt, "币": it["symbol"], "周期": period,
                "起": "" if f_ is None else dt.datetime.fromtimestamp(f_ / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "止": "" if l_ is None else dt.datetime.fromtimestamp(l_ / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "覆盖IS": int(f_ is not None and l_ is not None and f_ <= W["IS"][0] and l_ >= W["IS"][1]),
                "覆盖Val": int(f_ is not None and l_ is not None and f_ <= W["Val"][0] and l_ >= W["Val"][1]),
                "有IS前数据": int(f_ is not None and f_ <= W["IS"][0]),
                "有90天预热": int(f_ is not None and f_ <= W["预热90(IS前)"][0]),
                "IS缺口段": seg_is, "IS缺槽": slot_is,
                "Val缺口段": seg_v, "Val缺槽": slot_v,
            })
    with open(os.path.join(OUT, "IS_VAL逐币覆盖_主周期.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(per_sym[0].keys()))
        w.writeheader(); w.writerows(per_sym)

    # ---------- 5. metrics 原始归档（OI + 4 多空比列）按固定窗口 ----------
    orig = {}
    p = os.path.join(OUT, "metrics原件逐币连续性.csv")
    if os.path.exists(p):
        d = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        def dms(x):
            return ms(x + "T00:00:00Z") if x else None
        def cnt(w0, w1):
            return sum(1 for r in d if r.get("起") and r.get("止")
                       and dms(r["起"]) <= w0 and dms(r["止"]) >= w1)
        orig["原件日覆盖_币数"] = len([r for r in d if r.get("起")])
        orig["原件覆盖IS"] = cnt(*W["IS"])
        orig["原件覆盖Val"] = cnt(*W["Val"])
        orig["原件覆盖IS+Val"] = cnt(*W["IS+Val"])
        orig["原件覆盖预热90"] = cnt(*W["预热90(IS前)"])
        orig["原件全期缺日合计"] = sum(int(r["缺日"]) for r in d if r.get("缺日"))
        orig["原件起止范围"] = [min(r["起"] for r in d if r.get("起")), max(r["止"] for r in d if r.get("止"))]

    # 多空比列（M5/M6）：已有逐币可用性 JSON
    ratio = {}
    p = os.path.join(OUT, "metrics多空比逐币可用性.json")
    if os.path.exists(p):
        ratio = json.load(open(p, encoding="utf-8"))

    summary = {
        "生成说明": "按 v3.1 固定 IS/Val 与预热切分，仅复用本窗已计算聚合产物，不新增触及 Holdout 的扫描。",
        "窗口定义": {k: [dt.datetime.fromtimestamp(a / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        dt.datetime.fromtimestamp(b / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
                   for k, (a, b) in W.items()},
        "metrics原件(OI/多空比ZIP)": orig,
        "metrics多空比逐币可用性": ratio,
    }
    json.dump(summary, open(os.path.join(OUT, "IS_VAL与预热_库存缺项.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 控制台摘要
    print("汇总行", len(rows), "逐币主周期行", len(per_sym))
    for k in ("candle_heads", "oi", "funding"):
        for wname in ("IS", "Val", "预热90(IS前)"):
            agg = [r for r in rows if r["指标"] == k and r["窗口"] == wname]
            if agg:
                tot = sum(r["覆盖该窗口币数"] for r in agg)
                nos = sum(r["窗口起点前无数据币数"] for r in agg)
                seg = sum(r["窗口内缺口段"] for r in agg)
                sl = sum(r["窗口内缺槽"] for r in agg)
                print(f"  {k:13s} {wname:10s} 覆盖币数合计={tot:5d} 起点前无数据={nos:5d} 窗口内缺口段={seg:5d} 缺槽={sl:7d}")
    print("metrics原件:", json.dumps(orig, ensure_ascii=False))


if __name__ == "__main__":
    main()
