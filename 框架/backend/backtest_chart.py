# -*- coding: utf-8 -*-
"""回测图表载荷的**唯一序列化契约**（写入侧）。

为什么需要它（2026-10-17 查实）
==============================
项目里有**两套** chart.json 写法：

1. `backtest_runner._run_event_trend()`（官方通道）；
2. 项目根 **7 个批量脚本**（`run_batch_round3_50/4_all_remain/5_100/6_100.py`、
   `run_clean_batch_test.py`、`run_expand_batch_test.py`、`run_random_50_batch_test.py`）
   各自复制了一份 `generate_chart_payload()`。

两份都各自写死了一批字段，落盘结果**互相不一致**（实测 375/375 个 run 无一例外）：

    signals: []                     ← 真实候选在 candidates.jsonl 里
    funding_events: []              ← 真实资金费在 funding.jsonl 里
    benchmark.return_pct: 0.0       ← 真实基准序列就在同一个 chart 里
    trades[].trade_id: null         ← 真实标识在 positions.jsonl 的 POSITION_CLOSED
    trades[].stop_trace: [{ts:…}]   ← 官方契约是 `t`，前端按 `t` 读 ⇒ 画不出
    candles: [[t,o,h,l,c,v], …]     ← 官方契约是对象 `{t,o,h,l,c,v}`
    json.dump(..., 无 indent)       ← 官方 `_write_json` 是 indent=2

本模块把这套写法**收敛到一处**，两侧都调它。契约（与前端 `LabChart` 一致）：

    candles         [{"t","o","h","l","c","v"}]            # 对象，不是数组元组
    signals         [{"t","kind","direction","signal_id","stop_price","atr",
                      "oi_score","oi_available","oi_quality"}]
    trades          [{"trade_id","symbol","direction","signal_type",
                      "entry":{"t","price","qty"},
                      "exit":{"t","price","reason"},
                      "stop_trace":[{"t","stop","reason"}], "net_pnl"}]
    equity_curve    [{"t","equity","cash","unrealized"}]
    funding_events  [{"t","rate","mark","amount","trade_id","trade_link"}]
    benchmark       {"symbol","return_pct","price_only_return_pct",
                     "net_return_pct","cost_model","equity_series":[{"t","equity"}]}

纪律
────
· **只搬运，不推算**：每个值都取自本 run 自己的落盘文件（或调用方传入的真实 K 线）。
· **不猜标识**：`trade_id` 只来自 `positions.jsonl` 的 `POSITION_CLOSED.payload.trade_id`；
  取不到就是 `None`，**绝不用 `trd_{i+1}` 按行号补**。
· **缺就是缺**：`rate`/`mark` 没落盘就写 `None`，不写 `0.0` 冒充。
· 基准序列是**纯价格**口径 ⇒ `net_return_pct=None` + `cost_model` 明写，不冒充净收益。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

CANDLE_KEYS = ("t", "o", "h", "l", "c", "v")

# 与 `backtest_store._write_json` 保持一致（indent=2 + default=str），
# 使「磁盘上只有一种 chart 格式」。
_JSON_KW = dict(ensure_ascii=False, indent=2, default=str)

PRICE_ONLY_COST_MODEL = "price_only（纯价格：不含手续费/滑点/资金费）"

__all__ = [
    "CANDLE_KEYS", "PRICE_ONLY_COST_MODEL",
    "chart_dumps", "write_chart_json", "normalize_candle", "candles_from_frame",
    "normalize_stop_trace", "trades_from_run_dir", "signals_from_run_dir",
    "funding_events_from_run_dir", "equity_from_run_dir", "benchmark_from_frame",
    "build_chart", "generate_chart_payload",
]


# ── 序列化 ────────────────────────────────────────────────────────────────

def chart_dumps(chart: Dict[str, Any]) -> str:
    """唯一的 chart 序列化入口（与后端 `_write_json` 同格式）。"""
    return json.dumps(chart, **_JSON_KW)


def write_chart_json(run_dir, chart: Dict[str, Any]) -> Path:
    """原子写入 `<run_dir>/chart.json`（唯一格式）。"""
    p = Path(run_dir) / "chart.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".chart.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(chart_dumps(chart))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


# ── K 线 ──────────────────────────────────────────────────────────────────

def normalize_candle(x: Any) -> Optional[Dict[str, float]]:
    """把数组元组 `[t,o,h,l,c,v]` 或对象 `{t,o,h,l,c,v}` 统一成**对象**。"""
    if isinstance(x, dict):
        if x.get("t") is None:
            return None
        return {k: x.get(k) for k in CANDLE_KEYS}
    if isinstance(x, (list, tuple)) and len(x) >= 6:
        return dict(zip(CANDLE_KEYS, x[:6]))
    return None


def candles_from_frame(df_15m) -> List[Dict[str, float]]:
    """15m DataFrame → 官方契约的 K 线对象列表。"""
    out: List[Dict[str, float]] = []
    if df_15m is None or len(df_15m) == 0:
        return out
    for r in df_15m.itertuples(index=False):
        out.append({"t": int(r.open_time), "o": float(r.open), "h": float(r.high),
                    "l": float(r.low), "c": float(r.close), "v": float(r.volume)})
    return out


# ── 止损轨迹 ──────────────────────────────────────────────────────────────

def normalize_stop_trace(points: Iterable[Any]) -> List[Dict[str, Any]]:
    """止损点统一成官方契约 `{"t","stop","reason"}`（`ts` → `t`；两者都没有则丢弃）。"""
    out: List[Dict[str, Any]] = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        raw = p.get("t", p.get("ts"))
        if raw is None:
            continue
        try:
            t = int(raw)
        except (TypeError, ValueError):
            continue
        out.append({"t": t, "stop": p.get("stop"), "reason": p.get("reason")})
    return out


# ── 逐笔 ──────────────────────────────────────────────────────────────────

def _facts(run_dir, run_id: Optional[str]):
    try:
        import ledger_links as LL
        return LL.LedgerFacts(Path(run_dir), run_id or Path(run_dir).name)
    except Exception:                                       # noqa: BLE001
        return None


def trades_from_run_dir(run_dir, symbol: str, run_id: Optional[str] = None) -> List[dict]:
    """逐笔列表（真实 `trade_id` + 契约化的 `stop_trace`）。"""
    facts = _facts(run_dir, run_id)
    out: List[dict] = []
    if facts is not None and facts.trade_rows:
        for r in facts.trade_rows:
            out.append({
                "trade_id": r.get("trade_id"),
                "symbol": r.get("symbol") or symbol,
                "direction": "long",
                "signal_type": r.get("signal_type") or 1,
                "entry": {"t": r.get("entry_time_ms"),
                          "price": r.get("entry_price"),
                          "qty": r.get("qty")},
                "exit": {"t": r.get("exit_time_ms"),
                         "price": r.get("exit_price"),
                         "reason": r.get("exit_reason")},
                "stop_trace": [{"t": p["t"], "stop": p["stop"], "reason": p["reason"]}
                               for p in (r.get("stop_trace") or [])],
                "net_pnl": r.get("net_pnl"),
                "holding_hours": r.get("holding_hours"),
                "funding_events": r.get("funding_events"),
                "exit_reason": r.get("exit_reason"),
            })
        return out
    # 退化路径：账本不可读时，只搬 trades.jsonl 原字段（不补 trade_id）
    try:
        import ledger_links as LL
        rows = LL.read_jsonl(Path(run_dir) / "trades.jsonl")
    except Exception:                                       # noqa: BLE001
        rows = []
    for t in rows:
        out.append({
            "trade_id": t.get("trade_id"),
            "symbol": symbol,
            "direction": "long",
            "signal_type": t.get("signal_type") or 1,
            "entry": {"t": int(t.get("entry_time") or 0),
                      "price": t.get("entry_price"), "qty": t.get("qty")},
            "exit": {"t": int(t.get("exit_time") or 0),
                     "price": t.get("exit_price"), "reason": t.get("exit_reason")},
            "stop_trace": normalize_stop_trace(t.get("stop_trace")),
            "net_pnl": t.get("net_pnl"),
            "exit_reason": t.get("exit_reason"),
        })
    return out


# ── 信号 ──────────────────────────────────────────────────────────────────

def signals_from_run_dir(run_dir) -> List[dict]:
    """候选信号（箱体突破候选）→ 官方契约。`t` 取候选的**可用时刻**。"""
    try:
        import ledger_links as LL
        rows = LL.read_jsonl(Path(run_dir) / "candidates.jsonl")
    except Exception:                                       # noqa: BLE001
        return []
    out: List[dict] = []
    for c in rows:
        t_ms = (c.get("available_at_ms") or c.get("snapshot_time_ms")
                or c.get("execution_earliest_ms"))
        try:
            t_ms = int(t_ms)
        except (TypeError, ValueError):
            continue
        if t_ms <= 0:
            continue
        atr = c.get("box_atr_a")
        oi_g = c.get("oi_growth_6h")
        out.append({
            "t": t_ms,
            "kind": ("box_breakout" if c.get("breakout_close") is not None else "candidate"),
            "direction": "long",
            "signal_id": c.get("candidate_id"),
            "stop_price": (float(c["initial_stop_price"])
                           if c.get("initial_stop_price") is not None else None),
            "atr": (float(atr) if atr is not None else None),
            "oi_score": (float(oi_g) if oi_g is not None else None),
            "oi_available": oi_g is not None,
            "oi_quality": ("real_5m" if oi_g is not None else None),
        })
    return out


# ── 资金费 ────────────────────────────────────────────────────────────────

def funding_events_from_run_dir(run_dir, run_id: Optional[str] = None) -> List[dict]:
    """资金费事件（真实 `delta` → `amount`；`trade_id` 走时间窗唯一归属）。"""
    facts = _facts(run_dir, run_id)
    out: List[dict] = []
    if facts is not None:
        for f in facts.funding_rows:
            t_ms = f.get("funding_time_ms")
            if not isinstance(t_ms, (int, float)) or t_ms <= 0:
                continue
            out.append({"t": int(t_ms), "rate": f.get("rate"), "mark": f.get("mark"),
                        "amount": f.get("amount"), "trade_id": f.get("trade_id"),
                        "trade_link": f.get("trade_link")})
        return out
    try:
        import ledger_links as LL
        for e in LL.read_jsonl(Path(run_dir) / "funding.jsonl"):
            try:
                t_ms = int(e.get("event_time") or 0)
            except (TypeError, ValueError):
                continue
            if t_ms <= 0:
                continue
            pl = e.get("payload") or {}
            out.append({"t": t_ms, "rate": None, "mark": None,
                        "amount": (float(pl["delta"]) if pl.get("delta") is not None else None),
                        "trade_id": None, "trade_link": "unresolved"})
    except Exception:                                       # noqa: BLE001
        return []
    return out


# ── 权益 / 基准 ───────────────────────────────────────────────────────────

def equity_from_run_dir(run_dir) -> List[dict]:
    try:
        import ledger_links as LL
        rows = LL.read_jsonl(Path(run_dir) / "equity_curve.jsonl")
    except Exception:                                       # noqa: BLE001
        return []
    out: List[dict] = []
    for e in rows:
        try:
            out.append({"t": int(e.get("timestamp") or e.get("t") or 0),
                        "equity": float(e.get("total_equity") or e.get("equity")),
                        "cash": float(e.get("cash_equity") or e.get("cash") or 0.0),
                        "unrealized": float(e.get("unrealized_pnl") or 0.0)})
        except (TypeError, ValueError):
            continue
    return out


def benchmark_from_frame(df_15m, initial_equity: float, symbol: str) -> dict:
    """买入持有基准**逐点净值**（纯价格口径）。

    口径必须明写：序列 = `initial_equity × close_i / open_0`，**不含**手续费/滑点/资金费
    ⇒ `net_return_pct=None`、`cost_model=price_only`，避免被当成「同成本基准」比较。
    """
    series: List[dict] = []
    price_only_ret: Optional[float] = None
    if df_15m is not None and len(df_15m) > 0:
        open_0 = float(df_15m["open"].iloc[0])
        if open_0:
            series = [{"t": int(r.open_time),
                       "equity": float(initial_equity * (float(r.close) / open_0))}
                      for r in df_15m.iloc[::4].itertuples(index=False)]
            close_n = float(df_15m["close"].iloc[-1])
            price_only_ret = (close_n - open_0) / open_0 * 100.0
    return {
        "symbol": symbol,
        "return_pct": price_only_ret,
        "price_only_return_pct": price_only_ret,
        "net_return_pct": None,
        "cost_model": PRICE_ONLY_COST_MODEL,
        "equity_series": series,
    }


# ── 组装 ──────────────────────────────────────────────────────────────────

def build_chart(*, symbol: str, run_dir, candles: List[dict],
                window_start_ms: int, window_end_ms: int,
                initial_equity: float, run_id: Optional[str] = None,
                benchmark: Optional[dict] = None,
                df_15m=None) -> Dict[str, Any]:
    """按**唯一契约**组装 chart 载荷（只搬运，不推算）。"""
    bm = benchmark if benchmark is not None else benchmark_from_frame(
        df_15m, initial_equity, symbol)
    return {
        "symbol": symbol,
        "exec_timeframe": "15m",
        "window": {"start_ms": int(window_start_ms), "end_ms": int(window_end_ms)},
        "candles": [normalize_candle(c) for c in (candles or []) if normalize_candle(c)],
        "signals": signals_from_run_dir(run_dir),
        "trades": trades_from_run_dir(run_dir, symbol, run_id),
        "equity_curve": equity_from_run_dir(run_dir),
        "funding_events": funding_events_from_run_dir(run_dir, run_id),
        "benchmark": bm,
        "stop_trace_source": ("ledger:trades.jsonl.stop_trace（官方 `t` 毫秒；"
                              "无后续调整时只有 1 个 init 点，不合成）"),
    }


def generate_chart_payload(symbol: str, run_dir, df_1h, df_15m,
                           initial_equity: float) -> Dict[str, Any]:
    """批量脚本的统一入口（签名与 7 个脚本原函数一致，直接替换即可）。"""
    run_id = Path(run_dir).name
    try:
        ws = int(df_1h["timestamp"].iloc[0])
        we = int(df_1h["timestamp"].iloc[-1]) + 3600 * 1000
    except Exception:                                       # noqa: BLE001
        ws = we = 0
    chart = build_chart(
        symbol=symbol, run_dir=run_dir, candles=candles_from_frame(df_15m),
        window_start_ms=ws, window_end_ms=we,
        initial_equity=float(initial_equity), run_id=run_id, df_15m=df_15m)
    write_chart_json(run_dir, chart)
    return chart
