# -*- coding: utf-8 -*-
"""回测指标口径（只读账本 + 真实权益曲线；缺失一律 None，不填 0 冒充）。

本模块存在的理由（2026-10-17 查实）
==================================
`GET /backtests/{id}/metrics` 原先直接返回 `summary.json` 里的字段，而这些 run 的
`summary.json` **只落了** `fee_total / funding_total / net_pnl / trade_count / …`：

    wins: 0, losses: 0        ← summary 里**没有** win_count/loss_count，`or 0` 把它们
                                 写成了 0 ⇒ 一笔净亏 −5.24 的交易被统计成「0 胜 0 负」
    max_drawdown_pct: 0.0     ← summary 里**没有**该字段，`or 0.0` 把「没算」写成「回撤 0%」
    profit_factor: null / avg_win_loss_ratio: null   ← 同上，未算
    benchmark_buy_hold: null / monthly_returns: null ← 未算

而真实证据**就在同一个 run 目录里**：
  · `trades.jsonl` 逐笔含 `gross_pnl / entry_fee / exit_fee / fees / funding_fee / net_pnl`；
  · `equity_curve.jsonl` 逐 15m 含 `total_equity / unrealized_pnl`（**含浮盈浮亏**）。

口径（全部写进返回值，页面可直接展示）
=====================================
1. **胜负**：按**扣费后净盈亏** `net_pnl` 判定 —— `>0` 盈 / `<0` 亏 / `==0` 平。
   `net_pnl = gross_pnl − fees − funding_fee`，逐笔由账本自证（见 `trades.jsonl`）。
2. **最大回撤**：取 `equity_curve.jsonl` 的 `total_equity`（**含未实现浮盈浮亏**）逐点算
   峰值→谷值最大跌幅，`max_drawdown_pct = max((peak − eq) / peak) × 100`。
   曲线不足 2 点 ⇒ `None`（页面显示「未计算」），**不填 0**。
3. **Profit Factor 与平均盈亏比严格分开，不混用**：
   · `profit_factor = Σ(盈利净额) / |Σ(亏损净额)|`（**聚合口径**）；
   · `avg_win_loss_ratio = 平均盈利 / |平均亏损|`（**均值口径**）。
   两者在笔数不均衡时**必然不同**，返回值里各自带 `basis` 字符串。
4. 分母为 0 的情形**不编数字**：无盈利单 ⇒ `avg_win_loss_ratio=None`；
   无亏损单 ⇒ `profit_factor=None`（不输出 inf）。
   但「无盈利单且有亏损单」时 `profit_factor = 0.0` 是**有定义的**（0 / Σ|亏|），照实给 0。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ledger_links import load_ledger_facts

__all__ = [
    "WIN_LOSS_BASIS",
    "MDD_BASIS",
    "PF_BASIS",
    "AWLR_BASIS",
    "compute_metrics",
    "compute_benchmark",
    "compute_monthly_returns",
]

WIN_LOSS_BASIS = "net_pnl（已扣手续费与资金费）>0 为盈、<0 为亏、=0 为平"
MDD_BASIS = ("equity_curve.total_equity 逐点峰值回撤（含未实现浮盈浮亏，"
             "15m 粒度；峰值起点=首个权益点）")
PF_BASIS = "Σ盈利净额 / |Σ亏损净额|（聚合口径，扣费后）"
AWLR_BASIS = "平均盈利净额 / |平均亏损净额|（均值口径，扣费后）"
WR_BASIS = "盈利笔数 / 已平仓笔数（含平盘）"


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:                                            # NaN
            return None
        return x
    except (TypeError, ValueError):
        return None


def _mean(xs: List[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _max_drawdown_pct(equity: List[float]) -> Tuple[Optional[float], Optional[dict]]:
    """逐点峰值回撤。返回 (pct, 明细)。不足 2 点 ⇒ (None, None)。"""
    if len(equity) < 2:
        return None, None
    peak = equity[0]
    peak_i = 0
    best = 0.0
    best_peak_i = best_trough_i = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
            continue
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > best:
                best, best_peak_i, best_trough_i = dd, peak_i, i
    return best, {"peak_index": best_peak_i, "trough_index": best_trough_i,
                  "peak_equity": equity[best_peak_i],
                  "trough_equity": equity[best_trough_i],
                  "points": len(equity)}


def compute_metrics(run_dir, run_id: str, doc: Optional[dict] = None) -> Tuple[dict, List[str]]:
    """按真实账本计算指标。返回 (metrics, notes)。

    绝不读 `summary.json` 里不存在的字段来「凑」数字；每一处缺失都在 `notes` 里写明原因。
    """
    notes: List[str] = []
    facts = load_ledger_facts(run_dir, run_id)

    rows = facts.trade_rows
    nets = [_f(r.get("net_pnl")) for r in rows]
    nets = [x for x in nets if x is not None]

    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    flats = [x for x in nets if x == 0]
    n = len(nets)

    # 初始权益：账本 equity 首点（真实），退化到 trades/summary 则标注来源
    eq_rows = facts.equity
    eq_vals = [_f(e.get("total_equity", e.get("equity"))) for e in eq_rows]
    eq_vals = [x for x in eq_vals if x is not None]
    initial_equity = eq_vals[0] if eq_vals else None
    final_equity = eq_vals[-1] if eq_vals else None

    gross = [_f(r.get("gross_pnl")) for r in rows]
    gross = [x for x in gross if x is not None]
    fee_side = [(_f(r.get("entry_fee")) or 0.0) + (_f(r.get("exit_fee")) or 0.0) for r in rows]
    fund_side = [(_f(r.get("funding_fee")) or 0.0) for r in rows]

    sum_win = sum(wins)
    sum_loss_abs = abs(sum(losses))
    avg_win = _mean(wins)
    avg_loss = _mean(losses)

    if n == 0:
        win_rate = None
        pf = None
        awlr = None
        notes.append("该 run 无已平仓交易 ⇒ 胜率/盈亏比/PF 均无证据，一律 None（不填 0）")
    else:
        win_rate = len(wins) / n * 100.0
        if not losses:
            pf = None
            notes.append("无亏损交易 ⇒ profit_factor 分母为 0，无定义（不输出 inf）")
        else:
            pf = (sum_win / sum_loss_abs) if sum_win else 0.0
        awlr = (avg_win / abs(avg_loss)) if (avg_win is not None and avg_loss) else None
        if awlr is None:
            notes.append("缺少盈利单或亏损单 ⇒ 平均盈亏比无定义（不填 0）")

    mdd, mdd_detail = _max_drawdown_pct(eq_vals)
    if mdd is None:
        notes.append(f"权益曲线不足 2 点（实测 {len(eq_vals)} 点）⇒ 最大回撤返回缺失值，页面显示「未计算」")

    metrics: Dict[str, Any] = {
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "net_pnl": (sum(nets) if nets else None),
        "net_return_pct": (((final_equity - initial_equity) / initial_equity * 100.0)
                           if (initial_equity and final_equity is not None) else None),
        "gross_pnl": (sum(gross) if gross else None),
        "total_entry_fee": (sum(_f(r.get("entry_fee")) or 0.0 for r in rows) if rows else None),
        "total_exit_fee": (sum(_f(r.get("exit_fee")) or 0.0 for r in rows) if rows else None),
        "total_fees": (sum(fee_side) if rows else None),
        "total_funding_fee": (sum(fund_side) if rows else None),
        "trades_count": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate_pct": win_rate,
        "win_rate_basis": WR_BASIS,
        "win_loss_basis": WIN_LOSS_BASIS,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_win_loss_ratio": awlr,
        "avg_win_loss_ratio_basis": AWLR_BASIS,
        "profit_factor": pf,
        "profit_factor_basis": PF_BASIS,
        "expectancy_usd": _mean(nets),
        "max_drawdown_pct": mdd,
        "max_drawdown_basis": MDD_BASIS,
        "max_drawdown_detail": mdd_detail,
        "equity_points": len(eq_vals),
        "open_positions_remaining": None,
        "note": ("指标全部由本 run 账本现算：胜负=扣费后 net_pnl；回撤=逐点 total_equity；"
                 "PF 为聚合口径、平均盈亏比为均值口径（两者不混用）"),
        "funding_events_total": sum(1 for f in facts.funding_rows),
    }
    notes.extend(facts.notes)
    return metrics, notes


def compute_benchmark(chart: Optional[dict]) -> Optional[dict]:
    """把 `chart.benchmark` 归一成前端 `LabBenchmark`，并**如实标注成本口径**。

    关键诚实性约束：批量脚本写出的基准序列是 `initial_equity × close_i / open_0`，
    即**纯价格**口径（不含手续费 / 滑点 / 资金费）。因此：

    * `price_only_return_pct` = 由该序列真实算出；
    * `net_return_pct` = **None**（成本模型没落盘 ⇒ 不把纯价格收益冒充「净收益」）；
    * `cost_model` 明写 `price_only`，页面直接展示。

    只有当序列点带 `cum_funding`（同成本口径的标记）时才认为可给 `net_return_pct`。
    """
    if not isinstance(chart, dict):
        return None
    b = chart.get("benchmark")
    if not isinstance(b, dict):
        return None
    series = b.get("equity_series") or []
    pts = [p for p in series if isinstance(p, dict) and _f(p.get("equity")) is not None]
    if not pts:
        return None
    eqs = [_f(p["equity"]) for p in pts]
    same_cost = all(("cum_funding" in p) for p in pts)

    # 基数：优先用基准序列自己的首点（同本金口径由序列自证），退化到 chart.equity_curve 首点
    base = eqs[0]
    if not base:
        for e in (chart.get("equity_curve") or []):
            v = _f((e or {}).get("equity")) if isinstance(e, dict) else None
            if v:
                base = v
                break
    last = eqs[-1]
    ret = ((last - base) / base * 100.0) if base else None
    mdd, _ = _max_drawdown_pct(eqs)

    out: Dict[str, Any] = {
        "benchmark_kind": ("buy_and_hold_price_only" if not same_cost
                           else "buy_and_hold_same_cost"),
        "symbol": b.get("symbol"),
        "entry_time_ms": (pts[0].get("t") if isinstance(pts[0].get("t"), (int, float)) else None),
        "exit_time_ms": (pts[-1].get("t") if isinstance(pts[-1].get("t"), (int, float)) else None),
        "entry_price": None,
        "exit_price": None,
        "qty": None,
        "cum_funding": (_f(pts[-1].get("cum_funding")) if same_cost else None),
        "gross_return_pct": ret,
        "price_only_return_pct": ret,
        "net_return_pct": (ret if same_cost else None),
        "final_equity": last,
        "series_last_equity": last,
        "max_drawdown_pct": mdd,
        "cost_model": ("same_cost（含手续费/滑点/真实资金费）" if same_cost
                       else "price_only（纯价格：不含手续费/滑点/资金费）"),
        "points": len(pts),
        "note": ("本 run 落盘的基准序列为纯价格口径 ⇒ 只给 price_only_return_pct，"
                 "net_return_pct 如实留空（不与含成本的策略净收益直接比较）"
                 if not same_cost else
                 "基准序列为同成本口径，net_return_pct 可直接与策略净收益比较"),
    }
    return out


def compute_monthly_returns(equity_rows: List[dict], initial_equity: Optional[float]) -> Optional[dict]:
    """月度收益：账户**月界净值**口径，首月无基准 ⇒ `return_pct=None`（不填 0）。

    与项目既有约定一致：`月度收益 = 本月末净值 / 上月末净值 − 1`，逐月不叠加。
    """
    import datetime as _dt

    pts: List[Tuple[int, float]] = []
    for e in equity_rows or []:
        if not isinstance(e, dict):
            continue
        ts = _f(e.get("timestamp", e.get("ts_ms", e.get("t"))))
        v = _f(e.get("total_equity", e.get("equity")))
        if ts is None or v is None:
            continue
        pts.append((int(ts), v))
    if len(pts) < 2:
        return None
    pts.sort(key=lambda x: x[0])

    def _ym(ms: int) -> str:
        d = _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc)
        return f"{d.year:04d}-{d.month:02d}"

    # 每月最后一个点（月界净值）
    month_last: Dict[str, float] = {}
    for ms, v in pts:
        month_last[_ym(ms)] = v
    months = sorted(month_last)
    out: List[dict] = []
    prev: Optional[float] = None
    for m in months:
        eq = month_last[m]
        r = ((eq / prev - 1.0) * 100.0) if prev else None
        out.append({
            "month": m,
            "month_end_equity": eq,
            "basis_equity": prev,
            "return_pct": r,
            "partial_month": (m == months[0]),
            "basis_note": (None if prev else "首月无上月月界净值 ⇒ 收益率为缺失（不填 0）"),
        })
        prev = eq
    return {"months": out,
            "initial_equity": initial_equity,
            "note": "月界净值口径：本月末 total_equity / 上月末 total_equity − 1；首月无基准为 null"}
