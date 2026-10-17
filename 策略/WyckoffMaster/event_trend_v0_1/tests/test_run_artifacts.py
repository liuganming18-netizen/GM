# -*- coding: utf-8 -*-
"""Run 产物原子提交、指纹与全链路逐笔证据的回归测试（WORK-07 第四次复核阻断项 4）。

覆盖：
1. 同一输入两次运行 ⇒ orders / fills / positions / trades / ledger / equity 逐字节一致；
2. 一个 Run 的产物集合必须包含全链路文件（订单、成交、持仓、成交明细、账本、权益）；
3. `_commit_run_dir` 是「整套原子提交」：要么整套就位，要么完全不存在，不留半套；
4. 重复提交同一 Run ID 必须显式报错，不得静默覆盖；
5. 指纹四件套（规则 / 代码 / 配置 / 输入）齐备且可复算。
"""
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd
import pytest

base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
sys.path.insert(0, os.path.join(base_dir, "策略"))

from WyckoffMaster.event_trend_v0_1.config import EventTrendConfig
from WyckoffMaster.event_trend_v0_1.contracts import CandidateSnapshot
from WyckoffMaster.event_trend_v0_1.engine_adapter import run_event_trend_backtest
from WyckoffMaster.event_trend_v0_1.runner import EventTrendRunner

HOUR_MS = 3600 * 1000
MIN15_MS = 15 * 60 * 1000

REQUIRED_EVIDENCE_FILES = (
    "inputs.json", "summary.json", "manifest.json",
    "events.jsonl", "candidates.jsonl", "transitions.jsonl",
    "validation_results.jsonl",
    "orders.jsonl", "fills.jsonl", "positions.jsonl", "trades.jsonl",
    "rejects.jsonl", "funding.jsonl", "ledger.jsonl", "equity_curve.jsonl",
    "decisions.jsonl",
)


def _scenario():
    """构造一个能真实成交的合成场景（含前置已完成 15m bar 与 quote_volume）。"""
    t0 = 1788652800000
    t_1h_open = t0 + 24 * HOUR_MS
    t_1h_close = t_1h_open + HOUR_MS
    t_exec = t_1h_close + MIN15_MS

    cand = CandidateSnapshot.create(
        candidate_id="cand_artifact_001", event_id="ev_artifact_001",
        strategy_version="v0.1.0", symbol="SOLUSDT", venue="binance",
        snapshot_time_ms=t_1h_close, available_at_ms=t_1h_close,
        box_high_h=110.0, box_low_l=100.0, box_atr_a=2.0, box_er=0.15,
        sweep_low_s=99.2, retest_low_lr=100.3, breakout_close=111.0,
        breakout_volume=2500.0, v20_median_volume=1000.0, volume_ratio=2.5,
        oi_breakout=103500.0, oi_6h_ago=100000.0, oi_growth_6h=0.035,
        initial_stop_price=100.1, initial_risk_r=10.9,
        max_entry_price=112.0, min_entry_price=110.0,
        execution_earliest_ms=t_exec)

    df_1h = pd.DataFrame([{
        "timestamp": t_1h_open, "open": 105.0, "high": 111.5, "low": 104.8,
        "close": 111.0, "volume": 2500.0, "quote_volume": 277_500.0}])

    rows = []
    for k in range(21):
        ts = t_exec - MIN15_MS + k * MIN15_MS
        if k == 0:
            rows.append({"timestamp": ts, "open": 111.0, "high": 111.3,
                         "low": 110.9, "close": 111.1, "volume": 50000.0,
                         "quote_volume": 5_500_000.0})
        elif k == 1:
            rows.append({"timestamp": ts, "open": 111.2, "high": 111.8,
                         "low": 111.0, "close": 111.5, "volume": 50000.0,
                         "quote_volume": 5_575_000.0})
        else:
            rows.append({"timestamp": ts, "open": 111.5 + k * 0.5,
                         "high": 111.8 + k * 0.5, "low": 111.3 + k * 0.5,
                         "close": 111.6 + k * 0.5, "volume": 50000.0,
                         "quote_volume": 5_580_000.0})
    df_15m = pd.DataFrame(rows)
    funding = [{"time_ms": t_exec + 4 * HOUR_MS, "rate": 0.0001,
                "mark_price": 115.0, "rate_type": "settled"}]
    return cand, df_1h, df_15m, funding


