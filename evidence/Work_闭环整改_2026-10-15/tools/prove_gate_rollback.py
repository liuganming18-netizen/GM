#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段A·总闸**可回滚性**证明（只读+临时改控制文件，不触碰任何账本/不下单）。

流程：false → 改 true → 读闸（应为 allow）→ 改回 false → 读闸（应为 block）。
期间**不调用** open_position，不产生任何成交/持仓/订单。
"""
import json
import sys
from pathlib import Path

BACKEND = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现")
sys.path.insert(0, str(BACKEND))
import paper_engine as PE  # noqa: E402

gf = PE.GATE_FILE
orig = json.loads(gf.read_text(encoding="utf-8"))
out = {"gate_file": str(gf), "steps": []}
try:
    out["steps"].append({"step": "as_configured", "allow": PE.new_entry_gate()["allow_new_entries"]})
    tmp = dict(orig)
    tmp["allow_new_entries"] = True
    tmp["reason"] = "rollback_proof_temporary"
    gf.write_text(json.dumps(tmp, ensure_ascii=False, indent=2), encoding="utf-8")
    out["steps"].append({"step": "flipped_true", "allow": PE.new_entry_gate()["allow_new_entries"]})
finally:
    gf.write_text(json.dumps(orig, ensure_ascii=False, indent=2), encoding="utf-8")
out["steps"].append({"step": "restored", "allow": PE.new_entry_gate()["allow_new_entries"]})
out["rollback_ok"] = (out["steps"][0]["allow"] is False
                      and out["steps"][1]["allow"] is True
                      and out["steps"][2]["allow"] is False)
print(json.dumps(out, ensure_ascii=False, indent=1))
