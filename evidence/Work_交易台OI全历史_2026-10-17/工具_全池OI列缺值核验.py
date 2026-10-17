#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全池「总OI持仓」列缺值核验（只读；等价于界面第 6 列的数据源）。

界面第 6 列取 `/api/v1/markets` 每行的 `open_interest_usd`；本脚本对全池逐行判定
「缺失」= `None` 或空串（**不把 0 当缺失**，也不把缺失当 0），并按交易所拆分。

用法：
    python3 工具_全池OI列缺值核验.py
结果写入 `对账_全池OI列缺值_2026-10-17.json`。
"""
from __future__ import annotations

import json
import os
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE = os.environ.get("OI_PROBE_BASE", "http://127.0.0.1:8787")
HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ["open_interest_usd", "open_interest_total", "oi_time"]


def main():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(f"{BASE}/api/v1/markets?limit=1000", timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    items = d.get("items") or []

    miss = {f: [] for f in FIELDS}
    for it in items:
        for f in FIELDS:
            v = it.get(f)
            if v is None or v == "":
                miss[f].append(it.get("market_id") or it.get("symbol"))

    print(f"全池标的数 = {len(items)}")
    for f in FIELDS:
        print(f"  {f:<22} 缺值 {len(miss[f]):>4}  样例 {miss[f][:8]}")
    print("按交易所:", dict(Counter(i.get("exchange") for i in items)))
    byex = Counter(i.get("exchange") for i in items if i.get("open_interest_usd") in (None, ""))
    print("OI 缺值按交易所:", dict(byex) or "无缺值")

    out = {
        "生成时间": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "接口": f"{BASE}/api/v1/markets?limit=1000",
        "全池标的数": len(items),
        "缺值": {f: {"数量": len(miss[f]), "样例": miss[f][:20]} for f in FIELDS},
        "按交易所总数": dict(Counter(i.get("exchange") for i in items)),
        "OI缺值按交易所": dict(byex),
    }
    p = os.path.join(HERE, "对账_全池OI列缺值_2026-10-17.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("写入:", os.path.basename(p))


if __name__ == "__main__":
    main()
