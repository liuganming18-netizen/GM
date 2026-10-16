# -*- coding: utf-8 -*-
"""WORK-02 · Shared Trading Core 契约验收套件（15 个指定用例）。

覆盖施工单第十七节「测试要求」逐条命名：

  Strategy   test_oi_quality_is_row_aligned      （W2-0 逐行质量，不得整列广播）
             test_oi_threshold_behavior          （W2-1 门槛三态语义）
             test_parameter_hash_changes         （W2-2 覆盖面）
             test_future_data_prefix_invariance  （W2-5 / E11 无未来函数）
             test_pit_timestamp                  （W2-5 PIT 可得性）
  Signal     test_signal_contract_complete       （W2-4 七问齐备）
             test_signal_available_at            （W2-4 四时点分离）
             test_signal_deterministic           （W2-8 确定性 signal_id）
  Execution  test_execution_policy_deterministic （W2-6A 纯决策确定性）
             test_next_open_execution            （下一根执行 bar 开盘成交）
             test_gap_behavior                   （相位 B 只用 open）
             test_fee_once                       （费用只计一次）
             test_slippage_once                  （滑点只计一次）
  Order      test_order_idempotency              （W2-8 确定性幂等）
             test_duplicate_order_rejected       （W2-7 重复单/非法状态机）

纯合成、离线：不读库、不联网、不下单、不写任何业务状态。

运行（从工程根）：
  PYTHONDONTWRITEBYTECODE=1 <py> -m pytest \
      策略/WyckoffMaster/test_work02_core_contract.py -q
"""
from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "策略") not in sys.path:
    sys.path.insert(0, str(ROOT / "策略"))

from WyckoffMaster.oi_resonance_v1 import config as C          # noqa: E402
from WyckoffMaster.oi_resonance_v1 import core as CORE         # noqa: E402
from WyckoffMaster.oi_resonance_v1 import execution as EXEC    # noqa: E402
from WyckoffMaster.oi_resonance_v1 import registry_entry as REG  # noqa: E402
from WyckoffMaster.trading_core import contracts as CT         # noqa: E402
from WyckoffMaster.trading_core import fill as FILL            # noqa: E402
from WyckoffMaster.trading_core import ledger as LED           # noqa: E402
from WyckoffMaster.trading_core import order as ORD            # noqa: E402
from WyckoffMaster.trading_core import pit as PIT              # noqa: E402
from WyckoffMaster.trading_core import policy as POL           # noqa: E402
from WyckoffMaster.trading_core import signal as SIG           # noqa: E402

TRADING_CORE = ROOT / "策略" / "WyckoffMaster" / "trading_core"

# ── 时间网格：与整点 / 15 分钟同时对齐（1699999200 / 900 = 1888888）──
T0_MS = 1_699_999_200_000
T0_SEC = T0_MS // 1000
H_MS = 3_600_000
M15_MS = 900_000
HOUR = 3_600
MIN15 = 900

BREAKOUT_IDX = 60          # 小时 bar 下标（0..60，第 60 根突破）
CONFIRM_MS = T0_MS + (BREAKOUT_IDX + 1) * H_MS
ENTRY_J15 = 4 * (BREAKOUT_IDX + 1)      # = 244
N15 = 400

# 突破场景下的确定值
#   atr[60] = 2×(13/14) + 4.0/14 = 2.142857…（突破根自身 TR = max(3.5, |108.5−104.5|, …) = 4.0）
#   raw_stop = 108.0 − 3×atr[60] = 101.571428…；step=0.01 向更低取整 ⇒ 101.57
SCEN_RAW_OPEN = 105.0
SCEN_RAW_EXIT = 105.0
SCEN_STOP_REF = 101.57


# ══════════════════════════ 合成夹具 ══════════════════════════

def osc_rows(n: int):
    """n 根震荡小时 bar：h=106 / l=104（⇒ 前 20 根最高 106、最低 104，ATR≈2）。"""
    rows = []
    for i in range(n):
        o, c = (104.5, 105.5) if i % 2 == 0 else (105.5, 104.5)
        rows.append({"open": o, "high": 106.0, "low": 104.0, "close": c,
                     "volume": 1000.0})
    return rows


def hourly_frame() -> pd.DataFrame:
    """61 根小时 bar：0..59 震荡、60 突破。无 open_interest 列（OI 不可用）。"""
    rows = osc_rows(BREAKOUT_IDX)
    rows.append({"open": 106.5, "high": 108.5, "low": 105.0, "close": 108.0,
                 "volume": 1000.0})
    df = pd.DataFrame(rows)
    df["timestamp"] = [T0_MS + i * H_MS for i in range(len(df))]
    return df


def long_frame(n: int = 600, oi_nan_until: int = 100, seed: int = 20261016) -> pd.DataFrame:
    """n 根小时 bar（随机游走价 + 随机游走 OI）。

    `oi_nan_until` 之前 `open_interest` 为 NaN ⇒ 用于构造三段不同数据质量。
    OI 用**随机游走**（非单调）⇒ `f_oi_level_rank` 会真正变化，分数可低于门槛。
    价格与 OI 用**两个独立生成器**，同 seed 下前 k 根**逐位一致**（不因总长变化而漂移）
    ⇒ 供前缀不变性比对。
    """
    rng_p = np.random.default_rng(seed)
    rng_o = np.random.default_rng(seed + 1)
    close = 100.0 + np.cumsum(rng_p.normal(0.0, 0.3, n))
    oi_path = 1000.0 + np.cumsum(rng_o.normal(0.0, 5.0, n))
    op = np.concatenate([[close[0]], close[:-1]])
    idx = np.arange(n)
    oi = np.where(idx >= oi_nan_until, oi_path, np.nan)
    return pd.DataFrame({
        "timestamp": [T0_MS + i * H_MS for i in range(n)],
        "open": op, "high": close + 0.8, "low": close - 0.8, "close": close,
        "volume": 1000.0 + (idx % 11) * 10.0,
        "open_interest": oi,
    })


def intrabar_frame(hourly_close, n_hours: int) -> pd.DataFrame:
    """把小时收盘铺成 4 根 15m bar（确定、前缀一致）。"""
    ts, op, hi, lo, cl = [], [], [], [], []
    for h in range(n_hours):
        base = float(hourly_close[h])
        for k in range(4):
            o = base * (1.0 + 0.0002 * k)
            c = base * (1.0 + 0.0002 * (k + 1))
            ts.append(T0_MS + (h * 4 + k) * M15_MS)
            op.append(o)
            cl.append(c)
            hi.append(max(o, c) * 1.0005)
            lo.append(min(o, c) * 0.9995)
    return pd.DataFrame({"timestamp": ts, "open": op, "high": hi,
                         "low": lo, "close": cl})


