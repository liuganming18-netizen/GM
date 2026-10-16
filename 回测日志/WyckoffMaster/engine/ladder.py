# -*- coding: utf-8 -*-
"""v3.1 阶梯编排：M0→M1→M2→(条件)M2b→M3_BE/M3_Step→M4（**跨段晋级驱动**）。

三件事：
  1. **配置继承**：每层只改一个维度，其余维度**逐字继承父层胜出配置**（不偷换父策略）。
     父层由 `config/ladder.json` 的 `promotion_rules` **跨 IS+Val 晋级判定**决定，不硬编码：
       · M2  父 = `M1 晋级 ? M1 : M0`（`parent_rule`）
       · M3  父 = `M2/M2b 胜出者`（`M2b.selection`）
       · M4  父 = `M2/M3 胜出者`（`must_rerun_kline_winner`）
  2. **消费证明**：记录每层真实传给核心的 entry_mode/stop_mode/trail_mode/rank_mode，
     以及各买点类型计数；证明"实验差异真实生效"而不是只补 levels 字段。
  3. **条件 M2b**：按 `core.m2_over_limit_stats` / `m2b_trigger` 判 IS+Val 合并超限拒单率 >30%。
M5/M6 不进本模块（前向验证）。

晋级判定的 IS/Val 合成口径（**声明式实现，可审计**）：
  · `trades`        = Σ(IS, Val)（规则明示「IS+Val 交易数」）
  · `max_drawdown`  = max(IS, Val)
  · `win_months`    = 两段月桶合并后，有差异月份中获胜月 > 50%
  · `improvement`   = 池化日收益（两段日收益按时间拼接，日收益无量纲）的年化 Calmar 相对提升
                      ≥15% 或 Sharpe 绝对提升 ≥0.15
  · `val_positive`  = Val 的 Calmar 与 Sharpe 均为正且优于父配置
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from . import paths as P
from . import loader as LD
from .engine import Backtest, LevelSpec

# 层级顺序（父 → 子）与"只改的单一维度"
LADDER_ORDER = ("M0", "M1", "M2", "M2b", "M3_BE", "M3_Step", "M4")
CHANGED_DIM = {
    "M0": None, "M1": "entry_mode", "M2": "stop_mode", "M2b": "stop_mode(m2b)",
    "M3_BE": "trail_mode", "M3_Step": "trail_mode", "M4": "rank_mode",
}

BASE_MODES = {"entry_mode": "donchian_only", "stop_mode": "uniform", "trail_mode": "none",
              "rank_mode": "quote_vol_30d_median"}

# 冻结晋级规则（来源：config/ladder.json promotion_rules；阈值不得在调用处改动）
PROMOTION = {
    "trades_frac_of_parent": 0.60,
    "win_months_strictly_gt": 0.50,
    "calmar_rel_improve": 0.15,
    "sharpe_abs_improve": 0.15,
    "dd_worsen_rel_max": 0.10,
}


def spec_for(level, parent_winner: dict | None = None) -> LevelSpec:
    """按"父层胜出配置 + 只改一个维度"构造本层规格。"""
    base = dict(BASE_MODES if not parent_winner else {
        "entry_mode": parent_winner["entry_mode"], "stop_mode": parent_winner["stop_mode"],
        "trail_mode": parent_winner["trail_mode"], "rank_mode": parent_winner["rank_mode"]})
    if level == "M0":
        return LevelSpec("M0", "donchian_only", "uniform", "none", "quote_vol_30d_median", "")
    if level == "M1":
        base["entry_mode"] = "three_buy_points"
    elif level == "M2":
        base["stop_mode"] = "structural"
    elif level == "M2b":
        base["stop_mode"] = "structural_m2b"
    elif level == "M3_BE":
        base["trail_mode"] = "naive_be"
    elif level == "M3_Step":
        base["trail_mode"] = "step_swing"
    elif level == "M4":
        base["rank_mode"] = "oi_change_90d_pct"
    else:
        raise ValueError(level)
    return LevelSpec(level, base["entry_mode"], base["stop_mode"], base["trail_mode"],
                     base["rank_mode"], parent_winner.get("level", "") if parent_winner else "")


def inheritance_check(spec: LevelSpec, parent: LevelSpec | None) -> dict:
    """核验本层与父层：除 CHANGED_DIM 外所有维度逐字相同（父层为空则基线）。"""
    if parent is None:
        return {"level": spec.level, "parent": None, "ok": True, "diff_dims": []}
    dims = ["entry_mode", "stop_mode", "trail_mode", "rank_mode"]
    diff = [d for d in dims if getattr(spec, d) != getattr(parent, d)]
    expected = {"M1": ["entry_mode"], "M2": ["stop_mode"], "M2b": ["stop_mode"],
                "M3_BE": ["trail_mode"], "M3_Step": ["trail_mode"], "M4": ["rank_mode"]}
    ok = diff == expected.get(spec.level, diff)
    return {"level": spec.level, "parent": parent.level, "diff_dims": diff,
            "expected": expected.get(spec.level), "ok": ok,
            "child": spec.as_dict(), "parent_cfg": parent.as_dict()}


def ledger_metrics(path: Path) -> dict:
    """从账本计算年化/DD/Sharpe/Calmar（按日权益）。"""
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()
    rev = con.execute("SELECT * FROM review LIMIT 1").fetchone()
    con.close()
    if not rows or rev is None:
        return {"error": "empty"}
    eq = [float(r["equity"]) for r in rows]
    ts = [int(r["ts"]) for r in rows]
    daily = {}
    for t, e in zip(ts, eq):
        daily[t // P.DAY_MS * P.DAY_MS] = e
    d = sorted(daily)
    curve = [daily[k] for k in d]
    if len(curve) < 3:
        return {"error": "too_few_days", "n_days": len(curve)}
    rets = [(curve[i] / curve[i - 1] - 1.0) for i in range(1, len(curve)) if curve[i - 1] > 0]
    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    var = sum((r - mean) ** 2 for r in rets) / n if n else 0.0
    sd = math.sqrt(var)
    sharpe = (mean / sd * math.sqrt(365)) if sd > 0 else 0.0
    years = len(curve) / 365.0
    total_ret = curve[-1] / curve[0] - 1.0
    ann = ((curve[-1] / curve[0]) ** (1 / years) - 1.0) if years > 0 and curve[0] > 0 else 0.0
    peak = curve[0]; mdd = 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 0.0)
    calmar = (ann / mdd) if mdd > 0 else None
    return {"trades": int(rev["trades"]), "final_equity": float(rev["final_equity"]),
            "net_pnl": float(rev["net_pnl"]), "gross_pnl": float(rev["gross_pnl"]),
            "fees": float(rev["fees"]), "funding_net": float(rev["funding_net"]),
            "max_drawdown": float(rev["max_drawdown"]), "halted": int(rev["halted"]),
            "n_days": len(curve), "ann_return": ann, "sharpe": sharpe, "calmar": calmar,
            "total_return": total_ret, "daily_dates": d, "daily_returns": rets}


def monthly_net(path: Path) -> dict:
    """按月净利（用于"获胜月>50%"晋级判定）。"""
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT exit_time, net_pnl FROM positions").fetchall()
    con.close()
    m = {}
    for r in rows:
        if r["exit_time"] is None:
            continue
        key = int(r["exit_time"]) // (P.DAY_MS * 30)   # 近似月桶（用于差异月比较）
        m[key] = m.get(key, 0.0) + float(r["net_pnl"])
    return m


# ══════════════════════════ IS/Val 合成与晋级判定 ══════════════════════════

def combine_segments(per_seg: dict) -> dict:
    """把 IS/Val 两段结果合成用于晋级判定的指标（口径见模块 docstring）。"""
    segs = [s for s in per_seg if per_seg[s].get("metrics", {}).get("trades") is not None]
    if not segs:
        return {"error": "no_segments"}
    trades = sum(per_seg[s]["metrics"]["trades"] for s in segs)
    max_dd = max(per_seg[s]["metrics"]["max_drawdown"] for s in segs)
    # 池化日收益（两段按时间拼接；日收益无量纲，故可直接串成一条曲线）
    pooled = []
    for s in sorted(segs, key=lambda x: per_seg[x]["metrics"]["daily_dates"][0]):
        pooled += list(per_seg[s]["metrics"]["daily_returns"])
    n = len(pooled)
    mean = sum(pooled) / n if n else 0.0
    var = sum((r - mean) ** 2 for r in pooled) / n if n else 0.0
    sd = math.sqrt(var)
    sharpe = (mean / sd * math.sqrt(365)) if sd > 0 else 0.0
    curve = [1.0]
    for r in pooled:
        curve.append(curve[-1] * (1.0 + r))
    years = len(curve) / 365.0
    ann = ((curve[-1] / curve[0]) ** (1 / years) - 1.0) if years > 0 else 0.0
    calmar = (ann / max_dd) if max_dd > 0 else None
    monthly = {}
    for s in segs:
        for k, v in per_seg[s]["monthly"].items():
            monthly[k] = monthly.get(k, 0.0) + v
    val = per_seg.get("Val", {}).get("metrics", {})
    return {"trades": trades, "max_drawdown": max_dd, "sharpe": sharpe, "calmar": calmar,
            "ann_return": ann, "monthly": monthly, "pooled_days": len(curve),
            "val_calmar": val.get("calmar"), "val_sharpe": val.get("sharpe"),
            "per_segment": {s: {"trades": per_seg[s]["metrics"]["trades"],
                                "max_drawdown": per_seg[s]["metrics"]["max_drawdown"],
                                "calmar": per_seg[s]["metrics"].get("calmar"),
                                "sharpe": per_seg[s]["metrics"].get("sharpe")} for s in segs}}


def promote(child_m: dict, parent_m: dict) -> dict:
    """冻结晋级标准（config/ladder.json `promotion_rules`）。不新增、不改阈值。

    `child_m` / `parent_m` 为 `combine_segments` 的输出（含 trades/max_drawdown/sharpe/
    calmar/monthly/val_calmar/val_sharpe）。
    """
    reasons = {}
    p_trades = parent_m.get("trades", 0)
    reasons["trades_ge_60pct_parent"] = (child_m["trades"] >= PROMOTION["trades_frac_of_parent"] * p_trades
                                         if p_trades > 0 else False)
    cm, pm = child_m.get("monthly", {}), parent_m.get("monthly", {})
    common = set(cm) & set(pm)
    diff_months = [k for k in common if abs(cm[k] - pm[k]) > 1e-9]
    wins = sum(1 for k in diff_months if cm[k] > pm[k])
    reasons["win_months_gt_50pct"] = (wins > len(diff_months) / 2) if diff_months else False
    reasons["diff_months"] = len(diff_months)
    reasons["win_months"] = wins
    p_cal, c_cal = parent_m.get("calmar"), child_m.get("calmar")
    if p_cal and p_cal > 0:
        reasons["calmar_rel_ge_15pct"] = (c_cal or 0) >= p_cal * (1 + PROMOTION["calmar_rel_improve"])
        reasons["improve_branch"] = "calmar" if reasons["calmar_rel_ge_15pct"] else "sharpe"
    else:
        reasons["calmar_rel_ge_15pct"] = None
        reasons["improve_branch"] = "sharpe(no_positive_baseline_calmar)"
    reasons["sharpe_abs_ge_0p15"] = ((child_m["sharpe"] - parent_m["sharpe"])
                                     >= PROMOTION["sharpe_abs_improve"])
    if parent_m.get("max_drawdown", 0) > 0:
        reasons["dd_worsen_le_10pct"] = (child_m["max_drawdown"]
                                         <= parent_m["max_drawdown"] * (1 + PROMOTION["dd_worsen_rel_max"]))
    else:
        reasons["dd_worsen_le_10pct"] = True
    vc, vs = child_m.get("val_calmar"), child_m.get("val_sharpe")
    pvc, pvs = parent_m.get("val_calmar"), parent_m.get("val_sharpe")
    reasons["val_positive_and_better"] = bool(
        vc is not None and vs is not None and vc > 0 and vs > 0
        and (vc > (pvc or -1e9)) and (vs > (pvs or -1e9)))
    improved = bool(reasons.get("calmar_rel_ge_15pct")) or reasons["sharpe_abs_ge_0p15"]
    promoted = bool(reasons["trades_ge_60pct_parent"] and reasons["win_months_gt_50pct"]
                    and improved and reasons["dd_worsen_le_10pct"]
                    and reasons["val_positive_and_better"])
    return {"promoted": promoted, "reasons": reasons}


# ══════════════════════════ 单层单段执行 ══════════════════════════

def _run_one(seg_data, spec, tf, start_ms, end_ms, symbols, run_tag, led_dir, code_sha,
             config_sha, input_sha, commit_every, collect=False, rank_provider=None, log=print):
    path = led_dir / f"ladder_{run_tag}_{tf}_{seg_data.seg}_{spec.level}.sqlite3"
    bt = Backtest(seg_data=seg_data, spec=spec, timeframe=tf, start_ms=start_ms, end_ms=end_ms,
                  run_id=f"{run_tag}_{tf}_{seg_data.seg}_{spec.level}", ledger_path=path,
                  symbols=symbols, commit_every=commit_every, code_sha=code_sha,
                  config_sha=config_sha, input_sha=input_sha, collect_candidates=collect,
                  rank_provider=rank_provider)
    res = bt.run()
    bt.led.close()
    return {"result": res, "metrics": ledger_metrics(path), "monthly": monthly_net(path),
            "ledger": str(path), "candidates": list(bt.candidates)}


# ══════════════════════════ 跨段晋级驱动的阶梯 ══════════════════════════

def run_ladder(*, segments, tf, run_tag, led_dir, code_sha="", config_sha="", input_sha="",
               commit_every=1, log=print, rank_provider_by_seg=None, m2_collect=True):
    """按 **IS+Val 跨段晋级** 顺序跑 M0→M1→M2→(条件)M2b→M3_BE/M3_Step→M4。

    `segments`：`{seg_name: (SegmentData, start_ms, end_ms)}`（只允许 IS/Val）。
    返回每段每层结果 + 继承核验 + 消费证明 + 晋级判定轨迹 + 父层选择。
    """
    segs = list(segments)
    rank_provider_by_seg = rank_provider_by_seg or {}
    levels = {lv: {} for lv in LADDER_ORDER}       # level -> {seg: _run_one 结果}
    specs = {}                                     # level -> LevelSpec（全段同配置）
    decisions = []

    def run_level(level, parent_spec):
        sp = spec_for(level, parent_spec.as_dict() if parent_spec else None)
        specs[level] = sp
        for seg in segs:
            sd, s_ms, e_ms = segments[seg]
            syms = sd.symbols(tf)
            levels[level][seg] = _run_one(
                sd, sp, tf, s_ms, e_ms, syms, run_tag, led_dir, code_sha, config_sha,
                input_sha, commit_every, collect=(m2_collect and level == "M2"),
                rank_provider=rank_provider_by_seg.get(seg), log=log)
            log(f"  [{run_tag}/{seg}] {level} trades={levels[level][seg]['result']['trades']} "
                f"net={levels[level][seg]['result']['net']:.1f}")

    def combined(level):
        return combine_segments(levels[level])

    # ① M0（基线）
    run_level("M0", None)
    # ② M1（父 = M0）
    run_level("M1", specs["M0"])
    # ③ M2 父：M1 晋级 ? M1 : M0
    m1_promo = promote(combined("M1"), combined("M0"))
    m2_parent = specs["M1"] if m1_promo["promoted"] else specs["M0"]
    decisions.append({"step": "M2_parent", "rule": "M1 promoted ? M1 : M0",
                      "m1_vs_m0": m1_promo, "chosen": m2_parent.level})
    run_level("M2", m2_parent)

    # ④ 条件 M2b（IS+Val 合并超限率 > 30%）
    cand_is = levels["M2"].get("IS", {}).get("candidates", [])
    cand_val = levels["M2"].get("Val", {}).get("candidates", [])
    from WyckoffMaster import core as WC
    st_is = WC.m2_over_limit_stats(cand_is)
    st_val = WC.m2_over_limit_stats(cand_val)
    trig = WC.m2b_trigger(st_is, st_val, 0.30)
    m2b_ran = False
    if trig["triggered"]:
        run_level("M2b", specs["M2"])
        m2b_ran = True
    decisions.append({"step": "M2b_trigger", "stats_IS": st_is, "stats_Val": st_val,
                      "trigger": trig, "ran": m2b_ran})

    # ⑤ M3 父：M2/M2b 胜出者
    if m2b_ran:
        m2b_promo = promote(combined("M2b"), combined("M2"))
        m3_parent = specs["M2b"] if m2b_promo["promoted"] else specs["M2"]
        decisions.append({"step": "M3_parent", "rule": "M2b promoted over M2 ? M2b : M2",
                          "m2b_vs_m2": m2b_promo, "chosen": m3_parent.level})
    else:
        m3_parent = specs["M2"]
        decisions.append({"step": "M3_parent", "rule": "M2b 未触发 ⇒ 父 = M2",
                          "chosen": m3_parent.level})
    run_level("M3_BE", m3_parent)
    run_level("M3_Step", m3_parent)

    # ⑥ M4 父：M2/M3 胜出者（M3_Step 需同时优于父层与 M3_BE）
    m3_base = m3_parent
    winner = specs[m3_base.level]
    be_promo = promote(combined("M3_BE"), combined(m3_base.level))
    if be_promo["promoted"]:
        winner = specs["M3_BE"]
    step_promo = promote(combined("M3_Step"), combined(m3_base.level))
    step_vs_be = promote(combined("M3_Step"), combined("M3_BE"))
    if step_promo["promoted"] and step_vs_be["promoted"]:
        winner = specs["M3_Step"]
    decisions.append({"step": "M4_parent", "rule": "M3_Step > (父层 且 M3_BE) ? M3_Step : "
                                                  "(M3_BE 晋级 ? M3_BE : 父层)",
                      "m3_be_vs_parent": be_promo, "m3_step_vs_parent": step_promo,
                      "m3_step_vs_m3_be": step_vs_be, "chosen": winner.level})
    run_level("M4", winner)

    # ── 继承核验 + 消费证明 ─────────────────────────────────────────────
    inh = {}
    for lv, sp in specs.items():
        par = specs.get(sp.parent) if sp.parent in specs else None
        inh[lv] = inheritance_check(sp, par)
    consumed = {lv: {seg: levels[lv][seg]["result"]["consumed"] for seg in segs}
                for lv in levels if levels[lv]}
    return {"run_tag": run_tag, "tf": tf, "segments": segs,
            "n_symbols": {seg: levels["M0"][seg]["result"]["n_symbols"] for seg in segs},
            "results": {lv: {seg: levels[lv][seg]["result"] for seg in levels[lv]}
                        for lv in levels if levels[lv]},
            "metrics": {lv: {seg: levels[lv][seg]["metrics"] for seg in levels[lv]}
                        for lv in levels if levels[lv]},
            "monthly": {lv: {seg: levels[lv][seg]["monthly"] for seg in levels[lv]}
                        for lv in levels if levels[lv]},
            "inheritance": inh, "consumed": consumed, "decisions": decisions,
            "m2_over_limit": {"IS": st_is, "Val": st_val, "merged_trigger": trig},
            "m2_candidates_n": {"IS": len(cand_is), "Val": len(cand_val)},
            "specs": {lv: specs[lv].as_dict() for lv in specs},
            "levels_run": [lv for lv in LADDER_ORDER if levels[lv]]}
