#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据保全 · 云端复制接线**状态核查**（只读，绝不读取输出密钥/配置文件内容）。

原则：
  · 只调用 `rclone version / listremotes / about / lsd / size` 等**只读**子命令；
  · **禁止** sync / delete / purge / move；本轮不执行任何上传；
  · 不 `cat` rclone.conf、不打印 token、不猜账号/目录；
  · 连接器（GitHub/Notion 等）授权 **不等于** rclone 授权，不混用。

用法：
  $PY cloud_status.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops_common as OC  # noqa: E402


def _run(args: list[str], timeout: int = 30) -> dict:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    except FileNotFoundError:
        return {"rc": 127, "stdout": "", "stderr": "not found"}
    except Exception as e:
        return {"rc": -1, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    cfg = OC.load_config(a.config)
    cloud = cfg.get("cloud", {})

    rep = {"checked_at_ms": int(time.time() * 1000),
           "tool": cloud.get("tool", "rclone"),
           "read_only_only": True,
           "forbidden_verbs": cloud.get("forbidden_verbs", []),
           "status": "unknown", "remotes": [], "blockers": []}

    exe = shutil.which(cloud.get("tool", "rclone"))
    rep["binary_path"] = exe
    if not exe:
        rep["status"] = "TOOL_NOT_INSTALLED"
        rep["blockers"].append("系统未安装 rclone：无法生成/执行云端复制接线")
        rep["blockers"].append("缺目标账号/目录/授权/容量：不得猜账号、不得绕过 OAuth、不得把连接器授权当 rclone 授权")
        print("[云] rclone 未安装 → 状态 = TOOL_NOT_INSTALLED（如实报告，不假上传）")
        print("[云] 阻塞：缺 rclone 可执行文件；缺目标 remote/目录/授权/容量")
        if a.json:
            OC.jdump(rep, a.json)
        return 2

    rep["version"] = _run([exe, "version"])["stdout"].splitlines()[:2]
    lr = _run([exe, "listremotes"])
    rep["listremotes_rc"] = lr["rc"]
    remotes = [x.strip() for x in lr["stdout"].splitlines() if x.strip()]
    rep["remote_names"] = remotes
    if not remotes:
        rep["status"] = "NO_REMOTE_CONFIGURED"
        rep["blockers"].append("rclone 已安装但未配置任何 remote（需目标账号/授权/容量）")
        print("[云] rclone 已安装，但无 remote → 状态 = NO_REMOTE_CONFIGURED")
    for r in remotes:
        entry = {"remote": r}
        about = _run([exe, "about", r, "--json"])
        entry["about_rc"] = about["rc"]
        entry["about"] = about["stdout"][:800] or about["stderr"][:400]
        lsd = _run([exe, "lsd", r])
        entry["lsd_rc"] = lsd["rc"]
        entry["lsd_head"] = "\n".join(lsd["stdout"].splitlines()[:10])
        rep["remotes"].append(entry)
    if remotes:
        rep["status"] = "REMOTE_PRESENT_NEEDS_TARGET_DIR"
        rep["blockers"].append("已有 remote，但尚需明确项目目标目录/容量/授权范围后方可接线")
    for r in rep["remotes"]:
        print(f"[云] remote={r['remote']} about_rc={r['about_rc']} lsd_rc={r['lsd_rc']}")
        print(f"      {r['about'][:200]}")
    print(f"[云] 状态 = {rep['status']}")
    if a.json:
        OC.jdump(rep, a.json)
        print(f"[写出] {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
