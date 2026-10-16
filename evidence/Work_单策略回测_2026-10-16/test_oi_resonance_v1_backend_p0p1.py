# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 · 后端接入轮「请求配置生效 / 快照 / 质量」回归（2026-10-16）。

对应 Codex 审计的 P0-2 与 P1-1/P1-3：

  P0-2  请求配置**真正生效**
        · `fee_rate` / `slippage_bps` / `initial_equity` 属 **exec 作用域**，
          必须进入 `exec_overrides` 并能在 `exec_params(**...)` 上看到实效；
          修复前它们被丢进 `signal_params` 而被忽略（`ep` 恒为冻结默认值）。
        · 显式 UTC 窗口必须被解析/校验（半开、15m 网格、>=1 天），且不与 window_days 并用。
        · `symbols` 长度 != 1 ⇒ 显式 400（不取 [0] 静默丢弃）。

  P1-1  输入不可变快照 + 代码 hash 覆盖后端文件。
  P1-3  资金费**毫秒**逐事件重算（`funding_evidence`）。

运行：
  /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python -m pytest \
      evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_p0p1.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "策略"))
sys.path.insert(0, str(ROOT / "框架" / "backend"))

import backtest_store as S        # noqa: E402
import oi_resonance_bridge as BR  # noqa: E402
import backtest_runner as RUN     # noqa: E402

WS = "2026-10-09T00:00:00Z"
WE = "2026-10-16T00:00:00Z"


# ══════════════════════════ P0-2：窗口解析 ══════════════════════════

def test_explicit_iso_window_ok():
    clean, errs = BR.validate_run_spec({"window_start": WS, "window_end": WE})
    assert errs == []
    w = clean["window"]
    assert w["mode"] == "explicit"
    assert w["start_ms"] == BR.parse_utc_ms(WS, name="x")[0]
    assert w["end_ms"] == BR.parse_utc_ms(WE, name="x")[0]
    assert (w["end_ms"] - w["start_ms"]) % BR.WINDOW_GRID_MS == 0


def test_explicit_epoch_ms_window_ok():
    s = 1_791_504_000_000          # 2026-10-09T00:00:00Z
    e = 1_792_108_800_000          # 2026-10-16T00:00:00Z
    clean, errs = BR.validate_run_spec({"window_start": s, "window_end": e})
    assert errs == [] and clean["window"]["start_ms"] == s


def test_window_single_endpoint_rejected():
    _, errs = BR.validate_run_spec({"window_start": WS})
    assert any("必须同时给出" in e for e in errs)


def test_window_off_grid_rejected():
    _, errs = BR.validate_run_spec({"window_start": "2026-10-09T00:07:00Z",
                                    "window_end": WE})
    assert any("网格" in e for e in errs)


def test_window_too_short_rejected():
    _, errs = BR.validate_run_spec({"window_start": "2026-10-15T00:00:00Z",
                                    "window_end": "2026-10-16T00:00:00Z"})
    assert errs == []                                   # 恰好 1 天 ⇒ 放行
    _, errs2 = BR.validate_run_spec({"window_start": "2026-10-15T12:00:00Z",
                                     "window_end": "2026-10-16T00:00:00Z"})
    assert any("不足 1 天" in e for e in errs2)


def test_window_days_and_explicit_mutually_exclusive():
    _, errs = BR.validate_run_spec({"window_start": WS, "window_end": WE,
                                    "window_days": 7})
    assert any("互斥" in e for e in errs)


def test_parse_utc_ms_rejects_subsecond():
    ms, err = BR.parse_utc_ms("2026-10-09T00:00:00.500Z", name="window_start")
    assert ms is None and "亚秒" in err


# ══════════════════════════ P0-2：参数作用域 ══════════════════════════

