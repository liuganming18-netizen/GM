#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据保全 · 不可变防删保护（chmod a-w + chflags uchg）与证据。

规则（按总体方案 §五）：
  · **只保护**「已完成校验的 rescued 原件」与「已校验 ZIP」等静态文件；
  · **不锁** 正在写入的热库 / WAL / SHM / 活动状态清单 / 下载临时件 / 整个可写父目录；
  · 只对**文件**加旗标（`-type f`），**不给目录加 uchg**（否则会阻断新增文件）；
  · 跳过最近 `--min-age-min` 分钟内改动过的文件，避免碰到正在写的件。

诚实的边界：`chflags uchg` / `chmod a-w` 只防「误删/误改」，**同一拥有者仍可撤销**，
不构成账号级不可绕过的只读隔离。强隔离需目录 ACL / 独立账号，见 README。

用法：
  $PY protect_immutable.py --plan                      # 只列计划与清单，不落旗标
  $PY protect_immutable.py --apply --evidence out.json # 实际落旗标并出证据
  $PY protect_immutable.py --verify --evidence out.json# 只读核验当前旗标
  $PY protect_immutable.py --unprotect --evidence out.json  # 可逆回滚（去旗标）
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops_common as OC  # noqa: E402

UF_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0x00000002)

# ── 静态 rescued 原件（已完成 quick_check + integrity_check 校验） ─────────────
RESCUED_FILES = [
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
    "热库抢救_2026-10-14/rescued/market_v2.sqlite3",
]
# 同目录 -wal/-shm 按「不锁 WAL/SHM」规则**不纳入**保护，只登记说明。

# ── 已校验 ZIP 语料根（官方 CHECKSUM 随件） ────────────────────────────────
ZIP_ROOTS = [
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw",
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics",
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_rest_gap",
]
ZIP_SUFFIXES = (".zip", ".zip.CHECKSUM")


def _flags(path: str) -> int:
    try:
        return os.stat(path, follow_symlinks=False).st_flags
    except Exception:
        return 0


def _mode(path: str) -> int:
    try:
        return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    except Exception:
        return 0


def _snapshot_meta(path: str) -> dict:
    st = os.stat(path, follow_symlinks=False)
    return {"mode": oct(stat.S_IMODE(st.st_mode)), "flags": st.st_flags,
            "uchg": bool(st.st_flags & UF_IMMUTABLE),
            "size": st.st_size, "mtime": int(st.st_mtime)}


def _protect_file(path: str) -> dict:
    before = _snapshot_meta(path)
    if before["uchg"]:
        return {"path": path, "before": before, "after": before,
                "changed": False, "skipped": "已 uchg"}
    os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) & ~0o222)  # 去写位
    os.chflags(path, before["flags"] | UF_IMMUTABLE)             # 加 uchg
    after = _snapshot_meta(path)
    return {"path": path, "before": before, "after": after,
            "changed": (before != after)}


def _unprotect_file(path: str) -> dict:
    before = _snapshot_meta(path)
    os.chflags(path, before["flags"] & ~UF_IMMUTABLE)
    os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) | 0o200)
    after = _snapshot_meta(path)
    return {"path": path, "before": before, "after": after, "changed": (before != after)}


