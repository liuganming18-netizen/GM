# -*- coding: utf-8 -*-
"""PRECISION-03 · 隔离核验：新格式产物 / run.json 路径下的状态穿透。

为什么需要这个：现场 375 个 run 全部是**旧格式**（summary.json 里根本没有
`formal_pass_allowed` / `spec_status` / `precision`），因此「新字段是否真的能穿透到
页面」在现网**无法直接观察**。本脚本用**合成夹具**在隔离目录里复现新格式产物，
再调用工程自己的 `read_run` / `_quality_of` / 前端判据，看它们是否把
「底层明确不可正式通过」显示成「质量通过」。

隔离措施（硬约束）：
  · `backtest_store.RUNS_ROOT` 在本进程内被改指到 `evidence/PRECISION-03/夹具/runs`；
  · **不**调用 create_run / execute_run / 任何写接口，不碰 `回测日志/`；
  · 夹具 run_id 带 `FIXTURE` 字样，永不进入真实列表。

输出：隔离核验.json + 控制台表。
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
import event_trend_bridge as BR       # noqa: E402

OUT = ROOT / "evidence" / "PRECISION-03"
FIX = OUT / "夹具" / "runs"


# ── 合成新格式 summary.json（字段取自 runner._build_run_files 的真实形状）──
def new_summary(run_id: str, *, formal: bool, run_valid: bool,
                reasons: list, unclosed: list, variant_b_formal=None):
    sm = {
        "run_id": run_id, "symbol": "FIXUSDT", "variant": "A",
        "funnel": {"boxes": 1, "sweeps": 1, "retests": 0, "breakouts": 1,
                   "volume_rejected": 0, "oi_rejected": 0, "oi_unavailable": 0,
                   "expired": 0, "invalidated": 0, "candidates": 1, "executed_trades": 0},
        "trading_summary": {
            "initial_equity": 2000.0, "final_equity": 2000.0, "net_pnl": 0.0,
            "trade_count": 0, "order_count": 2, "fee_total": 0.0,
            "slippage_total": 0.0, "slippage_pure_total": 0.0, "rounding_total": 0.0,
            "slippage_split_ok": True, "funding_total": 0.0,
            "spec_status": "spec_applied", "formal_pass_allowed": formal,
        },
        "identity": {"chain_ok": True, "identity_breaks": [], "orphan_trades": [],
                     "trades_total": 0, "trades_matched_by_identity": 0,
                     "candidates_total": 1, "candidates_executed": 0},
        "reconcile": {"cash_identity_ok": True, "equity_identity_ok": True,
                      "equity_curve_tail_matches_final": True,
                      "slippage_split_ok": True},
        "precision": {
            "spec_status": "spec_applied",
            "specs": {"FIXUSDT": {"symbol": "FIXUSDT", "price_step": "0.01"}},
            "price_steps": {"FIXUSDT": "0.01"},
            "rejects": [], "rejects_count": 0, "audit": [], "audit_count": 0,
            "run_valid": run_valid,
            "invalid_reasons": reasons,
            "unclosed_positions": unclosed,
        },
        "instrument_spec": {
            "symbol": "FIXUSDT", "price_step": "0.01", "qty_step": "1",
            "applicability": "unverified_historical",
            "applicability_note": "单一快照无法证明覆盖该窗口",
            "formal_pass_allowed": formal,
        },
        "spec_status": "spec_applied",
        "formal_pass_allowed": formal,
        "summary_variant_a": {"initial_equity": 2000.0, "final_equity": 2000.0,
                             "net_pnl": 0.0, "trade_count": 0,
                             "spec_status": "spec_applied",
                             "formal_pass_allowed": formal},
        "summary_variant_b": {"initial_equity": 2000.0, "final_equity": 2000.0,
                             "net_pnl": 0.0, "trade_count": 0,
                             "spec_status": "spec_applied",
                             "formal_pass_allowed": (formal if variant_b_formal is None
                                                     else variant_b_formal)},
        "summary_variant_c": {"initial_equity": 2000.0, "final_equity": 2000.0,
                             "net_pnl": 0.0, "trade_count": 0,
                             "spec_status": "spec_applied",
                             "formal_pass_allowed": formal},
    }
    return sm


def ui_verdict(doc: dict, q_env: dict) -> tuple[bool, str]:
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


def _write_case(rid: str, *, summary=None, run_json=None):
    d = FIX / rid
    d.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        (d / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if run_json is not None:
        (d / "run.json").write_text(
            json.dumps(run_json, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def main() -> int:
    # 隔离：把运行根改指到证据目录（仅本进程）
    S.RUNS_ROOT = FIX
    FIX.mkdir(parents=True, exist_ok=True)

    cases = {}

    # F1 新格式：底层明确不可正式通过 + 运行有效性为假
    rid = "run_WX_EVENT_TREND_V0_1_FIXTURE_A_9000000001"
    _write_case(rid, summary=new_summary(
        rid, formal=False, run_valid=False,
        reasons=["exit_fill_rejected:rounding_to_zero", "unclosed_position_at_end"],
        unclosed=["FIXUSDT"]))
    cases[rid] = "F1 新格式 summary：formal=False / run_valid=False（出场被拒、仓位未平）"

    # F2 新格式：底层允许（假设未来规格 verified）
    rid2 = "run_WX_EVENT_TREND_V0_1_FIXTURE_B_9000000002"
    _write_case(rid2, summary=new_summary(rid2, formal=True, run_valid=True,
                                         reasons=[], unclosed=[]))
    cases[rid2] = "F2 新格式 summary：formal=True / run_valid=True（对照组）"

    # F3 主运行顶层=A，B 变体单独为 False ⇒ 看顶层是否做 AND 聚合
    rid3 = "run_WX_EVENT_TREND_V0_1_FIXTURE_C_9000000003"
    _write_case(rid3, summary=new_summary(rid3, formal=True, run_valid=True,
                                         reasons=[], unclosed=[],
                                         variant_b_formal=False))
    cases[rid3] = "F3 主运行顶层 formal=True，但 summary_variant_b.formal=False"

    # F4 对照：连 reconcile/identity 都没有 ⇒ 唯一能翻成「未通过」的情形
    rid4 = "run_WX_EVENT_TREND_V0_1_FIXTURE_D_9000000004"
    _write_case(rid4, summary={"run_id": rid4, "symbol": "FIXUSDT", "variant": "A"})
    cases[rid4] = "F4 对照：summary 无 reconcile/identity（⇒ dq=unknown）"

    # F5 API 创建路径：run.json 存在，_run_event_trend 的 data_quality（只看 reconcile/identity）
    rid5 = "run_WX_EVENT_TREND_V0_1_FIXTURE_E_9000000005"
    _write_case(rid5, run_json={
        "run_id": rid5, "strategy_id": "WX_EVENT_TREND_V0_1", "status": "succeeded",
        "spec": {"strategy_id": "WX_EVENT_TREND_V0_1", "symbols": ["FIXUSDT"],
                 "initial_equity": 2000.0},
        "metrics": {"initial_equity": 2000.0, "final_equity": 2000.0, "net_pnl": 0.0,
                    "net_return_pct": 0.0, "trades_count": 0,
                    "max_drawdown_pct": None},
        "data_quality": {"status": "ok", "reasons": [],
                         "reconcile_ok": True,
                         "source": "summary.json:reconcile+identity（回测程序自检，非独立证据）"},
        "engine": {"strategy_id": "WX_EVENT_TREND_V0_1"},
    })
    cases[rid5] = "F5 POST 创建路径的 run.json（_run_event_trend 写出的形状）"

    rows = []
    for rid, note in cases.items():
        doc = S.read_run(rid)
        q_env = {"status": doc.get("status"),
                 "quality": API._quality_of(doc),
                 "data_quality": doc.get("data_quality")}
        ok, verdict = ui_verdict(doc, q_env)
        sm = {}
        try:
            sm = json.loads((FIX / rid / "summary.json").read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            sm = {}
        row = {
            "run_id": rid,
            "case": note,
            "doc_source": "run.json" if (FIX / rid / "run.json").exists() else "_summary_to_run_doc",
            "bottom_formal_pass_allowed": sm.get("formal_pass_allowed"),
            "bottom_spec_status": sm.get("spec_status"),
            "bottom_run_valid": (sm.get("precision") or {}).get("run_valid"),
            "bottom_invalid_reasons": (sm.get("precision") or {}).get("invalid_reasons"),
            "bottom_unclosed_positions": (sm.get("precision") or {}).get("unclosed_positions"),
            "api_doc_status": doc.get("status"),
            "api_dq_status": (doc.get("data_quality") or {}).get("status"),
            "api_quality_status": (q_env["quality"] or {}).get("status"),
            "ui_quality_ok": ok,
            "ui_verdict": verdict,
        }
        rows.append(row)

    gate = BR.formal_run_gate()
    result = {
        "note": ("夹具为**合成**数据，只用于验证状态穿透；不代表任何真实 run 的结果。"
                 "RUNS_ROOT 已在进程内改指到本目录，未触碰 回测日志/。"),
        "runs_root_used": str(FIX),
        "formal_run_gate_static": gate,
        "rows": rows,
        "conclusions": {
            "ui_says_pass_when_bottom_formal_false": [
                r["run_id"] for r in rows
                if r["ui_quality_ok"] and r["bottom_formal_pass_allowed"] is False],
            "ui_says_pass_when_run_valid_false": [
                r["run_id"] for r in rows
                if r["ui_quality_ok"] and r["bottom_run_valid"] is False],
            "ui_says_pass_when_no_flag_at_all": [
                r["run_id"] for r in rows
                if r["ui_quality_ok"] and r["bottom_formal_pass_allowed"] is None],
            "main_top_level_flag_ignores_variant_b": [
                r["run_id"] for r in rows
                if r["bottom_formal_pass_allowed"] is True
                and "summary_variant_b.formal=False" in r["case"]],
        },
    }
    (OUT / "隔离核验.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 100)
    print("PRECISION-03 · 隔离核验（合成夹具，RUNS_ROOT →", FIX, "）")
    print("=" * 100)
    hdr = f"{'case':<46} {'bottom formal':<14} {'run_valid':<10} {'api quality':<12} {'UI':<6} verdict"
    print(hdr)
    print("-" * 100)
    for r in rows:
        print(f"{r['case'][:46]:<46} {str(r['bottom_formal_pass_allowed']):<14} "
              f"{str(r['bottom_run_valid']):<10} {str(r['api_quality_status']):<12} "
              f"{('通过' if r['ui_quality_ok'] else '未通过'):<6} {r['ui_verdict']}")
    print("-" * 100)
    print("static formal_run_gate:", json.dumps(gate.get("gate_status"), ensure_ascii=False),
          "| formal_run_allowed =", gate.get("formal_run_allowed"))
    for k, v in result["conclusions"].items():
        print(f"  {k}: {len(v)} → {v}")
    print(f"\n明细：{OUT / '隔离核验.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
