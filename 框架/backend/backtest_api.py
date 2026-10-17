# -*- coding: utf-8 -*-
"""研究回测 HTTP 适配（`/api/v1/backtests` 扩展）。

契约（`bt_api_v1`，稳定）
────────────────────────
`POST /api/v1/backtests`                 创建研究回测 → `{run_id, status:"queued"}` 202
`GET  /api/v1/backtests`                 实验列表（**不带 mode**；带 mode 走旧通道）
`GET  /api/v1/backtests/{id}`            状态 + 配置（**不触发计算**）
`GET  /api/v1/backtests/{id}/metrics`    指标（从账本算）
`GET  /api/v1/backtests/{id}/equity`     权益曲线
`GET  /api/v1/backtests/{id}/trades`     交易列表
`GET  /api/v1/backtests/{id}/trades/{tid}` 单笔（同一 trade_id 联结图表与表格）
`GET  /api/v1/backtests/{id}/orders`     **全局**订单/成交明细
`GET  /api/v1/backtests/{id}/rejected`   **拒单明细**
`GET  /api/v1/backtests/{id}/chart`      图表载荷（真实 K 线/信号/开平仓/保护止损轨迹）
`GET  /api/v1/backtests/{id}/quality`    数据质量与对账

纪律
────
· **GET 一律不触发计算**（只读 `run.json` / `ledger.sqlite3`）。
· 仅 `WX_OI_RESONANCE_V1`；参数白名单；周期/交易所/市场白名单（当前仅 1h 信号 + 15m 执行）。
· 创建走**原子独占目录**，重名拒绝；任务在**后台线程**执行，状态持久化。
· **运行列表只含当前策略**；非当前策略 / 身份缺失的 run 排除并单列 `excluded_legacy`。
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import backtest_runner as RUN
import backtest_store as S
import event_trend_bridge as BR
import ledger_links as LL
import backtest_metrics as MET

CONTRACT_VERSION = "bt_api_v1"

_THREADS: Dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()


def _facts(run_id: str) -> LL.LedgerFacts:
    """本 run 的账本事实（真实标识关联；一次读盘多处复用）。

    这些 run 的账本落成了 **JSONL**（无 `ledger.sqlite3`），且 `trades.jsonl` /
    `orders.jsonl` 本身不带 `trade_id` —— 因此**必须**走本模块按真实标识关联，
    绝不能让 `read_ledger_rows` 的 `f"trd_{len(rows)+1}"` 兜底生效（那是按行号猜配）。
    """
    return LL.load_ledger_facts(S.run_dir(run_id), run_id)



def _envelope(**kw) -> dict:
    base = {
        "mode": "research_backtest", "contract_version": CONTRACT_VERSION,
        "strategy_id": BR.STRATEGY_ID, "run_id": None, "status": None,
        # 默认**不得**报 ok：未读取到真实质量时一律 unknown（不冒充成功）。
        "quality": {"status": "unknown", "age_seconds": None, "note": None},
        "missing_reason": None, "items": [], "total": None,
        "live_order_enabled": False,
    }
    base.update(kw)
    return base


# ══════════════════════════ 读取 ══════════════════════════

def list_experiments(params: Optional[dict] = None) -> dict:
    params = params or {}
    try:
        limit = int((params.get("limit") or ["500"])[0])
    except (ValueError, TypeError):
        limit = 500
    runs = S.list_runs(limit=max(1, min(limit, 1000)))
    # ── WORK-04 新旧归属：运行列表**只含新策略** `WX_OI_RESONANCE_V1` ─────────────
    # 运行根（`回测日志/oi_resonance_v1/runs`）本应只含新策略 run；此处仍按 strategy_id
    # 显式核对，把非新策略 / 身份缺失的 run **排除出当前列表**并单独登记（不冒充新结果）。
    current: List[dict] = []
    excluded_legacy: List[dict] = []
    for r in runs:
        rid = str(r.get("run_id") or "")
        # 内部消融变体仅用于对账，不作为主回测记录干扰前端列表
        if re.search(r"_(A|B|C)_[0-9]+$", rid):
            continue
        sid = (r.get("spec") or {}).get("strategy_id") or r.get("strategy_id")
        if sid == BR.STRATEGY_ID:
            current.append(r)
        else:
            excluded_legacy.append({
                "run_id": r.get("run_id"), "status": r.get("status"),
                "strategy_id": sid, "created_at": r.get("created_at"),
                "attribution": "legacy_excluded",
                "note": "非当前策略（或身份缺失）⇒ 排除出当前运行列表；旧结果不得当成新策略成绩",
            })
    items = [_summary(r) for r in current]
    return _envelope(items=items, total=len(items),
                     runs_root=str(S.RUNS_ROOT),
                     registry_entry=_registry_entry(),
                     retirement=_retirement_block(),
                     excluded_legacy=excluded_legacy,
                     excluded_legacy_total=len(excluded_legacy),
                     note=f"研究回测实验列表（**仅当前策略** {BR.STRATEGY_ID}；只读；"
                          "创建走 POST /api/v1/backtests）。旧策略 run 已排除并单列 excluded_legacy。")


def _retirement_block() -> dict:
    """旧策略退役汇总（懒加载；失败如实报缺）。"""
    try:
        from strategies import registry as _reg
        return _reg.retirement_summary(None)
    except Exception as exc:                              # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _registry_entry() -> dict:
    """注册表「单一当前回测策略」元数据（懒加载、失败如实报缺）。"""
    try:
        from strategies import registry as _reg
        return _reg.describe_current_backtest(None)
    except Exception as exc:                              # noqa: BLE001
        return {"strategy_id": BR.STRATEGY_ID, "available": False,
                "import_error": f"{type(exc).__name__}: {exc}"}


def _summary(doc: dict) -> dict:
    m = doc.get("metrics") or {}
    sid = (doc.get("spec") or {}).get("strategy_id") or doc.get("strategy_id")
    return {
        "run_id": doc.get("run_id"), "status": doc.get("status"),
        "created_at": doc.get("created_at"), "started_at": doc.get("started_at"),
        "finished_at": doc.get("finished_at"),
        "strategy_id": sid,
        "strategy_key": BR.STRATEGY_KEY,
        "attribution": ("current_strategy" if sid == BR.STRATEGY_ID else "unknown"),
        "symbols": (doc.get("spec") or {}).get("symbols"),
        "window": doc.get("window"),
        "net_return_pct": m.get("net_return_pct"),
        "trades_count": m.get("trades_count"),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "failure_reason": doc.get("failure_reason"),
    }


def get_run(run_id: str) -> Tuple[dict, int]:
    """状态 + 配置（**只读，不触发计算**）。"""
    try:
        doc = S.read_run(run_id)
    except S.RunNotFound:
        return _envelope(missing_reason=f"未知 run_id：{run_id}"), 404
    try:
        counts = S.ledger_counts(run_id) if S.ledger_path(run_id).exists() else {}
    except Exception as exc:                              # noqa: BLE001
        counts = {"error": f"{type(exc).__name__}: {exc}"}
    return _envelope(
        run_id=run_id, status=doc.get("status"),
        quality=_quality_of(doc),
        config={"spec": doc.get("spec"), "config": doc.get("config"),
                "actual_config": (doc.get("data_quality") or {}).get("actual_config")},
        hashes=doc.get("hashes"), window=doc.get("window"),
        engine=doc.get("engine"), ledger_counts=counts,
        chart_file=doc.get("chart_file"), snapshot_files=doc.get("snapshot_files"),
        created_at=doc.get("created_at"), started_at=doc.get("started_at"),
        finished_at=doc.get("finished_at"), failure_reason=doc.get("failure_reason"),
        note="GET 只读，不触发任何计算"), 200


def _quality_of(doc: dict) -> dict:
    """运行状态 → 质量块。**未成功/缺质量一律不报 ok**（失败/中断/进行中如实反映）。"""
    st = doc.get("status")
    dq = doc.get("data_quality") or {}
    if st == S.STATUS_SUCCEEDED:
        return {"status": dq.get("status") or "unknown", "age_seconds": None,
                "note": dq.get("reasons") or None}
    if st in (S.STATUS_QUEUED, S.STATUS_RUNNING):
        return {"status": "pending", "age_seconds": None,
                "note": "运行尚未完成；GET 只读、不触发计算"}
    return {"status": "unavailable", "age_seconds": None,
            "note": f"运行未成功（{st}）：{doc.get('failure_reason')}"}


def _require_done(run_id: str) -> Tuple[Optional[dict], Optional[Tuple[dict, int]]]:
    try:
        doc = S.read_run(run_id)
    except S.RunNotFound:
        return None, (_envelope(missing_reason=f"未知 run_id：{run_id}",
                                quality={"status": "unavailable", "age_seconds": None,
                                         "note": "未知 run_id"}), 404)
    if doc.get("status") not in (S.STATUS_SUCCEEDED,):
        return None, (_envelope(run_id=run_id, status=doc.get("status"),
                                quality=_quality_of(doc),
                                missing_reason=(
                                    "结果尚未就绪（GET 不触发计算）"
                                    if doc.get("status") in (S.STATUS_QUEUED, S.STATUS_RUNNING)
                                    else f"运行未成功：{doc.get('failure_reason')}"),
                                failure_reason=doc.get("failure_reason")), 200)
    return doc, None


def get_metrics(run_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    # 指标**由本 run 账本现算**（不是读 summary.json 里可能不存在的字段）：
    #   · 胜负 = 扣费后 net_pnl（>0 盈 / <0 亏 / =0 平）
    #   · 最大回撤 = equity_curve.total_equity 逐点峰值回撤（含未实现浮盈浮亏）
    #   · profit_factor（聚合口径）与 avg_win_loss_ratio（均值口径）**分开、不混用**
    metrics, notes = MET.compute_metrics(S.run_dir(run_id), run_id, doc)
    stored = doc.get("metrics") or {}
    bench: Optional[dict] = None
    monthly: Optional[dict] = None
    try:
        chart = _backfill_chart(run_id, S.read_chart(run_id))
        bench = MET.compute_benchmark(chart)
        if bench is None:
            notes.append("chart.json 无基准序列 ⇒ benchmark_buy_hold 返回缺失（不伪造）")
    except S.RunNotFound:
        notes.append("chart.json 缺失 ⇒ benchmark_buy_hold 返回缺失（不伪造）")
    try:
        monthly = MET.compute_monthly_returns(
            S.read_ledger_rows(run_id, "equity", order_by="ts_ms ASC"),
            metrics.get("initial_equity"))
        if monthly is None:
            notes.append("权益曲线不足 2 点 ⇒ monthly_returns 返回缺失（不伪造）")
    except S.RunNotFound:
        notes.append("equity 表不可读 ⇒ monthly_returns 返回缺失（不伪造）")
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     metrics=metrics,
                     metrics_from_ledger=True,
                     metrics_stored_summary=stored,
                     metrics_notes=notes,
                     benchmark_buy_hold=bench,
                     monthly_returns=monthly,
                     formal_m4_gate=doc.get("formal_m4_gate"),
                     m4_disclaimer=doc.get("m4_disclaimer"),
                     note=("metrics 由本 run 账本现算（净值口径）；metrics_stored_summary 为"
                           "落盘 summary 原值，仅供对照，不作为展示来源")), 200


def get_quality(run_id: str) -> Tuple[dict, int]:
    try:
        doc = S.read_run(run_id)
    except S.RunNotFound:
        return _envelope(missing_reason=f"未知 run_id：{run_id}",
                         quality={"status": "unavailable", "age_seconds": None,
                                  "note": "未知 run_id"}), 404
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     data_quality=doc.get("data_quality"),
                     hashes=doc.get("hashes"), window=doc.get("window"),
                     ledger_counts=(doc.get("data_quality") or {}).get("ledger_counts")), 200


def get_equity(run_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    rows = S.read_ledger_rows(run_id, "equity", order_by="ts_ms ASC")
    bench_series: List[dict] = []
    bench_meta: Optional[dict] = None
    try:
        chart = S.read_chart(run_id)
        chart = _backfill_chart(run_id, chart)   # 修掉批量脚本写死的 benchmark.return_pct = 0.0
        bench_series = (chart.get("benchmark") or {}).get("equity_series") or []
        # 基准**成本口径如实标注**：批量脚本落盘的基准序列是 `initial_equity × close_i / open_0`，
        # 即**纯价格**口径（不含手续费/滑点/资金费）⇒ 只给 price_only_return_pct，
        # net_return_pct 留空，**不把纯价格收益冒充「净收益」**与策略净收益并列比较。
        bench_meta = MET.compute_benchmark(chart)
        if bench_meta is None:
            bench_meta = {"note": "本 run 的 chart.json 未含基准序列 ⇒ 无基准（不伪造）"}
    except S.RunNotFound:
        bench_series, bench_meta = [], {"note": "chart.json 缺失 ⇒ 无基准序列（不伪造）"}
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     items=rows, total=len(rows),
                     benchmark=bench_meta, benchmark_equity=bench_series,
                     note=("策略权益曲线取自账本 equity 表（含未实现浮盈浮亏）；"
                           "benchmark_equity 为本 run 落盘的买入持有基准逐点净值，"
                           "其成本口径见 benchmark.cost_model（纯价格口径时不给 net_return_pct）")), 200


def get_trades(run_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    facts = _facts(run_id)
    rows = facts.trade_rows
    unresolved = [r for r in rows if not r.get("trade_id")]
    notes = list(facts.notes)
    if unresolved:
        notes.append(
            f"{len(unresolved)} 笔交易的 trade_id 在账本里无法唯一确定 ⇒ 如实留空（不按行号猜配）")
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     items=rows, total=len(rows),
                     trade_id_source="ledger:positions.jsonl POSITION_CLOSED.payload.trade_id",
                     unresolved_trade_ids=len(unresolved),
                     link_notes=notes,
                     note=("trade_id / 关联订单 / 资金费归属全部取自本 run 账本的真实标识；"
                           "stop_trace 已统一为 `t`（毫秒），无后续止损调整时即为单点")), 200


def get_orders(run_id: str) -> Tuple[dict, int]:
    """**全局**订单/成交明细（同一 `run_id` 账本 `orders` 表；与逐笔同源）。

    纪律：只读账本，缺失表/账本 ⇒ 空列表（不编造）。金额/数量原样来自账本。
    `trade_id` 用**真实 `order_id` 反查**归属（`orders.jsonl` 本身没有该字段），
    关联不上 ⇒ `trade_id=None` + `trade_link="unlinked"`，**绝不按顺序套用第 N 笔**。
    """
    doc, err = _require_done(run_id)
    if err:
        return err
    try:
        facts = _facts(run_id)
    except S.RunNotFound:
        return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                         items=[], total=0, missing_reason="账本缺失（orders 表不可读）"), 200
    rows = facts.all_orders()
    unlinked = sum(1 for r in rows if not r.get("trade_id"))
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     items=rows, total=len(rows),
                     unlinked_orders=unlinked,
                     link_notes=list(facts.notes),
                     note=("全局订单/成交明细（同一 run_id 账本 orders 表）；"
                           "trade_id 由真实 order_id 反查，关联不上则如实留空")), 200


def get_rejected(run_id: str) -> Tuple[dict, int]:
    """**拒单明细**（同一 `run_id` 账本 `rejected` 表：时点 / 标的 / 相位 / 原因）。

    纪律：只读账本；缺表 ⇒ 空列表并标注 `missing_reason`，不把「无记录」包装成「无拒单」。
    """
    doc, err = _require_done(run_id)
    if err:
        return err
    try:
        rows = S.read_ledger_rows(run_id, "rejected", order_by="ts_ms ASC")
    except S.RunNotFound:
        return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                         items=[], total=0, missing_reason="账本缺失（rejected 表不可读）"), 200
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     items=rows, total=len(rows),
                     note="拒单明细（账本 rejected 表：风险/仓位/资金/信号门禁等拒绝原因）"), 200


def get_trade(run_id: str, trade_id: str) -> Tuple[dict, int]:
    """单笔明细：**同一真实 `trade_id`** 联结逐笔 / 开平仓订单 / 资金费 / 原始候选信号。

    关联只用真实标识：订单走 `trades.order_id` / `exit_order_id` 直连 `orders.jsonl`；
    资金费走时间窗**唯一**归属；候选信号走 `trades.signal_id == candidates.candidate_id`。
    任一处不唯一 ⇒ 该处为空并附 `missing_reason`，**不按行号/顺序补**。
    """
    doc, err = _require_done(run_id)
    if err:
        return err
    facts = _facts(run_id)
    t = facts.trade_by_id(trade_id)
    if not t:
        known = [r.get("trade_id") for r in facts.trade_rows if r.get("trade_id")]
        return _envelope(
            run_id=run_id,
            missing_reason=(f"未知 trade_id：{trade_id}"
                            + ("" if known else "；本 run 账本里没有可唯一确定的 trade_id")),
            known_trade_ids=known), 404
    orders = facts.orders_of(trade_id)
    funding = facts.funding_of(trade_id)
    sig_id = t.get("signal_id")
    sig = facts.signal_by_id.get(str(sig_id)) if sig_id else None
    cross = facts.funding_cross_check(trade_id)
    missing: List[str] = []
    if t.get("linked_order_count") != 2:
        missing.append("开仓/平仓订单未能各关联到 1 条 ⇒ 订单明细不完整（不补）")
    if not sig:
        missing.append("原始候选信号未在 candidates.jsonl 中按 signal_id 命中 ⇒ 返回缺失")
    if funding and not cross.get("match"):
        missing.append("资金费事件归属与账本 funding_fee 交叉校验不一致 ⇒ 见 funding_cross_check")
    if not funding:
        missing.append("本笔无归属资金费事件（账本中 0 条，或时间窗归属不唯一）")
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     trade=t, orders=orders, funding=funding,
                     original_signal=sig,
                     signal_id=sig_id,
                     funding_cross_check=cross,
                     linked_order_count=t.get("linked_order_count"),
                     trade_id_source=t.get("trade_id_source"),
                     missing=list(missing),
                     link_notes=list(facts.notes),
                     items=[t], total=1), 200


def _generate_fallback_chart(run_id: str, doc: dict) -> dict:
    from WyckoffMaster.event_trend_v0_1 import EventTrendRunner
    import json
    symbol = ((doc.get("spec") or {}).get("symbols") or ["BTCUSDT"])[0]
    runner = EventTrendRunner()
    df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events = runner.load_symbol_data(
        symbol=symbol, start_date="2024-01-01", end_date="2024-06-30"
    )
    candles = [
        {"t": int(r.open_time), "o": float(r.open), "h": float(r.high),
         "l": float(r.low), "c": float(r.close), "v": float(r.volume)}
        for r in df_15m.itertuples(index=False)
    ]
    d = S.run_dir(run_id)
    # 真实 trade_id 一律取账本（positions.jsonl 的 POSITION_CLOSED）——**不按行号猜配**。
    try:
        _f = _facts(run_id)
    except Exception:                                      # noqa: BLE001
        _f = None
    chart_trades = []
    trades_file = d / "trades.jsonl"
    if trades_file.exists():
        for tid_idx, line in enumerate(trades_file.read_text(encoding="utf-8").splitlines()):
            if not line.strip(): continue
            try:
                t = json.loads(line)
                _et = int(t.get("entry_time") or t.get("entry_time_ms") or 0)
                _xt = int(t.get("exit_time") or t.get("exit_time_ms") or 0)
                _tid = None
                if _f is not None:
                    _row = next((r for r in _f.trade_rows
                                 if r.get("entry_time_ms") == _et and r.get("exit_time_ms") == _xt),
                                None)
                    _tid = (_row or {}).get("trade_id")
                chart_trades.append({
                    "trade_id": _tid,
                    "symbol": symbol,
                    "direction": "long",
                    "signal_type": 1,
                    "entry": {"t": _et,
                              "price": float(t.get("entry_price") or 0.0),
                              "qty": float(t.get("qty") or 0.0)},
                    "exit": {"t": _xt,
                             "price": float(t.get("exit_price") or 0.0),
                             "reason": str(t.get("exit_reason") or "")},
                    "net_pnl": float(t.get("net_pnl") or 0.0),
                    "initial_stop": float(t.get("initial_stop") or 0.0),
                    "stop_trace": [{"t": int(x.get("t", x.get("ts", 0))),
                                    "stop": float(x.get("stop", 0)),
                                    "reason": str(x.get("reason", ""))}
                                   for x in t.get("stop_trace", [])],
                })
            except Exception: pass

    chart_equity = []
    eq_file = d / "equity_curve.jsonl"
    init_eq = float((doc.get("spec") or {}).get("initial_equity") or 2000.0)
    if eq_file.exists():
        for line in eq_file.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try:
                e = json.loads(line)
                chart_equity.append({
                    "t": int(e.get("timestamp") or e.get("t") or 0),
                    "equity": float(e.get("total_equity") or e.get("equity") or init_eq),
                    "cash": float(e.get("cash_equity") or e.get("cash") or init_eq),
                    "unrealized": float(e.get("unrealized_pnl") or 0.0),
                })
            except Exception: pass

    open_0 = float(df_15m['open'].iloc[0]) if len(df_15m) else 1.0
    bench_series = [
        {"t": int(r.open_time), "equity": float(init_eq * (float(r.close) / open_0))}
        for r in df_15m.iloc[::4].itertuples(index=False)
    ]

    chart = {
        "symbol": symbol,
        "exec_timeframe": "15m",
        "window": {
            "start_ms": int(df_1h["timestamp"].iloc[0]),
            "end_ms": int(df_1h["timestamp"].iloc[-1]) + 3600 * 1000,
        },
        "candles": candles,
        "signals": [],
        "trades": chart_trades,
        "equity_curve": chart_equity,
        "funding_events": [],
        "benchmark": {"symbol": symbol, "return_pct": 0.0, "equity_series": bench_series}
    }
    try:
        S.write_chart(run_id, chart)
    except Exception:
        pass
    return chart


def _read_jsonl(p) -> list:
    """逐行 JSON 读取；单行坏掉只跳过该行，不因一行毁掉整个载荷。"""
    import json as _j
    out = []
    try:
        if not p.exists():
            return out
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(_j.loads(line))
                except Exception:                          # noqa: BLE001
                    continue
    except Exception:                                      # noqa: BLE001
        return []
    return out


def _candle_tuple(x):
    """K 线两种落盘格式归一：数组 `[t,o,h,l,c,v]` 或对象 `{t,o,h,l,c,v}`。"""
    if isinstance(x, (list, tuple)) and len(x) >= 5:
        return x
    if isinstance(x, dict):
        return [x.get("t"), x.get("o"), x.get("h"), x.get("l"), x.get("c"), x.get("v")]
    return None


def _backfill_chart(run_id: str, chart: dict) -> dict:
    """把 chart.json 里**被批量脚本写死为空**的字段，用本 run 自己的真实落盘文件补齐。

    背景（2026-10-17 查实）：项目根目录的 7 个 `run_batch_*.py` / `run_*_batch_test.py`
    自己拼 chart.json，把 `signals` 写成 `[]`、`funding_events` 写成 `[]`、
    `benchmark.return_pct` 写成 `0.0`、`trade_id` 直通 `null`（实测 344 个 run 无一例外）。
    而这些字段的真实数据其实都在**同一目录**的 `candidates.jsonl` / `funding.jsonl` /
    `trades.jsonl` / `equity_curve.jsonl` 里。

    本函数**只做补全，不做任何推算**：
    - 每个值都取自本 run 自己的落盘文件；
    - 取不到就保持原样（宁可空着，不造数）；
    - `candles` / `equity_curve` 一律不动；
    - 只覆盖「现状无效」的字段（空数组 / `t<=0` / `trade_id` 为空 / `return_pct` 为 0）。

    返回同一个 dict（就地补全）。
    """
    if not isinstance(chart, dict):
        return chart
    try:
        d = S.run_dir(run_id)
    except Exception:                                      # noqa: BLE001
        return chart

    # ① 信号（箱体突破候选）—— 源：candidates.jsonl
    cur_sig = chart.get("signals") or []
    valid_sig = [s for s in cur_sig
                 if isinstance(s, dict) and isinstance(s.get("t"), (int, float)) and s.get("t") > 0]
    if not valid_sig:
        sig = []
        for c in _read_jsonl(d / "candidates.jsonl"):
            t_ms = c.get("available_at_ms") or c.get("snapshot_time_ms") or c.get("execution_earliest_ms")
            try:
                t_ms = int(t_ms)
            except (TypeError, ValueError):
                continue
            if t_ms <= 0:
                continue
            atr = c.get("box_atr_a")
            oi_g = c.get("oi_growth_6h")
            sig.append({
                "t": t_ms,
                "kind": ("box_breakout" if c.get("breakout_close") is not None else "candidate"),
                "direction": "long",
                "signal_id": c.get("candidate_id"),
                "stop_price": (float(c["initial_stop_price"])
                               if c.get("initial_stop_price") is not None else None),
                "atr": float(atr) if atr is not None else None,
                "oi_score": float(oi_g) if oi_g is not None else None,
                "oi_available": oi_g is not None,
                "oi_quality": ("real_5m" if oi_g is not None else None),
            })
        if sig:
            chart["signals"] = sig

    # ② 逐笔 trade_id —— 源：**账本真实标识**（positions.jsonl 的
    #    POSITION_CLOSED.payload.trade_id，按 (symbol, exit_time) 对齐）。
    #    ⚠ 这里**绝不**再用 `f"trd_{i+1}"`：那是按行号猜配，会让「图上选中的那一笔」
    #    与「账本里真正的那一笔」失去可核对性（`trd_1` 与真实 `trd_<16hex>` 对不上）。
    try:
        facts = _facts(run_id)
    except Exception:                                      # noqa: BLE001
        facts = None
    if facts is not None:
        by_key: Dict[Tuple[int, int], str] = {}
        for r in facts.trade_rows:
            et, xt, tid = r.get("entry_time_ms"), r.get("exit_time_ms"), r.get("trade_id")
            if et is None or xt is None or not tid:
                continue
            by_key[(int(et), int(xt))] = tid
        for t in (chart.get("trades") or []):
            if not isinstance(t, dict) or t.get("trade_id"):
                continue
            try:
                k = (int((t.get("entry") or {}).get("t") or 0),
                     int((t.get("exit") or {}).get("t") or 0))
            except (TypeError, ValueError):
                continue
            tid = by_key.get(k)
            if tid:
                t["trade_id"] = tid

    # ②b 止损轨迹字段契约统一：官方 runner 落 `t`，批量脚本落 `ts`。
    #     这里**只补 `t`、不改原值**（补不出 `t` 的点保持原样），保证前端按官方契约能画。
    #     注意：账本只有 1 个 `init` 点时，本函数**不合成任何后续轨迹点**。
    for t in (chart.get("trades") or []):
        if not isinstance(t, dict):
            continue
        for p in (t.get("stop_trace") or []):
            if not isinstance(p, dict):
                continue
            if not isinstance(p.get("t"), (int, float)) and isinstance(p.get("ts"), (int, float)):
                p["t"] = int(p["ts"])
    if chart.get("stop_trace_source") is None and (chart.get("trades") or []):
        chart["stop_trace_source"] = (
            "ledger:trades.jsonl.stop_trace（官方 `t` 毫秒；无后续调整时只有 1 个 init 点，不合成）")

    # ③ 买入持有基准收益率 —— 源：本 run 自己的基准净值序列（缺则退回 K 线首开→末收）
    bench = chart.get("benchmark")
    if isinstance(bench, dict) and not bench.get("return_pct"):
        series = bench.get("equity_series") or []
        eq0 = None
        for e in (chart.get("equity_curve") or []):
            if isinstance(e, dict) and e.get("equity") is not None:
                eq0 = e.get("equity")
                break
        try:
            if series and eq0:
                last_eq = float(series[-1].get("equity"))
                base = float(eq0)
                if base:
                    bench["return_pct"] = (last_eq - base) / base * 100.0
            else:
                cc = [_candle_tuple(x) for x in (chart.get("candles") or [])]
                cc = [x for x in cc if x and x[1] is not None and x[4] is not None]
                if cc:
                    o0, cl = float(cc[0][1]), float(cc[-1][4])
                    if o0:
                        bench["return_pct"] = (cl - o0) / o0 * 100.0
        except (TypeError, ValueError, IndexError):
            pass

    # ④ 资金费事件 —— 源：funding.jsonl（本 run 只落了 delta；rate/mark 未落盘 ⇒ 保持 None）
    #    `trade_id`：funding 事件**本身不带 trade 标识** ⇒ 走时间窗**唯一**归属；
    #    归属不唯一/在持仓之外 ⇒ None（`trade_link` 写明原因），**不按顺序套用第 N 笔**。
    if not (chart.get("funding_events") or []):
        fe = []
        if facts is not None:
            for f in facts.funding_rows:
                t_ms = f.get("funding_time_ms")
                if not isinstance(t_ms, (int, float)) or t_ms <= 0:
                    continue
                fe.append({"t": int(t_ms), "rate": f.get("rate"), "mark": f.get("mark"),
                           "amount": f.get("amount"), "trade_id": f.get("trade_id"),
                           "trade_link": f.get("trade_link")})
        if fe:
            chart["funding_events"] = fe

    return chart


def get_chart(run_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    try:
        chart = S.read_chart(run_id)
    except S.RunNotFound:
        try:
            chart = _generate_fallback_chart(run_id, doc)
        except Exception:
            return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                             missing_reason="chart.json 缺失（不伪造图表）"), 200
    # 读取侧补全：修掉批量脚本写死的空字段（只取自本 run 自己的落盘文件，不推算）
    chart = _backfill_chart(run_id, chart)
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     chart=chart), 200


# ══════════════════════════ 创建 ══════════════════════════

def _spec_hash(clean_spec: dict) -> str:
    import hashlib
    import json as _j
    return hashlib.sha256(
        _j.dumps(clean_spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _run_in_thread(run_id: str) -> None:
    try:
        RUN.execute_run(run_id)
    finally:
        with _THREADS_LOCK:
            _THREADS.pop(run_id, None)


def _runnable_gate() -> Optional[str]:
    """**策略可运行性硬闸**（WORK-04）：返回拒绝原因，或 `None`（放行）。

    唯一可运行策略是 `WX_OI_RESONANCE_V1`；旧 3a–3d / A / B 已退役。
    注册表不可用时不静默放行 —— 返回原因并拒绝创建。
    """
    try:
        from strategies import registry as _reg
        return _reg.guard_runnable(BR.STRATEGY_ID)
    except Exception as exc:                              # noqa: BLE001
        return f"策略注册表不可用：{type(exc).__name__}: {exc}"


def create_run(payload: Optional[dict]) -> Tuple[dict, int]:
    """创建研究回测（后台执行）。返回 202 + run_id。"""
    if not BR.AVAILABLE:
        return _envelope(missing_reason=f"候选策略包不可用：{BR.IMPORT_ERROR}"), 503
    # ── WORK-04：可运行性硬闸（旧策略 / 未知键一律拒绝，不回退任何旧执行通道）──
    gate_err = _runnable_gate()
    if gate_err:
        return _envelope(missing_reason=f"策略不可运行：{gate_err}"), 409
    clean, errs = BR.validate_run_spec(payload)
    if errs:
        return _envelope(missing_reason="运行规格校验失败", errors=errs), 400

    sh = _spec_hash(clean)
    run_id = S.new_run_id(sh)
    # 落盘请求覆盖后实际生效的参数
    cfg_actual = {
        "params": clean.get("params", {}),
        "actual_config": {
            "initial_equity": float(clean.get("initial_equity", 2000.0)),
            "risk_budget_per_trade": float(clean.get("params", {}).get("risk_budget_per_trade", 0.005)),
            "taker_fee_bps": float(clean.get("params", {}).get("taker_fee_bps", 5.0)),
            "slippage_bps": float(clean.get("params", {}).get("slippage_bps", 3.0)),
        }
    }
    doc = {
        "run_id": run_id, "strategy_id": BR.STRATEGY_ID, "status": S.STATUS_QUEUED,
        "spec": clean, "spec_sha256": sh,
        "config": cfg_actual,
        "engine": BR.version_block(),
        "contract_version": CONTRACT_VERSION,
        "requested_at": S.utc_now_iso(),
        "live_order_enabled": False,
        "note": "WX_EVENT_TREND_V0_1 真实数据低频事件回测；2000U 本金",
    }
    try:
        S.create_run(run_id, doc)
    except S.RunExists as exc:
        return _envelope(missing_reason=str(exc)), 409

    th = threading.Thread(target=_run_in_thread, args=(run_id,),
                          name=f"bt-{run_id}", daemon=True)
    with _THREADS_LOCK:
        _THREADS[run_id] = th
    th.start()
    return _envelope(run_id=run_id, status=S.STATUS_QUEUED, items=[_summary(doc)],
                     total=1,
                     note="已受理，后台执行；用 GET /api/v1/backtests/{id} 轮询状态"), 202


# ══════════════════════════ 路由 ══════════════════════════

_SUBRES = ("metrics", "equity", "trades", "chart", "quality")


def route(seg: List[str], params: Optional[dict] = None) -> Tuple[dict, int]:
    """`GET /api/v1/backtests[...]`（`seg` = path 去掉 `api/v1/backtests` 前缀后的段）。"""
    if not seg:
        return list_experiments(params), 200
    rid = seg[0]
    if len(seg) == 1:
        return get_run(rid)
    sub = seg[1]
    if sub == "metrics":
        return get_metrics(rid)
    if sub == "equity":
        return get_equity(rid)
    if sub == "chart":
        return get_chart(rid)
    if sub == "quality":
        return get_quality(rid)
    if sub == "orders":
        return get_orders(rid)
    if sub == "rejected":
        return get_rejected(rid)
    if sub == "trades":
        if len(seg) == 2:
            return get_trades(rid)
        if len(seg) == 3:
            return get_trade(rid, seg[2])
    return _envelope(missing_reason=f"未知子资源：{'/'.join(seg)}"), 404


def route_post(path: str, payload: Optional[dict]) -> Optional[Tuple[dict, int]]:
    """`POST` 分派：命中返回 `(body, status)`，未命中返回 `None`（交给其它写入口）。"""
    if path == "/api/v1/backtests":
        return create_run(payload)
    return None


def startup() -> List[str]:
    """服务启动钩子：把遗留 `queued|running` 标 `interrupted`。"""
    return S.mark_orphans_interrupted()


__all__ = [
    "CONTRACT_VERSION", "list_experiments", "get_run", "get_metrics", "get_equity",
    "get_trades", "get_trade", "get_orders", "get_rejected", "get_chart", "get_quality",
    "create_run", "route", "route_post", "startup",
]
