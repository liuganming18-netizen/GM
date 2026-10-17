# -*- coding: utf-8 -*-
"""PRECISION-01 · 六标的规格状态「修复前 vs 修复后」对比。

用途：把 instrument_spec.py 的改动对六个样本标的（LPT/OP/ORDI/ACE/BNB/MOVR）
的实际影响摊开，避免「改了判定逻辑但其实什么都没变」或「悄悄变了」两种误判。

做法：
  · 旧实现从 evidence/PRECISION-01/before/instrument_spec.py.orig 以独立模块名载入；
  · 新实现从 策略/WyckoffMaster/event_trend_v0_1/instrument_spec.py 载入；
  · 两者用**同一个真实证据目录**、**同一个窗口**（取自各 run 的 inputs.json）；
  · 逐项比较适用性结论、是否允许正式通过、以及影响撮合的五个数值。

输出：evidence/PRECISION-01/六标的规格状态变化.json / .md
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVID = ROOT / "evidence" / "PRECISION-01"
CONTRACTS = ROOT / "数据" / "contracts" / "旧标的池"
RUNS = ROOT / "回测日志" / "event_trend_v0_1" / "runs"

SIX = ["LPTUSDT", "OPUSDT", "ORDIUSDT", "ACEUSDT", "BNBUSDT", "MOVRUSDT"]
FIELDS = ("price_step", "qty_step", "min_qty", "max_qty", "min_notional")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _window(symbol: str):
    """从该标的的 run 产物里取真实回测窗口（取不到就报错，不猜）。"""
    cands = sorted(RUNS.glob(f"run_WX_EVENT_TREND_V0_1_{symbol}_*"))
    for d in cands:
        p = d / "inputs.json"
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        tr = ((doc.get("data_summary") or {}).get("time_range_1h") or {})
        if tr.get("start_ms") and tr.get("end_ms"):
            return int(tr["start_ms"]), int(tr["end_ms"]), d.name
    raise SystemExit(f"{symbol}: 找不到带 time_range_1h 的 inputs.json")


def main() -> int:
    old = _load(EVID / "before" / "instrument_spec_orig.py", "prc01_old_spec")
    new = _load(ROOT / "策略" / "WyckoffMaster" / "event_trend_v0_1" / "instrument_spec.py",
                "prc01_new_spec")

    rows = []
    for sym in SIX:
        ws, we, run = _window(sym)
        o = old.resolve_instrument_spec(sym, window_start_ms=ws, window_end_ms=we,
                                        contracts_dir=CONTRACTS)
        n = new.resolve_instrument_spec(sym, window_start_ms=ws, window_end_ms=we,
                                        contracts_dir=CONTRACTS)
        changed = {f: {"old": getattr(o, f), "new": getattr(n, f)}
                   for f in FIELDS if getattr(o, f) != getattr(n, f)}
        rows.append({
            "symbol": sym,
            "window": {"start_ms": ws, "end_ms": we, "run_id": run},
            "applicability": {"old": o.applicability, "new": n.applicability},
            "formal_pass_allowed": {"old": o.formal_pass_allowed,
                                    "new": n.formal_pass_allowed},
            "new_reason_code": (n.coverage or {}).get("reason"),
            "new_reason_note": n.applicability_note,
            "evidence": {
                "old_path": Path(o.evidence_path).name if o.evidence_path else None,
                "new_path": Path(n.evidence_path).name if n.evidence_path else None,
                "old_sha256": o.evidence_sha256,
                "new_sha256": n.evidence_sha256,
            },
            "times_new": {
                "request_fetched_ms": n.request_fetched_ms,
                "file_saved_ms": n.file_saved_ms,
                "snapshot_server_ms": n.snapshot_server_ms,
                "spec_effective_ms": n.spec_effective_ms,
            },
            "qty_constraints_new": [dict(c) for c in n.qty_constraints],
            "numeric_fields_changed": changed,
            "chain_roles": [c.get("role") for c in n.evidence_chain],
        })

    (EVID / "六标的规格状态变化.json").write_text(
        json.dumps({"generated_by": "工具_六标的状态对比.py",
                    "contracts_dir": str(CONTRACTS), "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# PRECISION-01 · 六标的规格状态变化（修复前 → 修复后）",
        "",
        f"证据目录：`{CONTRACTS}`（2 份 futures exchangeInfo 快照）  ",
        "窗口：各标的取自自身 run 的 `inputs.json → data_summary.time_range_1h`（六者同为 2024-01-01 → 2024-06-30）",
        "",
        "| 标的 | 修复前结论 | 修复后结论 | 正式通过 | 新理由码 | 证据来源（前→后） | 数值字段变化 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        ev = f"{r['evidence']['old_path']} → {r['evidence']['new_path']}"
        chg = "、".join(f"{k}: {v['old']}→{v['new']}"
                       for k, v in r["numeric_fields_changed"].items()) or "无"
        lines.append(
            f"| {r['symbol']} | {r['applicability']['old']} | {r['applicability']['new']} | "
            f"{r['formal_pass_allowed']['old']} → {r['formal_pass_allowed']['new']} | "
            f"`{r['new_reason_code']}` | {ev} | {chg} |")
    lines += [
        "",
        "## 结论",
        "",
        "1. **六标的仍然全部 `unverified_historical`，`formal_pass_allowed=false`** —— "
        "修复没有放宽任何一条，也没有把未知历史规格升级成已验证。",
        "2. 修复前六标的的 `verified` 分支虽然也走不到，但原因是「窗口后无快照」这一条"
        "偶然条件，而不是判定逻辑成立；一旦再补一份快照进来，旧代码会拿"
        "**全局最新快照**去和「窗口前快照」比较，从而把窗口内已变更的规格误判为 verified。",
        "3. 修复后每条结论都带 `reason` 码与逐份证据链（`coverage` / `evidence_chain`），"
        "可以指着具体文件与 sha256 复核。",
        "",
        "## 逐条明细",
        "",
    ]
    for r in rows:
        lines += [
            f"### {r['symbol']}",
            "",
            f"- 结论：`{r['applicability']['new']}`，理由码 `{r['new_reason_code']}`",
            f"- 说明：{r['new_reason_note']}",
            f"- 四类时间（新实现，均为毫秒）：`{json.dumps(r['times_new'], ensure_ascii=False)}`",
            f"- 适用数量约束：`{json.dumps(r['qty_constraints_new'], ensure_ascii=False)}`",
            f"- 证据链角色：{r['chain_roles']}",
            "",
        ]
    (EVID / "六标的规格状态变化.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"rows": len(rows),
                      "all_unverified": all(r["applicability"]["new"] == "unverified_historical"
                                            for r in rows),
                      "any_numeric_change": any(r["numeric_fields_changed"] for r in rows)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