def test_exec_cost_params_land_in_exec_scope_and_take_effect():
    """fee_rate / slippage_bps / initial_equity 必须进 exec 作用域并**真正生效**。"""
    clean, errs = BR.validate_run_spec({
        "window_start": WS, "window_end": WE,
        "params": {"fee_rate": 0.001, "slippage_bps": 12.0, "initial_equity": 50_000.0}})
    assert errs == []
    assert clean["signal_overrides"] == {}
    assert set(clean["exec_overrides"]) == {"fee_rate", "slippage_bps", "initial_equity"}
    ep = BR.exec_params(**clean["exec_overrides"])
    assert ep.fee_rate == pytest.approx(0.001)
    assert ep.slippage_bps == pytest.approx(12.0)
    assert ep.initial_equity == pytest.approx(50_000.0)
    # 与冻结默认值确实不同 ⇒ 请求生效（不是「接受但忽略」）
    assert ep.fee_rate != BR.C.EXEC_FROZEN.fee_rate
    assert ep.initial_equity != BR.C.EXEC_FROZEN.initial_equity


def test_signal_param_lands_in_signal_scope():
    clean, errs = BR.validate_run_spec({"params": {"oi_min_resonance": 0.3}})
    assert errs == []
    assert clean["signal_overrides"] == {"oi_min_resonance": 0.3}
    assert clean["exec_overrides"] == {}
    assert BR.signal_params(**clean["signal_overrides"]).oi_min_resonance == 0.3


def test_unknown_param_rejected():
    _, errs = BR.validate_run_spec({"params": {"not_a_real_param": 1}})
    assert any("白名单" in e for e in errs)


def test_live_order_enabled_rejected():
    _, errs = BR.validate_run_spec({"params": {"live_order_enabled": True}})
    assert any("白名单" in e for e in errs)


def test_out_of_range_exec_param_rejected():
    _, errs = BR.validate_run_spec({"params": {"fee_rate": 2.0}})
    assert any("fee_rate" in e for e in errs)


def test_invalid_signal_combo_rejected():
    """候选包自身校验（exit_window <= entry_window）也要在 POST 阶段拦住。"""
    _, errs = BR.validate_run_spec({"params": {"donchian_exit_window": 30}})
    assert any("exit_window" in e for e in errs)


# ══════════════════════════ P0-2：symbols ══════════════════════════

def test_symbols_multi_rejected_explicitly():
    _, errs = BR.validate_run_spec({"symbols": ["BTCUSDT", "ETHUSDT"]})
    assert any("单标的" in e for e in errs)


def test_symbols_single_ok():
    clean, errs = BR.validate_run_spec({"symbols": ["BTCUSDT"]})
    assert errs == [] and clean["symbols"] == ["BTCUSDT"]


# ══════════════════════════ P1-1：代码 hash 覆盖 ══════════════════════════

def test_code_hash_covers_backend_files():
    man = BR.code_hash_manifest()
    assert any(k.startswith("strategy/") for k in man)
    for n in BR.BACKEND_CODE_FILES:
        assert f"backend/{n}" in man
        assert man[f"backend/{n}"] != "MISSING"


# ══════════════════════════ P1-3：毫秒逐事件重算 ══════════════════════════

def test_funding_evidence_ms_counts_2ms_after_entry():
    """Codex 报的真实样本：开仓后 2ms 结算必须计入（修复前被截秒排除）。"""
    trade = {"entry_time": 1_791_734_400_000, "exit_time": 1_791_753_300_000,
             "qty": 1.9124088323822217, "direction": 1}
    events = [{"time_ms": 1_791_734_400_002, "rate": 0.00001347, "mark_price": 83868.7}]
    rows, tot = RUN.funding_evidence(trade, events)
    assert len(rows) == 1
    assert tot == pytest.approx(2.1604700383663875, rel=1e-12)


