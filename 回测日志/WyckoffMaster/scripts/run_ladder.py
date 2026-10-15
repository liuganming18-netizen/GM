#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 阶梯回测 · 唯一调度器（门禁守卫 + Holdout 红线守卫）。

本文件是**调度器**，不是策略实现：不重写信号策略、不新建引擎、不下载全池。

它只做五件事：
  1. 跑 `verify_gates.py`；**两个门禁都实际 PASS 且都绑定 v3.1 才继续**（不信 ready=true）。
  2. 读 `config/frozen_segments.json`（v3.1 IS/Val/Holdout）与 `config/ladder.json`。
  3. 从 `strategy_ready.json` 读**层级→臂绑定**（代码/配置 SHA 已在门禁里核过）。
  4. 顺序实验共用**一次** InputStore，逐层逐段写独立账本（只写 `回测日志/WyckoffMaster/`）。
  5. 每段结束后跑 **v3.1 段末强平局部适配**（最后 15m 收盘，不改生产引擎）。

v3.1 最高红线（由 `adapters/holdout_guard.py` 物理强制）：
  · 选型阶段（kline/factor）任何与 Holdout 有交集的窗口一律拒绝；
  · `--phase holdout` 需 `state/winner_lock.json`（IS/Val 赢家已冻结）才允许。

运行：
    python3 run_ladder.py --phase kline      # M0→M1→M2(+M2b)→M3_BE/M3_Step（IS/Val）
    python3 run_ladder.py --phase factor     # M4（IS/Val；仅 M0–M3 有 IS/Val 正 Calmar）
    python3 run_ladder.py --phase holdout    # 最终配置冻结后仅一次
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WM = HERE.parent
sys.path.insert(0, str(WM / "adapters"))

UTC = dt.timezone.utc
PHASE_LEVELS = {
    "kline": ["M0", "M1", "M2", "M3_BE", "M3_Step"],
    "factor": ["M4"],
    "holdout": ["FINAL"],
}
SELECTION_SEGMENTS = ("IS", "Val")


def _now() -> str:
    return dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(iso: str) -> int:
    return int(dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=UTC).timestamp() * 1000)


def gate_pass() -> dict:
    r = subprocess.run([sys.executable, str(HERE / "verify_gates.py"),
                        "--json-out", str(WM / "state" / "gate_verify.json")],
                       capture_output=True, text=True)
    rep = json.loads((WM / "state" / "gate_verify.json").read_text(encoding="utf-8"))
    return {"rc": r.returncode, "overall": rep.get("overall"), "report": rep,
            "stdout": r.stdout.strip()}


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def refuse(reason: str, deps: list[str]) -> int:
    doc = {"schema": "wyckoffmaster_run_ladder/v2_v31", "generated_utc": _now(),
           "status": "REFUSED_NOT_READY", "reason": reason, "dependencies": deps}
    (WM / "state" / "ladder_status.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[REFUSED] {reason}")
    for d in deps:
        print(f"  - 依赖：{d}")
    print("未启动任何回测（不空转、不跑假回测）。")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("kline", "factor", "holdout"), required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import holdout_guard as HG

    # ── 0. Holdout 红线（最先检查，先于任何数据读取）──────────────────────────
    try:
        HG.assert_phase_allowed(a.phase)
    except HG.HoldoutViolation as e:
        return refuse(str(e), ["state/winner_lock.json（IS/Val 选型锁定后写入）"])

    # ── 1. 门禁（含 v3.1 版本绑定）──────────────────────────────────────────
    g = gate_pass()
    if g["overall"] != "BOTH_PASS":
        return refuse(f"门禁未同时 PASS（overall={g['overall']}）",
                      ["evidence/Work_策略优化_2026-10-15/strategy_ready.json (v3.1)",
                       "evidence/Work_策略数据_2026-10-15/data_ready.json (v3.1)",
                       "（scripts/verify_gates.py 查看逐项未过项）"])

    seg_doc = load_json(WM / "config" / "frozen_segments.json")
    load_json(WM / "config" / "ladder.json")
    load_json(WM / "config" / "m2_rejection.json")
    strat = load_json(Path(g["report"]["strategy_ready"]["path"]))
    levels = strat.get("levels") or {}
    need = PHASE_LEVELS[a.phase]
    if a.phase != "holdout":
        missing = [l for l in need if l not in levels]
        if missing:
            return refuse(f"strategy_ready.json 缺层级绑定：{missing}",
                          [f"levels[{m}]（arm/signal_module/config_sha256/signal_sha256）"
                           for m in missing])

    # ── 2. 段选择（选型阶段绝不含 Holdout）──────────────────────────────────
    if a.phase == "holdout":
        segs = [s for s in seg_doc["segments"] if s["name"] == "Holdout"]
    else:
        segs = [s for s in seg_doc["segments"] if s["name"] in SELECTION_SEGMENTS]

    if a.dry_run:
        print(f"[DRY-RUN] 门禁 BOTH_PASS(v3.1)；phase={a.phase}；层级={need}")
        print(f"  段：{[(s['name'], s['start_utc'], s['end_excl_utc']) for s in segs]}")
        print(f"  段末强平：{seg_doc['segment_end_liquidation']['reason_code']}")
        return 0

    # ── 3. 顺序实验：共用一次 InputStore；逐段独立账户 ───────────────────────
    import ra_bridge as B
    import segment_end_liquidation as SEL
    B.assert_production_untouched()
    eng = B.load_engine()
    rk, adm, meta = eng["W5"].load_daily_maps(doc_path=str(B.DAILY_DOC), panel="DEV")
    symbols = sorted({k for m in adm.values() for k, v in m.items() if v})
    led_dir = WM / "ledgers"
    led_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for seg in segs:
        s_ms, e_ms = _ms(seg["start_utc"]), _ms(seg["end_excl_utc"])
        dv = f"dv:wm31:{a.phase}:{seg['name']}:{seg['start_utc']}"
        store, _ = B.make_shared_store(symbols, s_ms, e_ms, data_version=dv,
                                       log=print, phase=a.phase)
        bars = SEL.make_store_bars(store)
        for lv in need:
            bind = levels.get(lv) or levels.get("FINAL", {})
            arm = bind.get("arm", lv)
            run_id = f"wm31_{a.phase}_{seg['name']}_{lv}"
            raw_lp = led_dir / f"raw_{seg['name']}_{lv}.sqlite3"
            fin_lp = led_dir / f"ledger_{seg['name']}_{lv}.sqlite3"
            out = B.run_segment_arm(eng=eng, store=store, arm=arm, symbols=symbols,
                                    start_ms=s_ms, end_ms=e_ms, run_id=run_id,
                                    ledger_path=raw_lp, log=print, phase=a.phase)
            rep = SEL.apply_v31_segment_end(
                src_ledger=raw_lp, dst_ledger=fin_lp, bars=bars, ex_module=eng["X"],
                segment_end_ms=e_ms)
            results.append({"segment": seg["name"], "level": lv, "run_id": run_id,
                            "raw_ledger": str(raw_lp), "ledger": str(fin_lp),
                            "trades": (out["run"] or {}).get("trades"),
                            "funding_rows": (out["run"] or {}).get("funding_rows"),
                            "segment_end_v31": {"n": rep["n_terminal_liquidations"],
                                                "cash_delta": rep["cash_delta"]}})
        del store
    (WM / "state" / "ladder_status.json").write_text(json.dumps(
        {"schema": "wyckoffmaster_run_ladder/v2_v31", "generated_utc": _now(),
         "status": "COMPLETED", "phase": a.phase, "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] phase={a.phase} 段×层 = {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
