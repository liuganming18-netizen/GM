#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 阶梯回测 · 唯一调度器（门禁守卫 + Holdout 红线守卫）。

本文件是**调度器**，不是策略实现：不重写信号策略、不新建引擎、不下载全池。

它只做五件事：
  1. 跑 `verify_gates.py`；**两个门禁都实际 PASS 且都绑定 v3.1 才继续**（不信 ready=true）。
  2. 读 `config/frozen_segments.json`（v3.1 IS/Val/Holdout）与 `config/ladder.json`。
  3. 从 `strategy_ready.json` 读**层级→臂绑定**（代码/配置 SHA 已在门禁里核过）。
  4. 顺序实验共用**一次** `SegmentData`（项目内 v3.1 研究输入），逐层逐段写独立账本
     （只写 `回测日志/WyckoffMaster/`）。
  5. 段末强平由引擎内置（本段最后 15m 收盘；`segment_end_force_close_last_15m_close`）。

v3.1 最高红线（由 `adapters/holdout_guard.py` 物理强制）：
  · 选型阶段（kline/factor）任何与 Holdout 有交集的窗口一律拒绝；
  · `--phase holdout` 需 `state/winner_lock.json`（IS/Val 赢家已冻结）才允许。

运行：
    python3 run_ladder.py --phase kline      # M0→M1→M2(+M2b)→M3_BE/M3_Step（IS/Val）
    python3 run_ladder.py --phase factor     # M4（IS/Val；继承 kline 阶段父配置）
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
sys.path.insert(0, str(WM))
sys.path.insert(0, str(WM / "adapters"))

UTC = dt.timezone.utc
PHASE_LEVELS = {
    "kline": ["M0", "M1", "M2", "M2b", "M3_BE", "M3_Step"],
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


def refuse(reason: str, deps: list) -> int:
    doc = {"schema": "wyckoffmaster_run_ladder/v3_v31", "generated_utc": _now(),
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
    import ra_bridge as B

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

    # ── 2. 配置与隔离前置核验 ───────────────────────────────────────────────
    seg_doc = load_json(WM / "config" / "frozen_segments.json")
    load_json(WM / "config" / "ladder.json")
    load_json(WM / "config" / "m2_rejection.json")
    B.assert_production_untouched()
    B.assert_no_legacy_research_dir()
    strat = load_json(Path(g["report"]["strategy_ready"]["path"]))
    levels = strat.get("levels") or {}
    need = PHASE_LEVELS[a.phase]
    if a.phase != "holdout":
        missing = [l for l in need if l not in levels]
        if missing:
            return refuse(f"strategy_ready.json 缺层级绑定：{missing}",
                          [f"levels[{m}]（arm/signal_module/config_sha256/signal_sha256）"
                           for m in missing])

    # ── 3. 段选择（选型阶段绝不含 Holdout）──────────────────────────────────
    if a.phase == "holdout":
        segs = [s for s in seg_doc["segments"] if s["name"] == "Holdout"]
    else:
        segs = [s for s in seg_doc["segments"] if s["name"] in SELECTION_SEGMENTS]

    if a.dry_run:
        print(f"[DRY-RUN] 门禁 BOTH_PASS(v3.1)；phase={a.phase}；层级={need}")
        print(f"  段：{[(s['name'], s['start_utc'], s['end_excl_utc']) for s in segs]}")
        print(f"  段末强平：{seg_doc['segment_end_liquidation']['reason_code']}")
        return 0

    # ── 4. 顺序实验：逐段独立账户；逐层独立账本 ─────────────────────────────
    from engine import ladder as LAD
    from engine.m4 import make_m4_rank_provider
    from WyckoffMaster import experiments as EX

    engine_files = list((WM / "engine").glob("*.py"))
    strat_files = list((WM.parent.parent / "策略" / "WyckoffMaster").glob("*.py"))

    def bundle_sha(files):
        import hashlib
        h = hashlib.sha256()
        for p in sorted(files):
            h.update(p.name.encode())
            h.update(B.sha256_file(p).encode())
        return h.hexdigest()

    code_sha = bundle_sha(engine_files + strat_files)
    config_sha = EX.experiment_config_hash()
    input_sha = B.input_manifest_sha()
    parent_specs = None
    if a.phase == "factor":
        kline_state = WM / "state" / "ladder_kline.json"
        if not kline_state.exists():
            return refuse("factor 阶段需要 kline 阶段结果以继承父配置",
                          [str(kline_state)])
        parent_specs = json.loads(kline_state.read_text(encoding="utf-8"))["specs"]

    led_dir = WM / "ledgers"
    led_dir.mkdir(parents=True, exist_ok=True)

    from engine import loader as LD
    tf = "1h"
    segments, rank_by_seg = {}, {}
    for seg in segs:
        s_ms, e_ms = _ms(seg["start_utc"]), _ms(seg["end_excl_utc"])
        sd = LD.SegmentData(seg["name"])
        segments[seg["name"]] = (sd, s_ms, e_ms)
        rank_by_seg[seg["name"]] = make_m4_rank_provider(sd, tf)

    res = LAD.run_ladder(segments=segments, tf=tf, run_tag=f"wm31_{a.phase}", led_dir=led_dir,
                         code_sha=code_sha, config_sha=config_sha, input_sha=input_sha,
                         m2_collect=(a.phase == "kline"), rank_provider_by_seg=rank_by_seg,
                         log=print)

    seg_names = [s["name"] for s in segs]
    results = {}
    for seg_name in seg_names:
        results[seg_name] = {
            "n_symbols": res["n_symbols"][seg_name],
            "specs": res["specs"],
            "inheritance": res["inheritance"],
            "consumed": {lv: res["consumed"][lv][seg_name] for lv in res["consumed"]},
            "metrics": {lv: res["metrics"][lv][seg_name] for lv in res["metrics"]},
            "trades": {lv: res["results"][lv][seg_name]["trades"] for lv in res["results"]},
            "net": {lv: res["results"][lv][seg_name]["net"] for lv in res["results"]},
        }
        (WM / "state" / f"ladder_{a.phase}_{seg_name}.json").write_text(
            json.dumps(results[seg_name], ensure_ascii=False, indent=2), encoding="utf-8")

    (WM / "state" / f"ladder_{a.phase}.json").write_text(
        json.dumps({"schema": "wyckoffmaster_ladder_phase/v1", "generated_utc": _now(),
                    "phase": a.phase, "levels": res["levels_run"], "code_sha256": code_sha,
                    "config_sha256": config_sha, "input_manifest_sha256": input_sha,
                    "decisions": res.get("decisions"), "combined": res.get("combined"),
                    "final_winner": res.get("final_winner"),
                    "segments": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    (WM / "state" / "ladder_status.json").write_text(json.dumps(
        {"schema": "wyckoffmaster_run_ladder/v3_v31", "generated_utc": _now(),
         "status": "COMPLETED", "phase": a.phase, "levels": res["levels_run"],
         "segments": {k: {"trades": v["trades"], "net": v["net"]} for k, v in results.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] phase={a.phase} levels={res['levels_run']}")
    return 0


def _input_sha():
    from engine import paths as P
    return P.input_manifest_sha()


if __name__ == "__main__":
    raise SystemExit(main())