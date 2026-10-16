#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据保全 · 冷层 catalog/state **配对版本 manifest**（不发布、不上传）。

按总体方案 §五：变化的 `_state.json`/`_catalog.json` 从**同一次**已完成归档
生成**配对快照**，保存到 `manifests/<batch-id>/`；分片上传校验后**最后**才发布
该版本清单。禁止用 `--immutable` 向同名远端清单持续覆盖。

本脚本只做本地配对快照 + manifest；**不做任何云动作**。

用法：
  $PY version_manifest.py --batch-id 20261014T131200Z
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops_common as OC  # noqa: E402

# W1（2026-10-16）幽灵依赖清理：原写死外部工作区归档根（已删除）。
# 现由**工程根**推导（冷档 Parquet 根 = `数据/市场数据/parquet`），可被环境变量覆盖。
ARC = os.environ.get("MARKET_ARCHIVE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "数据", "市场数据", "parquet")
PAIR = ["_state.json", "_catalog.json"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--batch-id", default=None)
    a = ap.parse_args()
    cfg = OC.load_config(a.config)
    batch_id = a.batch_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(cfg["manifests_root"]) / batch_id
    out.mkdir(parents=True, exist_ok=True)

    man = {"batch_id": batch_id, "generated_at_ms": int(time.time() * 1000),
           "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "source_dir": ARC, "publish_state": "NOT_PUBLISHED (本地配对快照，未上传/未发布)",
           "publish_rule": ("分片 copy --immutable 上传并校验后，最后发布本版本清单；"
                            "不得用 --immutable 覆盖同名远端清单。"),
           "files": [], "missing": [],
           "archive_in_progress_note": ("若冷档正在重建，本配对快照记录的是**当前（可能进行中）**的 "
                                        "state/catalog 状态，不代表一个已完成的归档版本；"
                                        "发布前须等归档/水位完成并重新配对。")}
    for fn in PAIR:
        src = Path(ARC) / fn
        if not src.is_file():
            man["missing"].append(fn)
            continue
        dst = out / fn
        shutil.copy2(src, dst)
        man["files"].append({"name": fn, "src": str(src), "copy": str(dst),
                             "bytes": dst.stat().st_size,
                             "sha256": OC.sha256_file(dst),
                             "src_mtime": int(src.stat().st_mtime)})
    # catalog 摘要
    cat = out / "_catalog.json"
    if cat.is_file():
        try:
            data = json.loads(cat.read_text())
            shards = data.get("shards", {})
            man["catalog_summary"] = {"shards": len(shards),
                                      "rows_declared": sum(v.get("rows", 0) for v in shards.values())}
        except Exception as e:
            man["catalog_summary"] = {"error": str(e)}
    st = out / "_state.json"
    if st.is_file():
        try:
            man["state_summary"] = {"watermark_ms": json.loads(st.read_text()).get("watermark_ms")}
        except Exception as e:
            man["state_summary"] = {"error": str(e)}
    OC.jdump(man, out / "version_manifest.json")
    for f in man["files"]:
        print(f"[配对] {f['name']} {f['bytes']}B sha={f['sha256'][:16]}…")
    for m in man["missing"]:
        print(f"[缺失] {m} 不存在（如实报告）")
    print(f"[完成] 版本 manifest: {out / 'version_manifest.json'}（未发布/未上传）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
