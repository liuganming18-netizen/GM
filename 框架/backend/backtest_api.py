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
`GET  /api/v1/backtests/{id}/chart`      图表载荷（真实 K 线/信号/开平仓/保护止损轨迹）
`GET  /api/v1/backtests/{id}/quality`    数据质量与对账

纪律
────
· **GET 一律不触发计算**（只读 `run.json` / `ledger.sqlite3`）。
· 仅 `WX_OI_RESONANCE_V1`；参数白名单；周期/交易所/市场白名单（当前仅 1h 信号 + 15m 执行）。
· 创建走**原子独占目录**，重名拒绝；任务在**后台线程**执行，状态持久化。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

import backtest_runner as RUN
import backtest_store as S
import oi_resonance_bridge as BR

CONTRACT_VERSION = "bt_api_v1"

_THREADS: Dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()


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
        limit = int((params.get("limit") or ["50"])[0])
    except (ValueError, TypeError):
        limit = 50
    runs = S.list_runs(limit=max(1, min(limit, 500)))
    items = [_summary(r) for r in runs]
    return _envelope(items=items, total=len(items),
                     runs_root=str(S.RUNS_ROOT),
                     registry_entry=_registry_entry(),
                     note="研究回测实验列表（只读；创建走 POST /api/v1/backtests）")


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
    return {
        "run_id": doc.get("run_id"), "status": doc.get("status"),
        "created_at": doc.get("created_at"), "started_at": doc.get("started_at"),
        "finished_at": doc.get("finished_at"),
        "strategy_id": (doc.get("spec") or {}).get("strategy_id") or doc.get("strategy_id"),
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
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     metrics=doc.get("metrics"),
                     benchmark_buy_hold=doc.get("benchmark_buy_hold"),
                     monthly_returns=doc.get("monthly_returns"),
                     formal_m4_gate=doc.get("formal_m4_gate"),
                     m4_disclaimer=doc.get("m4_disclaimer")), 200


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
        b = chart.get("benchmark") or {}
        bench_series = b.get("equity_series") or []
        if b:
            bench_meta = {k: v for k, v in b.items() if k != "equity_series"}
    except S.RunNotFound:
        bench_series, bench_meta = [], {"note": "chart.json 缺失 ⇒ 无基准序列（不伪造）"}
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     items=rows, total=len(rows),
                     benchmark=bench_meta, benchmark_equity=bench_series,
                     note=("策略权益曲线取自账本 equity 表；benchmark_equity 为**同成本**"
                           "买入持有基准逐点净值（同手续费/滑点/真实资金费）")), 200


def get_trades(run_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    rows = S.read_ledger_rows(run_id, "trades", order_by="entry_time_ms ASC")
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     items=rows, total=len(rows)), 200


def get_trade(run_id: str, trade_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    rows = [r for r in S.read_ledger_rows(run_id, "trades") if r["trade_id"] == trade_id]
    if not rows:
        return _envelope(run_id=run_id, missing_reason=f"未知 trade_id：{trade_id}"), 404
    t = rows[0]
    orders = [r for r in S.read_ledger_rows(run_id, "orders") if r.get("trade_id") == trade_id]
    funding = [r for r in S.read_ledger_rows(run_id, "funding") if r.get("trade_id") == trade_id]
    sig = ([r for r in S.read_ledger_rows(run_id, "signals")
            if r.get("signal_id") == t.get("signal_id")] if t.get("signal_id") else [])
    return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                     trade=t, orders=orders, funding=funding,
                     original_signal=(sig[0] if sig else None),
                     signal_id=t.get("signal_id"),
                     items=[t], total=1), 200


def get_chart(run_id: str) -> Tuple[dict, int]:
    doc, err = _require_done(run_id)
    if err:
        return err
    try:
        chart = S.read_chart(run_id)
    except S.RunNotFound:
        return _envelope(run_id=run_id, status=doc.get("status"), quality=_quality_of(doc),
                         missing_reason="chart.json 缺失（不伪造图表）"), 200
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


def create_run(payload: Optional[dict]) -> Tuple[dict, int]:
    """创建研究回测（后台执行）。返回 202 + run_id。"""
    if not BR.AVAILABLE:
        return _envelope(missing_reason=f"候选策略包不可用：{BR.IMPORT_ERROR}"), 503
    clean, errs = BR.validate_run_spec(payload)
    if errs:
        return _envelope(missing_reason="运行规格校验失败", errors=errs), 400

    sh = _spec_hash(clean)
    run_id = S.new_run_id(sh)
    # 落盘**请求覆盖后实际生效**的参数（不是冻结默认值）⇒ 与运行时/快照一致。
    try:
        sp_actual = BR.signal_params(**(clean.get("signal_overrides") or {}))
        ep_actual = BR.exec_params(**(clean.get("exec_overrides") or {}))
        cfg_actual = {"signal_params": sp_actual.identity(),
                      "exec_params": ep_actual.identity(),
                      "signal_params_hash": sp_actual.hash(),
                      "exec_params_hash": ep_actual.hash()}
    except Exception as exc:                              # noqa: BLE001
        return _envelope(missing_reason=f"参数构造失败：{type(exc).__name__}: {exc}"), 400
    doc = {
        "run_id": run_id, "strategy_id": BR.STRATEGY_ID, "status": S.STATUS_QUEUED,
        "spec": clean, "spec_sha256": sh,
        "config": cfg_actual,
        "engine": BR.version_block(),
        "contract_version": CONTRACT_VERSION,
        "requested_at": S.utc_now_iso(),
        "live_order_enabled": False,
        "note": "研究回测（非正式 M4）；预热不交易；真实 settled 资金费（毫秒口径）；不写 paper 账户",
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
    "get_trades", "get_trade", "get_chart", "get_quality", "create_run",
    "route", "route_post", "startup",
]
