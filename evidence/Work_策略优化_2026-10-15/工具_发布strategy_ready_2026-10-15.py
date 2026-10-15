#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布 v3.1 strategy_ready.json（策略窗口门禁产物，供回测窗口只读消费）。

只做三件事：① 复算代码/配置 SHA；② 读取 v3.1 核验 JSON；③ 按实际结果写 ready 实际值。
不伪造：核验未全过则 ready=false；M4 之外的因子依赖单列 blocking_items。

用法：
  PYTHONPATH=<策略目录> python 工具_发布strategy_ready_2026-10-15.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STRAT_DIR = os.path.join(ROOT, "策略")
MODULE_DIR = os.path.join(STRAT_DIR, "WyckoffMaster")
VERIFY_JSON = os.path.join(HERE, "20_核验结果_verify_v31.json")
OUT = os.path.join(HERE, "strategy_ready.json")
PY = sys.executable


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_bundle():
    files = sorted(f for f in os.listdir(MODULE_DIR)
                   if f.endswith(".py") or f == "README.md")
    per = {f: sha256_file(os.path.join(MODULE_DIR, f)) for f in files}
    bundle = hashlib.sha256("|".join(f"{f}:{per[f]}" for f in files).encode()).hexdigest()
    return {"bundle_sha256": bundle, "per_file": per, "module_dir": MODULE_DIR}


def config_dump():
    code = (
        "import json,sys;"
        "sys.path.insert(0,%r);"
        "from WyckoffMaster.params import FROZEN, VERSION;"
        "from WyckoffMaster.experiments import experiment_config_hash, EXPERIMENTS,"
        "HISTORICAL_LADDER, FORWARD_VALIDATION;"
        "from WyckoffMaster.segments import spec;"
        "print(json.dumps({'version':VERSION,'params':FROZEN.identity(),"
        "'experiment_config_hash':experiment_config_hash(),"
        "'experiments':{k:v.to_dict() for k,v in EXPERIMENTS.items()},"
        "'historical_ladder':list(HISTORICAL_LADDER),"
        "'forward_validation':list(FORWARD_VALIDATION),"
        "'segments':spec()}, sort_keys=True, ensure_ascii=False))" % STRAT_DIR
    )
    return json.loads(subprocess.check_output([PY, "-c", code], text=True))


