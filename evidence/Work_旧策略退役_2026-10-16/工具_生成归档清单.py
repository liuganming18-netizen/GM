# -*- coding: utf-8 -*-
"""生成「旧策略退役」可恢复只读归档清单（WORK-04 · 2026-10-16）。

纪律
────
本工具**只读**：只统计、只算 SHA256、只写清单 JSON。**不移动、不改名、不删除**任何原件。
「归档」= 记录原件位置 + 逐文件 SHA256 ⇒ 恢复 = 按清单逐字节核对（原件仍在原位）。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]

# ── 退役对象：逐个文件给出**分类**（不含 registry.py —— 那是当前注册表，非旧策略件）──
LEGACY_FILES: list[tuple[str, str, str]] = [
    # (相对路径, 分类, 说明)
    ("框架/backend/strategies/strategy_a.py", "legacy_ab_source", "旧 A 策略（唐奇安突破）"),
    ("框架/backend/strategies/strategy_b.py", "legacy_ab_source", "旧 B 策略（异常量）"),
    ("框架/backend/strategies/wx_donchian_wyckoff.py", "legacy_fwx_source", "旧 F_WX 四臂 3a–3d 核心"),
    ("框架/backend/strategies/wx_config.py", "legacy_fwx_source", "旧 F_WX 参数解析"),
    ("框架/backend/strategies/wx_daily_375.py", "legacy_fwx_source", "旧 F_WX 日线 375 分支"),
    ("框架/backend/strategies/wx_engine.py", "legacy_fwx_source", "旧 F_WX 引擎"),
    ("框架/backend/strategies/wx_execution.py", "legacy_fwx_source", "旧 F_WX 执行核心 v3.1"),
    ("框架/backend/strategies/wx_gate.py", "legacy_fwx_source", "旧 F_WX 门禁"),
    ("框架/backend/strategies/wx_ledger.py", "legacy_fwx_source", "旧 F_WX 账本"),
    ("框架/backend/strategies/common.py", "legacy_shared_util", "旧件共享工具（仍被 import，保留）"),
    ("框架/backend/strategies/funding_interval_meta_source.py", "legacy_shared_util",
     "资金费间隔元数据源（仍被 import，保留）"),
    ("框架/backend/strategies/__init__.py", "legacy_shared_util", "strategies 包初始化（保留）"),
    # 当前注册表：**未退役**，仅登记其指纹以便核对「退役标记已生效」
    ("框架/backend/strategies/registry.py", "current_registry",
     "当前注册表（WORK-04 已加 retired/runnable 标记；非旧策略件）"),
]

# ── 旧 v3.1 核心：逐字节封存目录 ──
LEGACY_V31_DIR = "策略/WyckoffMaster/_legacy_archive_v31"

# ── 旧回测 / 复盘 runs（只读保留，不进入新策略运行列表）──
LEGACY_RUN_DIRS = ["回测日志/WyckoffMaster"]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry_for(path: Path, kind: str, note: str) -> dict:
    st = path.stat()
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "kind": kind,
        "note": note,
        "bytes": st.st_size,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "sha256": sha256_of(path),
    }


def walk_dir(rel_dir: str, kind: str, note: str) -> list[dict]:
    root = PROJECT_ROOT / rel_dir
    out: list[dict] = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
            continue
        if p.name == ".DS_Store":
            continue
        out.append(entry_for(p, kind, note))
    return out


def main() -> int:
    entries: list[dict] = []
    missing: list[str] = []

    for rel, kind, note in LEGACY_FILES:
        p = PROJECT_ROOT / rel
        if p.exists():
            entries.append(entry_for(p, kind, note))
        else:
            missing.append(rel)

    entries.extend(walk_dir(LEGACY_V31_DIR, "legacy_v31_core_archive",
                            "旧 v3.1 核心逐字节封存（只读保留）"))
    for d in LEGACY_RUN_DIRS:
        entries.extend(walk_dir(d, "legacy_run_dir", "旧回测/复盘 runs（只读保留，不进入新策略运行列表）"))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "retired_at": "2026-10-16",
        "work_order": "WORK-04",
        "retired_strategy_keys": ["3a", "3b", "3c", "3d", "A", "B"],
        "successor_strategy_id": "WX_OI_RESONANCE_V1",
        "successor_strategy_key": "wx_oi_resonance_v1",
        "policy": "只退役「可运行入口」；**原件不删、不改、不移动**。"
                  "恢复 = 按本清单逐文件 SHA256 核对（原件仍在原位）。",
        "classification": {
            "legacy_ab_source": "旧 A/B 策略源码（已退役可运行入口；仍被当前 registry import）",
            "legacy_fwx_source": "旧 F_WX 3a–3d 源码（已退役可运行入口；仍被当前 registry import）",
            "legacy_shared_util": "旧件共享工具（未退役，保留）",
            "current_registry": "当前注册表（未退役；登记指纹用于核对退役标记）",
            "legacy_v31_core_archive": "旧 v3.1 核心逐字节封存",
            "legacy_run_dir": "旧回测/复盘 runs（只读保留）",
        },
        "totals": {
            "files": len(entries),
            "bytes": sum(e["bytes"] for e in entries),
            "by_kind": {},
        },
        "missing_paths": missing,
        "entries": entries,
    }
    for e in entries:
        manifest["totals"]["by_kind"][e["kind"]] = manifest["totals"]["by_kind"].get(e["kind"], 0) + 1

    out = HERE / "归档清单_旧策略_2026-10-16.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"写入 {out}")
    print(f"文件数 {len(entries)} · 总字节 {manifest['totals']['bytes']}")
    print("分类计数:", json.dumps(manifest["totals"]["by_kind"], ensure_ascii=False))
    if missing:
        print("缺失（未登记）:", missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
