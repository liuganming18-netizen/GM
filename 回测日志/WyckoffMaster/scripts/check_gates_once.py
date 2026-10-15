#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""有界、低频的门禁状态检查（探测 → 一次快照 → 退出，不常驻空转）。

设计目的：门禁（strategy_ready.json / data_ready.json）由其它窗口异步发布。
本窗口**不轮询空转**，而是：每次调用只做一次有界探测，写一份快照 JSON，然后退出。
调度由外层（人工/定时器）按需触发；检查本身毫秒级、无副作用、不启动回测。

用法：
    python3 check_gates_once.py            # 单次探测
    python3 check_gates_once.py --loop 6 --interval 300   # 最多 6 次、每次间隔 300s（有界）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
from pathlib import Path

PROJECT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
WM = PROJECT / "回测日志" / "WyckoffMaster"
TARGETS = {
    "strategy_ready": PROJECT / "evidence" / "Work_策略优化_2026-10-15" / "strategy_ready.json",
    "data_ready": PROJECT / "evidence" / "Work_策略数据_2026-10-15" / "data_ready.json",
}
STATE = WM / "state" / "gate_probe.json"
UTC = dt.timezone.utc


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def probe() -> dict:
    now = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = {}
    for name, p in TARGETS.items():
        if p.exists():
            st = p.stat()
            items[name] = {"exists": True, "path": str(p), "bytes": st.st_size,
                           "mtime_utc": dt.datetime.fromtimestamp(st.st_mtime, UTC)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "sha256": _sha(p)}
        else:
            items[name] = {"exists": False, "path": str(p)}
    ready = all(v["exists"] for v in items.values())
    return {"schema": "wyckoffmaster_gate_probe/v1", "checked_utc": now,
            "both_published": ready, "items": items}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1, help="最多探测次数（有界）")
    ap.add_argument("--interval", type=float, default=300.0, help="每次间隔秒（低频）")
    a = ap.parse_args()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for i in range(max(1, a.loop)):
        last = probe()
        STATE.write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{last['checked_utc']}] both_published={last['both_published']} "
              + " ".join(f"{k}={'yes' if v['exists'] else 'no'}"
                         for k, v in last["items"].items()))
        if last["both_published"]:
            print("两个门禁均已发布 → 下一步：python3 scripts/verify_gates.py（独立核查，不信 ready=true）")
            return 0
        if i < a.loop - 1:
            time.sleep(a.interval)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
