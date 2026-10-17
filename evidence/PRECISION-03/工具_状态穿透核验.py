# -*- coding: utf-8 -*-
"""PRECISION-03 · 只读穿透核验（不写任何 run、不重启服务、不建回测）。

做法：**直接调用工程自己的函数**（不复制一份判据），把每个 run 的
「底层事实」与「上层呈现」并排算出来，逐 run 对账。

  · 底层事实：summary.json / inputs.json / instrument_spec.json 里真实落了什么
  · 上层呈现：backtest_store.read_run → backtest_api._quality_of → 前端 qualityOk 判据

前端判据（`BacktestWorkbench.tsx` L698 / L1301）是：
    qualityOk = !error && status==='succeeded' && quality.status==='ok' && data_quality!=null
本脚本用同一判据复算，输出「UI 会说通过」的 run 数。

输出：result.json + 控制台摘要。全部只读。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
BACKEND = ROOT / "框架" / "backend"
for p in (str(BACKEND), str(ROOT), str(ROOT / "策略")):
    if p not in sys.path:
        sys.path.insert(0, p)

import backtest_store as S            # noqa: E402
import backtest_api as API            # noqa: E402

OUT = ROOT / "evidence" / "PRECISION-03"


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def ui_verdict(doc: dict, q_env: dict) -> tuple[bool, str]:
    """复算前端 `qualityOk` + verdict 文案（BacktestWorkbench L698 / L707）。"""
    env_status = doc.get("status")
    quality_status = (q_env.get("quality") or {}).get("status")
    dq = q_env.get("data_quality")
    ok = bool(env_status == "succeeded" and quality_status == "ok" and dq is not None)
    if env_status != "succeeded":
        return False, f"运行未成功（status={env_status}）"
    if quality_status != "ok":
        return False, f"质量未通过（quality.status={quality_status}）"
    if dq is None:
        return False, "缺 data_quality 明细 ⇒ 不能断言通过"
    return ok, "通过（运行成功 + 质量 ok + 有质量明细）"


def scan_one(rid: str) -> dict:
    d = S.run_dir(rid)
    sm = _load(d / "summary.json")
    inp = _load(d / "inputs.json")
    isp = _load(d / "instrument_spec.json")
    has_run_json = (d / "run.json").exists()

    # ── 底层落盘事实 ──
    sm = sm or {}
    prec = sm.get("precision") or {}
    tv = sm.get("trading_summary") or {}
    var_a = sm.get("summary_variant_a") or {}
    row = {
        "run_id": rid,
        "has_run_json": has_run_json,
        "summary_present": bool(sm),
        # 四个状态字段是否真实落盘
        "sm_has_formal_pass_allowed": "formal_pass_allowed" in sm,
        "sm_formal_pass_allowed": sm.get("formal_pass_allowed"),
        "sm_has_spec_status": "spec_status" in sm,
        "sm_spec_status": sm.get("spec_status"),
        "sm_has_precision": bool(prec),
        "sm_has_instrument_spec": "instrument_spec" in sm,
        "sm_instrument_spec_applicability": (
            (sm.get("instrument_spec") or {}).get("applicability")
            if isinstance(sm.get("instrument_spec"), dict) else None),
        "precision_run_valid": prec.get("run_valid"),
        "precision_invalid_reasons": prec.get("invalid_reasons"),
        "precision_unclosed_positions": prec.get("unclosed_positions"),
        "precision_rejects_count": prec.get("rejects_count"),
        "precision_spec_status": prec.get("spec_status"),
        # 变体级
        "var_a_has_formal_pass_allowed": "formal_pass_allowed" in var_a,
        "var_a_formal_pass_allowed": var_a.get("formal_pass_allowed"),
        "var_b_has_formal_pass_allowed": "formal_pass_allowed" in (sm.get("summary_variant_b") or {}),
        "var_c_has_formal_pass_allowed": "formal_pass_allowed" in (sm.get("summary_variant_c") or {}),
        "var_b_formal_pass_allowed": (sm.get("summary_variant_b") or {}).get("formal_pass_allowed"),
        "var_c_formal_pass_allowed": (sm.get("summary_variant_c") or {}).get("formal_pass_allowed"),
        # 业务数值（用工程自己的口径）
        "trade_count": tv.get("trade_count", var_a.get("trade_count")),
        "net_pnl": tv.get("net_pnl", var_a.get("net_pnl")),
        "inputs_has_spec_status": "spec_status" in (inp or {}),
        "inputs_spec_status": (inp or {}).get("spec_status"),
        "instrument_spec_json_present": isp is not None,
        "instrument_spec_json_spec_status": (isp or {}).get("spec_status"),
    }

    # ── 上层呈现（调工程真函数）──
    try:
        doc = S.read_run(rid)                 # run.json 优先，否则 _summary_to_run_doc
        row["doc_source"] = "run.json" if has_run_json else "_summary_to_run_doc"
        row["doc_status"] = doc.get("status")
        row["doc_dq_status"] = (doc.get("data_quality") or {}).get("status")
        row["doc_dq_reasons"] = (doc.get("data_quality") or {}).get("reasons")
        q_env = {
            "status": doc.get("status"),
            "quality": API._quality_of(doc),
            "data_quality": doc.get("data_quality"),
        }
        row["api_quality_status"] = (q_env["quality"] or {}).get("status")
        ok, verdict = ui_verdict(doc, q_env)
        row["ui_quality_ok"] = ok
        row["ui_verdict"] = verdict
    except Exception as exc:                  # noqa: BLE001
        row["doc_source"] = "ERROR"
        row["doc_error"] = f"{type(exc).__name__}: {exc}"
        row["ui_quality_ok"] = False
        row["ui_verdict"] = "读取失败"
    return row


def main() -> int:
    runs = sorted(p.name for p in (S.RUNS_ROOT).iterdir()
                  if p.is_dir() and p.name.startswith("run_"))
    rows = [scan_one(r) for r in runs]

    n = len(rows)
    agg = {
        "runs_total": n,
        "has_formal_pass_allowed": sum(1 for r in rows if r["sm_has_formal_pass_allowed"]),
        "formal_pass_allowed_true": sum(1 for r in rows if r["sm_formal_pass_allowed"] is True),
        "formal_pass_allowed_false": sum(1 for r in rows if r["sm_formal_pass_allowed"] is False),
        "formal_pass_allowed_absent": sum(1 for r in rows if not r["sm_has_formal_pass_allowed"]),
        "has_spec_status": sum(1 for r in rows if r["sm_has_spec_status"]),
        "has_precision": sum(1 for r in rows if r["sm_has_precision"]),
        "precision_run_valid_true": sum(1 for r in rows if r["precision_run_valid"] is True),
        "precision_run_valid_false": sum(1 for r in rows if r["precision_run_valid"] is False),
        "precision_run_valid_absent": sum(1 for r in rows if r["precision_run_valid"] is None),
        "unclosed_nonempty": sum(1 for r in rows if r["precision_unclosed_positions"]),
        "doc_status_succeeded": sum(1 for r in rows if r["doc_status"] == "succeeded"),
        "doc_source_summary_json": sum(1 for r in rows if r["doc_source"] == "_summary_to_run_doc"),
        "doc_source_run_json": sum(1 for r in rows if r["doc_source"] == "run.json"),
        "api_quality_ok": sum(1 for r in rows if r["api_quality_status"] == "ok"),
        "ui_quality_ok": sum(1 for r in rows if r["ui_quality_ok"]),
        # ★ 核心矛盾计数：UI 说通过，但底层没有/为假
        "ui_ok_but_formal_false": sum(
            1 for r in rows if r["ui_quality_ok"] and r["sm_formal_pass_allowed"] is False),
        "ui_ok_but_formal_absent": sum(
            1 for r in rows if r["ui_quality_ok"] and not r["sm_has_formal_pass_allowed"]),
        "ui_ok_and_precision_invalid": sum(
            1 for r in rows if r["ui_quality_ok"] and r["precision_run_valid"] is False),
    }
    agg["ui_ok_without_any_spec_verification"] = (
        agg["ui_ok_but_formal_false"] + agg["ui_ok_but_formal_absent"])

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(
        json.dumps({"aggregate": agg, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print("=" * 78)
    print("PRECISION-03 · 状态穿透核验（只读）")
    print("=" * 78)
    for k, v in agg.items():
        print(f"  {k:<42} {v}")
    print("-" * 78)
    print("样例（前 5 个 UI 判为通过、但底层 formal_pass_allowed 缺失的 run）：")
    shown = 0
    for r in rows:
        if r["ui_quality_ok"] and not r["sm_has_formal_pass_allowed"]:
            print(f"  {r['run_id']}  doc_source={r['doc_source']} "
                  f"dq={r['doc_dq_status']} ui={r['ui_verdict']}")
            shown += 1
            if shown >= 5:
                break
    if shown == 0:
        print("  （无）")
    print(f"\n明细：{OUT / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
