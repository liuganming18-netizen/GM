#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据保全与进程保活 · ops 公共库（纯标准库）。

复用要点（不重复造轮子）：
  · 一致性副本 = **SQLite 官方在线 backup API**（`sqlite3.Connection.backup`），
    不是文件级 cp；源库一律 `mode=ro` + `PRAGMA query_only=ON`。
  · 容量护栏 = `os.statvfs`（本卷 `df` 才权威，`stat -f %a` 对 APFS 不可靠）。
  · 长驻启动 = 复用既有 `后端实现/spawn_detached.py`（本库只封装，不重写）。
  · 云传输 = 复用系统 rclone（本库只读状态，不读密钥、不执行删除类动作）。

纪律：本库只读源库；写只落 `ops/` 与 `backups/`。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = OPS_DIR / "config" / "ops_config.json"

MiB = 1024 * 1024


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 容量 ────────────────────────────────────────────────────────────────────
def free_bytes(path: str | os.PathLike) -> int:
    """目标路径所在卷的可用字节（以 df 的权威口径 = statvfs）。"""
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


def human(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:.2f}{unit}"
        n /= 1024.0
    return f"{n:.2f}PiB"


# ── 哈希 ────────────────────────────────────────────────────────────────────
def sha256_file(path: str | os.PathLike, chunk: int = 8 * MiB) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


# ── SQLite 在线备份 ─────────────────────────────────────────────────────────
def sqlite_backup(src: str | os.PathLike, dst: str | os.PathLike) -> dict:
    """用官方在线 backup API 生成一致性副本。源库只读打开，不动源库。

    返回 {src_bytes, dst_bytes, src_pages, dst_pages, elapsed_ms}。
    """
    src = str(src)
    dst = str(dst)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        src_con.execute("PRAGMA query_only=ON")
        src_pages = src_con.execute("PRAGMA page_count").fetchone()[0]
        dst_con = sqlite3.connect(dst, timeout=30)
        try:
            src_con.backup(dst_con)
            dst_con.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst_con.close()
    finally:
        src_con.close()
    return {
        "src_bytes": os.path.getsize(src),
        "dst_bytes": os.path.getsize(dst),
        "src_pages": src_pages,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def quick_check(path: str | os.PathLike) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute("PRAGMA quick_check(1)").fetchone()[0]
    finally:
        con.close()


def table_counts(path: str | os.PathLike, tables: list[str]) -> dict:
    out: dict[str, int | None] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    try:
        con.execute("PRAGMA query_only=ON")
        for t in tables:
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = None
    finally:
        con.close()
    return out


def watermark(path: str | os.PathLike, specs: list[dict]) -> list[dict]:
    """按配置读各表时间水位截点（MAX(column)）。列不存在则记 null。"""
    out: list[dict] = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    try:
        con.execute("PRAGMA query_only=ON")
        for s in specs:
            rec = {"table": s["table"], "column": s["column"], "label": s.get("label", "")}
            try:
                rec["value"] = con.execute(
                    f"SELECT MAX({s['column']}) FROM {s['table']}").fetchone()[0]
            except Exception as e:
                rec["value"] = None
                rec["error"] = str(e)
            out.append(rec)
    finally:
        con.close()
    return out


# ── 进程 ────────────────────────────────────────────────────────────────────
def pgrep(pattern: str) -> list[int]:
    """用 pgrep -f 找真实 PID（沙箱内 grep/ps 可能静默空 ⇒ 用 pgrep -fl 的真实输出）。"""
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=15)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def open_fds(pid: int) -> list[str]:
    """真实 FD 指向的路径（用 lsof -Fn，避免 grep 静默空）。"""
    try:
        r = subprocess.run(["lsof", "-p", str(pid), "-Fn"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    return [ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n")]


def spawn_detached(cfg: dict, cmd: list[str], pid_path: str, log_path: str,
                   cwd: str | None = None) -> int:
    """复用既有 spawn_detached.py 启动长驻任务，返回孙进程真实 PID。

    注意：**不得**用 spawn_detached 的退出码或 proc.poll() 判「子任务结束」；
    只能轮询 pid 文件拿到真实 PID 再 kill -0。此处只负责启动并回传 PID。
    """
    py = cfg["python"]
    sd = cfg["spawn_detached"]
    argv = [py, sd, "--pid", pid_path, "--log", log_path,
            "--cwd", cwd or os.getcwd(), "--", *cmd]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"spawn_detached 启动失败 rc={r.returncode}: {r.stderr.strip()}")
    return int(r.stdout.strip())


def jdump(obj, path: str | os.PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
