# -*- coding: utf-8 -*-
"""【事后重建】历史遗留 Run 目录的补全工具 —— 明确标注为「重建」，不冒充原运行时证据。

背景（WORK-07 第四次独立复核阻断项 4）
────────────────────────────────────
旧版脚本把**用当前代码重新跑出来的**内容直接写进历史 Run 目录，并把
`timestamp_created` 写死为 1792164990，等价于「事后重建冒充原运行时证据」。

本版规则（不可回退）
────────────────────
1. **不写入历史 Run 目录**：历史 Run 目录保持原样（含残缺状态），只读；
2. 重建产物一律写入独立目录 `回测日志/event_trend_v0_1/reconstructed/<原 run_id>/`；
3. 每个重建目录必须带 `RECONSTRUCTION_NOTICE.json`，写明：
   · 原 run_id 与重建 run_id（新 Run ID）；
   · 重建时刻、重建所用的代码/配置/输入/规则指纹；
   · 明确声明「这是事后重建，不是原运行时证据」；
4. 不再写死任何 `timestamp_created`。
"""
from __future__ import annotations

import json
import os
import sys
import time

base_dir = "/Users/boos/Desktop/威科夫因子共振趋势"
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from 策略.WyckoffMaster.event_trend_v0_1.config import EventTrendConfig
from 策略.WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner

# 历史遗留 Run（原目录保持只读，不覆盖）
LEGACY_RUNS = {
    "BTCUSDT": "run_WX_EVENT_TREND_V0_1_BTCUSDT_1792164990",
    "ETHUSDT": "run_WX_EVENT_TREND_V0_1_ETHUSDT_1792164993",
    "SOLUSDT": "run_WX_EVENT_TREND_V0_1_SOLUSDT_1792164996",
}

RECONSTRUCTED_SUBDIR = "回测日志/event_trend_v0_1/reconstructed"


def reconstruct():
    runner = EventTrendRunner(base_dir=base_dir)
    cfg = EventTrendConfig()
    out_root = os.path.join(base_dir, RECONSTRUCTED_SUBDIR)
    os.makedirs(out_root, exist_ok=True)

    code_fp = runner.code_fingerprint()
    rules_fp = runner.rules_fingerprint()
    config_fp = runner.config_fingerprint(cfg)

    print(">>> 事后重建历史 Run 的**事件产物**（不写入原目录，明确标注为重建）...")
    for sym, legacy_id in LEGACY_RUNS.items():
        recon_run_id = f"reconstructed_{legacy_id}_{int(time.time())}"
        target_dir = os.path.join(out_root, recon_run_id)
        os.makedirs(target_dir, exist_ok=True)

        df_4h, df_1h, df_15m, oi_ts, oi_vals, fund = runner.load_symbol_data(
            sym, "2024-01-01", "2024-06-30")
        sm_a = runner.run_state_machine_simulation(
            sym, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True)

        inputs_fp = runner.input_fingerprint(df_4h, df_1h, df_15m, oi_ts, oi_vals, fund)

        notice = {
            "notice": "POST_HOC_RECONSTRUCTION_NOT_ORIGINAL_RUNTIME_EVIDENCE",
            "中文说明": ("本目录内容是用**当前代码**重新运行得到的重建产物，"
                     "不是原 Run 运行时的原始证据。原 Run 目录保持只读、未被覆盖。"),
            "original_run_id": legacy_id,
            "original_run_dir_readonly": os.path.join(
                "回测日志/event_trend_v0_1/runs", legacy_id),
            "reconstruction_run_id": recon_run_id,
            "reconstructed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "symbol": sym,
            "data_window": "2024-01-01 ~ 2024-06-30",
            "fingerprints": {
                "code": code_fp,
                "config": config_fp,
                "rules": rules_fp,
                "inputs": inputs_fp,
            },
            "known_limitations": [
                "原 Run 的 inputs/events 内容无法恢复，本目录只是同参数同数据的重跑结果",
                "不得用本目录内容宣称原 Run 当时的产出",
                "重建产物无 order/fill/ledger 全链路证据（原 Run 从未生成）",
            ],
        }
        with open(os.path.join(target_dir, "RECONSTRUCTION_NOTICE.json"), "w",
                  encoding="utf-8") as f:
            json.dump(notice, f, indent=2, ensure_ascii=False, sort_keys=True)

        events = [ev.to_dict() for ev in sm_a.completed_events]
        candidates = [c.to_dict() for c in sm_a.candidates]
        transitions = [t.to_dict() for t in sm_a.transitions]
        for name, rows in (("events.jsonl", events),
                           ("candidates.jsonl", candidates),
                           ("transitions.jsonl", transitions)):
            with open(os.path.join(target_dir, name), "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n")

        print(f"[{sym}] 重建产物 -> {recon_run_id}"
              f"（原目录 {legacy_id} 未改动；events={len(events)} candidates={len(candidates)}）")

    print(f"\n>>> 重建根目录：{out_root}")
    print(">>> 提醒：重建产物不得作为原运行时证据使用。")


if __name__ == "__main__":
    reconstruct()