def test_funding_evidence_excludes_out_of_interval():
    trade = {"entry_time": 1_000_000, "exit_time": 2_000_000, "qty": 1.0, "direction": 1}
    events = [{"time_ms": 1_000_000, "rate": 0.001, "mark_price": 100.0},   # == entry ⇒ 排除
              {"time_ms": 2_000_000, "rate": 0.001, "mark_price": 100.0},   # == exit  ⇒ 计入
              {"time_ms": 3_000_000, "rate": 0.001, "mark_price": 100.0}]   # > exit   ⇒ 排除
    rows, tot = RUN.funding_evidence(trade, events)
    assert [r["funding_time_ms"] for r in rows] == [2_000_000]
    assert tot == pytest.approx(0.1)


# ══════════════════════════ P1-3：全窗覆盖判定 ══════════════════════════

Q = 900_000


def _rows(start, n, iv=Q, closed=1):
    return [{"open_time": start + i * iv, "is_closed": closed} for i in range(n)]


def test_coverage_empty_rejected():
    rep = RUN._closed_contiguous([], "15m", "t", expect_start_ms=0, expect_end_ms=4 * Q)
    assert rep["full_window_ok"] is False and rep["empty"] is True


def test_coverage_tail_truncated_rejected():
    """尾段被 limit 截断 ⇒ 相邻差仍连续，但全窗覆盖必须判不通过。"""
    rep = RUN._closed_contiguous(_rows(0, 3), "15m", "t",
                                 expect_start_ms=0, expect_end_ms=4 * Q)
    assert rep["contiguous"] is True          # 相邻差全对
    assert rep["tail_ok"] is False and rep["full_window_ok"] is False


def test_coverage_head_missing_rejected():
    rep = RUN._closed_contiguous(_rows(Q, 3), "15m", "t",
                                 expect_start_ms=0, expect_end_ms=4 * Q)
    assert rep["head_ok"] is False and rep["full_window_ok"] is False


def test_coverage_complete_ok():
    rep = RUN._closed_contiguous(_rows(0, 4), "15m", "t",
                                 expect_start_ms=0, expect_end_ms=4 * Q)
    assert rep["full_window_ok"] is True and rep["count_ok"] is True


def test_coverage_unclosed_bar_detected():
    rep = RUN._closed_contiguous(_rows(0, 2) + _rows(2 * Q, 1, closed=0), "15m", "t",
                                 expect_start_ms=0, expect_end_ms=3 * Q)
    assert rep["all_closed"] is False and rep["n_unclosed"] == 1


# ══════════════════════════ P1-3：资金费频率/覆盖 ══════════════════════════

def test_funding_frequency_unverified_without_events():
    f = RUN._funding_frequency([])
    assert f["frequency"] == "unverified" and f["interval_ms"] is None
    assert "不假设 8h" in f["note"]


def test_funding_frequency_observed_8h():
    ts = [0, 28_800_000, 57_600_000, 86_400_000]
    f = RUN._funding_frequency(ts)
    assert f["interval_ms"] == 28_800_000 and f["frequency"] == "8h"
    assert f["source"] == "observed_from_settled_events"


def test_expected_settlements_counts_grid_points():
    ts = [0, 28_800_000, 57_600_000, 86_400_000, 115_200_000]
    # 锚点 = 86.4M（恰落在窗口左端点，半开区间**含左端** ⇒ 必须计入）
    # [86.4M, 172.8M)：86.4M / 115.2M / 144M（172.8M 为开区间右端 ⇒ 不计）
    n = RUN._expected_settlements(ts, 28_800_000, 86_400_000, 172_800_000)
    assert n == 3, f"锚点在窗口起点必须计入，得 {n}"
    # [86.4M, 201.6M)：86.4M / 115.2M / 144M / 172.8M
    n2 = RUN._expected_settlements(ts, 28_800_000, 86_400_000, 201_600_000)
    assert n2 == 4


def test_expected_grid_excludes_anchor_strictly_before_window():
    ts = [0, 28_800_000, 57_600_000, 86_400_000]
    # 锚点 = 86.4M < win_start=100M ⇒ 锚点不计，网格从 115.2M 起
    grid = RUN._expected_settlement_grid(ts, 28_800_000, 100_000_000, 201_600_000)
    assert grid == [115_200_000, 144_000_000, 172_800_000], grid
    assert 86_400_000 not in grid