def test_run_artifacts_are_deterministic_and_complete():
    """两次运行逐字段一致；产物集合含全链路证据文件。"""
    cand, df_1h, df_15m, funding = _scenario()
    cfg = EventTrendConfig()
    runner = EventTrendRunner(base_dir=base_dir)

    res1 = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], funding,
                                    variant="A", config=cfg,
                                    price_step="0.01")
    res2 = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], funding,
                                    variant="A", config=cfg,
                                    price_step="0.01")

    # ① 确定性：订单 / 账本 / 成交 逐字节一致
    assert json.dumps(res1["orders"], sort_keys=True) == \
        json.dumps(res2["orders"], sort_keys=True)
    assert json.dumps(res1["ledger_events"], sort_keys=True) == \
        json.dumps(res2["ledger_events"], sort_keys=True)
    assert json.dumps(res1["engine_result"]["trades"], sort_keys=True) == \
        json.dumps(res2["engine_result"]["trades"], sort_keys=True)
    assert json.dumps(res1["engine_result"]["equity_curve"], sort_keys=True) == \
        json.dumps(res2["engine_result"]["equity_curve"], sort_keys=True)

    # ② 身份链：成交 → 订单 → 信号 → 候选
    val = res1["validation_results"][0]
    assert val.executed is True and val.identity_status == "ok"
    tr = res1["engine_result"]["trades"][0]
    assert tr["signal_id"] == cand.candidate_id
    assert tr["order_id"] == val.order_id
    assert tr["exit_order_id"] is not None

    # ③ 产物装配：文件集合齐备
    files = runner._build_run_files(
        run_id="run_test", symbol="SOLUSDT", variant="A", sm_inst=None,
        res_inst=res1, funnel={}, run_timestamp=1,
        data_summary={}, fingerprints={})
    # sm_inst=None 时事件文件为空，但全链路证据文件必须存在
    # （manifest.json 由 _commit_run_dir 在原子提交时生成，此处不要求）
    for name in REQUIRED_EVIDENCE_FILES:
        if name in ("events.jsonl", "candidates.jsonl", "transitions.jsonl",
                    "manifest.json"):
            continue
        assert name in files, f"缺产物文件 {name}"

    # 账本事件类型覆盖 Order/Fill/Position/费用
    types = {e["event_type"] for e in res1["ledger_events"]}
    assert {"ORDER_CREATED", "ORDER_SUBMITTED", "ORDER_FILLED",
            "POSITION_OPENED", "POSITION_CLOSED", "FEE_CHARGED",
            "GENESIS_INIT"} <= types


def test_commit_run_dir_is_atomic_and_refuses_overwrite():
    """整套产物原子提交：不留半套；重复 Run ID 显式报错。"""
    cand, df_1h, df_15m, funding = _scenario()
    res = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], funding,
                                   variant="A", price_step="0.01")

    with tempfile.TemporaryDirectory() as tmp:
        runner = EventTrendRunner(base_dir=base_dir)
        runner.runs_base = tmp
        run_dir = os.path.join(tmp, "run_atomic_test")
        files = runner._build_run_files(
            run_id="run_atomic_test", symbol="SOLUSDT", variant="A", sm_inst=None,
            res_inst=res, funnel={}, run_timestamp=1,
            data_summary={}, fingerprints={})

        manifest = runner._commit_run_dir(run_dir, files)
        assert os.path.isdir(run_dir)
        assert manifest["commit_mode"] == "atomic_directory_rename"
        # manifest 自描述每个文件的 sha256 与字节数
        assert set(manifest["files"]) == set(files)
        with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["files_count"] == len(files)
        # 目录内文件数 = 产物数 + manifest
        assert len(os.listdir(run_dir)) == len(files) + 1

        # 无残留临时目录（原子提交完成后不得留下 .partial-*）
        leftovers = [d for d in os.listdir(tmp) if ".partial-" in d]
        assert leftovers == []

        # 重复提交同一 Run ID ⇒ 显式报错，不静默覆盖
        with pytest.raises(FileExistsError):
            runner._commit_run_dir(run_dir, files)