def _walk_zip(root: str, min_age_min: float):
    now = time.time()
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(ZIP_SUFFIXES):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            age_min = (now - st.st_mtime) / 60.0
            yield p, age_min


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "apply", "verify", "unprotect"], default="plan")
    ap.add_argument("--min-age-min", type=float, default=10.0)
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--paths-gz", default=None, help="把受保护路径清单写成 .gz")
    ap.add_argument("--limit", type=int, default=0, help="每个 ZIP 根最多处理 N 个文件（0=不限）")
    ap.add_argument("--roots", default=None,
                    help="逗号分隔的 ZIP 根覆盖（默认用内置 ZIP_ROOTS）")
    ap.add_argument("--skip-rescued", action="store_true", help="跳过 rescued 原件（后续分阶段用）")
    a = ap.parse_args()
    zip_roots = [x.strip() for x in a.roots.split(",")] if a.roots else ZIP_ROOTS

    t0 = time.time()
    ev = {"mode": a.mode, "started_at_ms": int(t0 * 1000),
          "host": os.uname().nodename,
          "min_age_min": a.min_age_min,
          "policy": "仅文件加 uchg + chmod a-w；不给目录加旗标；不锁 WAL/SHM/活动状态/临时件",
          "rescued_files": [], "zip_roots": [], "paths_file": None,
          "caveat": "文件旗标不能替代账号权限/目录 ACL；同一拥有者仍可撤销。"}

    # 1) rescued 原件（小集，逐件出前后 SHA/模式/旗标）
    for p in ([] if a.skip_rescued else RESCUED_FILES):
        if not os.path.isfile(p):
            ev["rescued_files"].append({"path": p, "status": "MISSING"})
            continue
        rec = {"path": p, "sha256_before": OC.sha256_file(p)}
        if a.mode == "apply":
            rec.update(_protect_file(p))
        elif a.mode == "unprotect":
            rec.update(_unprotect_file(p))
        else:
            rec["meta"] = _snapshot_meta(p)
        rec["sha256_after"] = OC.sha256_file(p)
        rec["sha_unchanged"] = rec["sha256_before"] == rec["sha256_after"]
        ev["rescued_files"].append(rec)

    # 2) ZIP 语料（只文件，跳过新改动件）
    paths_all: list[str] = []
    for root in zip_roots:
        if not os.path.isdir(root):
            ev["zip_roots"].append({"root": root, "status": "MISSING"})
            continue
        total = flagged = skipped_new = 0
        bytes_total = 0
        sample = []
        for p, age_min in _walk_zip(root, a.min_age_min):
            total += 1
            if a.limit and flagged >= a.limit:
                continue
            if age_min < a.min_age_min:
                skipped_new += 1
                continue
            if a.mode in ("plan", "verify"):
                pass
            elif a.mode == "apply":
                if not _snapshot_meta(p)["uchg"]:
                    _protect_file(p)
            elif a.mode == "unprotect":
                if _snapshot_meta(p)["uchg"]:
                    _unprotect_file(p)
            meta = _snapshot_meta(p)
            if meta["uchg"]:
                flagged += 1
                paths_all.append(p)
            bytes_total += meta["size"]
            if len(sample) < 3:
                sample.append({"path": p, "mode": meta["mode"], "uchg": meta["uchg"]})
        ev["zip_roots"].append({"root": root, "files_total": total,
                                "files_flagged": flagged, "files_skipped_recent": skipped_new,
                                "bytes_flagged": bytes_total, "sample": sample})

    if a.paths_gz and paths_all:
        with gzip.open(a.paths_gz, "wt", encoding="utf-8") as f:
            f.write("\n".join(sorted(paths_all)) + "\n")
        ev["paths_file"] = {"path": a.paths_gz, "count": len(paths_all)}

    ev["finished_at_ms"] = int(time.time() * 1000)
    ev["elapsed_s"] = round(time.time() - t0, 1)
    if a.evidence:
        OC.jdump(ev, a.evidence)
    # 摘要
    for r in ev["rescued_files"]:
        print(f"[rescued] {Path(r['path']).name}: {r.get('status', 'ok')} "
              f"sha一致={r.get('sha_unchanged')} uchg={r.get('after', r.get('meta', {})).get('uchg')}")
    for r in ev["zip_roots"]:
        print(f"[zip] {r.get('root')}: total={r.get('files_total')} "
              f"flagged={r.get('files_flagged')} skip_new={r.get('files_skipped_recent')} "
              f"({OC.human(r.get('bytes_flagged', 0))})")
    print(f"[完成] mode={a.mode} 用时 {ev['elapsed_s']}s 受保护ZIP={len(paths_all)}")
    if a.evidence:
        print(f"[写出] {a.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
