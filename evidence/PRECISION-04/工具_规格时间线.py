#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-04 工具：把「所有可定位的官方响应时点」串成六标的规格时间线。

输入（全部只读）：
  · 存档快照/*.json                      —— Internet Archive 捕获的官方 exchangeInfo
  · 数据/contracts/旧标的池/exchangeInfo_futures_*.json —— 本地两份（同一次响应）
  · 当前响应_exchangeInfo.json            —— 本窗口实抓

输出 `规格时间线.json`：
  · 每个时点的四类时间、sha256、来源
  · 六标的在每个时点的 tick / qtyStep / minQty / maxQty / minNotional
  · **只报「相邻两次观测不同」的跳变**，跳变的确切生效时刻另由公告确定
  · 明确区分「观测到的变化」与「未观测到变化」（后者不是「未变更」）
"""
import datetime
import glob
import hashlib
import json
import os
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
CONTRACTS = os.path.join(ROOT, "数据/contracts/旧标的池")
SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def dstr(v):
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v)).normalize()
        return format(d, "f")
    except Exception:  # noqa: BLE001
        return str(v)


def spec_of(doc_sym, order_type="MARKET"):
    fl = {f.get("filterType"): f for f in (doc_sym.get("filters") or [])}
    pf = fl.get("PRICE_FILTER") or {}
    lot = fl.get("LOT_SIZE") or {}
    mkt = fl.get("MARKET_LOT_SIZE") or {}
    mn = fl.get("MIN_NOTIONAL") or fl.get("NOTIONAL") or {}
    return {
        "tick": dstr(pf.get("tickSize")),
        "price_min": dstr(pf.get("minPrice")),
        "price_max": dstr(pf.get("maxPrice")),
        "lot_step": dstr(lot.get("stepSize")),
        "lot_min": dstr(lot.get("minQty")),
        "lot_max": dstr(lot.get("maxQty")),
        "mkt_step": dstr(mkt.get("stepSize")),
        "mkt_min": dstr(mkt.get("minQty")),
        "mkt_max": dstr(mkt.get("maxQty")),
        "min_notional": dstr(mn.get("notional")),
        "pricePrecision": doc_sym.get("pricePrecision"),
        "quantityPrecision": doc_sym.get("quantityPrecision"),
        "orderTypes": doc_sym.get("orderTypes"),
        "timeInForce": doc_sym.get("timeInForce"),
        "onboard_ms": doc_sym.get("onboardDate"),
        "status": doc_sym.get("status"),
    }


def collect():
    out = []
    # 存档捕获只取通过自洽校验的那批（serverTime 与捕获时刻相差 ≤24h）
    idx_path = os.path.join(HERE, "存档快照索引.json")
    usable = set()
    if os.path.exists(idx_path):
        idx = json.load(open(idx_path, encoding="utf-8"))
        usable = {c["local_file"] for c in idx["captures"] if c.get("usable")}
    for p in sorted(glob.glob(os.path.join(HERE, "存档快照", "*.json"))):
        if usable and os.path.basename(p) not in usable:
            continue
        doc = json.load(open(p, encoding="utf-8"))
        st = doc.get("serverTime")
        if not st:
            continue
        out.append({"kind": "archive_capture", "path": p,
                    "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest(),
                    "server_ms": st, "server_utc": iso(st), "doc": doc})
    for p in sorted(glob.glob(os.path.join(CONTRACTS, "exchangeInfo_futures_*.json"))):
        doc = json.load(open(p, encoding="utf-8"))
        out.append({"kind": "local_snapshot", "path": p,
                    "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest(),
                    "server_ms": doc.get("serverTime"), "server_utc": iso(doc.get("serverTime")),
                    "doc": doc})
    p = os.path.join(HERE, "当前响应_exchangeInfo.json")
    if os.path.exists(p):
        doc = json.load(open(p, encoding="utf-8"))
        out.append({"kind": "live_fetch", "path": p,
                    "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest(),
                    "server_ms": doc.get("serverTime"), "server_utc": iso(doc.get("serverTime")),
                    "doc": doc})
    out.sort(key=lambda x: (x["server_ms"], x["sha256"]))
    return out


def main():
    snaps = collect()
    timeline = []
    for s in snaps:
        syms = {x["symbol"]: x for x in (s["doc"].get("symbols") or [])}
        rec = {"kind": s["kind"], "server_utc": s["server_utc"], "server_ms": s["server_ms"],
               "sha256": s["sha256"], "file": os.path.basename(s["path"]),
               "symbol_count": len(syms), "present": {}, "absent": []}
        for sym in SYMBOLS:
            if sym in syms:
                rec["present"][sym] = spec_of(syms[sym])
            else:
                rec["absent"].append(sym)
        timeline.append(rec)

    # 跳变：只看相邻两次「都有该标的」的观测
    changes = {}
    for sym in SYMBOLS:
        seen = [(t["server_ms"], t) for t in timeline if sym in t["present"]]
        ch = []
        for (m0, a), (m1, b) in zip(seen, seen[1:]):
            sa, sb = a["present"][sym], b["present"][sym]
            diff = {k: {"from": sa.get(k), "to": sb.get(k)}
                    for k in sa if k not in ("orderTypes", "timeInForce") and sa.get(k) != sb.get(k)}
            if diff:
                ch.append({"between": [iso(m0), iso(m1)], "from_file": a["file"],
                           "to_file": b["file"], "diff": diff})
        changes[sym] = ch

    out = {"generated_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "observations": len(timeline), "timeline": timeline, "observed_changes": changes}
    with open(os.path.join(HERE, "规格时间线.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"观测时点 {len(timeline)} 个：")
    for t in timeline:
        print(f"  {t['server_utc']}  {t['kind']:<15} 合约数={t['symbol_count']:<4} "
              f"在场={len(t['present'])} 缺={t['absent']}")
    print("\n观测到的跳变：")
    for sym in SYMBOLS:
        ch = changes[sym]
        print(f"  {sym}: {len(ch)} 处")
        for c in ch:
            for k, v in c["diff"].items():
                print(f"     {c['between'][0]} → {c['between'][1]}  {k}: {v['from']} → {v['to']}")


if __name__ == "__main__":
    main()
