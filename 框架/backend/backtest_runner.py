# -*- coding: utf-8 -*-
"""研究回测运行器：取数 → 组装 → **复用候选包唯一执行核心** → 落账 → 指标/基准/图表/质量。

职责边界
────────
· **只复用**：`MarketDataRepository.from_archive / candles / oi / funding`
  + 候选包 `execution.run_portfolio_execution`（唯一撮合/止损/资金费核心）。
· **不另写**撮合、止损、资金费、仓位大小；不换框架；不联网；不新下载。
· 只读 `market_v2.sqlite3`；只写 `runs/<run_id>/`（原子独占目录）。

口径（逐条对齐施工要求）
────────────────────────
· 时间一律 **UTC 半开区间** `[start, end)`；只用**已收盘**（is_closed=1）且**连续**的 bar。
· 预热段**单独不交易**：信号序列含预热（保证窗口起点指标已热），但
  `trade_start_sec` 之前的信号一律丢弃（候选包最小修复）。
· OI **按当时可得向后对齐**（as-of：bar 收盘时刻之前最近一条），缺 OI 如实标缺，**不伪造 0.5**。
· 资金费只认 **真实 settled 事件** `(rate, mark)`；缺字段记 `funding_gap`，**不补 0**。
· 手续费/滑点实际参与（滑点已含在成交价里，**不再重复扣**）；资金费用真实事件 rate×mark。
· 指标**从账本算**；并对每笔资金费做**独立逐事件重算**与引擎累计值对账，不等即失败。
· 正式 M4 门禁**不绕过**：本运行是**独立候选研究**，不得冒称 M4 已通过。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import backtest_store as S
import oi_resonance_bridge as BR
from market_repository import MarketDataRepository

HOUR_MS = 3_600_000
Q_MS = 900_000
DAY_MS = 86_400_000
INTERVAL_MS = {"1h": HOUR_MS, "15m": Q_MS}

CANDLE_PAGE = 2000                 # `MarketDataRepository.candles` 单次上限
CANDLE_HARD_CAP = 20000            # 分页总量上限（超出 ⇒ 拒绝，不静默截断）
FUNDING_LOOKBACK_MS = 7 * DAY_MS   # 资金费**频率**观测回看窗（用于判定结算覆盖）
FUNDING_GRID_TOL_MS = 60_000       # 网格时刻 vs 真实事件的匹配容差（funding_time 可能有毫秒抖动）

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_EXEC_LOCK = threading.Lock()


# ══════════════════════════ 工具 ══════════════════════════

def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _df_sha(df: pd.DataFrame, cols: List[str]) -> str:
    """DataFrame 指定列 → 规范化 CSV → SHA256（可人工复核、跨运行可比）。"""
    use = [c for c in cols if c in df.columns]
    csv = df[use].to_csv(index=False, float_format="%.10f", lineterminator="\n")
    return _sha_bytes(csv.encode("utf-8"))


def _df_canon_csv(df: pd.DataFrame, cols: List[str]) -> bytes:
    """与 `_df_sha` **同一**规范化 CSV 字节 ⇒ 落盘快照的文件哈希 == 记录哈希。"""
    use = [c for c in cols if c in df.columns]
    return df[use].to_csv(index=False, float_format="%.10f",
                          lineterminator="\n").encode("utf-8")


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_midnight_floor(ms: int) -> int:
    return (int(ms) // DAY_MS) * DAY_MS


def market_db_path() -> Path:
    return Path(os.environ.get(
        "MARKET_DB", str(PROJECT_ROOT / "数据" / "市场数据" / "db" / "market_v2.sqlite3")))


def archive_root() -> Path:
    return Path(os.environ.get(
        "MARKET_ARCHIVE", str(PROJECT_ROOT / "数据" / "市场数据" / "parquet")))


def _repo() -> MarketDataRepository:
    return MarketDataRepository.from_archive(str(market_db_path()), str(archive_root()))


# ══════════════════════════ 取数（分页取满，不静默截断）══════════════════════════

def _fetch_candles_all(repo: MarketDataRepository, ex: str, mk: str, sym: str, tf: str,
                       start_ms: int, end_ms: int) -> List[dict]:
    """分页取满半开区间 `[start_ms, end_ms)` 的**全部** K 线（升序、按 open_time 去重）。

    `repo.candles` 单次上限 2000 根且返回区间内**最新**的 limit 根 ⇒ 逆序翻页直到取尽。
    总量超过 `CANDLE_HARD_CAP` ⇒ 抛错（**拒绝静默截断**）。
    """
    start_ms, end_ms = int(start_ms), int(end_ms)
    pages: List[List[dict]] = []
    end = end_ms
    total = 0
    while True:
        rows = repo.candles(ex, mk, sym, tf, start=start_ms, end=end, limit=CANDLE_PAGE)
        if not rows:
            break
        pages.append(rows)
        total += len(rows)
        first = int(rows[0]["open_time"])
        if len(rows) < CANDLE_PAGE or first <= start_ms:
            break
        end = first                                   # 下一页取更早（end 为半开右端）
        if total > CANDLE_HARD_CAP:
            raise RuntimeError(
                f"K 线分页超过上限 {CANDLE_HARD_CAP}（{ex}/{mk}/{sym}/{tf} "
                f"{_iso(start_ms)}..{_iso(end_ms)}）⇒ 拒绝静默截断，请缩小窗口")
    merged: Dict[int, dict] = {}
    for rows in reversed(pages):                      # 早页在前
        for r in rows:
            merged[int(r["open_time"])] = r
    return [merged[k] for k in sorted(merged)]


def _last_closed_bar_close_ms(repo: MarketDataRepository, ex: str, mk: str, sym: str,
                              tf: str) -> Optional[int]:
    """该序列**最后一根已收盘** bar 的收盘时刻（毫秒）。无已收盘 bar ⇒ None。"""
    iv = INTERVAL_MS[tf]
    rows = repo.candles(ex, mk, sym, tf, start=0, end=None, limit=5)
    for r in reversed(rows):
        if int(r.get("is_closed") or 0):
            return int(r["open_time"]) + iv
    return None


# ══════════════════════════ 窗口解析 ══════════════════════════

def resolve_window(spec: dict, repo: MarketDataRepository) -> dict:
    """窗口解析：**显式 UTC 窗口优先**；未给则用近期 `window_days` 天。

    · 显式窗口（`spec["window"]["mode"]=="explicit"`）已在桥层校验（UTC 毫秒、半开、
      15m 网格、>=1 天）⇒ 此处**按原值使用**，**不静默替换**成「近期 7 天」。
    · 窗口必须落在**实际可得数据**之内：`win_end <= 末根已收盘 bar 收盘时刻`；
      超出 ⇒ 抛错（拒绝静默缩窗）。
    · 预热 = `win_start` 之前的 `warmup_bars` 根信号 bar（供指标/因子滚动，**不交易**）。
    """
    sym = spec["symbols"][0]
    ex, mk = spec["exchange"], spec["market"]
    sig_tf, ex_tf = spec["signal_timeframe"], spec["exec_timeframe"]
    iv_sig = INTERVAL_MS[sig_tf]

    last_sig_close = _last_closed_bar_close_ms(repo, ex, mk, sym, sig_tf)
    last_exec_close = _last_closed_bar_close_ms(repo, ex, mk, sym, ex_tf)
    if last_sig_close is None or last_exec_close is None:
        raise RuntimeError(f"数据缺失：{ex}/{mk}/{sym} {sig_tf}/{ex_tf} 无**已收盘** K 线")
    # 各序列**各自网格**的末端（信号序列按信号周期对齐、执行序列按执行周期对齐），
    # 避免用「两周期最小值」去卡某一周期而导致假的尾部不齐。
    sig_end_ms, exec_end_ms = int(last_sig_close), int(last_exec_close)
    data_end = min(sig_end_ms, exec_end_ms)

    win_spec = spec.get("window") or {}
    if win_spec.get("mode") == "explicit":
        win_start = int(win_spec["start_ms"])
        win_end = int(win_spec["end_ms"])
        win_source = "explicit_utc"
        days = (win_end - win_start) / DAY_MS
        if win_end > data_end:
            raise RuntimeError(
                f"显式窗口超出可用数据：win_end={_iso(win_end)} > 数据末端={_iso(data_end)}"
                "（拒绝静默缩窗；请改窗口或先补数据）")
    else:
        days = int(win_spec.get("window_days") or spec.get("window_days") or 7)
        win_end = _utc_midnight_floor(data_end)
        if win_end == data_end:                       # 恰在日界 ⇒ 用前一整日
            win_end -= DAY_MS
        win_start = win_end - days * DAY_MS
        win_source = "auto_recent_days"
        if win_start < 0:
            raise RuntimeError(f"自动窗口起点非法：{win_start}")

    # 预热**显式有界**（默认 240 根 ≈ 10 天）：既满足候选包 OI 因子 min_periods
    # （= max(20, bars_per_day×oi_rank_min_days) = 24×7 = 168），又使喂入范围确定可控。
    warmup_bars = int(spec.get("warmup_bars") or 240)
    sig_start_req = win_start - warmup_bars * iv_sig
    if sig_start_req < 0:
        raise RuntimeError(f"预热起点非法：{sig_start_req}")

    return {
        "symbol": sym, "exchange": ex, "market": mk,
        "signal_timeframe": sig_tf, "exec_timeframe": ex_tf,
        "window_source": win_source,
        "data_end_ms": data_end, "sig_end_ms": sig_end_ms, "exec_end_ms": exec_end_ms,
        "signal_series_start_ms": sig_start_req,
        "win_start_ms": win_start, "win_end_ms": win_end,
        "window_days": days, "warmup_bars_requested": warmup_bars,
        "warmup_bars": warmup_bars,
        "trade_start_sec": int(win_start // 1000),
        "convention": ("UTC 半开区间 [win_start, win_end)；预热段（warmup_bars 根信号 bar）"
                       "只喂数据不交易；bar 均须已收盘且连续、且**全窗覆盖**"),
    }


# ══════════════════════════ 取数与组装 ══════════════════════════

def _closed_contiguous(rows: List[dict], tf: str, label: str, *,
                       expect_start_ms: Optional[int] = None,
                       expect_end_ms: Optional[int] = None) -> dict:
    """校验：全部已收盘 + 时间戳严格等间隔连续 + **全窗覆盖**（首/末/期望根数）。

    只查「相邻差 == 间隔」是不够的：空序列、被 limit 截掉的**尾段**、整段缺失都不会被
    相邻差发现。故当给出 `expect_start_ms`/`expect_end_ms`（半开 `[start, end)`）时，
    额外判定：非空、首根 == start、末根 + iv == end、实际根数 == 期望根数。
    """
    iv = INTERVAL_MS[tf]
    ts = [int(r["open_time"]) for r in rows]
    unclosed = [int(r["open_time"]) for r in rows if not int(r.get("is_closed") or 0)]
    bad = [(ts[i], ts[i + 1]) for i in range(len(ts) - 1) if ts[i + 1] - ts[i] != iv]
    rep = {
        "label": label, "rows": len(rows), "interval_ms": iv,
        "first_ms": ts[0] if ts else None, "last_ms": ts[-1] if ts else None,
        "first_utc": _iso(ts[0]) if ts else None, "last_utc": _iso(ts[-1]) if ts else None,
        "n_unclosed": len(unclosed), "n_gaps": len(bad),
        "gap_samples": bad[:5], "contiguous": (len(bad) == 0), "all_closed": (len(unclosed) == 0),
    }
    if expect_start_ms is not None and expect_end_ms is not None:
        exp = (int(expect_end_ms) - int(expect_start_ms)) // iv
        rep.update({
            "expect_start_ms": int(expect_start_ms), "expect_end_ms": int(expect_end_ms),
            "expected_bars": int(exp), "empty": (len(ts) == 0),
            "head_ok": bool(ts) and ts[0] == int(expect_start_ms),
            "tail_ok": bool(ts) and (ts[-1] + iv) == int(expect_end_ms),
            "count_ok": (len(ts) == exp),
        })
        rep["full_window_ok"] = bool(rep["head_ok"] and rep["tail_ok"] and rep["count_ok"])
    return rep


def _funding_frequency(ts_sorted: List[int]) -> dict:
    """从**真实 settled 事件**观测该标的的资金费结算频率（不假设「所有合约都 8h」）。"""
    diffs = [b - a for a, b in zip(ts_sorted, ts_sorted[1:])]
    if not diffs:
        return {"interval_ms": None, "frequency": "unverified", "source": "no_events",
                "note": "无相邻事件 ⇒ 频率未经验证（**不假设 8h**）"}
    from collections import Counter
    c = Counter(int(round(d / 1000)) for d in diffs)
    mode_sec, cnt = c.most_common(1)[0]
    iv = mode_sec * 1000
    return {
        "interval_ms": int(iv), "interval_hours": iv / HOUR_MS,
        "frequency": f"{iv / HOUR_MS:g}h", "source": "observed_from_settled_events",
        "n_intervals": len(diffs), "mode_count": int(cnt),
        "distinct_intervals_sec": sorted(c)[:8],
        "note": "频率由该标的**真实结算事件间隔**的众数得到（不假设 8h）",
    }


def _expected_settlement_grid(ts_sorted: List[int], interval_ms: Optional[int],
                              win_start_ms: int, win_end_ms: int) -> Optional[List[int]]:
    """按观测频率推算窗口内**应有**结算的网格时刻（半开 `[start, end)`，**含左端点**）。

    纪律：锚点 = 回看窗内**最后一个 `<= win_start_ms` 的真实事件**；由它按观测间隔向前
    步进生成网格。**锚点自身若恰落在窗口起点（`anchor == win_start_ms`）必须计入**
    （半开区间左端点属于窗口）——旧实现从 `anchor+interval` 起数，漏算该笔，导致
    「应有次数」少 1，进而让 `actual >= expected` 恒真、掩盖真实缺口。
    未验证频率 / 无事件 ⇒ None（不假设 8h）。
    """
    if not interval_ms or not ts_sorted:
        return None
    anchor = None
    for t in ts_sorted:
        if t <= win_start_ms:
            anchor = t
        else:
            break
    if anchor is None:
        anchor = ts_sorted[0]
        while anchor - interval_ms >= win_start_ms:
            anchor -= interval_ms
    grid: List[int] = []
    t = anchor
    while t < win_end_ms:
        if win_start_ms <= t < win_end_ms:
            grid.append(int(t))
        t += interval_ms
    return grid


def _expected_settlements(ts_sorted: List[int], interval_ms: Optional[int],
                          win_start_ms: int, win_end_ms: int) -> Optional[int]:
    """窗口内**应有**结算次数（半开 `[start, end)`，含左端点）。未验证 ⇒ None。"""
    grid = _expected_settlement_grid(ts_sorted, interval_ms, win_start_ms, win_end_ms)
    return None if grid is None else int(len(grid))


def load_inputs(spec: dict, win: dict, repo: MarketDataRepository) -> dict:
    """取 1h/15m K 线、OI（5m）、资金费（settled，**保留原始毫秒**），并对齐 OI。

    · K 线**分页取满**全窗（不靠单次 limit 截断）。
    · 覆盖判定用**全窗期望/实际/首/末**（空窗或尾段被截 ⇒ 拒绝）。
    · 资金费事件**原样毫秒**（`time_ms`），不 `//1000` 截秒。
    · 资金费**频率**由真实事件观测（用于结算覆盖判定），不假设 8h。
    """
    sym, ex, mk = win["symbol"], win["exchange"], win["market"]
    sig_tf, ex_tf = win["signal_timeframe"], win["exec_timeframe"]
    iv_sig = INTERVAL_MS[sig_tf]

    sig_rows = _fetch_candles_all(repo, ex, mk, sym, sig_tf,
                                  win["signal_series_start_ms"], win["sig_end_ms"])
    exec_rows = _fetch_candles_all(repo, ex, mk, sym, ex_tf,
                                   win["win_start_ms"], win["win_end_ms"])
    sig_cov = _closed_contiguous(sig_rows, sig_tf, f"{sym}@{sig_tf} 信号序列(含预热)",
                                 expect_start_ms=win["signal_series_start_ms"],
                                 expect_end_ms=win["sig_end_ms"])
    exec_cov = _closed_contiguous(exec_rows, ex_tf, f"{sym}@{ex_tf} 执行序列(窗口)",
                                  expect_start_ms=win["win_start_ms"],
                                  expect_end_ms=win["win_end_ms"])
    if not sig_cov["full_window_ok"] or not sig_cov["contiguous"] or not sig_cov["all_closed"]:
        raise RuntimeError(f"信号序列不满足「已收盘 + 连续 + 全窗覆盖」：{sig_cov}")
    if not exec_cov["full_window_ok"] or not exec_cov["contiguous"] or not exec_cov["all_closed"]:
        raise RuntimeError(f"执行序列不满足「已收盘 + 连续 + 全窗覆盖」：{exec_cov}")

    # 实际预热根数（覆盖不足时如实记录；请求了就必须满足，不静默缩窗）
    actual_warm = int((win["win_start_ms"] - int(sig_cov["first_ms"])) // iv_sig)
    sig_cov["warmup_bars_requested"] = int(win["warmup_bars_requested"])
    sig_cov["warmup_bars_actual"] = actual_warm
    sig_cov["warmup_shortfall_bars"] = int(win["warmup_bars_requested"]) - actual_warm
    # 候选包 OI 因子 min_periods = min(window, max(20, bars_per_day×oi_rank_min_days))
    _p = BR.C.SIGNAL_FROZEN
    oi_min_periods = max(20, int(_p.bars_per_day) * int(_p.oi_rank_min_days))
    sig_cov["oi_factor_min_periods"] = oi_min_periods
    sig_cov["oi_factor_ready_at_win_start"] = bool(actual_warm + 1 >= oi_min_periods)
    need_warm = max(20, oi_min_periods)
    if actual_warm < need_warm:
        raise RuntimeError(
            f"预热不足（实际 {actual_warm} 根 < 需要 {need_warm} 根，"
            f"请求 {win['warmup_bars_requested']}）⇒ 窗口起点指标未热，拒绝运行（不静默缩窗）")
    win["warmup_bars"] = actual_warm

    oi_rows = repo.oi(ex, mk, sym, period="5m", start=win["signal_series_start_ms"],
                      end=win["sig_end_ms"], limit=20000)
    # 资金费：半开 [win_start, win_end)；另取一段回看窗用于**频率**观测
    fu_rows = repo.funding(ex, mk, sym, start=win["win_start_ms"] - FUNDING_LOOKBACK_MS,
                           end=win["win_end_ms"] - 1, limit=2000)
    fu_rows = [r for r in fu_rows if str(r.get("rate_type") or "") == "settled"]

    sig_df = pd.DataFrame({
        "timestamp": [int(r["open_time"]) for r in sig_rows],
        "open": [float(r["open"]) for r in sig_rows],
        "high": [float(r["high"]) for r in sig_rows],
        "low": [float(r["low"]) for r in sig_rows],
        "close": [float(r["close"]) for r in sig_rows],
        "volume": [float(r["volume"]) for r in sig_rows],
    })
    exec_df = pd.DataFrame({
        "timestamp": [int(r["open_time"]) for r in exec_rows],
        "open": [float(r["open"]) for r in exec_rows],
        "high": [float(r["high"]) for r in exec_rows],
        "low": [float(r["low"]) for r in exec_rows],
        "close": [float(r["close"]) for r in exec_rows],
        "volume": [float(r["volume"]) for r in exec_rows],
    })

    # ── OI 按当时可得向后对齐（as-of：bar 收盘时刻之前最近一条 5m OI）──
    oi_ts = np.array([int(r["ts"]) for r in oi_rows], dtype=np.int64)
    oi_val = np.array([float(r["open_interest"]) for r in oi_rows], dtype=np.float64)
    order = np.argsort(oi_ts, kind="stable")
    oi_ts, oi_val = oi_ts[order], oi_val[order]
    close_ms = sig_df["timestamp"].to_numpy(np.int64) + iv_sig
    aligned = np.full(len(sig_df), np.nan, dtype=np.float64)
    if len(oi_ts):
        pos = np.searchsorted(oi_ts, close_ms, side="right") - 1     # <= close 的最近一条
        ok = pos >= 0
        aligned[ok] = oi_val[pos[ok]]
    sig_df["open_interest"] = aligned
    oi_meta = {
        "source_period": "5m", "rows": int(len(oi_ts)),
        "first_ms": int(oi_ts[0]) if len(oi_ts) else None,
        "last_ms": int(oi_ts[-1]) if len(oi_ts) else None,
        "first_utc": _iso(int(oi_ts[0])) if len(oi_ts) else None,
        "last_utc": _iso(int(oi_ts[-1])) if len(oi_ts) else None,
        "align_rule": "as-of：bar 收盘时刻(open_time+tf) 之前最近一条 OI（ts <= close）",
        "bars": int(len(sig_df)),
        "bars_with_oi": int(np.isfinite(aligned).sum()),
        "bars_missing_oi": int((~np.isfinite(aligned)).sum()),
        "fake_fill": "无（缺 OI 记 NaN，不写 0 / 0.5）",
    }

    # ── 资金费事件：**保留原始毫秒**（不 //1000 截秒）──
    all_events: List[dict] = []
    for r in fu_rows:
        row = {"time_ms": int(r["funding_time"]), "rate_type": str(r.get("rate_type") or "")}
        try:
            row["rate"] = float(r["funding_rate"])
            row["mark_price"] = float(r["mark_price"])
        except (TypeError, ValueError):
            row["rate"] = None
            row["mark_price"] = None
        all_events.append(row)
    all_events.sort(key=lambda x: x["time_ms"])
    win_start_ms, win_end_ms = int(win["win_start_ms"]), int(win["win_end_ms"])
    funding_events = [e for e in all_events if win_start_ms <= e["time_ms"] < win_end_ms]
    for e in funding_events:
        e["in_window"] = True

    ts_all = [e["time_ms"] for e in all_events]
    freq = _funding_frequency(ts_all)
    grid = _expected_settlement_grid(ts_all, freq.get("interval_ms"), win_start_ms, win_end_ms)
    expected = None if grid is None else len(grid)
    bad_fields = [e["time_ms"] for e in funding_events
                  if e.get("rate") is None or e.get("mark_price") is None]
    # 逐网格点核对：每个「应有」结算时刻都须有真实事件命中（容差内）
    # ⇒ 判定不依赖 `actual >= expected`（欠算的 expected 会让该式恒真、掩盖缺口）。
    actual_ms = sorted(e["time_ms"] for e in funding_events)
    missing_settlements: List[int] = []
    if grid:
        for g in grid:
            i = bisect.bisect_left(actual_ms, g - FUNDING_GRID_TOL_MS)
            hit = (i < len(actual_ms)) and (actual_ms[i] <= g + FUNDING_GRID_TOL_MS)
            if not hit:
                missing_settlements.append(int(g))
    coverage_ok = bool(expected is not None and not bad_fields and not missing_settlements)
    fu_meta = {
        "rows_settled_in_window": len(funding_events),
        "rows_settled_lookback_total": len(all_events),
        "rate_type_filter": "settled",
        "time_unit": "ms（原始 funding_time，未截秒）",
        "lookback_ms": FUNDING_LOOKBACK_MS,
        "first_ms": funding_events[0]["time_ms"] if funding_events else None,
        "last_ms": funding_events[-1]["time_ms"] if funding_events else None,
        "first_utc": _iso(funding_events[0]["time_ms"]) if funding_events else None,
        "last_utc": _iso(funding_events[-1]["time_ms"]) if funding_events else None,
        "frequency": freq,
        "expected_settlements_in_window": expected,
        "actual_settlements_in_window": len(funding_events),
        "grid_points_in_window": expected,
        "missing_settlements_in_window": missing_settlements[:10],
        "missing_settlements_count": len(missing_settlements),
        "match_tolerance_ms": FUNDING_GRID_TOL_MS,
        "events_missing_rate_or_mark": bad_fields[:10],
        "coverage_ok": coverage_ok,
        "note": ("只认真实 settled 事件（毫秒）；缺 rate/mark 记 funding_gap，不补 0；"
                 "结算覆盖 = 按观测频率推算的**每个网格时刻**都要有真实事件命中"
                 "（含窗口左端点，容差内），不只是次数 ≥ 期望"),
    }

    return {"sig_df": sig_df, "exec_df": exec_df, "funding_events": funding_events,
            "funding_events_all": all_events, "oi_rows": oi_rows,
            "coverage": {"signal": sig_cov, "exec": exec_cov, "oi": oi_meta, "funding": fu_meta}}


# ══════════════════════════ 指标（从账本算）══════════════════════════

def _daily_equity(equity_curve: List[dict], win_start_ms: int, win_end_ms: int) -> List[Tuple[int, float]]:
    """完整等间隔 UTC 逐日净值（保留平盘日）：每个日界取「当日前最后一根 bar」的权益。"""
    if not equity_curve:
        return []
    ts = np.array([int(e["timestamp"]) for e in equity_curve], dtype=np.int64)
    eq = np.array([float(e["total_equity"]) for e in equity_curve], dtype=np.float64)
    days = []
    d = _utc_midnight_floor(win_start_ms) + DAY_MS
    while d <= win_end_ms:
        pos = np.searchsorted(ts, d, side="left") - 1      # 严格早于日界
        if pos >= 0:
            days.append((int(d), float(eq[pos])))
        d += DAY_MS
    return days


def compute_metrics(res: dict, win: dict, exec_cfg) -> dict:
    """全部指标**从账本/权益曲线算**，不从引擎汇总字段抄。"""
    trades = res["trades"]
    eq = res["equity_curve"]
    init = float(exec_cfg.initial_equity)

    nets = [float(t["net_pnl"]) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    flats = [x for x in nets if x == 0]
    gross_profit = float(sum(wins))
    gross_loss = float(-sum(losses))
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None

    final_equity = float(eq[-1]["total_equity"]) if eq else init
    # 回撤峰**从初始权益起算**（首点之前账户即为 init，不能等第一根 bar 才建峰）。
    peak = init
    max_dd = 0.0
    for e in eq:
        v = float(e["total_equity"])
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    daily = _daily_equity(eq, win["win_start_ms"], win["win_end_ms"])
    rets = []
    for i in range(1, len(daily)):
        p0, p1 = daily[i - 1][1], daily[i][1]
        if p0 > 0:
            rets.append(p1 / p0 - 1.0)
    sharpe = None
    if len(rets) >= 2:
        sd = float(np.std(np.array(rets), ddof=1))
        if sd > 0:
            sharpe = float(np.mean(rets) / sd * math.sqrt(365))

    return {
        "initial_equity": init,
        "final_equity": final_equity,
        "net_pnl": final_equity - init,
        "net_return_pct": ((final_equity - init) / init * 100.0) if init else None,
        "gross_pnl": float(res["reconcile"]["sum_gross_pnl"]),
        "total_entry_fee": float(res["reconcile"]["sum_entry_fee"]),
        "total_exit_fee": float(res["reconcile"]["sum_exit_fee"]),
        "total_fees": float(res["reconcile"]["sum_entry_fee"] + res["reconcile"]["sum_exit_fee"]),
        "total_funding_fee": float(res["reconcile"]["sum_funding_fee"]),
        "trades_count": len(trades),
        "wins": len(wins), "losses": len(losses), "flats": len(flats),
        "win_rate_pct": (len(wins) / len(nets) * 100.0) if nets else None,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "avg_win_loss_ratio": (avg_win / avg_loss) if (avg_win and avg_loss) else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "expectancy_usd": (sum(nets) / len(nets)) if nets else None,
        "max_drawdown_pct": max_dd * 100.0,
        "sharpe_annualized_sqrt365": sharpe,
        "sharpe_basis": "完整等间隔 UTC 逐日净值收益（保留平盘日），年化 sqrt(365)",
        "daily_points": len(daily),
        "open_positions_remaining": res.get("open_positions_remaining") or [],
        "note": "指标由账本/权益曲线独立计算；profit_factor=Σ盈/|Σ亏|（聚合），"
                "avg_win_loss_ratio=平均盈/平均亏（均值），二者不同口径、分别给出",
    }


def _monthly_returns(equity_curve: List[dict], init_equity: float, win_start_ms: int) -> dict:
    """账户月界净值口径：本月末/上月末 − 1。

    **首月以窗口起点初始权益为基准**（该月是区间内的不完整月），并标 `partial_month=True`；
    否则「首月无基准 ⇒ null」会让只有一个月数据（如 7 天窗口）的图表整段为空。
    """
    if not equity_curve:
        return {"months": [], "initial_equity": float(init_equity), "note": "无权益曲线"}
    by_month: Dict[str, float] = {}
    for e in equity_curve:
        d = datetime.fromtimestamp(int(e["timestamp"]) / 1000, timezone.utc)
        by_month[f"{d.year:04d}-{d.month:02d}"] = float(e["total_equity"])
    first_month = datetime.fromtimestamp(int(win_start_ms) / 1000,
                                        timezone.utc).strftime("%Y-%m")
    months, prev = [], float(init_equity)
    for k in sorted(by_month):
        cur = by_month[k]
        partial = (k == first_month)
        months.append({
            "month": k, "month_end_equity": cur, "basis_equity": prev,
            "return_pct": ((cur / prev - 1.0) * 100.0) if prev else None,
            "partial_month": bool(partial),
            "basis_note": ("首月以窗口起点初始权益为基准（区间内不完整月）"
                           if partial else "上月末账户净值"),
        })
        prev = cur
    return {"months": months, "initial_equity": float(init_equity),
            "note": ("账户月界净值（本月末/上月末 − 1）；首月以**窗口起点初始权益**为基准"
                     "并标 partial_month，不再恒为 null")}


def buy_and_hold_benchmark(exec_df: pd.DataFrame, exec_cfg, funding_events: List[dict],
                           init_equity: float) -> dict:
    """同期同标的**买入持有**基准（**同成本口径**：同手续费/滑点 + **同真实资金费**）。

    · 标的取自传入序列（**不写死 BTC**）。
    · 进场 = 窗口首根 15m 开盘 ×(1+滑点)（全额名义、含入场费）；逐 bar 以收盘盯市；
      资金费按**真实 settled 事件**逐笔 `mark×qty×rate` 计入（与策略同一口径）。
    · 另给「纯价格（无成本、无资金费）」参考值，**明确标注**不可与策略直接比。
    """
    ts = exec_df["timestamp"].to_numpy(np.int64)
    o = exec_df["open"].to_numpy(np.float64)
    c = exec_df["close"].to_numpy(np.float64)
    fee = float(exec_cfg.fee_rate)
    slip = float(exec_cfg.slippage_bps) * 1e-4

    entry_ms = int(ts[0])
    entry_fill = float(o[0]) * (1.0 + slip)
    qty = float(init_equity) / (entry_fill * (1.0 + fee))     # 全额名义（含入场费）
    ev = sorted([e for e in funding_events
                 if int(e["time_ms"]) > entry_ms
                 and e.get("rate") is not None and e.get("mark_price") is not None],
                key=lambda x: int(x["time_ms"]))

    series: List[dict] = []
    cum, j = 0.0, 0
    for i in range(len(ts)):
        t = int(ts[i])
        while j < len(ev) and int(ev[j]["time_ms"]) <= t:
            cum += (float(ev[j]["mark_price"]) * qty) * float(ev[j]["rate"]) * 1
            j += 1
        series.append({"t": t, "equity": qty * float(c[i]) * (1.0 - fee) - cum,
                       "cum_funding": cum, "mark": float(c[i])})

    exit_fill = float(c[-1]) * (1.0 - slip)
    final_eq = qty * exit_fill * (1.0 - fee) - cum
    # 收尾点回写 final_eq（含出场滑点/手续费）⇒ 曲线末点与 final_equity/net_return_pct 一致，
    # 且回撤在**同一序列**（含收尾点）上计算，不再出现「末点 ≠ 最终净值」的口径分裂。
    series[-1] = {**series[-1], "equity": final_eq, "cum_funding": cum,
                  "is_exit": True, "exit_fill": exit_fill}
    peak, mdd = float(init_equity), 0.0
    for p in series:
        peak = max(peak, p["equity"])
        if peak > 0:
            mdd = max(mdd, (peak - p["equity"]) / peak)
    # 末点净值必须等于 final_eq（自检，不静默）
    assert abs(float(series[-1]["equity"]) - final_eq) < 1e-9, "基准末点与 final_eq 不一致"
    return {
        "benchmark_kind": "perp_buy_and_hold_same_cost",
        "symbol": None,                       # 由调用方按实际标的名填充（不写死 BTC）
        "entry_time_ms": entry_ms,
        "exit_time_ms": int(ts[-1]),
        "entry_price": float(o[0]), "exit_price": float(c[-1]),
        "qty": qty, "cum_funding": cum,
        "gross_return_pct": (float(c[-1]) / float(o[0]) - 1.0) * 100.0,
        "net_return_pct": ((final_eq - float(init_equity)) / float(init_equity) * 100.0
                           if init_equity else None),
        "final_equity": final_eq,
        "series_last_equity": float(series[-1]["equity"]),
        "max_drawdown_pct": mdd * 100.0,
        "price_only_return_pct": (float(c[-1]) / float(o[0]) - 1.0) * 100.0,
        "cost_model": (f"双边手续费 {fee} + 双边滑点 {slip} + **真实 settled 资金费**"
                       f"（逐事件 mark×qty×rate，与策略同口径）"),
        "note": ("同期同标的买入持有（同成本：手续费+滑点+真实资金费）⇒ 可与策略净值直接对比；"
                 "price_only_return_pct 为**纯价格**参考（无成本、无资金费），不可直接与策略比"),
        "equity_series": series,
    }


# ══════════════════════════ 资金费逐事件独立重算 ══════════════════════════

def funding_evidence(trade: dict, events: List[dict]) -> Tuple[List[dict], float]:
    """按**只持仓区间** `(entry_ms, exit_ms]` 独立重算逐事件资金费（**原始毫秒**）。

    与引擎 `_settle_funding` 同一判据（毫秒半开左闭右）与同一表达式顺序
    `(mark × qty) × rate × direction` ⇒ 可与引擎累计值逐位对账。
    不使用 `//1000` 截秒 ⇒ 开仓后数毫秒的真实结算事件同样计入（不漏计）。
    """
    e_ms = int(trade["entry_time"])
    x_ms = int(trade["exit_time"])
    qty = float(trade["qty"])
    d = int(trade["direction"])
    out: List[dict] = []
    tot = 0.0
    for ev in events:
        s_ms = int(ev["time_ms"])
        if not (e_ms < s_ms <= x_ms):
            continue
        r, m = ev.get("rate"), ev.get("mark_price")
        ok = (r is not None and m is not None and math.isfinite(float(r))
              and math.isfinite(float(m)) and float(m) > 0)
        if not ok:
            continue
        amount = (float(m) * qty) * float(r) * d
        tot += amount
        out.append({"funding_time_ms": s_ms, "rate": float(r), "mark": float(m),
                    "qty": qty, "amount": amount, "source": "binance.fapi.settled"})
    return out, tot


# ══════════════════════════ 指标：从账本独立重算 ══════════════════════════

def recompute_metrics_from_ledger(run_id: str) -> dict:
    """**只读账本行**重算指标（不引用引擎汇总字段）⇒ 与 run.json 指标逐项对账。

    口径与 `compute_metrics` 一致：净收益/最大回撤（峰含初始权益）/胜率/
    平均盈亏比/profit_factor/交易数，另加现金与权益恒等式。
    """
    trades = S.read_ledger_rows(run_id, "trades", order_by="entry_time_ms ASC")
    equity = S.read_ledger_rows(run_id, "equity", order_by="ts_ms ASC")
    if not equity:
        return {"available": False, "reason": "账本无 equity 行"}
    init = None
    for row in S.read_ledger_rows(run_id, "meta"):
        if row.get("key") == "initial_equity":
            init = float(row["value"])
    if init is None:
        init = float(equity[0]["total_equity"])

    nets = [float(t["net_pnl"]) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gp, gl = float(sum(wins)), float(-sum(losses))
    avg_win = (gp / len(wins)) if wins else None
    avg_loss = (gl / len(losses)) if losses else None

    final_eq = float(equity[-1]["total_equity"])
    peak, mdd = float(init), 0.0
    for e in equity:
        v = float(e["total_equity"])
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)

    sum_gross = sum(float(t["gross_pnl"]) for t in trades)
    sum_ef = sum(float(t["entry_fee"]) for t in trades)
    sum_xf = sum(float(t["exit_fee"]) for t in trades)
    sum_fu = sum(float(t["funding_fee"]) for t in trades)
    exp_cash = init + sum_gross - sum_ef - sum_xf - sum_fu
    final_cash = float(equity[-1]["cash_equity"])
    net_sum = sum(nets)
    # 末行（时间最大）的持仓数 —— 不是「整段持仓过的行数」（旧实现语义错，会把中途平掉的仓也算进去）
    open_positions_last = int(equity[-1]["open_positions_count"])
    open_positions_rows = sum(1 for e in equity if int(e["open_positions_count"]) > 0)
    return {
        "available": True,
        "initial_equity": init, "final_equity": final_eq,
        "net_pnl": final_eq - init,
        "net_return_pct": ((final_eq - init) / init * 100.0) if init else None,
        "trades_count": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": (len(wins) / len(nets) * 100.0) if nets else None,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "avg_win_loss_ratio": (avg_win / avg_loss) if (avg_win and avg_loss) else None,
        "profit_factor": (gp / gl) if gl > 0 else None,
        "max_drawdown_pct": mdd * 100.0,
        "total_fees": sum_ef + sum_xf, "total_funding_fee": sum_fu,
        "cash_identity_ok": abs(final_cash - exp_cash) < 1e-6,
        "expected_cash": exp_cash, "final_cash": final_cash,
        "equity_identity_ok": abs(final_eq - (init + net_sum)) < 1e-6,
        "open_positions_last": open_positions_last,
        "open_positions_rows": open_positions_rows,
        "source": "ledger.sqlite3（trades/equity/meta 只读重算）",
    }


# ══════════════════════════ 主流程 ══════════════════════════

def _metrics_match(metrics: dict, led: dict) -> Tuple[bool, List[str]]:
    """逐项比对「引擎侧指标」与「账本只读重算指标」。不等 ⇒ 记录差异。"""
    if not led.get("available"):
        return False, ["账本重算不可用"]
    pairs = (("net_return_pct", 1e-6), ("max_drawdown_pct", 1e-6), ("trades_count", 0),
             ("profit_factor", 1e-9), ("avg_win_loss_ratio", 1e-9), ("win_rate_pct", 1e-9))
    bad: List[str] = []
    for k, tol in pairs:
        a, b = metrics.get(k), led.get(k)
        if a is None and b is None:
            continue
        if a is None or b is None:
            bad.append(f"{k}: 引擎={a} 账本={b}")
            continue
        if abs(float(a) - float(b)) > tol:
            bad.append(f"{k}: 引擎={a} 账本={b}")
    if not (led.get("cash_identity_ok") and led.get("equity_identity_ok")):
        bad.append("账本恒等式未通过")
    return (len(bad) == 0), bad


def _snap(run_id: str, name: str, blob: bytes, **extra) -> dict:
    """落盘一份实际输入切片并返回 manifest 条目（文件哈希 == 记录哈希）。"""
    S.write_snapshot(run_id, name, blob)
    return {"file": f"inputs/{name}", "sha256": _sha_bytes(blob),
            "bytes": len(blob), **extra}


def _run(run_id: str, doc: dict) -> dict:
    spec = doc["spec"]
    syms = spec.get("symbols") or []
    if len(syms) != 1:
        raise RuntimeError(f"运行规格 symbols 必须为长度 1（得到 {syms!r}）；"
                           "多标的未验证，拒绝静默取第一个")
    repo = _repo()
    win = resolve_window(spec, repo)
    data = load_inputs(spec, win, repo)
    sig_df, exec_df = data["sig_df"], data["exec_df"]
    events = data["funding_events"]                       # 窗口内真实 settled（毫秒）

    # ── 请求配置**真正生效**：信号/执行覆盖按白名单作用域分别构造 ──
    sp = BR.signal_params(**(spec.get("signal_overrides") or {}))
    ep = BR.exec_params(**(spec.get("exec_overrides") or {}))
    for _e in BR.C.validate_signal_params(sp):
        raise RuntimeError(f"signal 参数非法（请求未生效）：{_e}")
    for _e in BR.C.validate_exec_params(ep):
        raise RuntimeError(f"exec 参数非法（请求未生效）：{_e}")
    price_step = float(sp.price_step)

    # 信号帧（与引擎内部同一纯函数、同一输入 ⇒ 逐位一致；此处用于证据/图表）
    sig_frame = BR.CORE.compute_signals(sig_df, sp, "ms", price_step)
    sig_sha = _df_sha(sig_frame, ["timestamp", "signal", "direction", "stop_price", "atr",
                                  "oi_resonance_score", "confirm_time"])

    md = {win["symbol"]: {"signal_bars": sig_df, "intrabar_bars": exec_df,
                          "time_unit": "ms", "funding_events": events,
                          "funding_time_unit": "ms"}}
    res = BR.EXEC.run_portfolio_execution(
        md, strategy_params=sp, exec_cfg=ep, time_unit="ms",
        trade_start_sec=win["trade_start_sec"])

    # 原始信号 id（与 signals 表同一构造）⇒ trade/order 可联结回**原始信号**
    sig_id_by_bar: Dict[int, str] = {}
    for _, r in sig_frame.iterrows():
        s = int(r["signal"])
        if s == BR.CORE.SIG_NONE:
            continue
        bar_ms = int(r["timestamp"])
        sig_id_by_bar[bar_ms] = _sha_bytes(f"{win['symbol']}|{bar_ms}|{s}".encode())[:16]

    entry_sigbar: Dict[Tuple[str, int], int] = {}
    for d in res["decisions"]:
        if d.get("action") == "entry":
            entry_sigbar[(d["symbol"], int(d["timestamp"]))] = int(
                d.get("signal_bar_open_ms", -1))

    def _sig_id_for(sym: str, entry_ms: int) -> Optional[str]:
        return sig_id_by_bar.get(entry_sigbar.get((sym, int(entry_ms)), -1))

    # ── 落账（独立 schema；原子独占目录内）──
    trades_by_id: Dict[str, dict] = {}
    trade_sig_id: Dict[str, Optional[str]] = {}
    funding_all: List[dict] = []
    funding_mismatch: List[dict] = []
    ordered_trades = sorted(res["trades"], key=lambda x: int(x["entry_time"]))
    with S.Ledger(run_id) as lg:
        # signals
        for _, r in sig_frame.iterrows():
            s = int(r["signal"])
            if s == BR.CORE.SIG_NONE:
                continue
            confirm = int(r["confirm_time"])
            lg.signal(
                signal_id=sig_id_by_bar[int(r["timestamp"])],
                symbol=win["symbol"], signal_bar_open_ms=int(r["timestamp"]),
                confirm_ms=confirm * 1000, kind=str(r["signal_name"]),
                direction="long" if int(r["direction"]) == 1 else "short",
                stop_price=(float(r["stop_price"]) if math.isfinite(float(r["stop_price"])) else None),
                atr=(float(r["atr"]) if math.isfinite(float(r["atr"])) else None),
                oi_score=(float(r["oi_resonance_score"])
                          if r["oi_resonance_score"] is not None
                          and math.isfinite(float(r["oi_resonance_score"])) else None),
                oi_available=int(bool(r["oi_resonance_available"])),
                oi_quality=str(r["oi_resonance_quality"]),
                rank_key=None,
                in_window=int(win["win_start_ms"] <= confirm * 1000 < win["win_end_ms"]))
        # trades + funding 逐事件（**毫秒**判据）
        for n, t in enumerate(ordered_trades):
            tid = S.new_trade_id(run_id, t["symbol"], int(t["entry_time"]), n)
            trades_by_id[tid] = t
            sid = _sig_id_for(t["symbol"], int(t["entry_time"]))
            trade_sig_id[tid] = sid
            ev_rows, ev_sum = funding_evidence(t, events)
            if abs(ev_sum - float(t["funding_fee"])) > 1e-9:
                funding_mismatch.append({
                    "trade_id": tid, "engine": float(t["funding_fee"]),
                    "recomputed": ev_sum, "diff": ev_sum - float(t["funding_fee"])})
            for e in ev_rows:
                row = dict(e)
                row["trade_id"] = tid
                funding_all.append(row)
                lg.funding(**row)
            lg.trade(
                trade_id=tid, signal_id=sid, symbol=t["symbol"],
                direction=("long" if int(t["direction"]) == 1 else "short"),
                signal_type=int(t["signal_type"]),
                entry_time_ms=int(t["entry_time"]), exit_time_ms=int(t["exit_time"]),
                entry_price=float(t["entry_price"]), exit_price=float(t["exit_price"]),
                qty=float(t["qty"]), gross_pnl=float(t["gross_pnl"]),
                entry_fee=float(t["entry_fee"]), exit_fee=float(t["exit_fee"]),
                fees=float(t["fees"]), funding_fee=float(t["funding_fee"]),
                net_pnl=float(t["net_pnl"]), exit_reason=str(t["exit_reason"]),
                mfe=float(t["mfe"]), mae=float(t["mae"]),
                holding_hours=(int(t["exit_time"]) - int(t["entry_time"])) / HOUR_MS,
                funding_events=len(ev_rows),
                funding_gaps_json=json.dumps(t.get("funding_gaps") or [], ensure_ascii=False),
                stop_trace_json=json.dumps(t.get("stop_trace") or [], ensure_ascii=False))
        if funding_mismatch:
            raise RuntimeError(f"资金费逐事件重算与引擎不一致（对账失败即失败）：{funding_mismatch}")
        # orders（决策）与 trade_id / signal_id 联结
        last_entry: Dict[str, str] = {}
        entry_by_key: Dict[Tuple[str, int], str] = {
            (t["symbol"], int(t["entry_time"])): S.new_trade_id(
                run_id, t["symbol"], int(t["entry_time"]), n)
            for n, t in enumerate(ordered_trades)}
        for i, d in enumerate(res["decisions"]):
            sym = d["symbol"]
            tid = None
            sid = None
            if d.get("action") == "entry":
                tid = entry_by_key.get((sym, int(d["timestamp"])))
                if tid:
                    last_entry[sym] = tid
                sid = sig_id_by_bar.get(int(d.get("signal_bar_open_ms", -1)))
            else:
                tid = last_entry.get(sym)
                sid = trade_sig_id.get(tid) if tid else None
            lg.order(order_id=S.new_order_id(run_id, i, int(d["timestamp"]), sym,
                                             str(d.get("action"))),
                     trade_id=tid, signal_id=sid, symbol=sym,
                     side=("buy" if d.get("action") == "entry" else "sell"),
                     phase=str(d.get("phase")), ts_ms=int(d["timestamp"]),
                     action=str(d.get("action")), reason=str(d.get("reason") or ""),
                     price=(float(d["price"]) if d.get("price") is not None else None),
                     qty=(float(d["qty"]) if d.get("qty") is not None else None),
                     fee=(float(d["entry_fee"]) if d.get("entry_fee") is not None else None),
                     stop_price=(float(d["stop_price"]) if d.get("stop_price") is not None else None),
                     signal_type=(int(d["signal_type"]) if d.get("signal_type") is not None else None))
        for d in res["rejected_orders"]:
            lg.rejected(ts_ms=int(d["timestamp"]), symbol=d["symbol"],
                        phase=str(d.get("phase")), reason=str(d.get("reason")))
        for e in res["equity_curve"]:
            lg.equity(ts_ms=int(e["timestamp"]), cash_equity=float(e["cash_equity"]),
                      unrealized_pnl=float(e["unrealized_pnl"]),
                      total_equity=float(e["total_equity"]),
                      open_positions_count=int(e["open_positions_count"]),
                      open_risk_pct=float(e["open_risk_pct"]))
        lg.meta("strategy_id", BR.STRATEGY_ID)
        lg.meta("code_sha256", BR.code_hash())
        lg.meta("signal_frame_sha256", sig_sha)
        lg.meta("reconcile_json", json.dumps(res["reconcile"], ensure_ascii=False))
        lg.meta("trade_start_sec", win["trade_start_sec"])
        lg.meta("initial_equity", float(ep.initial_equity))
        lg.meta("fee_rate", float(ep.fee_rate))
        lg.meta("slippage_bps", float(ep.slippage_bps))
        lg.flush()
        ledger_counts = dict(lg.counts)

    metrics = compute_metrics(res, win, ep)
    metrics["funding_events_total"] = len(funding_all)

    # ── 指标：从账本**独立重算**并与引擎侧逐项对账 ──
    ledger_metrics = recompute_metrics_from_ledger(run_id)
    metrics_match, metrics_diffs = _metrics_match(metrics, ledger_metrics)
    metrics["metrics_from_ledger"] = ledger_metrics
    metrics["metrics_match"] = bool(metrics_match)
    metrics["metrics_match_diffs"] = metrics_diffs

    # ── 实际输入切片**不可变快照**（文件 + 精确 SHA256；manifest 指向相对文件）──
    oi_rows = data.get("oi_rows") or []
    oi_df = pd.DataFrame({
        "ts": [int(r["ts"]) for r in oi_rows],
        "open_interest": [float(r["open_interest"]) for r in oi_rows],
        "period": [str(r.get("period") or "5m") for r in oi_rows],
    })
    funding_raw = [{"time_ms": int(e["time_ms"]), "rate": e.get("rate"),
                    "mark_price": e.get("mark_price"),
                    "rate_type": e.get("rate_type")} for e in events]
    # 回看窗原始事件（用于独立重推「应有结算网格」；键为 time_ms，与账本行 funding_time_ms 不同）
    funding_lookback_raw = [{"time_ms": int(e["time_ms"]), "rate": e.get("rate"),
                             "mark_price": e.get("mark_price"),
                             "rate_type": e.get("rate_type")}
                            for e in (data.get("funding_events_all") or [])]
    snap = {
        "signal_bars": _snap(
            run_id, "signal_bars.csv", _df_canon_csv(
                sig_df, ["timestamp", "open", "high", "low", "close", "volume",
                         "open_interest"]),
            rows=len(sig_df), columns=list(sig_df.columns),
            first_utc=_iso(int(sig_df["timestamp"].iloc[0])),
            last_utc=_iso(int(sig_df["timestamp"].iloc[-1])),
            note="信号周期 bar（含预热）+ as-of 对齐后的 open_interest"),
        "intrabar_bars": _snap(
            run_id, "intrabar_bars.csv", _df_canon_csv(
                exec_df, ["timestamp", "open", "high", "low", "close", "volume"]),
            rows=len(exec_df), columns=list(exec_df.columns),
            first_utc=_iso(int(exec_df["timestamp"].iloc[0])),
            last_utc=_iso(int(exec_df["timestamp"].iloc[-1])),
            note="执行周期 bar（窗口内）"),
        "oi_5m": _snap(
            run_id, "oi_5m.csv", _df_canon_csv(oi_df, ["ts", "open_interest", "period"]),
            rows=len(oi_df), note="原始 5m OI 切片（未对齐前）"),
        "funding_events": _snap(
            run_id, "funding_events.json", _json_bytes(funding_raw),
            rows=len(funding_raw), time_unit="ms",
            first_utc=_iso(funding_raw[0]["time_ms"]) if funding_raw else None,
            last_utc=_iso(funding_raw[-1]["time_ms"]) if funding_raw else None,
            note="窗口内真实 settled 资金费事件（**原始毫秒**，未截秒）"),
        "funding_events_lookback": _snap(
            run_id, "funding_events_lookback.json", _json_bytes(funding_lookback_raw),
            rows=len(funding_lookback_raw), time_unit="ms",
            first_utc=_iso(funding_lookback_raw[0]["time_ms"]) if funding_lookback_raw else None,
            last_utc=_iso(funding_lookback_raw[-1]["time_ms"]) if funding_lookback_raw else None,
            note="资金费**频率/锚点观测**回看窗（win_start-7d .. win_end）全部 settled 事件，"
                 "供独立审计重推应有结算网格（含窗口左端点）"),
        "signal_frame": _snap(
            run_id, "signal_frame.csv", _df_canon_csv(sig_frame, list(sig_frame.columns)),
            rows=int(len(sig_frame)), columns=[c for c in sig_frame.columns],
            note="候选包 compute_signals 输出帧（同一纯函数、同一输入）"),
    }

    inputs_manifest = {
        "run_id": run_id,
        "generated_at": S.utc_now_iso(),
        "window": win,
        "market_db": str(market_db_path()),
        "market_db_stat": _stat(market_db_path()),
        "archive_root": str(archive_root()),
        "archive_state": _read_json(archive_root() / "_state.json"),
        "snapshot_root": f"inputs/（相对本 run 目录）",
        "series": snap,
        "coverage": data["coverage"],
        "spec": spec,
        "spec_sha256": _sha_bytes(_canon(spec)),
        "config": {"signal_params": sp.identity(), "exec_params": ep.identity(),
                   "signal_params_hash": sp.hash(), "exec_params_hash": ep.hash(),
                   "note": "**请求覆盖后实际生效**的参数（非冻结默认值）"},
        "code_sha256": BR.code_hash(),
        "code_files": BR.code_hash_manifest(),
        "engine": BR.version_block(),
    }
    S.write_inputs_manifest(run_id, inputs_manifest)

    sig_cov = data["coverage"]["signal"]
    exec_cov = data["coverage"]["exec"]
    fu_meta = data["coverage"]["funding"]
    reconcile_ok = bool(res["reconcile"].get("cash_identity_ok")
                        and res["reconcile"].get("equity_identity_ok")
                        and res["reconcile"].get("equity_curve_tail_matches_final"))
    funding_ok = bool(fu_meta["coverage_ok"] and not (res.get("funding_gaps") or [])
                      and not funding_mismatch
                      and not res["funding_is_assumption"])
    reasons: List[str] = []
    if not sig_cov.get("full_window_ok"):
        reasons.append("信号序列未全窗覆盖（空/首/尾/根数）")
    if not exec_cov.get("full_window_ok"):
        reasons.append("执行序列未全窗覆盖（空/首/尾/根数）")
    if not (sig_cov["contiguous"] and exec_cov["contiguous"]):
        reasons.append("序列不连续")
    if not (sig_cov["all_closed"] and exec_cov["all_closed"]):
        reasons.append("存在未收盘 bar")
    if not fu_meta["coverage_ok"]:
        if fu_meta.get("expected_settlements_in_window") is None:
            reasons.append("资金费频率未经验证（无 settled 事件）")
        elif fu_meta.get("missing_settlements_count"):
            reasons.append(
                f"资金费结算缺格 {fu_meta['missing_settlements_count']} 处"
                f"（应有 {fu_meta['expected_settlements_in_window']}，"
                f"实际 {fu_meta['actual_settlements_in_window']}，"
                f"频率 {fu_meta['frequency'].get('frequency')}）")
        else:
            reasons.append(
                f"资金费结算覆盖不足（实际 {fu_meta['actual_settlements_in_window']} / "
                f"应有 {fu_meta['expected_settlements_in_window']}，"
                f"频率 {fu_meta['frequency'].get('frequency')}）")
    if res.get("funding_gaps"):
        reasons.append(f"引擎记录资金费缺口 {len(res['funding_gaps'])} 条")
    if res["funding_is_assumption"]:
        reasons.append("资金费为**假设模型**（非真实结算事件）")
    if not reconcile_ok:
        reasons.append("引擎恒等式对账未通过")
    if not metrics_match:
        reasons.append(f"指标与账本重算不一致：{metrics_diffs}")
    q_status = "ok" if not reasons else "degraded"

    data_quality = {
        "status": q_status,
        "reasons": reasons,
        "signal_full_window_ok": sig_cov.get("full_window_ok"),
        "exec_full_window_ok": exec_cov.get("full_window_ok"),
        "signal_contiguous": sig_cov["contiguous"], "exec_contiguous": exec_cov["contiguous"],
        "all_closed": (sig_cov["all_closed"] and exec_cov["all_closed"]),
        "signal_expected_bars": sig_cov.get("expected_bars"),
        "signal_actual_bars": sig_cov.get("rows"),
        "exec_expected_bars": exec_cov.get("expected_bars"),
        "exec_actual_bars": exec_cov.get("rows"),
        "oi_bars_missing": data["coverage"]["oi"]["bars_missing_oi"],
        "oi_fake_fill": "无",
        "funding_model": res["funding_model"],
        "funding_is_assumption": res["funding_is_assumption"],
        "funding_frequency": fu_meta["frequency"],
        "funding_expected_settlements": fu_meta["expected_settlements_in_window"],
        "funding_actual_settlements": fu_meta["actual_settlements_in_window"],
        "funding_missing_settlements_count": fu_meta["missing_settlements_count"],
        "funding_missing_settlements": fu_meta["missing_settlements_in_window"],
        "funding_match_tolerance_ms": fu_meta["match_tolerance_ms"],
        "funding_coverage_ok": fu_meta["coverage_ok"],
        "funding_events_missing_fields": fu_meta["events_missing_rate_or_mark"],
        "funding_gaps": res.get("funding_gaps") or [],
        "funding_events_used": len(events),
        "funding_events_recomputed": len(funding_all),
        "funding_evidence_reconcile": ("逐事件独立重算(毫秒) == 引擎累计值（已通过）"
                                       if not funding_mismatch else "不一致"),
        "oi_min_resonance": float(sp.oi_min_resonance),
        "oi_gate_enabled": bool(float(sp.oi_min_resonance) > 0.0),
        "oi_gate_note": ("默认 oi_min_resonance=0.0 ⇒ **因子门槛未启用**（因子只作证据记录，"
                         "不拒单）；本运行**未擅自改阈值**"),
        "reconcile": res["reconcile"],
        "reconcile_ok": reconcile_ok,
        "metrics_match": bool(metrics_match),
        "metrics_match_diffs": metrics_diffs,
        "ledger_counts": ledger_counts,
        "time_precision_note": ("入场/退出时刻均为 15m bar **开盘**毫秒；bar 内止损的真实成交"
                                "时刻在 OHLC 下不可知 ⇒ 退出时刻属**显式 bar 级假设**"
                                "（不隐瞒、不伪造 tick 时刻）"),
        "actual_config": {"signal_params_hash": sp.hash(), "exec_params_hash": ep.hash(),
                          "initial_equity": float(ep.initial_equity),
                          "fee_rate": float(ep.fee_rate),
                          "maker_fee_rate": float(getattr(ep, "maker_fee_rate", 0.0002)),
                          "taker_fee_rate": float(getattr(ep, "taker_fee_rate", ep.fee_rate)),
                          "slippage_bps": float(ep.slippage_bps),
                          "max_concurrent_trades": int(getattr(ep, "max_concurrent_trades", 3)),
                          "max_portfolio_margin_pct": float(getattr(ep, "max_portfolio_margin_pct", 0.40))},
    }

    cost_summary = {
        "sum_fees": float(res["reconcile"].get("sum_fees", 0.0)),
        "sum_entry_fee": float(res["reconcile"].get("sum_entry_fee", 0.0)),
        "sum_exit_fee": float(res["reconcile"].get("sum_exit_fee", 0.0)),
        "sum_slippage_loss": float(res["reconcile"].get("sum_slippage_loss", 0.0)),
        "sum_funding_fee": float(res["reconcile"].get("sum_funding_fee", 0.0)),
        "fee_tier": "Binance_VIP0",
        "maker_fee_rate": float(getattr(ep, "maker_fee_rate", 0.0002)),
        "taker_fee_rate": float(getattr(ep, "taker_fee_rate", ep.fee_rate)),
        "funding_model": res["funding_model"],
    }

    bench = buy_and_hold_benchmark(exec_df, ep, events, float(ep.initial_equity))
    bench["symbol"] = win["symbol"]
    monthly = _monthly_returns(res["equity_curve"], float(ep.initial_equity),
                               win["win_start_ms"])

    chart = build_chart(win, exec_df, sig_frame, res, trades_by_id, funding_all)
    chart["benchmark"] = bench          # 同成本基准净值序列随图表落盘（供 equity 接口读取）
    # 图表单独落盘（`chart.json`），**不塞进 run.json**：列表接口要逐个读 run.json，
    # 内嵌 672 根 K 线会让列表读取无谓变重。
    S.write_chart(run_id, chart)

    return {
        "window": win,
        "cost_summary": cost_summary,
        "hashes": {
            "code_sha256": BR.code_hash(),
            "signal_params_hash": sp.hash(),
            "exec_params_hash": ep.hash(),
            "input_sha256": _sha_bytes(_canon(snap)),
            "spec_sha256": inputs_manifest["spec_sha256"],
            "signal_frame_sha256": sig_sha,
        },
        "metrics": metrics,
        "benchmark_buy_hold": bench,
        "monthly_returns": monthly,
        "data_quality": data_quality,
        "chart_file": "chart.json",
        "snapshot_files": sorted(v["file"] for v in snap.values()),
        "engine": BR.version_block(),
        "formal_m4_gate": BR.formal_run_gate(),
        "m4_disclaimer": ("本运行是 **WX_OI_RESONANCE_V1 独立候选研究**，非正式 M4；"
                          "不继承 3a–3d / M0–M6 任何成绩，不冒称 M4 已通过；"
                          "正式 M4 仍需唯一合格父赢家 + 共同样本 + 同样本父级对照，G01–G05 未过。"),
        "trades_preview": [
            {"trade_id": tid, "signal_id": trade_sig_id.get(tid), "symbol": t["symbol"],
             "entry_time_ms": int(t["entry_time"]), "exit_time_ms": int(t["exit_time"]),
             "net_pnl": float(t["net_pnl"]), "exit_reason": t["exit_reason"],
             "funding_fee": float(t["funding_fee"])}
            for tid, t in trades_by_id.items()],
    }


def _stat(p: Path) -> dict:
    try:
        st = p.stat()
        return {"path": str(p), "size": st.st_size,
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")}
    except OSError:
        return {"path": str(p), "missing": True}


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return None


def build_chart(win: dict, exec_df: pd.DataFrame, sig_frame: pd.DataFrame, res: dict,
                trades_by_id: Dict[str, dict], funding_all: List[dict]) -> dict:
    """图表载荷：真实 15m K 线 + volume + 信号证据 + 开平仓 + 保护止损轨迹（引擎实况）。

    **无止盈规则** ⇒ 显式声明「无」，不造点。
    """
    candles = [
        {"t": int(r.timestamp), "o": float(r.open), "h": float(r.high),
         "l": float(r.low), "c": float(r.close), "v": float(r.volume)}
        for r in exec_df.itertuples(index=False)]
    signals = []
    for _, r in sig_frame.iterrows():
        s = int(r["signal"])
        if s == BR.CORE.SIG_NONE:
            continue
        confirm_ms = int(r["confirm_time"]) * 1000
        # 只画**窗口内**确认的信号：`confirm ∈ [win_start, win_end)`（半开）。
        if not (win["win_start_ms"] <= confirm_ms < win["win_end_ms"]):
            continue
        signals.append({"t": confirm_ms, "kind": str(r["signal_name"]),
                        "direction": "long" if int(r["direction"]) == 1 else "short",
                        "stop_price": (float(r["stop_price"])
                                       if math.isfinite(float(r["stop_price"])) else None),
                        "atr": float(r["atr"]) if math.isfinite(float(r["atr"])) else None,
                        "oi_score": (float(r["oi_resonance_score"])
                                     if r["oi_resonance_score"] is not None
                                     and math.isfinite(float(r["oi_resonance_score"])) else None),
                        "oi_available": bool(r["oi_resonance_available"]),
                        "oi_quality": str(r["oi_resonance_quality"])})
    trades = []
    for tid, t in trades_by_id.items():
        trades.append({
            "trade_id": tid, "symbol": t["symbol"],
            "direction": "long" if int(t["direction"]) == 1 else "short",
            "signal_type": int(t["signal_type"]),
            "entry": {"t": int(t["entry_time"]), "price": float(t["entry_price"]),
                      "qty": float(t["qty"])},
            "exit": {"t": int(t["exit_time"]), "price": float(t["exit_price"]),
                     "reason": t["exit_reason"]},
            "stop_trace": [{"t": int(x["ts"]), "stop": float(x["stop"]),
                            "reason": x["reason"]} for x in (t.get("stop_trace") or [])],
            "initial_stop": float(t["initial_stop"]) if t.get("initial_stop") is not None else None,
            "net_pnl": float(t["net_pnl"]),
        })
    return {
        "symbol": win["symbol"], "exec_timeframe": win["exec_timeframe"],
        "window": {"start_ms": win["win_start_ms"], "end_ms": win["win_end_ms"]},
        "candles": candles, "signals": signals, "trades": trades,
        "take_profit": {"exists": False,
                        "note": "本策略**无止盈规则**：退出仅由保护止损（初始 uniform / "
                                "lps_structural + 唐奇安当期通道单调锁利）与段末强平触发"},
        "time_precision": {
            "entry_time": "15m bar **开盘**毫秒（引擎相位 C 用当根 open 成交）",
            "exit_time": ("15m bar **开盘**毫秒（引擎相位 B 跳空退出 / D bar 内止损 / 段末强平）；"
                          "bar 内止损的**真实成交时刻**在 OHLC 下不可知 ⇒ 退出时刻按该 bar "
                          "开盘记录，属**显式 bar 级假设**（非 tick 精确，未隐瞒）"),
            "phase_order": "A(锁利→资金费)→B(跳空退出)→C(新开仓)→D(bar 内止损)→E(盯市权益)",
            "funding_boundary": ("资金费只结算 `(entry_ms, t_ms]` 的真实事件；退出 bar 开盘"
                                 "**当刻**（含）的应结事件已计入，开盘之后的不计"),
        },
        "stop_trace_source": "引擎实况记录（execution.OpenPosition.stop_trace），不重建、不造点",
        "equity_curve": [{"t": int(e["timestamp"]), "equity": float(e["total_equity"]),
                          "cash": float(e["cash_equity"]),
                          "unrealized": float(e["unrealized_pnl"])}
                         for e in res["equity_curve"]],
        "funding_events": [{"t": int(f["funding_time_ms"]), "rate": f["rate"],
                            "mark": f["mark"], "amount": f["amount"],
                            "trade_id": f.get("trade_id")} for f in funding_all],
    }


def execute_run(run_id: str) -> dict:
    """执行一次研究回测：状态机 + 失败留原因（**不吞异常**）。"""
    doc = S.read_run(run_id)
    S.set_status(run_id, S.STATUS_RUNNING)
    with _EXEC_LOCK:
        try:
            out = _run(run_id, doc)
        except Exception as exc:                          # noqa: BLE001
            tb = traceback.format_exc()
            S.set_status(run_id, S.STATUS_FAILED,
                         failure_reason=f"{type(exc).__name__}: {exc}",
                         traceback=tb[-4000:])
            return S.read_run(run_id)
    S.set_status(run_id, S.STATUS_SUCCEEDED, **out)
    return S.read_run(run_id)


__all__ = [
    "resolve_window", "load_inputs", "compute_metrics", "buy_and_hold_benchmark",
    "funding_evidence", "recompute_metrics_from_ledger", "build_chart", "execute_run",
    "market_db_path", "archive_root",
]
