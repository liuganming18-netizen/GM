#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-04 工具：生成「六标的逐时间段证据表」。

等级定义（本报告口径，与 instrument_spec.py 的 verified 不是同一件事）：
  A 可确认   —— 该时段内存在官方证据，直接给出该字段的值。
                只主张「段内某时点确实是这个值」，**不主张整段恒定**；
                若段内观测到变化，另记 `observed_changes_in_segment`。
  B 部分覆盖 —— 段内无观测，但段外两侧（或上线公告）证据一致，只能约束不能坐实。
  C 无法确认 —— 没有任何可定位到该段的官方证据。

数据来源：公告规格变更明细.json + 规格时间线.json。不引入第三方行情数据，
不从 K 线小数位推断 tick。
"""
import datetime
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TL = json.load(open(os.path.join(HERE, "规格时间线.json"), encoding="utf-8"))

SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]

WINDOWS = {
    "SPACEUSDT":    ("2026-01-31T20:00:00Z", "2026-07-30T19:00:00Z"),
    "AIGENSYNUSDT": ("2026-05-07T23:00:00Z", "2026-09-14T06:00:00Z"),
    "TRUTHUSDT":    ("2025-10-09T20:00:00Z", "2026-04-07T20:00:00Z"),
    "COTIUSDT":     ("2024-01-01T00:00:00Z", "2024-06-30T00:00:00Z"),
    "BNBUSDT":      ("2024-01-01T00:00:00Z", "2024-06-30T00:00:00Z"),
    "ORDIUSDT":     ("2024-01-01T00:00:00Z", "2024-06-30T00:00:00Z"),
}

# 官方上线公告：(上线/生效时刻, 公告发布时刻, 该公告是否给出规格表, 字段覆盖)
LAUNCH = {
    "SPACEUSDT":    ("2026-01-23T11:45:00Z", "2026-01-23T10:16:02Z", True,
                     {"tick": "0.000001", "min_qty": "1", "min_notional": "5"}),
    "AIGENSYNUSDT": ("2026-04-29T14:15:00Z", "2026-04-29T13:27:04Z", True,
                     {"tick": "0.00001", "min_qty": "1", "min_notional": "5"}),
    "TRUTHUSDT":    ("2025-10-01T12:00:00Z", "2025-09-29T06:00:03Z", True,
                     {"tick": "0.000001"}),   # 表内无 Minimum Trade Amount / Min Notional 行
    "ORDIUSDT":     ("2023-11-07T12:30:00Z", "2023-11-07T07:30:03Z", True,
                     {"tick": "0.001"}),      # 旧格式：无 Minimum Trade Amount 行
    "COTIUSDT":     ("2021-03-10T07:00:00Z", "2021-03-09T07:48:02Z", False, {}),
    "BNBUSDT":      ("2020-02-10T08:00:00Z", "2020-02-07T10:43:21Z", False, {}),
}

FIELDS = [("tick", "价格步长"), ("qty_step", "数量步长"), ("min_qty", "最小数量"),
          ("max_qty", "最大数量(LOT)"), ("mkt_max_qty", "最大数量(MARKET)"),
          ("min_notional", "最小名义额")]

# 上线公告**自述其规格表是暂定值**的标的。
# 交易所明说该值会在上线前 15 分钟更新 ⇒ 这张表只能当「计划值」，
# 不能当「上线之后那段时期实际生效的值」的证据，因此不给 B 而给 C。
LAUNCH_PROVISIONAL = {
    "TRUTHUSDT": "公告原文：Tick size details will be updated 15 minutes before the launch time.",
}

# 已由官方公告确认的变更点，在分段时切开（否则一个段里混着两种规格）
EXTRA_SPLITS = {
    "BNBUSDT": ["2021-05-11T03:30:00Z"],
    "COTIUSDT": ["2026-07-21T06:40:00Z"],
}

MAP = {"tick": "tick", "qty_step": "lot_step", "min_qty": "lot_min",
       "max_qty": "lot_max", "mkt_max_qty": "mkt_max", "min_notional": "min_notional"}


def obs(sym):
    """按**时刻**去重，不按文件去重。

    本地那两份 futures 快照的 sha256 不同（serverTime 差 2ms），但 serverTime 精确到秒
    完全一致 ⇒ 是同一瞬间的两次落盘，只能算 1 个观测点。按 sha 去重会把它们算成 2 个，
    凭空多出一个"证据时点"。
    """
    seen, out = set(), []
    for t in TL["timeline"]:
        p = t["present"].get(sym)
        if not p:
            continue
        key = t.get("server_utc") or t["sha256"]
        if key in seen:
            continue
        seen.add(key)
        out.append({"utc": t["server_utc"], "kind": t["kind"], "file": t["file"],
                    "spec": {k: p.get(v) for k, v in MAP.items()},
                    "order_types": p["orderTypes"], "time_in_force": p["timeInForce"]})
    return out


def grade_field(sym, field, t0, t1, o, launch):
    """返回 (grade, value, source, note)。"""
    in_seg = [x for x in o if t0 <= x["utc"] <= t1]
    before = [x for x in o if x["utc"] < t0]
    after = [x for x in o if x["utc"] > t1]
    lv = launch[3].get(field)

    if in_seg:
        vals = [x["spec"][field] for x in in_seg]
        nchg = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        note = "段内观测到 %d 次变化" % nchg if nchg else "段内观测值一致"
        return ("A", vals[-1], in_seg[-1]["utc"], note)

    # 段内无观测
    if field in ("max_qty", "mkt_max_qty"):
        return ("C", None, None, "该字段从未出现在任何官方公告里，段内又没有快照 ⇒ 无从确认")

    if lv is not None and (not before or before[-1]["spec"][field] == lv):
        if sym in LAUNCH_PROVISIONAL:
            return ("C", None, None,
                    "上线公告的规格表自述为暂定值（上线前 15 分钟更新）⇒ 不能作为上线后时段的证据；"
                    "计划值仅作参考：" + str(lv))
        return ("B", lv, f"上线公告 {launch[0]}", "只有上线公告给出起点值，段内无快照")

    if before and after and before[-1]["spec"][field] == after[0]["spec"][field]:
        return ("B", before[-1]["spec"][field],
                f"段前 {before[-1]['utc']} 与段后 {after[0]['utc']} 两端一致",
                "两端一致，但按纪律不构成『段内未变更』的证明")

    return ("C", None, None, "段外证据不一致或缺一侧")


def build():
    rows = []
    for sym in SYMBOLS:
        ws, we = WINDOWS[sym]
        launch = LAUNCH[sym]
        o = obs(sym)
        last = o[-1]["utc"] if o else we
        bounds = sorted({launch[0], ws, we, last} |
                        {x for x in EXTRA_SPLITS.get(sym, []) if launch[0] < x < last})
        segs = []
        for t0, t1 in zip(bounds, bounds[1:]):
            name = ("上线 → 窗口开始" if t1 == ws else
                    "窗口内" if t0 == ws else
                    "窗口后 → 最近观测" if t1 == last else "变更点切分")
            rec = {"segment": f"{t0} → {t1}", "name": name,
                   "in_segment_observations": [x["utc"] for x in o if t0 <= x["utc"] <= t1],
                   "fields": {}}
            for f, _label in FIELDS:
                g, v, src, note = grade_field(sym, f, t0, t1, o, launch)
                rec["fields"][f] = {"grade": g, "value": v, "source": src, "note": note}
            segs.append(rec)
        rows.append({
            "symbol": sym,
            "launch": {"effective_utc": launch[0], "announced_utc": launch[1],
                       "has_spec_table": launch[2], "announced_fields": launch[3]},
            "window": [ws, we],
            "observations": o,
            "segments": segs,
        })

    out = {
        "generated_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grade_definitions": {
            "A": "该时段内存在官方证据，直接给出该字段值；只主张『段内某时点确实是这个值』，不主张整段恒定",
            "B": "段内无观测，只有段外（上线公告 / 段前段后快照）证据，只能约束不能坐实",
            "C": "没有任何可定位到该段的官方证据",
        },
        "warning": "A 级不等于 verified，也不等于可用于正式回测。verified 还要求『覆盖整窗的变更证据』，"
                   "本窗口无法诚实出具该记录。",
        "symbols": rows,
    }
    with open(os.path.join(HERE, "六标的逐时间段证据.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for r in rows:
        print("=" * 78)
        print(r["symbol"], "窗口", r["window"][0], "→", r["window"][1])
        for s in r["segments"]:
            print(" ", s["name"], "|", s["segment"])
            print("    段内观测:", len(s["in_segment_observations"]),
                  s["in_segment_observations"][:3], "…" if len(s["in_segment_observations"]) > 3 else "")
            for f, lab in FIELDS:
                d = s["fields"][f]
                print(f"      {lab:<14} {d['grade']}  {str(d['value']):<12} {d['note']}")


if __name__ == "__main__":
    build()
