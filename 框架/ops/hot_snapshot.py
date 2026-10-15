#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据保全 · 热层在线快照（market_v2 + paper_3a~3d）。

每库用 **SQLite 官方在线 backup API** 生成独立时间戳一致副本，再：
  quick_check → SHA-256 → 源/副本大小 → 各库水位截点 → 表行数 → manifest + 锁。

用法：
  PY=.../3.13.12/bin/python3
  $PY hot_snapshot.py                 # 实际执行一批
  $PY hot_snapshot.py --dry-run       # 只算容量与计划，不写副本
  $PY hot_snapshot.py --capacity-only # 只报容量

跨库不天然同一事务：本脚本给每个库记录各自的 backup 起止时刻与水位截点，
不宣称「跨库同一事务一致」。一致口径见 manifest 的 cross_db_note。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops_common as OC  # noqa: E402


def _targets(cfg: dict) -> list[dict]:
    """返回待快照目标。缺库**如实标记缺失**，不用空库冒充。"""
    out = []
    hot = cfg["hot_db"]
    out.append({"name": "market_v2.sqlite3", "kind": "hot_market", "src": hot,
                "key_tables": ["candle_versions", "candle_heads", "oi_history",
                               "funding_history", "realtime_klines", "signals", "sources"]})
    pdir = cfg["paper_dir"]
    for arm in cfg["paper_arms"]:
        p = os.path.join(pdir, f"paper_{arm}.sqlite3")
        out.append({"name": f"paper_{arm}.sqlite3", "kind": "paper_ledger", "src": p,
                    "key_tables": ["paper_account", "paper_state", "paper_position",
                                   "paper_order", "paper_fill", "paper_equity",
                                   "paper_pending", "paper_blocked"]})
    return out


def _wm_specs(cfg: dict, name: str) -> list[dict]:
    return cfg.get("watermarks", {}).get(name, [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--capacity-only", action="store_true")
    a = ap.parse_args()

    cfg = OC.load_config(a.config)
    root = Path(cfg["backups_root"])
    root.mkdir(parents=True, exist_ok=True)

    tgts = _targets(cfg)
    present, missing = [], []
    for t in tgts:
        (present if os.path.isfile(t["src"]) else missing).append(t)

    est = 0
    for t in present:
        est += os.path.getsize(t["src"])
    est = int(est * 1.08) + 16 * OC.MiB

    free = OC.free_bytes(root)
    reserve = cfg["reserve_bytes"]
    print(f"[容量] 备份根 = {root}")
    print(f"[容量] 可用 = {OC.human(free)}；预计新增 = {OC.human(est)}；保留护栏 = {OC.human(reserve)}")
    print(f"[容量] 扣预留后余量 = {OC.human(free - est - reserve)}")
    ok = (free - est) >= reserve
    print(f"[容量] 判定 = {'OK' if ok else 'BLOCKED(不足)'}")
    for t in missing:
        print(f"[缺失] {t['name']} 不存在：{t['src']} —— 如实报告，不用空库冒充")
    if a.capacity_only:
        return 0 if ok else 3
    if not ok:
        print("[中止] 容量不足，未执行任何副本写入。")
        return 3

    batch_id = a.batch_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bdir = root / batch_id
    if a.dry_run:
        print(f"[dry-run] 将写入 {bdir}，目标 {len(present)} 库；不实际执行。")
        return 0

    bdir.mkdir(parents=True, exist_ok=True)
    lock = bdir / "batch.lock"
    OC.jdump({"pid": os.getpid(), "host": socket.gethostname(),
              "batch_id": batch_id, "started_at_ms": int(time.time() * 1000),
              "state": "running"}, lock)

    manifest = {
        "batch_id": batch_id,
        "generated_at_ms": int(time.time() * 1000),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": socket.gethostname(),
        "safe_root": cfg["safe_root"],
        "method": "sqlite3 online backup API (mode=ro source, query_only=ON)",
        "cross_db_note": ("各库用同一批次窗口内**独立**的在线 backup；"
                          "每个库记录自身 backup 起止时刻与水位截点。"
                          "跨库**不宣称**同一事务一致。"),
        "reserve_bytes": reserve,
        "free_before_bytes": free,
        "estimated_new_bytes": est,
        "missing_sources": [{"name": m["name"], "src": m["src"]} for m in missing],
        "databases": [],
        "status": "running",
    }

    fail = 0
    for t in present:
        name = t["name"]
        dst = bdir / name
        rec = {"name": name, "kind": t["kind"], "source": t["src"],
               "copy": str(dst)}
        t0 = time.time()
        try:
            src_size_before = os.path.getsize(t["src"])
            src_mtime_before = os.path.getmtime(t["src"])
            info = OC.sqlite_backup(t["src"], dst)
            rec.update(info)
            rec["source_bytes_before"] = src_size_before
            rec["source_bytes_after"] = os.path.getsize(t["src"])
            rec["source_mtime_before"] = src_mtime_before
            rec["source_is_live"] = (rec["source_bytes_after"] != src_size_before)
            rec["backup_started_ms"] = int(t0 * 1000)
            rec["backup_finished_ms"] = int(time.time() * 1000)
            rec["copy_quick_check"] = OC.quick_check(dst)
            rec["copy_sha256"] = OC.sha256_file(dst)
            rec["copy_bytes"] = os.path.getsize(dst)
            rec["table_counts"] = OC.table_counts(dst, t["key_tables"])
            rec["watermarks"] = OC.watermark(dst, _wm_specs(cfg, name))
            if rec["copy_quick_check"] != "ok":
                fail += 1
                rec["error"] = "quick_check 非 ok"
        except Exception as e:
            fail += 1
            rec["error"] = f"{type(e).__name__}: {e}"
        manifest["databases"].append(rec)
        print(f"[快照] {name}: {OC.human(rec.get('copy_bytes', 0))} "
              f"quick_check={rec.get('copy_quick_check', 'ERR')} "
              f"sha={str(rec.get('copy_sha256', ''))[:16]}…")

    manifest["status"] = "ok" if fail == 0 else "partial"
    manifest["failures"] = fail
    manifest["finished_at_ms"] = int(time.time() * 1000)
    OC.jdump(manifest, bdir / "manifest.json")
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    print(f"[完成] 批次 {batch_id} 状态={manifest['status']} 失败={fail}")
    print(f"[完成] manifest: {bdir / 'manifest.json'}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