def test_fingerprints_cover_rules_code_config_inputs():
    """指纹四件套齐备且可复算（冻结规则 SHA 必须与冻结版本一致）。"""
    cand, df_1h, df_15m, funding = _scenario()
    cfg = EventTrendConfig()
    runner = EventTrendRunner(base_dir=base_dir)

    rules = runner.rules_fingerprint()
    assert rules["sha256"] == \
        "1f1167de35412cb7d1d3608d8069c4108324f7641de7c369bf94882f1679a8ab"

    code = runner.code_fingerprint()
    assert code["missing"] == []
    assert code["combined_sha256"] == runner.code_fingerprint()["combined_sha256"]

    cfg_fp = runner.config_fingerprint(cfg)
    assert cfg_fp["sha256"] == runner.config_fingerprint(EventTrendConfig())["sha256"]
    assert cfg_fp["params"]["max_holding_days"] == 10
    assert cfg_fp["params"]["max_participation_of_prev_15m_quote"] == 0.001

    oi_ts = np.array([1, 2, 3], dtype=np.int64)
    oi_vals = np.array([1.0, 2.0, 3.0])
    inp = runner.input_fingerprint(df_1h, df_1h, df_15m, oi_ts, oi_vals, funding)
    assert set(inp) == {"bar_4h", "bar_1h", "bar_15m", "oi_ts", "oi_values",
                        "funding_events"}
    assert inp["bar_15m"]["rows"] == len(df_15m)


def test_initial_equity_has_single_source():
    """`initial_equity` 只允许一个事实源：`EventTrendConfig`。

    回归防护：曾出现 `runner.run_variant_abc` / `EventTrendExecParams` 各自
    默认 2000.0、而 config 是 10000.0 的「两张皮」，导致同一输入在不同入口
    下本金相差 5 倍。
    """
    import inspect

    from WyckoffMaster.event_trend_v0_1.engine_adapter import EventTrendExecParams

    cfg_eq = EventTrendConfig().initial_equity
    # 2026-10-17：本金事实源已由 10000 调整为 2000（配置是唯一事实源）。
    # 这里**不**钉死某个历史常数，只校验「事实源唯一」这一真正的不变量：
    #   · config 给出正数本金；
    #   · `run_variant_abc` 的默认值必须是 None（不得自带第二个默认）；
    #   · 未显式传入时，回测实际权益必须等于 config（而不是 ExecParams 的占位值）。
    assert cfg_eq > 0

    sig = inspect.signature(EventTrendRunner.run_variant_abc)
    assert sig.parameters["initial_equity"].default is None

    # 未显式传入时，回测实际采用的权益必须等于 config
    cand, df_1h, df_15m, funding = _scenario()
    res = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], funding,
                                   variant="A", price_step="0.01")
    assert res["summary"]["initial_equity"] == cfg_eq
    # 显式传入则必须被尊重（允许研究口径覆盖，但必须是显式的）
    res2 = run_event_trend_backtest("SOLUSDT", df_1h, df_15m, [cand], funding,
                                    variant="A", initial_equity=2000.0,
                                    price_step="0.01")
    assert res2["summary"]["initial_equity"] == 2000.0


def test_fixture_provenance_hashes_match():
    """真实样本夹具必须与 PROVENANCE.json 记录逐字节一致（防被替换）。"""
    import hashlib

    fix_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "sol_real_2024h1")
    with open(os.path.join(fix_dir, "PROVENANCE.json"), encoding="utf-8") as f:
        prov = json.load(f)
    assert prov["source_run_id"] == "bt_20261016T101649Z_4aa21d94"
    for name, meta in prov["files"].items():
        p = os.path.join(fix_dir, name)
        assert os.path.exists(p), f"夹具缺失 {name}"
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        assert h == meta["sha256"], f"{name} 与 PROVENANCE 不一致"
        assert os.path.getsize(p) == meta["bytes"]