def test_expected_grid_none_when_interval_unverified():
    assert RUN._expected_settlements([], None, 0, 1) is None
    assert RUN._expected_settlement_grid([0], None, 0, 1) is None


def test_benchmark_last_point_equals_final_equity():
    """基准曲线末点必须等于 final_equity / net_return_pct（含出场滑点与费）。"""
    import pandas as pd
    n = 12
    ts = [i * 900_000 for i in range(n)]
    px = [100.0 + i for i in range(n)]
    df = pd.DataFrame({"timestamp": ts, "open": px, "high": px, "low": px, "close": px,
                       "volume": [1.0] * n})

    class _Ep:
        fee_rate = 0.0005
        slippage_bps = 5.0
    bench = RUN.buy_and_hold_benchmark(df, _Ep(), [], 5000.0)
    last = bench["equity_series"][-1]
    assert abs(last["equity"] - bench["final_equity"]) < 1e-9, (last["equity"], bench["final_equity"])
    assert abs(bench["series_last_equity"] - bench["final_equity"]) < 1e-9
    exp_ret = (bench["final_equity"] - 5000.0) / 5000.0 * 100.0
    assert abs(bench["net_return_pct"] - exp_ret) < 1e-9
    # 含出场滑点 ⇒ 末点净值 < 按收盘价盯市的「未出场」值
    assert bench["final_equity"] < bench["qty"] * px[-1] * (1.0 - _Ep.fee_rate) + 1e-9
    assert last.get("is_exit") is True


def test_open_positions_last_is_last_row_not_row_count():
    """`open_positions_last` 必须是**末行**持仓数，而非整段持仓过的行数。"""
    src = (Path(__file__).resolve().parents[2] / "框架" / "backend"
           / "backtest_runner.py").read_text(encoding="utf-8")
    assert 'open_positions_last": open_positions_last' in src
    assert 'int(equity[-1]["open_positions_count"])' in src
    assert "open_positions_last\": len(open_rem)" not in src


# ══════════════════════════ P1-1：快照读写 ══════════════════════════

_TMPROOT = Path(__file__).resolve().parent / "_tmp_store_test"


def _fresh_runs_root():
    import shutil
    if _TMPROOT.exists():
        shutil.rmtree(_TMPROOT)
    _TMPROOT.mkdir(parents=True, exist_ok=True)
    return _TMPROOT / "runs"


def test_snapshot_roundtrip_and_name_guard(monkeypatch):
    monkeypatch.setattr(S, "RUNS_ROOT", _fresh_runs_root())
    rid = "bt_20261016T000000Z_deadbeef"
    S.create_run(rid, {"run_id": rid, "status": S.STATUS_QUEUED, "spec": {}})
    blob = b"a,b\n1,2\n"
    p = S.write_snapshot(rid, "signal_bars.csv", blob)
    assert p.exists() and S.read_snapshot(rid, "signal_bars.csv") == blob
    with pytest.raises(S.IsolationError):
        S.write_snapshot(rid, "../escape.csv", b"x")


def test_chart_file_roundtrip(monkeypatch):
    monkeypatch.setattr(S, "RUNS_ROOT", _fresh_runs_root())
    rid = "bt_20261016T000000Z_deadbeef"
    S.create_run(rid, {"run_id": rid, "status": S.STATUS_QUEUED, "spec": {}})
    S.write_chart(rid, {"candles": [1, 2, 3]})
    assert S.read_chart(rid)["candles"] == [1, 2, 3]


def test_duplicate_run_id_rejected(monkeypatch):
    monkeypatch.setattr(S, "RUNS_ROOT", _fresh_runs_root())
    rid = "bt_20261016T000000Z_deadbeef"
    S.create_run(rid, {"run_id": rid, "status": S.STATUS_QUEUED, "spec": {}})
    with pytest.raises(S.RunExists):
        S.create_run(rid, {"run_id": rid, "status": S.STATUS_QUEUED, "spec": {}})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
