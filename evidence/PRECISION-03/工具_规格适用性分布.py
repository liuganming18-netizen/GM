# -*- coding: utf-8 -*-
"""PRECISION-03 · 375 个既有 run 若按**当前代码**重新解析规格，会得到什么结论。

只读：读每个 run 的 summary.json（取窗口），调工程自己的 `resolve_instrument_spec`。
不重跑回测、不写 run、不改任何文件（除本目录输出）。

目的：回答「formal_pass_allowed 到底有没有可能为 True」——
如果 375 个真实窗口全部落在 `unverified_historical`，那这条标志在当前证据下
**结构性地恒为 False**，任何上层显示「通过」都只能是漏传或误读。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
for p in (str(ROOT / "框架" / "backend"), str(ROOT), str(ROOT / "策略")):
    if p not in sys.path:
        sys.path.insert(0, p)

import backtest_store as S                                    # noqa: E402
from WyckoffMaster.event_trend_v0_1.instrument_spec import (  # noqa: E402
    resolve_instrument_spec)

OUT = ROOT / "evidence" / "PRECISION-03"


def main() -> int:
    runs = sorted(p for p in S.RUNS_ROOT.iterdir()
                  if p.is_dir() and p.name.startswith("run_"))
    rows = []
    dist = Counter()
    errs = Counter()
    for d in runs:
        rid = d.name
        symbol = None
        ws = we = None
        try:
            sm = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            symbol = sm.get("symbol")
            tr = ((sm.get("data_summary") or {}).get("time_range_1h") or {})
            ws, we = tr.get("start_ms"), tr.get("end_ms")
            window_source = "summary.json"
            if ws is None or we is None:
                # 375 个旧 run 的 summary.json **不落窗口**；窗口只在 inputs.json 里。
                inp = json.loads((d / "inputs.json").read_text(encoding="utf-8"))
                tr2 = ((inp.get("data_summary") or {}).get("time_range_1h") or {})
                ws, we = tr2.get("start_ms"), tr2.get("end_ms")
                window_source = "inputs.json"
        except Exception as exc:                              # noqa: BLE001
            errs[f"summary_read:{type(exc).__name__}"] += 1
            rows.append({"run_id": rid, "error": f"summary_read:{exc}"})
            continue
        try:
            sp = resolve_instrument_spec(symbol, window_start_ms=ws, window_end_ms=we,
                                         venue="binance", market="um", order_type="MARKET")
            app = sp.applicability
            dist[app] += 1
            rows.append({
                "run_id": rid, "symbol": symbol,
                "window_start_ms": ws, "window_end_ms": we,
                "window_source": window_source,
                "applicability": app,
                "formal_pass_allowed": bool(sp.formal_pass_allowed),
                "price_step": sp.price_step,
                "evidence_sha256": (sp.evidence_sha256 or "")[:16],
                "note": sp.applicability_note[:160],
            })
        except Exception as exc:                              # noqa: BLE001
            errs[f"resolve:{type(exc).__name__}:{str(exc)[:40]}"] += 1
            rows.append({"run_id": rid, "symbol": symbol, "error": str(exc)[:200]})

    agg = {
        "runs_total": len(runs),
        "applicability_distribution": dict(dist),
        "formal_pass_allowed_true": sum(1 for r in rows if r.get("formal_pass_allowed") is True),
        "errors": dict(errs),
    }
    (OUT / "规格适用性分布.json").write_text(
        json.dumps({"aggregate": agg, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print("=" * 78)
    print("PRECISION-03 · 375 个既有窗口的规格适用性（按当前代码重解析）")
    print("=" * 78)
    print(f"  runs_total              {agg['runs_total']}")
    print(f"  applicability           {agg['applicability_distribution']}")
    print(f"  formal_pass_allowed=True {agg['formal_pass_allowed_true']}")
    print(f"  errors                  {agg['errors'] or '{}'}")
    sample = next((r for r in rows if r.get("applicability")), None)
    if sample:
        print("-" * 78)
        print("  样例：", json.dumps(sample, ensure_ascii=False)[:500])
    print(f"\n明细：{OUT / '规格适用性分布.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
