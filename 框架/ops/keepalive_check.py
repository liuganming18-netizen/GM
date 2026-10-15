#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据保全 · 进程保活核查（真实 PID / FD / 目标路径 / 日志增长 / 退出记录）。

不把「写出 PID」当保活成功：对每个受管进程实测
  ① pgrep 真实 PID；② kill -0 存活；③ lsof 打开的 FD 是否含目标库/日志；
  ④ 日志大小与 mtime，并在采样窗内二次采样判断是否**在增长**；
  ⑤ pid 文件内容与真实 PID 是否一致。

用法：
  $PY keepalive_check.py            # 默认采样窗 3s
  $PY keepalive_check.py --window 5
  $PY keepalive_check.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops_common as OC  # noqa: E402

SHELL_HINT = ("zsh", "/bin/sh", "bash", "-c ", "pgrep", "codebuddy", "CODEBUDDY_")


def _is_real(pids_cmds: list[tuple[int, str]], pattern: str) -> list[tuple[int, str]]:
    """过滤掉 shell 包装/自身命令行，只留真正干活的进程。"""
    out = []
    for pid, cmd in pids_cmds:
        if any(h in cmd for h in SHELL_HINT):
            continue
        if pattern in cmd:
            out.append((pid, cmd))
    return out


def _pgrep_fl(pattern: str) -> list[tuple[int, str]]:
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True,
                           text=True, timeout=15)
    except Exception:
        return []
    res = []
    for ln in r.stdout.splitlines():
        parts = ln.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            res.append((int(parts[0]), parts[1]))
    return res


def check(cfg: dict, window: float = 3.0) -> dict:
    report = {"checked_at_ms": int(time.time() * 1000),
              "window_s": window, "processes": []}
    for spec in cfg["processes"]:
        name = spec["name"]
        pattern = spec["pattern"]
        rec = {"name": name, "role": spec.get("role", ""), "pattern": pattern,
               "expect_path": spec.get("expect_path"), "status": "unknown"}
        cand = _is_real(_pgrep_fl(pattern), pattern)
        rec["match_count"] = len(cand)
        rec["pids"] = [p for p, _ in cand]
        if not cand:
            rec["status"] = "DEAD(未找到真实进程)"
            report["processes"].append(rec)
            continue
        pid, cmd = cand[0]
        rec["pid"] = pid
        rec["cmd"] = cmd[:200]
        rec["alive"] = OC.pid_alive(pid)
        fds = OC.open_fds(pid)
        rec["fd_count"] = len(fds)
        exp = spec.get("expect_path")
        rec["expect_path_open"] = bool(exp and any(f == exp for f in fds))
        # pid 文件一致性
        pf = spec.get("pidfile")
        if pf and os.path.isfile(pf):
            try:
                rec["pidfile_value"] = int(open(pf).read().strip())
                rec["pidfile_matches"] = (rec["pidfile_value"] == pid)
            except Exception as e:
                rec["pidfile_error"] = str(e)
        # 日志增长
        log = spec.get("log")
        if log and os.path.isfile(log):
            s1 = os.path.getsize(log)
            m1 = os.path.getmtime(log)
            time.sleep(window)
            s2 = os.path.getsize(log)
            rec["log"] = log
            rec["log_bytes_before"] = s1
            rec["log_bytes_after"] = s2
            rec["log_growth_bytes"] = s2 - s1
            rec["log_mtime"] = m1
            rec["log_age_s"] = round(time.time() - m1, 1)
            rec["log_growing"] = (s2 > s1)
        # 判定（按间隔写日志的进程：3s 窗内无增长属正常，用 max_log_age_s 兜底）
        max_age = spec.get("max_log_age_s")
        fresh = (max_age is not None and rec.get("log_age_s") is not None
                 and rec["log_age_s"] <= max_age)
        if rec["alive"] and (rec.get("expect_path_open") or rec.get("log_growing") or fresh):
            rec["status"] = "HEALTHY"
            rec["healthy_reason"] = ("目标FD在开" if rec.get("expect_path_open")
                                     else "日志窗内增长" if rec.get("log_growing")
                                     else f"日志新鲜(<= {max_age}s)")
        elif rec["alive"]:
            rec["status"] = "ALIVE(未见目标FD/日志增长且日志过旧)"
        else:
            rec["status"] = "DEAD"
        report["processes"].append(rec)
    report["all_healthy"] = all(p["status"] == "HEALTHY" for p in report["processes"])
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--window", type=float, default=3.0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    cfg = OC.load_config(a.config)
    rep = check(cfg, a.window)
    for p in rep["processes"]:
        print(f"[{p['status']}] {p['name']} pid={p.get('pid')} "
              f"fd={p.get('fd_count')} 目标FD={p.get('expect_path_open')} "
              f"日志增长={p.get('log_growth_bytes')}B 年龄={p.get('log_age_s')}s "
              f"理由={p.get('healthy_reason', '-')} ({p.get('role', '')})")
    print(f"[总判定] all_healthy={rep['all_healthy']}")
    if a.json:
        OC.jdump(rep, a.json)
        print(f"[写出] {a.json}")
    return 0 if rep["all_healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