def scenario_market_data(p: C.SignalParams, n15: int = N15) -> dict:
    """突破场景：1 个标的、小时信号 bar × 61、15m 执行 bar × n15。"""
    bars = pd.DataFrame({
        "timestamp": [T0_MS + j * M15_MS for j in range(n15)],
        "open": SCEN_RAW_OPEN, "high": SCEN_RAW_OPEN + 0.2,
        "low": SCEN_RAW_OPEN - 0.2, "close": SCEN_RAW_EXIT,
    })
    return {"BTCUSDT": {"signal_bars": hourly_frame(), "intrabar_bars": bars,
                        "time_unit": "ms", "funding_events": []}}


SCEN_PARAMS = C.SignalParams(price_step=0.01)
EXEC_OPENS = [T0_MS + j * M15_MS for j in range(N15)]


# ══════════════════════════ 工具 ══════════════════════════

def _assert_prefix_equal(a: pd.DataFrame, b: pd.DataFrame, n: int, cols) -> None:
    for c in cols:
        va, vb = a[c].to_numpy()[:n], b[c].to_numpy()[:n]
        if va.dtype.kind == "f":
            assert np.array_equal(va, vb, equal_nan=True), f"列 {c} 前缀不一致"
        else:
            assert list(va) == list(vb), f"列 {c} 前缀不一致"


