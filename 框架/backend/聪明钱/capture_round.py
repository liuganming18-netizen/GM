#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集**轮次**证据抓取（只读本机快照 + 共享账本；**零网络**）。

用途：在持续采集推进过程中，按轮抓取「快照落盘时点 / 本机取回时点 / 源响应 SHA」，
用于证明 `snapshot_refreshed_at` 与 `fetched_at` **确实在推进**（而非一次取样）。

用法：
    python3 聪明钱/capture_round.py --label round1 [--symbols ARXUSDT,BTCUSDT,...]
输出：聪明钱/rounds/<label>.json 并打印摘要。
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import smart_money as sm                                       # noqa: E402

OUT_DIR = HERE / "rounds"
DEFAULT_SYMBOLS = ("ARXUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "1000000BOBUSDT")


def _sha_for(call_id):
    """从共享账本取该次上游请求的正文 SHA256 / HTTP 状态 / 字节数（只读）。"""
    return sm._call_sha(sm._gw().store, call_id)


def capture(symbols):
    idx = sm.read_index()
    rows = []
    for sym in symbols:
        snap = sm.read("binance", sym)
        if snap is None:
            rows.append({"symbol": sym, "snapshot": None,
                         "note": "无本机快照（本轮未覆盖或该合约无数据）"})
            continue
        refs = []
        for r in (snap.get("raw_refs") or []):
            sha = _sha_for(r.get("call_id"))
            refs.append({"endpoint": r.get("endpoint"), "call_id": r.get("call_id"),
                         "origin": r.get("origin"), "fetched_at": r.get("fetched_at"),
                         "sha256": (sha or {}).get("sha256"),
                         "http_status": (sha or {}).get("http_status"),
                         "bytes": (sha or {}).get("bytes")})
        rows.append({
            "symbol": sym,
            "available": snap.get("available"),
            "snapshot_refreshed_at": snap.get("snapshot_refreshed_at"),
            "snapshot_refreshed_at_ms": snap.get("snapshot_refreshed_at_ms"),
            "local_fetched_at": snap.get("local_fetched_at"),
            "trade_event_time": snap.get("trade_event_time"),
            "upstream_snapshot_time": snap.get("upstream_snapshot_time"),
            "update_time": snap.get("update_time"),
            "quality": sm._freshness(snap),
            "source_refs": refs,
        })
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "index": idx,
        "collector": sm._collector_liveness(idx),
        "coverage": {k: sm.coverage()[k] for k in
                     ("symbols_on_disk", "freshness", "age_seconds", "stale_after_s",
                      "continuous_set", "collector")},
        "symbols": rows,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="输出标签（文件名）")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    a = ap.parse_args(argv)
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    body = capture(syms)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{a.label}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written {path}")
    print("index targets =", body["index"].get("targets"),
          "| ok =", body["index"].get("ok"),
          "| elapsed_s =", body["index"].get("elapsed_s"),
          "| updated_at =", body["index"].get("updated_at"))
    print("collector =", json.dumps(body["collector"], ensure_ascii=False))
    print("coverage =", json.dumps(body["coverage"]["freshness"], ensure_ascii=False),
          "| symbols_on_disk =", body["coverage"]["symbols_on_disk"],
          "| stale_after_s =", body["coverage"]["stale_after_s"])
    for r in body["symbols"]:
        if r.get("snapshot") is None and r.get("available") is None:
            print(f"  {r['symbol']}: {r.get('note')}")
            continue
        q = r["quality"]
        print(f"  {r['symbol']}: refreshed_at={r['snapshot_refreshed_at']} "
              f"local_fetched_at={r['local_fetched_at']} "
              f"quality={q['status']}(age {q['age_seconds']}s)")
        for ref in r["source_refs"]:
            print(f"      {ref['endpoint']:14s} fetched_at={ref['fetched_at']} "
                  f"sha256={(ref['sha256'] or '')[:16]}… bytes={ref['bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
