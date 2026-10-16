# -*- coding: utf-8 -*-
"""校验「旧策略退役」归档清单（WORK-04 · 2026-10-16）。

只读：按 `归档清单_旧策略_2026-10-16.json` 逐文件重算 SHA256，与清单比对。
全绿 = 原件未被改动/删除（可恢复只读归档成立）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
MANIFEST = HERE / "归档清单_旧策略_2026-10-16.json"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ok = 0
    bad: list[dict] = []
    gone: list[str] = []
    for e in m["entries"]:
        p = PROJECT_ROOT / e["path"]
        if not p.exists():
            gone.append(e["path"])
            continue
        actual = sha256_of(p)
        if actual == e["sha256"]:
            ok += 1
        else:
            bad.append({"path": e["path"], "expected": e["sha256"], "actual": actual})

    total = len(m["entries"])
    print(f"清单文件数 : {total}")
    print(f"指纹一致   : {ok}")
    print(f"指纹不符   : {len(bad)}")
    print(f"文件缺失   : {len(gone)}")
    for b in bad[:20]:
        print("  不符:", b["path"])
    for g in gone[:20]:
        print("  缺失:", g)
    status = "PASS" if (not bad and not gone) else "FAIL"
    print(f"结论: {status}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
