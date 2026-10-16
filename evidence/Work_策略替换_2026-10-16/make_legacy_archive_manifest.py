#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `策略/WyckoffMaster/_legacy_archive_v31/ARCHIVE_MANIFEST.json`。

用途：把「替换前」的旧策略实现（含**未提交字节**）做成可追溯封存证据：
  · 每个文件的 sha256（归档副本 vs 原位文件，必须逐字节一致）
  · git blob sha（`git hash-object`）
  · git 状态码（M / ?? / 干净）
  · 对已跟踪且被修改的文件，附 `git diff --stat` 与 `git diff` 的行数摘要

只读：不修改任何业务文件、不写库、不动 Git 索引。
用法：python3 make_legacy_archive_manifest.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "策略" / "WyckoffMaster"
ARCHIVE = SRC / "_legacy_archive_v31"

FILES = ["__init__.py", "params.py", "series.py", "factors.py", "segments.py",
         "experiments.py", "verify.py", "core.py", "execution.py",
         "test_execution.py", "README.md", "EXECUTION.md"]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


def git(*args) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    head = git("rev-parse", "HEAD")
    porcelain = git("status", "--porcelain")
    status_by_path = {}
    for line in porcelain.splitlines():
        code, _, path = line[:2], line[2], line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        # git 输出非 ASCII 路径时带引号与八进制转义；用 -z 版本更稳，这里做宽松匹配
        status_by_path[path] = code.strip()

    entries = []
    for name in FILES:
        src = SRC / name
        arc = ARCHIVE / name
        if not src.exists() and not arc.exists():
            continue
        rel = str(src.relative_to(ROOT))
        e = {
            "file": name,
            "origin_path": rel,
            "archive_path": str(arc.relative_to(ROOT)),
            "exists_in_place": src.exists(),
            "exists_in_archive": arc.exists(),
        }
        if src.exists():
            e["origin_sha256"] = sha256(src)
            e["origin_size"] = src.stat().st_size
        if arc.exists():
            e["archive_sha256"] = sha256(arc)
            e["archive_size"] = arc.stat().st_size
        e["byte_identical"] = (e.get("origin_sha256") == e.get("archive_sha256"))
        e["git_blob_sha"] = git("hash-object", str(src)) if src.exists() else None
        e["git_tracked"] = bool(git("ls-files", "--error-unmatch", str(src))) if src.exists() else False
        # git status：用 -z 更稳，这里对相对路径做一次直查
        st = git("status", "--porcelain", "--", str(src))
        e["git_status"] = st[:2].strip() if st else "clean"
        if e["git_status"] == "M":
            diff = git("diff", "--numstat", "--", str(src))
            e["git_diff_numstat"] = diff
            dstat = git("diff", "--stat", "--", str(src))
            e["git_diff_stat"] = dstat
            e["git_diff_sha256"] = hashlib.sha256(
                git("diff", "--", str(src)).encode("utf-8")).hexdigest()
        entries.append(e)

    manifest = {
        "schema": "wyckoff_legacy_archive_manifest/v1",
        "namespace": "_legacy_archive_v31",
        "purpose": "替换前旧策略实现（F_WX 3a-3d 所依赖的 WyckoffMaster v3.1 核心）只读封存，供历史追溯",
        "read_only": True,
        "is_current_strategy": False,
        "project_root": str(ROOT),
        "git_branch": branch,
        "git_head": head,
        "superseded_by": "WX_OI_RESONANCE_V1",
        "entries": entries,
        "summary": {
            "n_files": len(entries),
            "all_byte_identical": all(e.get("byte_identical") for e in entries),
            "uncommitted_bytes_included": sorted(
                e["file"] for e in entries if e.get("git_status") in ("M", "??")),
        },
    }
    out = ARCHIVE / "ARCHIVE_MANIFEST.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=1))
    print("manifest ->", out)
    return 0 if manifest["summary"]["all_byte_identical"] else 2


if __name__ == "__main__":
    sys.exit(main())