def main():
    if not os.path.exists(VERIFY_JSON):
        raise SystemExit(f"缺少核验结果：{VERIFY_JSON}（先跑 WyckoffMaster.verify --sha --out ...）")
    with open(VERIFY_JSON, encoding="utf-8") as fh:
        verify = json.load(fh)
    checks = verify["checks"]
    passed = {k: bool(v["passed"]) for k, v in checks.items()}
    all_pass = all(passed.values())
    code = code_bundle()
    cfg = config_dump()

    blocking = [
        {"id": "B1", "item": "M4 依赖 OI 变化序列（自身 90 日历史）",
         "owner": "策略数据窗口（data_ready.json）",
         "impact": "M4 真实回测不可运行；M0–M3、M2b 不依赖该因子",
         "status": "未解除"},
        {"id": "B2", "item": "执行层按 SHA 绑定既有引擎（15m 成交/滑点/费用/真实资金费/回撤停机/"
                              "段末最后 15m 收盘强平/三段独立账户）",
         "owner": "回测窗口", "impact": "回测运行前必须完成",
         "status": "未解除（属回测窗口职责）"},
        {"id": "B3", "item": "1h/4h 信号数据、15m 执行数据、IS/Val 物理隔离研究输入",
         "owner": "策略数据窗口", "impact": "无物理隔离输入则无法按 v3.1 运行",
         "status": "未解除（属数据窗口职责）"},
        {"id": "B4", "item": "M5/M6 前向验证需真实前向采集 ≥30 自然日且 ≥90 唯一有效快照",
         "owner": "策略数据窗口（前向队列，Holdout 截止后开始）",
         "impact": "M5/M6 本轮不参与历史选型，属预期内",
         "status": "未解除（设计如此）"},
    ]

    doc = {
        "gate": "strategy",
        "schema": "strategy_ready/1",
        "version": "3.1",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "策略规则与实现（M0–M4 + 条件 M2b；M5/M6 前向接口）；不含数据与回测",
        "authority": "docs/WyckoffMaster_v3.1_执行补充与窗口交接_2026-10-15.md",
        "ready": all_pass,
        "ready_actual": all_pass,
        "ready_meaning": "策略门禁 PASS = v3.1 冻结规则与实现通过全部四件套及规格核验；"
                         "不等于可回测（回测另需 data_ready 门禁 PASS，且策略/数据版本都须为 v3.1）",
        "backtest_run_ready": False,
        "backtest_run_ready_reason": "data_ready.json 尚未发布；执行层绑定与样本切分由回测窗口完成",
        "code_sha256": code,
        "config": {
            "version": cfg["version"],
            "params": cfg["params"],
            "experiment_config_hash": cfg["experiment_config_hash"],
            "experiments": cfg["experiments"],
            "historical_ladder": cfg["historical_ladder"],
            "forward_validation": cfg["forward_validation"],
            "segments": cfg["segments"],
        },
        "four_piece_tests": {
            "T1_no_lookahead": passed.get("T1_test_no_lookahead"),
            "T2_anchor_no_overwrite_single_confirm":
                passed.get("T2_test_anchor_no_overwrite_single_confirm"),
            "T3_stop_limits_m2b_and_below_struct":
                passed.get("T3_test_stop_limits_m2b_and_below_struct"),
            "T4_step_trail_next_bar_monotonic":
                passed.get("T4_test_step_trail_next_bar_monotonic"),
        },
        "reject_stats_interface": {
            "module": "WyckoffMaster.core",
            "functions": ["m2_over_limit_stats(records)", "m2b_trigger(stats_is, stats_val)"],
            "denominator": "到达结构止损超限判断、具备有效结构（struct_low 有限且 raw_stop>0）"
                           "与有效执行价/ATR 的唯一候选信号数",
            "zero_denominator": "记 N/A，不触发",
            "trigger_rule": "任一类型 IS+Val 合并超限拒单率 > 30% ⇒ 加跑预注册 M2b",
            "over_limit_events": "需另留逐笔键，不与穿底/精度/资金不足混算",
        },
        "verifications": passed,
        "verification_detail": checks,
        "environment": verify.get("environment", {}),
        "data_read_disclosure": verify.get("data_read_disclosure"),
        "blocking_items": blocking,
        "notes": [
            "报告不等于 Codex 验收；需回测窗口独立核对。",
            "1h/4h 及 M0–M4/M2b 为研究版本，不增加生产账户、不切生产端口、不改他窗文件、不删旧代码。",
            "v3.1 变更：半开 UTC 三段各自 5000U 零仓零挂单起算、段末最后 15m 收盘强平；"
            "M2 逐类型超限统计 + 条件 M2b（按真实距离自然缩仓）；entry_below_struct_low 穿底拒单；"
            "历史只跑 M0–M4+M2b，M5/M6 改前向（30 日分位 / 30 日 Z 降序）。",
            "选型阶段不读取/运行 Holdout（segments.assert_no_holdout）。",
        ],
    }

    # 保留被 v3.1 取代的 v3 产物（不删除旧交付）
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("version") != "3.1":
                bak = os.path.join(HERE, "strategy_ready_v3_superseded.json")
                with open(bak, "w", encoding="utf-8") as fh:
                    json.dump(old, fh, ensure_ascii=False, indent=2)
                print(f"superseded v3 kept: {bak}")
        except Exception as exc:  # noqa: BLE001
            print(f"warn: 未备份旧产物：{exc}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"written: {OUT}")
    print(f"version={doc['version']} ready={doc['ready']} all_checks_pass={all_pass}")
    print(f"code_bundle_sha256={code['bundle_sha256']}")
    print(f"experiment_config_hash={cfg['experiment_config_hash']}")
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