def _code_names(path: Path):
    """AST 级提取标识符/导入模块名（**不看注释与文档串**，避免自证式误报）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names, mods = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return names, mods


def _run_in_subprocess(setup: str, expr: str) -> str:
    """在**独立进程**里求值（证明确定性不依赖进程内状态 / 哈希随机化）。"""
    code = (f"import sys; sys.path.insert(0, {str(ROOT / '策略')!r}); "
            f"{setup}; print({expr})")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, f"子进程失败：{r.stderr}"
    return r.stdout.strip()


# ══════════════════════════ Strategy ══════════════════════════

def test_oi_quality_is_row_aligned():
    """W2-0：`oi_resonance_quality` 必须**逐行**判定，不得整列广播同一标量。

    夹具三段：`[0,100)` OI 缺失 / `[100,300)` OI 有值但窗口未成形 /
    `[300,600)` 窗口成形。缺陷版（`out[C_QUALITY] = scalar`）会把 600 行写成
    同一个标签；修复后四类标签共存且与**每一行自身**的可观测事实一致。
    """
    n = 600
    df = long_frame(n)
    sig = CORE.compute_signals(df, C.SignalParams(), "ms")

    q = sig[C.C_QUALITY] if hasattr(C, "C_QUALITY") else sig["oi_resonance_quality"]
    labels = sorted(set(q.astype(str)))
    assert len(labels) >= 3, f"质量标签不足 3 类 ⇒ 疑似整列广播：{labels}"
    assert "ok" in labels and "missing_open_interest" in labels

    oi_present = df["open_interest"].notna().to_numpy()
    avail = sig["oi_resonance_available"].to_numpy(bool)
    qv = q.to_numpy()

    # 逐行一致：OI 缺失行必须恰为 missing_open_interest
    assert set(qv[~oi_present]) == {"missing_open_interest"}
    # 逐行一致：因子可用行必须恰为 ok（且只在这些行）
    assert set(qv[avail]) == {"ok"}
    assert not avail[~oi_present].any()
    # 有 OI 但不可用 ⇒ 必为「历史不足」两类之一（不是 missing，也不是 ok）
    mid = oi_present & (~avail)
    assert set(qv[mid]) <= {"insufficient_history", "partial_insufficient_history"}
    # 三段计数：缺失段恰为前 100 行
    assert int((qv == "missing_open_interest").sum()) == 100

    # 元信息必须如实披露「逐行判定」
    meta = sig.attrs["oi_factor_meta"]
    assert sum(meta["quality"].values()) == n
    assert "逐行" in meta["note"]


def test_oi_threshold_behavior():
    """W2-1：`oi_min_resonance` 三态 —— UNSET 不参与 / 0 不生效 / 正数真拒单。"""
    df = long_frame(600, oi_nan_until=0)

    sig_unset = CORE.compute_signals(df, C.SignalParams(), "ms")
    g_unset = sig_unset.attrs["oi_gate"]
    assert g_unset["status"] == "UNSET"
    assert g_unset["enabled"] is False
    assert g_unset["oi_min_resonance"] is None
    assert "oi_resonance_below_threshold" not in set(
        sig_unset["reject_reason"].astype(str))

    sig_zero = CORE.compute_signals(df, C.SignalParams(oi_min_resonance=0.0), "ms")
    assert sig_zero.attrs["oi_gate"]["status"] == "SET_ZERO_NO_GATE"
    assert sig_zero.attrs["oi_gate"]["enabled"] is False
    assert "oi_resonance_below_threshold" not in set(
        sig_zero["reject_reason"].astype(str))

    th = 0.5
    p_gate = C.SignalParams(oi_min_resonance=th)
    sig_gate = CORE.compute_signals(df, p_gate, "ms")
    g = sig_gate.attrs["oi_gate"]
    assert g["status"] == "SET" and g["enabled"] is True
    assert g["oi_min_resonance"] == th

    score = sig_gate["oi_resonance_score"].to_numpy(np.float64)
    avail = sig_gate["oi_resonance_available"].to_numpy(bool)
    rej = sig_gate["reject_reason"].astype(str).to_numpy()
    atr = sig_gate["atr"].to_numpy(np.float64)
    dch = sig_gate["dc_high_prev"].to_numpy(np.float64)
    live = np.isfinite(atr) & (atr > 0) & np.isfinite(dch)

    below = live & avail & np.isfinite(score) & (score < th)
    assert below.any(), "夹具未能构造出低于门槛的行"
    assert set(rej[below]) == {"oi_resonance_below_threshold"}

    accepted = sig_gate["signal"].to_numpy(int) != CORE.SIG_NONE
    assert not accepted[below].any(), "低于门槛的行不得出信号"
    if accepted.any():
        assert (score[accepted] >= th).all(), "已放行信号的分数必须 >= 门槛"

    # 门槛只可能**减少**信号，不可能增加
    base_accepted = int((sig_unset["signal"].to_numpy(int) != CORE.SIG_NONE).sum())
    assert int(accepted.sum()) <= base_accepted

    # PIT 未声明 ⇒ 逐 bar 不改策略规则（与 UNSET 同口径），但**运行级**必须拒绝
    assert g["pit_declared"] is False
    gate = REG.formal_run_gate(p_gate)
    assert gate["formal_run_allowed"] is False
    assert gate["oi_pit_declared"] is False
    assert any("oi_publication_lag_ms" in r for r in gate["blocking_reasons"])
    # 声明后该阻塞原因消失（其余原因仍在）
    gate2 = REG.formal_run_gate(C.SignalParams(oi_min_resonance=th,
                                               oi_publication_lag_ms=0))
    assert not any("oi_publication_lag_ms" in r for r in gate2["blocking_reasons"])
    # 冻结配置当前为 UNSET ⇒ 第一条阻塞原因是 W2-1
    assert REG.formal_run_gate()["blocking_reasons"][0].startswith("OI 共振门槛")
    assert REG.formal_run_gate()["formal_run_allowed"] is False


def test_parameter_hash_changes():
    """W2-2：`param_hash` 必须覆盖一切能改变订单结果的参数；改任一 ⇒ 必变。"""
    base = C.param_hash()
    assert C.param_hash() == base, "同输入必须同哈希"

    signal_muts = {
        "donchian_entry_window": 21, "donchian_exit_window": 9, "atr_period": 15,
        "atr_mult": 3.5, "structural_buffer_atr": 0.3, "pullback_window": 9,
        "pullback_proximity_atr": 0.6, "sc_vol_mult": 2.5, "sc_range_atr_mult": 1.6,
        "sc_lower_wick_frac": 0.4, "enable_short": True, "oi_min_resonance": 0.5,
        "bars_per_day": 48, "signal_bar_seconds": 7200, "oi_chg_window": 21,
        "oi_rank_days": 91, "oi_rank_min_days": 8, "oi_price_div_window": 13,
        "oi_mom_flux_window": 49, "oi_vol_ma_window": 49, "oi_vol_ratio_clip": 4.0,
        "oi_publication_lag_ms": 0, "price_step": 0.01,
    }
    for name, val in signal_muts.items():
        assert C.param_hash(signal=C.SignalParams(**{name: val})) != base, \
            f"改 {name} 后 param_hash 未变 ⇒ 覆盖面不足"

    exec_muts = {
        "initial_equity": 50000.0, "risk_per_trade": 0.01, "max_portfolio_risk": 0.05,
        "max_portfolio_margin_pct": 0.5, "max_concurrent_trades": 5,
        "max_leverage_per_symbol": 3.0, "fee_rate": 0.0004, "maker_fee_rate": 0.0001,
        "taker_fee_rate": 0.0006, "slippage_bps": 5.0,
        "enable_trailing_exit": False, "force_close_at_end": False,
        "funding_model": "assumption_fixed_8h", "allow_new_entries": False,
    }
    for name, val in exec_muts.items():
        assert C.param_hash(exec_cfg=C.ExecParams(**{name: val})) != base, \
            f"改 {name} 后 param_hash 未变 ⇒ 覆盖面不足"

    for kw in ({"timeframe": "4h"}, {"exec_timeframe": "5m"},
               {"exchange": "okx"}, {"market": "spot"}, {"direction": "short"}):
        assert C.param_hash(**kw) != base, f"改 {kw} 后 param_hash 未变"

    # 覆盖面自述必须与实际一致（逐项可对号）
    basis = list(C.PARAM_HASH_BASIS)
    for token in ("strategy_key", "timeframe", "exec_timeframe", "signal_params.*",
                  "exec_params.*", "oi_weights"):
        assert token in basis, f"PARAM_HASH_BASIS 缺 {token}"
    assert C.param_hash() in (C.config_identity()["param_hash"],
                              C.config_identity()["param_hash"])
    assert REG.interface()["param_hash"] == base


def test_future_data_prefix_invariance():
    """W2-5 / E11：追加未来数据后，`signal(t)` 前缀必须逐位一致。"""
    n_a, n_b = 600, 900
    df_a, df_b = long_frame(n_a), long_frame(n_b)          # 前 600 行逐位相同
    # 夹具自证：若前缀本身就不同，下面的比对毫无鉴别力
    for c in ("timestamp", "open", "high", "low", "close", "volume", "open_interest"):
        _assert_prefix_equal(df_a, df_b, n_a, (c,))
    sig_a = CORE.compute_signals(df_a, C.SignalParams(), "ms")
    sig_b = CORE.compute_signals(df_b, C.SignalParams(), "ms")
    assert len(sig_a) == n_a and len(sig_b) == n_b

    causal_cols = ("signal", "signal_name", "direction", "stop_price",
                   "reject_reason", "atr", "dc_high_prev", "dc_low_prev",
                   "exit_low_prev", "exit_high_prev", "exit_low_curr",
                   "exit_high_curr", "oi_resonance_score", "f_oi_level_rank",
                   "f_oi_price_div_rank", "f_oi_mom_flux_rank",
                   "oi_resonance_quality", "confirm_time")
    _assert_prefix_equal(sig_a, sig_b, n_a, causal_cols)

    # 执行层同样必须前缀一致（期末强平关闭 ⇒ 纯因果比对）
    md_a = {"BTCUSDT": {"signal_bars": df_a,
                        "intrabar_bars": intrabar_frame(df_a["close"], n_a),
                        "time_unit": "ms"}}
    md_b = {"BTCUSDT": {"signal_bars": df_b,
                        "intrabar_bars": intrabar_frame(df_b["close"], n_b),
                        "time_unit": "ms"}}
    r_a = EXEC.run_portfolio_execution(md_a, C.SignalParams(), C.EXEC_FROZEN,
                                       time_unit="ms", trade_start_sec=T0_SEC,
                                       close_open_positions_at_end=False)
    r_b = EXEC.run_portfolio_execution(md_b, C.SignalParams(), C.EXEC_FROZEN,
                                       time_unit="ms", trade_start_sec=T0_SEC,
                                       close_open_positions_at_end=False)
    assert r_a["close_open_positions_at_end"] is False
    assert r_a["close_open_positions_at_end_source"] == "runtime_override"

    eq_a, eq_b = r_a["equity_curve"], r_b["equity_curve"]
    assert len(eq_a) == 4 * n_a
    for i in range(len(eq_a)):
        assert eq_a[i]["timestamp"] == eq_b[i]["timestamp"]
        for k in ("cash_equity", "unrealized_pnl", "total_equity", "open_risk_pct"):
            assert abs(eq_a[i][k] - eq_b[i][k]) <= 1e-9, f"equity[{i}].{k} 前缀漂移"
        assert eq_a[i]["open_positions_count"] == eq_b[i]["open_positions_count"]

    # 成交明细：A 的全部成交必须是 B 成交列表的前缀
    tr_a, tr_b = r_a["trades"], r_b["trades"]
    assert len(tr_b) >= len(tr_a)
    for i, t in enumerate(tr_a):
        assert t == tr_b[i], f"第 {i} 笔成交在追加未来数据后发生变化"


def test_pit_timestamp():
    """W2-5：PIT 必须按「实际可见时间」判定；无法证明 ⇒ 标记不可用（不乐观使用）。"""
    df = pd.DataFrame({"timestamp": [T0_MS + i * H_MS for i in range(5)]})

    # ① 未声明发布延迟 ⇒ 一律不可证明，原因码明确，可用时刻为 NaN（不猜）
    out = PIT.annotate_oi_pit(df.copy(), C.SignalParams())
    assert not out[PIT.C_PIT_PROVEN].any()
    assert set(out["oi_pit_reason"]) == {PIT.REASON_UNDECLARED}
    assert out[PIT.C_AVAILABLE_AT].isna().all()
    assert not out[PIT.C_DECISION_USABLE].any()

    # ② 声明 0ms ⇒ 观测时刻（退回 bar 收盘）恰等于收盘 ⇒ 可证明，可用时刻 = 收盘
    out0 = PIT.annotate_oi_pit(df.copy(), C.SignalParams(oi_publication_lag_ms=0))
    assert out0[PIT.C_PIT_PROVEN].all()
    assert np.array_equal(out0[PIT.C_AVAILABLE_AT].to_numpy(),
                          (df["timestamp"] + H_MS).to_numpy())
    assert out0[PIT.C_DECISION_USABLE].all()

    # ③ 声明 1 小时 ⇒ 收盘后才可见 ⇒ 逐行 fail-closed（数据问题，不是配置问题）
    out1 = PIT.annotate_oi_pit(df.copy(), C.SignalParams(oi_publication_lag_ms=H_MS))
    assert not out1[PIT.C_PIT_PROVEN].any()
    assert set(out1["oi_pit_reason"]) == {PIT.REASON_LATE_OBSERVATION}

    # ④ 显式观测时刻：早于收盘 1ms ⇒ 可证明；晚于收盘 1ms ⇒ 不可证明
    d_ok = df.copy()
    d_ok["oi_observed_at"] = d_ok["timestamp"] + H_MS - 1
    assert PIT.annotate_oi_pit(d_ok, C.SignalParams(oi_publication_lag_ms=0))[
        PIT.C_PIT_PROVEN].all()
    d_late = df.copy()
    d_late["oi_observed_at"] = d_late["timestamp"] + H_MS + 1
    assert not PIT.annotate_oi_pit(d_late, C.SignalParams(oi_publication_lag_ms=0))[
        PIT.C_PIT_PROVEN].any()

    # ⑤ 缺观测时刻列且已声明 ⇒ 退回「最乐观假设 = 收盘」仍需延迟为 0 才放行
    assert not PIT.annotate_oi_pit(
        df.copy(), C.SignalParams(oi_publication_lag_ms=1))[PIT.C_PIT_PROVEN].any()

    # ⑥ decision_usable = pit_proven ∧ available（数值算得出）
    d2 = df.copy()
    d2["oi_resonance_available"] = [True, True, False, True, True]
    o2 = PIT.annotate_oi_pit(d2, C.SignalParams(oi_publication_lag_ms=0))
    assert list(o2[PIT.C_DECISION_USABLE]) == [True, True, False, True, True]

    # ⑦ 摘要如实计数；信号层把 PIT 结论透出到 attrs
    s = PIT.pit_summary(o2)
    assert s["pit_proven"] == 5 and s["decision_usable"] == 4
    sig = CORE.compute_signals(long_frame(80, oi_nan_until=0), C.SignalParams(), "ms")
    assert sig.attrs["oi_pit"]["pit_proven"] == 0
    assert sig.attrs["oi_gate"]["pit_declared"] is False
    sig2 = CORE.compute_signals(long_frame(80, oi_nan_until=0),
                               C.SignalParams(oi_publication_lag_ms=0), "ms")
    assert sig2.attrs["oi_pit"]["pit_proven"] == 80
    assert sig2.attrs["oi_gate"]["pit_declared"] is True


# ══════════════════════════ Signal ══════════════════════════

def _scenario_events():
    sig = CORE.compute_signals(hourly_frame(), SCEN_PARAMS, "ms")
    evs = SIG.build_signal_events(
        sig, symbol="BTCUSDT", timeframe="1h", strategy_key=C.STRATEGY_KEY,
        strategy_version=C.SIGNAL_VERSION, signal_bar_seconds=HOUR,
        source_id="binance:um:BTCUSDT", exec_bar_opens_ms=EXEC_OPENS,
        entry_policy=EXEC.build_policy(SCEN_PARAMS).identity(),
        exit_policy={"close_open_positions_at_end": True,
                     "exit_order_type": "STOP_MARKET",
                     "trailing": "exit_low_curr/exit_high_curr → 下一根执行 bar"},
        include_rejected=True)
    return sig, evs


def test_signal_contract_complete():
    """W2-4：Signal 必须能回答七问（何时产生/为什么/何时可知/方向/通过与否/因子/质量）。"""
    sig, evs = _scenario_events()
    assert int(sig["signal"].iloc[BREAKOUT_IDX]) == CORE.SIG_DONCHIAN_BREAKOUT

    acc = [e for e in evs if e.signal_type != "none"]
    assert len(acc) == 1
    e = acc[0]

    # ① 什么时候产生 / 什么时候可以被知道
    assert e.signal_time == T0_MS + BREAKOUT_IDX * H_MS
    assert e.confirm_time == CONFIRM_MS
    assert e.available_at == CONFIRM_MS
    assert e.trade_time == T0_MS + ENTRY_J15 * M15_MS
    assert e.time_order_ok()

    # ② 为什么产生 / 产生了什么方向
    assert e.signal_type == "donchian_breakout"
    assert e.direction == 1

    # ③ 为什么通过（空串 = 通过）
    assert e.reject_reason == ""

    # ④ 使用了哪些因子（四组必须齐备）
    assert set(e.factors) >= {"wyckoff", "oi", "oi_divergence", "oi_flux"}
    assert set(e.factors["wyckoff"]) >= {"signal_name", "dc_high_prev", "atr"}
    assert set(e.factors["oi"]) >= {"score", "available", "pit_proven", "quality"}
    assert e.factors["oi"]["available"] is False
    assert e.factors["oi"]["pit_proven"] is False       # 未声明发布延迟 ⇒ 不乐观

    # ⑤ 当时数据是否完整
    assert e.quality == "missing_open_interest"
    assert isinstance(e.data_quality, dict)
    assert e.data_quality.get("time_unit") == "ms"

    # ⑥ 条件 / 策略身份 / 出处
    assert e.conditions["stop_price"] == pytest.approx(SCEN_STOP_REF)
    assert e.conditions["stop_mode"] == "uniform"
    assert e.strategy_key == C.STRATEGY_KEY and e.strategy_version == C.SIGNAL_VERSION
    assert e.source_id == "binance:um:BTCUSDT"
    assert e.entry_policy and e.exit_policy

    # ⑦ 序列化字段齐备
    d = e.to_dict()
    for k in ("signal_id", "strategy_key", "strategy_version", "symbol", "timeframe",
              "signal_type", "direction", "signal_time", "confirm_time",
              "available_at", "trade_time", "source_id", "conditions", "factors",
              "score", "confidence", "quality", "entry_policy", "exit_policy",
              "data_quality", "reject_reason", "time_order_ok"):
        assert k in d, f"SignalEvent.to_dict 缺字段 {k}"
    assert d["time_order_ok"] is True

    # 拒绝事件：include_rejected=True 必须能回答「为什么拒绝」
    sig_g = CORE.compute_signals(long_frame(600, oi_nan_until=0),
                                 C.SignalParams(oi_min_resonance=0.5), "ms")
    evs_g = SIG.build_signal_events(
        sig_g, symbol="BTCUSDT", timeframe="1h", strategy_key=C.STRATEGY_KEY,
        strategy_version=C.SIGNAL_VERSION, signal_bar_seconds=HOUR,
        include_rejected=True)
    rej = [e for e in evs_g if e.signal_type == "none"]
    assert len(rej) > 0
    assert all(e.reject_reason for e in rej)
    assert all(e.time_order_ok() for e in rej)
    assert all(e.trade_time is None for e in rej)       # 无执行周期 ⇒ 不猜


def test_signal_available_at():
    """W2-4：四时点分离；`trade_time` 必须与执行引擎同一规则（不各说各话）。"""
    assert SIG.resolve_trade_time(None, EXEC_OPENS) is None
    assert SIG.resolve_trade_time(CONFIRM_MS, None) is None
    assert SIG.resolve_trade_time(CONFIRM_MS, []) is None

    opens = [1000, 2000, 3000]
    assert SIG.resolve_trade_time(999, opens) == 1000
    assert SIG.resolve_trade_time(1000, opens) == 1000
    assert SIG.resolve_trade_time(1500, opens) == 2000
    assert SIG.resolve_trade_time(2000, opens) == 2000
    assert SIG.resolve_trade_time(2001, opens) == 3000
    assert SIG.resolve_trade_time(3001, opens) is None      # 全早于 confirm ⇒ 不猜
    assert SIG.resolve_trade_time(3000, opens) == 3000

    arr = np.asarray(opens, dtype=np.int64)
    for c in (998, 999, 1000, 1500, 1999, 2000, 2001, 2999, 3000, 3001, 9999):
        j = int(np.searchsorted(arr, c, side="left"))
        expect = int(arr[j]) if j < len(arr) else None
        assert SIG.resolve_trade_time(c, opens) == expect, f"c={c} 与引擎规则不一致"

    _, evs = _scenario_events()
    for e in evs:
        assert e.available_at >= e.confirm_time
        assert e.signal_time <= e.confirm_time
        if e.trade_time is not None:
            assert e.available_at <= e.trade_time
    acc = [e for e in evs if e.signal_type != "none"]
    assert acc[0].available_at == acc[0].confirm_time       # OHLCV 驱动：收盘即可见


def test_signal_deterministic():
    """W2-8：signal_id / content_hash 必须确定性（禁 uuid4 / random / 时间+随机）。"""
    sig, evs = _scenario_events()
    evs2 = SIG.build_signal_events(
        sig, symbol="BTCUSDT", timeframe="1h", strategy_key=C.STRATEGY_KEY,
        strategy_version=C.SIGNAL_VERSION, signal_bar_seconds=HOUR,
        source_id="binance:um:BTCUSDT", exec_bar_opens_ms=EXEC_OPENS,
        include_rejected=True)
    assert [e.signal_id for e in evs] == [e.signal_id for e in evs2]
    assert [e.content_hash() for e in evs] == [e.content_hash() for e in evs2]

    # signal_id 必须可由输入直接复算（不含随机/时间成分）
    for e in evs:
        expect = SIG.make_signal_id(
            C.STRATEGY_KEY, "BTCUSDT", "1h", int(e.signal_time),
            e.signal_type if e.signal_type != "none" else f"rejected:{e.reject_reason}")
        assert e.signal_id == expect

    a = SIG.make_signal_id("k", "s", "1h", 100, "donchian_breakout")
    assert a == SIG.make_signal_id("k", "s", "1h", 100, "donchian_breakout")
    assert a != SIG.make_signal_id("k", "s", "1h", 101, "donchian_breakout")
    assert a != SIG.make_signal_id("k", "s", "4h", 100, "donchian_breakout")
    # 跨进程一致（不依赖进程内状态 / 哈希随机化）
    assert _run_in_subprocess(
        "from WyckoffMaster.trading_core.signal import make_signal_id",
        "make_signal_id('k','s','1h',100,'donchian_breakout')") == a

    # 契约产物里不得出现 uuid / random（AST 级，不看注释）
    for f in sorted(TRADING_CORE.glob("*.py")):
        names, mods = _code_names(f)
        assert "uuid" not in mods, f"{f.name} 引入了 uuid"
        assert "random" not in mods, f"{f.name} 引入了 random"
        assert not ({"uuid1", "uuid4", "uuid5"} & names), f"{f.name} 使用了 uuid 生成器"


# ══════════════════════════ Execution ══════════════════════════

def test_execution_policy_deterministic():
    """W2-6A：ExecutionPolicy 是纯决策 —— 同输入同输出，且身份稳定。"""
    p1, p2 = EXEC.build_policy(SCEN_PARAMS), EXEC.build_policy(SCEN_PARAMS)
    assert p1.identity() == p2.identity()
    assert p1.entry_order_type() == CT.OrderType.MARKET
    assert p1.exit_order_type() == CT.OrderType.STOP_MARKET

    adapter = EXEC.build_fill_adapter()
    assert adapter.kind == FILL.SIMULATED_BAR
    quote = adapter.quote_entry(raw_price=SCEN_RAW_OPEN, direction=1,
                                price_step=SCEN_PARAMS.price_step,
                                stop_price=SCEN_STOP_REF)
    kw = dict(symbol="BTCUSDT", signal_type=CORE.SIG_DONCHIAN_BREAKOUT, direction=1,
              raw_price=SCEN_RAW_OPEN, stop_ref=SCEN_STOP_REF, quote=quote,
              positions={}, cash=100000.0, equity_mtm=100000.0,
              signal_id="sig-x", order_sequence=0)
    d1, d2 = p1.decide_entry(**kw), p1.decide_entry(**kw)
    assert d1.accepted and d2.accepted
    assert d1.intent.order_id() == d2.intent.order_id()
    assert d1.intent.client_order_id() == d2.intent.client_order_id()
    assert d1.intent.idempotency_key() == d2.intent.idempotency_key()
    assert d1.intent.quantity == d2.intent.quantity
    assert d1.intent.phase == CT.ExecPhase.C_ENTRY.value
    assert d1.intent.action == POL.ACTION_OPEN
    assert d1.risk.quantity == d2.risk.quantity

    # 纯决策：本类不产生成交价，成交价一律来自 FillAdapter
    assert d1.intent.extra["entry_fill"] == quote.entry_fill
    assert d1.intent.extra["stop_fill"] == quote.stop_fill

    # 锁利通道：多头只上抬、空头只下压，否则不给新价
    assert p1.trail_level(direction=1, exit_low=104.0, exit_high=110.0,
                          current_stop=102.0) == 104.0
    assert p1.trail_level(direction=1, exit_low=101.0, exit_high=110.0,
                          current_stop=102.0) is None
    assert p1.trail_level(direction=-1, exit_high=99.0, exit_low=95.0,
                          current_stop=100.0) == 99.0
    assert p1.trail_level(direction=-1, exit_high=101.0, exit_low=95.0,
                          current_stop=100.0) is None

    # 期末强平开关：冻结配置 vs Runtime 显式覆盖，来源必须标注
    assert p1.close_open_positions_at_end(None) is True
    assert p1.close_at_end_source(None) == "frozen_config"
    assert p1.close_open_positions_at_end(False) is False
    assert p1.close_at_end_source(False) == "runtime_override"
    assert p1.close_open_positions_at_end(True) is True
    assert p1.close_at_end_source(True) == "runtime_override"

    # 两端的成交适配器必须不同（结构性分离）
    ex = FILL.ExchangeFillAdapter()
    assert ex.kind == FILL.EXCHANGE_ACK
    with pytest.raises(CT.LiveExecutionForbidden):
        ex.quote_entry(raw_price=100.0, direction=1, price_step=0.0, stop_price=99.0)
    with pytest.raises(CT.LiveExecutionForbidden):
        ex.quote_exit(target_px=100.0, direction=1, price_step=0.0, reason="x")
    with pytest.raises(CT.LiveExecutionForbidden):
        ex.submit(None, ack=None)


def test_next_open_execution():
    """下一根执行 bar **开盘**成交（滑点 + 取整最劣方向），时点必须与确认时刻对齐。"""
    md = scenario_market_data(SCEN_PARAMS)
    res = EXEC.run_portfolio_execution(md, SCEN_PARAMS, C.EXEC_FROZEN, time_unit="ms")
    assert res["trades_count"] == 1
    t = res["trades"][0]
    slip = C.EXEC_FROZEN.slippage_bps * 1e-4

    # 夹具自证：信号层的保护价必须等于本测试推导的常数（漂移即报）
    sig0 = CORE.compute_signals(hourly_frame(), SCEN_PARAMS, "ms")
    assert float(sig0["stop_price"].iloc[BREAKOUT_IDX]) == pytest.approx(SCEN_STOP_REF)
    assert float(sig0["atr"].iloc[BREAKOUT_IDX]) == pytest.approx(2.142857142857143)

    # 成交时刻 = 确认时刻之后**第一根** 15m bar 的开盘
    assert t["entry_time"] == T0_MS + ENTRY_J15 * M15_MS
    assert t["entry_time"] >= CONFIRM_MS
    assert t["entry_time"] - M15_MS < CONFIRM_MS
    assert t["entry_time"] % M15_MS == 0

    # 成交价 = 该 bar 开盘 × (1 + 滑点) 向**更贵**取整（与 FillAdapter 逐位一致）
    expect = FILL.round_buy_price(SCEN_RAW_OPEN * (1.0 + slip), SCEN_PARAMS.price_step)
    assert t["entry_price"] == pytest.approx(expect)
    assert t["entry_price"] > SCEN_RAW_OPEN          # 买入向更贵取整

    # 出场（期末强平）= 末根 15m 收盘向**更便宜**取整
    assert t["exit_reason"] == "force_close_eod"
    assert t["exit_price"] == pytest.approx(
        FILL.round_sell_price(SCEN_RAW_EXIT * (1.0 - slip), SCEN_PARAMS.price_step))
    assert t["exit_price"] < SCEN_RAW_EXIT

    # 数量不取整（与既有引擎逐位一致）
    assert t["qty"] > 0
    assert t["entry_time"] < t["exit_time"]
    # 决策记录相位为 C（开盘新开仓）
    assert res["decisions"][0]["phase"] == "C_entry"
    assert res["decisions"][0]["action"] == "entry"
    # 账本/订单契约产物
    tc = res["trading_core"]
    assert tc["orders_count"] == 2 and tc["registry_unique_keys"] == 2
    assert tc["fill_adapter"]["kind"] == FILL.SIMULATED_BAR
    assert tc["ledger_event_counts"][LED.ORDER_FILLED] == 2
    assert tc["ledger_event_counts"][LED.POSITION_OPENED] == 1
    assert tc["ledger_event_counts"][LED.POSITION_CLOSED] == 1
    assert tc["ledger_event_counts"][LED.SIGNAL_ACCEPTED] == 1


def test_gap_behavior():
    """相位 B 跳空退出：**只用 open**，不得用 bar 内 low/high 提前释放资金。"""
    pol = EXEC.build_policy(SCEN_PARAMS)
    pos = EXEC.OpenPosition(
        symbol="BTCUSDT", direction=1, signal_type=CORE.SIG_DONCHIAN_BREAKOUT,
        entry_time=T0_MS, entry_time_sec=T0_SEC, entry_price=105.0, qty=1.0,
        initial_stop=100.0, current_stop=100.0, entry_fee=0.0, last_mark_price=105.0)

    d = pol.decide_gap_exit(pos=pos, bar_open=99.0, symbol="BTCUSDT")
    assert d.should_exit and d.target_px == 99.0
    assert d.reason == POL.EXIT_GAP and d.phase == CT.ExecPhase.B_GAP.value
    assert d.order_type == CT.OrderType.STOP_MARKET
    # 成交价由 FillAdapter 决定（Policy 不给价）
    assert d.intent is None

    assert not pol.decide_gap_exit(pos=pos, bar_open=100.5, symbol="BTCUSDT").should_exit
    assert pol.decide_gap_exit(pos=pos, bar_open=100.0, symbol="BTCUSDT").should_exit

    short = EXEC.OpenPosition(
        symbol="BTCUSDT", direction=-1, signal_type=CORE.SIG_DONCHIAN_BREAKOUT,
        entry_time=T0_MS, entry_time_sec=T0_SEC, entry_price=95.0, qty=1.0,
        initial_stop=100.0, current_stop=100.0, entry_fee=0.0, last_mark_price=95.0)
    assert pol.decide_gap_exit(pos=short, bar_open=100.5, symbol="BTCUSDT").should_exit
    assert not pol.decide_gap_exit(pos=short, bar_open=99.5, symbol="BTCUSDT").should_exit

    # 结构性证据：跳空判定签名里**没有** high/low（想用也用不到）
    params = set(inspect.signature(POL.ExecutionPolicy.decide_gap_exit).parameters)
    assert params - {"self"} == {"pos", "bar_open", "symbol"}

    # bar 内止损是**另一相位**（D），且目标价 = 保护价，不是 open
    d2 = pol.decide_intrabar_exit(pos=pos, high=106.0, low=99.5, symbol="BTCUSDT")
    assert d2.should_exit and d2.reason == POL.EXIT_INTRABAR
    assert d2.phase == CT.ExecPhase.D_INTRABAR.value
    assert d2.target_px == pos.current_stop
    assert not pol.decide_intrabar_exit(pos=pos, high=106.0, low=100.5,
                                        symbol="BTCUSDT").should_exit

    # 端到端：构造一根开盘即跌破保护价的 bar ⇒ 必须走 gap 而不是 intrabar
    n15 = 300
    bars = pd.DataFrame({
        "timestamp": [T0_MS + j * M15_MS for j in range(n15)],
        "open": SCEN_RAW_OPEN, "high": SCEN_RAW_OPEN + 0.2,
        "low": SCEN_RAW_OPEN - 0.2, "close": SCEN_RAW_EXIT})
    j_entry = ENTRY_J15
    bars.loc[j_entry + 1, ["open", "high", "low", "close"]] = [90.0, 90.5, 89.5, 90.0]
    md = {"BTCUSDT": {"signal_bars": hourly_frame(), "intrabar_bars": bars,
                      "time_unit": "ms"}}
    res = EXEC.run_portfolio_execution(md, SCEN_PARAMS, C.EXEC_FROZEN, time_unit="ms")
    assert res["trades_count"] == 1
    t = res["trades"][0]
    assert t["exit_reason"] == POL.EXIT_GAP
    assert t["exit_time"] == T0_MS + (j_entry + 1) * M15_MS
    # 跳空出场价 = 该 bar 的 open 滑点后向更便宜取整（不是保护价 104.0）
    slip = C.EXEC_FROZEN.slippage_bps * 1e-4
    assert t["exit_price"] == pytest.approx(
        FILL.round_sell_price(90.0 * (1.0 - slip), SCEN_PARAMS.price_step))
    assert t["exit_price"] < 90.0


def test_fee_once():
    """费用只计一次：开仓费在现金中扣一次、平仓费一次，且账本一成交一事件。"""
    md = scenario_market_data(SCEN_PARAMS)
    res = EXEC.run_portfolio_execution(md, SCEN_PARAMS, C.EXEC_FROZEN, time_unit="ms")
    t = res["trades"][0]
    taker = C.EXEC_FROZEN.taker_fee_rate

    assert t["entry_fee"] == pytest.approx(t["entry_price"] * t["qty"] * taker)
    assert t["exit_fee"] == pytest.approx(t["exit_price"] * t["qty"] * taker)
    assert t["fees"] == pytest.approx(t["entry_fee"] + t["exit_fee"])
    # 净盈亏口径：入场费**只扣一次**（若重复扣，下面恒等式立刻不成立）
    assert t["net_pnl"] == pytest.approx(
        t["gross_pnl"] - t["entry_fee"] - t["exit_fee"] - t["funding_fee"])

    # 账本：FEE_CHARGED 恰等于成交笔数（一开一平 = 2），不重复计
    counts = res["trading_core"]["ledger_event_counts"]
    assert counts[LED.FEE_CHARGED] == 2
    assert counts[LED.ORDER_FILLED] == 2
    assert counts[LED.FEE_CHARGED] == counts[LED.ORDER_FILLED]

    # 现金恒等式与权益恒等式必须成立（费用口径自洽的硬判据）
    assert res["reconcile"]["cash_identity_ok"] is True
    assert res["reconcile"]["equity_identity_ok"] is True
    assert res["reconcile"]["equity_curve_tail_matches_final"] is True
    assert res["reconcile"]["sum_fees"] == pytest.approx(
        res["reconcile"]["sum_entry_fee"] + res["reconcile"]["sum_exit_fee"])
    # 期末无持仓 ⇒ 未实现净额归零
    assert res["reconcile"]["unrealized_net_open"] == pytest.approx(0.0)
    assert res["open_positions_remaining"] == []


def test_slippage_once():
    """滑点只计一次（单边），且方向最劣；滑点损失与成交价差逐位对账。"""
    md = scenario_market_data(SCEN_PARAMS)
    res = EXEC.run_portfolio_execution(md, SCEN_PARAMS, C.EXEC_FROZEN, time_unit="ms")
    t = res["trades"][0]
    slip = C.EXEC_FROZEN.slippage_bps * 1e-4
    step = SCEN_PARAMS.price_step

    once_in = FILL.round_buy_price(SCEN_RAW_OPEN * (1.0 + slip), step)
    twice_in = FILL.round_buy_price(SCEN_RAW_OPEN * (1.0 + slip) ** 2, step)
    once_out = FILL.round_sell_price(SCEN_RAW_EXIT * (1.0 - slip), step)
    twice_out = FILL.round_sell_price(SCEN_RAW_EXIT * (1.0 - slip) ** 2, step)

    assert t["entry_price"] == pytest.approx(once_in)
    assert t["exit_price"] == pytest.approx(once_out)
    # 若滑点被计两次，价必不同 ⇒ 下面两条是「只计一次」的硬判据
    assert once_in != pytest.approx(twice_in)
    assert once_out != pytest.approx(twice_out)
    assert t["entry_price"] != pytest.approx(twice_in)
    assert t["exit_price"] != pytest.approx(twice_out)

    # 滑点损失 = 入场差 + 出场差（各一次），与成交价逐位对账
    expect_slip = (abs(t["entry_price"] - SCEN_RAW_OPEN) * t["qty"]
                   + abs(t["exit_price"] - SCEN_RAW_EXIT) * t["qty"])
    assert t["slippage_loss"] == pytest.approx(expect_slip)
    assert res["reconcile"]["sum_slippage_loss"] == pytest.approx(expect_slip)

    # 费率模型披露：maker/taker 分列，实际按 taker
    assert res["reconcile"]["fee_model"]["taker_fee_rate"] == C.EXEC_FROZEN.taker_fee_rate
    assert res["reconcile"]["fee_model"]["maker_fee_rate"] == C.EXEC_FROZEN.maker_fee_rate
    adapter = EXEC.build_fill_adapter()
    assert adapter.fee_rate(liquidity=CT.Liquidity.TAKER) == C.EXEC_FROZEN.taker_fee_rate
    assert adapter.fee_rate(liquidity=CT.Liquidity.MAKER) == C.EXEC_FROZEN.maker_fee_rate


# ══════════════════════════ Order ══════════════════════════

def _intent(seq: int = 0, qty: float = 1.0, action: str = POL.ACTION_OPEN):
    return ORD.OrderIntent(
        strategy_key=C.STRATEGY_KEY, symbol="BTCUSDT", side=CT.Side.BUY,
        position_side=CT.PositionSide.LONG, order_type=CT.OrderType.MARKET,
        quantity=qty, action=action, phase=CT.ExecPhase.C_ENTRY.value,
        signal_id="sig-1", order_sequence=seq)


def test_order_idempotency():
    """W2-8：同输入重复提交 ⇒ 识别为**同一**订单（duplicate），不得新建第二个。"""
    intent = _intent()
    reg = ORD.OrderRegistry()
    r1 = reg.submit(intent, created_at=111)
    r2 = reg.submit(intent, created_at=222)
    assert r1.is_duplicate is False
    assert r2.is_duplicate is True
    assert r1.order is r2.order
    assert len(reg) == 1
    assert r2.order.created_at == 111            # 第二次不得覆盖首次
    assert r1.order.status == CT.OrderStatus.PENDING
    assert r1.order.is_terminal() is False
    assert reg.peek(intent) is r1.order

    # 不同序号 ⇒ 不同订单（同一信号可以有多笔合法发单）
    r3 = reg.submit(_intent(seq=1))
    assert r3.is_duplicate is False and len(reg) == 2

    # 确定性 ID：同输入同 ID；跨进程一致
    assert ORD.make_order_id("k", "s", "sig", "open", 0) == \
        ORD.make_order_id("k", "s", "sig", "open", 0)
    assert ORD.make_order_id("k", "s", "sig", "open", 0) != \
        ORD.make_order_id("k", "s", "sig", "open", 1)
    assert intent.order_id() == ORD.make_order_id(
        C.STRATEGY_KEY, "BTCUSDT", "sig-1", POL.ACTION_OPEN, 0)
    assert _run_in_subprocess(
        "from WyckoffMaster.trading_core.order import make_order_id",
        "make_order_id('k','s','sig','open',0)") == \
        ORD.make_order_id("k", "s", "sig", "open", 0)

    # 账本 event_id 同样确定性且幂等
    ev1 = LED.LedgerEvent.build(LED.FEE_CHARGED, strategy_key=C.STRATEGY_KEY,
                                symbol="BTCUSDT", event_time=1, payload={"fee": 1.0})
    ev2 = LED.LedgerEvent.build(LED.FEE_CHARGED, strategy_key=C.STRATEGY_KEY,
                                symbol="BTCUSDT", event_time=1, payload={"fee": 1.0})
    assert ev1.event_id == ev2.event_id
    book = LED.InMemoryLedger()
    assert book.emit(ev1) is True and book.emit(ev2) is False
    assert len(book) == 1
    assert book.counts() == {LED.FEE_CHARGED: 1}
    assert set(LED.LEDGER_EVENT_TYPES) == {
        LED.GENESIS_INIT, LED.SIGNAL_ACCEPTED, LED.SIGNAL_REJECTED,
        LED.ORDER_CREATED, LED.ORDER_SUBMITTED, LED.ORDER_FILLED,
        LED.ORDER_CANCELED, LED.FUNDING_FEE, LED.POSITION_OPENED,
        LED.POSITION_REDUCED, LED.POSITION_CLOSED, LED.FEE_CHARGED}


def test_duplicate_order_rejected():
    """W2-7：重复意图不得重开单；订单状态机非法跳转必须硬拒绝。"""
    reg = ORD.OrderRegistry()
    first = _intent(qty=1.0)
    dup = replace(first, quantity=99.0)          # 同 (策略,标的,信号,动作,序号)
    assert dup.idempotency_key() == first.idempotency_key()
    assert dup.order_id() == first.order_id()
    r1 = reg.submit(first)
    r2 = reg.submit(dup)
    assert r1.is_duplicate is False and r2.is_duplicate is True
    assert r2.order is r1.order
    assert r2.order.quantity == 1.0              # 未按第二份意图重开/改量
    assert len(reg) == 1
    assert reg.get(first.idempotency_key()).quantity == 1.0

    # 状态机：合法路径
    o = r1.order
    o.transition(CT.OrderStatus.SUBMITTED, at=10)
    assert o.status == CT.OrderStatus.SUBMITTED and o.submitted_at == 10
    o.transition(CT.OrderStatus.FILLED, at=11)
    assert o.filled_at == 11 and o.is_terminal()
    o.transition(CT.OrderStatus.CLOSED)
    assert o.status == CT.OrderStatus.CLOSED

    # 非法跳转：终态不得回退；跳级不得越权
    with pytest.raises(CT.TradingCoreError):
        o.transition(CT.OrderStatus.SUBMITTED)
    o2 = ORD.order_from_intent(_intent(seq=7))
    with pytest.raises(CT.TradingCoreError):
        o2.transition(CT.OrderStatus.FILLED)      # PENDING 不得直接 FILLED
    o2.transition(CT.OrderStatus.CANCELED)
    assert o2.is_terminal()
    with pytest.raises(CT.TradingCoreError):
        o2.transition(CT.OrderStatus.SUBMITTED)
    assert ORD.can_transition(CT.OrderStatus.PENDING, CT.OrderStatus.SUBMITTED) is True
    assert ORD.can_transition(CT.OrderStatus.CANCELED, CT.OrderStatus.SUBMITTED) is False
    assert set(CT.OrderStatus) == {
        CT.OrderStatus.PENDING, CT.OrderStatus.SUBMITTED,
        CT.OrderStatus.PARTIALLY_FILLED, CT.OrderStatus.FILLED,
        CT.OrderStatus.CANCELED, CT.OrderStatus.REJECTED, CT.OrderStatus.CLOSED}
    # 部分成交必须回到 SUBMITTED / 再成交 / 撤销之一，不得凭空终结
    assert ORD.can_transition(CT.OrderStatus.SUBMITTED,
                              CT.OrderStatus.PARTIALLY_FILLED) is True
    assert ORD.can_transition(CT.OrderStatus.PARTIALLY_FILLED,
                              CT.OrderStatus.CLOSED) is False


if __name__ == "__main__":                       # 允许直接 python 运行
    sys.exit(pytest.main([__file__, "-q"]))
