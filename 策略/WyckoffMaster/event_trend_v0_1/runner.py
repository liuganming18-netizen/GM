# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 真实数据回测与多维对账执行器 (Runner)。

具备：
1. 支持加载 BTCUSDT / ETHUSDT / SOLUSDT 真实多周期 Parquet 数据；
2. 驱动确定性事件状态机生成事件档案与候选快照；
3. 执行四大逐字段对账：
   - 重复全量运行 (Repeatability)
   - 逐条流式 vs 分块批量 (Streaming vs Chunked Batch)
   - 注入重复 Bar 抗扰 (Duplicate Injection)
   - 状态持久化与断点恢复 (Crash-Recovery)
4. 运行 Variant A (完整含OI)、Variant B (关OI消融)、Variant C (成本压力)；
5. 接入 trading_core 共享交易核心生成真实成本账本与 ValidationResult；
6. **整套产物原子提交**：先写临时目录、逐文件 fsync，再原子改名就位 ⇒ 不存在半套 Run；
7. **指纹**：代码 / 配置 / 输入内容 / 冻结规则 四类 sha256 写入 inputs.json 与 manifest.json；
8. **全链路逐笔证据**：orders / fills / positions / trades / ledger / equity_curve 各自独立落盘。
"""
from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .config import EventTrendConfig
from .contracts import CandidateSnapshot, EventDossier, EventState, StateTransition, ValidationResult
from .engine_adapter import run_event_trend_backtest
from .features import compute_wilder_atr_series
from .instrument_spec import resolve_instrument_spec
from .state_machine import EventStateMachine

# 冻结规则文件（其 SHA256 必须与冻结版本一致）
FROZEN_RULES_REL = "evidence/WX_EVENT_TREND_V0_1_策略与回测冻结规则.md"

# 参与本策略行为的代码文件（代码指纹覆盖面）
CODE_FILES = [
    "策略/WyckoffMaster/event_trend_v0_1/__init__.py",
    "策略/WyckoffMaster/event_trend_v0_1/config.py",
    "策略/WyckoffMaster/event_trend_v0_1/contracts.py",
    "策略/WyckoffMaster/event_trend_v0_1/features.py",
    "策略/WyckoffMaster/event_trend_v0_1/state_machine.py",
    "策略/WyckoffMaster/event_trend_v0_1/engine_adapter.py",
    "策略/WyckoffMaster/event_trend_v0_1/runner.py",
    "策略/WyckoffMaster/trading_core/contracts.py",
    "策略/WyckoffMaster/trading_core/signal.py",
    "策略/WyckoffMaster/trading_core/order.py",
    "策略/WyckoffMaster/trading_core/risk.py",
    "策略/WyckoffMaster/trading_core/policy.py",
    "策略/WyckoffMaster/trading_core/fill.py",
    "策略/WyckoffMaster/trading_core/position.py",
    "策略/WyckoffMaster/trading_core/funding.py",
    "策略/WyckoffMaster/trading_core/pit.py",
    "策略/WyckoffMaster/trading_core/ledger.py",
    "策略/WyckoffMaster/trading_core/engine.py",
]


def _sha256_file(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _frame_fingerprint(df: pd.DataFrame, cols: Optional[List[str]] = None) -> dict:
    """DataFrame 内容指纹（确定性；与行序相关，行序本身是输入的一部分）。"""
    use = [c for c in (cols or list(df.columns)) if c in df.columns]
    sub = df[use]
    h = hashlib.sha256()
    h.update(("|".join(str(c) for c in use)).encode("utf-8"))
    h.update(pd.util.hash_pandas_object(sub, index=False)
             .to_numpy(np.uint64).tobytes())
    return {"rows": int(len(sub)), "columns": [str(c) for c in use],
            "sha256": h.hexdigest()}


def _array_fingerprint(arr: np.ndarray, name: str) -> dict:
    a = np.asarray(arr)
    h = hashlib.sha256()
    if np.issubdtype(a.dtype, np.integer):
        h.update(a.astype(np.int64).tobytes())
    else:
        h.update(np.asarray(a, dtype=np.float64).tobytes())
    return {"name": name, "len": int(a.size), "sha256": h.hexdigest()}


def _jsonl_text(rows: List[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                   for r in rows)


class EventTrendRunner:
    """真实多标的事件趋势回测与对账执行器。"""

    def __init__(self, base_dir: str = "/Users/boos/Desktop/威科夫因子共振趋势"):
        self.base_dir = base_dir
        self.parquet_base = os.path.join(base_dir, "数据/市场数据/parquet")
        self.runs_base = os.path.join(base_dir, "回测日志/event_trend_v0_1/runs")
        os.makedirs(self.runs_base, exist_ok=True)

    # ══════════════════════ 指纹 ══════════════════════

    def code_fingerprint(self) -> dict:
        files: Dict[str, Optional[str]] = {}
        for rel in CODE_FILES:
            files[rel] = _sha256_file(os.path.join(self.base_dir, rel))
        combined = _sha256_text(json.dumps(files, sort_keys=True))
        return {"files": files, "combined_sha256": combined,
                "missing": [k for k, v in files.items() if v is None]}

    def rules_fingerprint(self) -> dict:
        p = os.path.join(self.base_dir, FROZEN_RULES_REL)
        return {"path": FROZEN_RULES_REL, "sha256": _sha256_file(p)}

    def config_fingerprint(self, cfg: EventTrendConfig) -> dict:
        payload = json.dumps(cfg.to_dict(), sort_keys=True, default=str)
        return {"sha256": _sha256_text(payload), "params": cfg.to_dict()}

    def input_fingerprint(self, df_4h, df_1h, df_15m, oi_ts, oi_vals,
                          funding_events: List[dict]) -> dict:
        return {
            "bar_4h": _frame_fingerprint(df_4h, ["open_time", "close_time", "open",
                                                 "high", "low", "close", "volume"]),
            "bar_1h": _frame_fingerprint(df_1h, ["timestamp", "open", "high",
                                                 "low", "close", "volume", "quote_volume"]),
            "bar_15m": _frame_fingerprint(df_15m, ["timestamp", "open", "high",
                                                   "low", "close", "volume", "quote_volume"]),
            "oi_ts": _array_fingerprint(oi_ts, "oi_ts"),
            "oi_values": _array_fingerprint(oi_vals, "oi_values"),
            "funding_events": {
                "count": len(funding_events),
                "sha256": _sha256_text(json.dumps(funding_events, sort_keys=True,
                                                  default=str)),
            },
        }

    # ══════════════════════ 原子提交 ══════════════════════

    def _commit_run_dir(self, run_dir: str, files: Dict[str, str]) -> dict:
        """整套产物原子提交：写临时目录 → 逐文件 fsync → 原子改名就位。

        返回 manifest（含每个文件的 sha256 与字节数），一并写入 manifest.json。
        """
        if os.path.exists(run_dir):
            raise FileExistsError(f"run_dir_already_exists:{run_dir}")
        partial = f"{run_dir}.partial-{os.getpid()}"
        if os.path.exists(partial):
            shutil.rmtree(partial)
        os.makedirs(partial, exist_ok=False)

        manifest_files: Dict[str, dict] = {}
        for name, text in files.items():
            fp = os.path.join(partial, name)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            manifest_files[name] = {"bytes": len(text.encode("utf-8")),
                                    "sha256": _sha256_text(text)}

        manifest = {
            "run_dir": os.path.basename(run_dir),
            "commit_mode": "atomic_directory_rename",
            "files": manifest_files,
            "files_count": len(manifest_files),
        }
        mp = os.path.join(partial, "manifest.json")
        with open(mp, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())

        # 目录级原子就位
        os.replace(partial, run_dir)
        return manifest

    def load_symbol_data(
        self,
        symbol: str,
        start_date: Optional[str] = "2024-01-01",
        end_date: Optional[str] = "2024-06-30"
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray, List[dict]]:
        """加载指定标的在此区间内的 4h(含预热), 1h, 15m, OI 5m 与 Funding 数据。"""
        # 1. 4h K 线
        f_4h = sorted(glob.glob(f"{self.parquet_base}/candles/binance/um/{symbol}/4h/*.parquet"))
        if not f_4h:
            raise FileNotFoundError(f"未找到 {symbol} 4h 数据")
        df_4h_raw = pd.concat([pd.read_parquet(f) for f in f_4h], ignore_index=True)
        df_4h_raw = df_4h_raw.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
        for c in ("open", "high", "low", "close", "volume", "quote_volume"):
            if c in df_4h_raw.columns:
                df_4h_raw[c] = pd.to_numeric(df_4h_raw[c], errors="coerce")

        # 计算全历史 Wilder ATR(14)
        df_4h_raw["atr"] = compute_wilder_atr_series(
            df_4h_raw["high"].to_numpy(np.float64),
            df_4h_raw["low"].to_numpy(np.float64),
            df_4h_raw["close"].to_numpy(np.float64),
            period=14
        )

        # 2. 1h K 线
        f_1h = sorted(glob.glob(f"{self.parquet_base}/candles/binance/um/{symbol}/1h/*.parquet"))
        df_1h_raw = pd.concat([pd.read_parquet(f) for f in f_1h], ignore_index=True)
        df_1h_raw = df_1h_raw.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)

        # 3. 15m K 线
        f_15m = sorted(glob.glob(f"{self.parquet_base}/candles/binance/um/{symbol}/15m/*.parquet"))
        df_15m_raw = pd.concat([pd.read_parquet(f) for f in f_15m], ignore_index=True)
        df_15m_raw = df_15m_raw.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)

        # 数值列统一为 float（冷层以字符串存储 OHLCV），保证指纹与撮合口径一致
        for dfx in (df_1h_raw, df_15m_raw):
            for c in ("open", "high", "low", "close", "volume", "quote_volume"):
                if c in dfx.columns:
                    dfx[c] = pd.to_numeric(dfx[c], errors="coerce")

        # 4. OI 5m 真实序列
        f_oi = sorted(glob.glob(f"{self.parquet_base}/oi/binance/um/{symbol}/*.parquet"))
        df_oi_raw = pd.concat([pd.read_parquet(f, columns=["ts", "open_interest"]) for f in f_oi], ignore_index=True)
        df_oi_raw = df_oi_raw.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        oi_ts = df_oi_raw["ts"].to_numpy(np.int64)
        oi_vals = df_oi_raw["open_interest"].to_numpy(np.float64)

        # 5. Funding 真实结算事件
        f_fund = f"{self.parquet_base}/funding/binance/um/{symbol}.parquet"
        funding_events: List[dict] = []
        if os.path.exists(f_fund):
            df_fund = pd.read_parquet(f_fund)
            time_col = "funding_time" if "funding_time" in df_fund.columns else "time_ms"
            for _, row in df_fund.iterrows():
                funding_events.append({
                    "time_ms": int(row[time_col]),
                    "rate": float(row["funding_rate"]),
                    "mark_price": float(row.get("mark_price", 0.0)),
                    "rate_type": "settled"
                })

        # 裁剪到目标区间
        start_ms = int(pd.to_datetime(start_date).timestamp() * 1000)
        end_ms = int(pd.to_datetime(end_date).timestamp() * 1000)

        # 1h 与 15m 截取目标区间
        df_1h = df_1h_raw[(df_1h_raw["open_time"] >= start_ms) & (df_1h_raw["close_time"] <= end_ms)].copy()
        df_15m = df_15m_raw[(df_15m_raw["open_time"] >= start_ms) & (df_15m_raw["close_time"] <= end_ms)].copy()

        # 自适应对齐：若该标的在固定区间未上线或数据不足，自适应采用其真实可得的最长 180 天窗口
        if (len(df_15m) < 2000 or len(df_1h) < 500) and len(df_15m_raw) >= 2000:
            earliest_15m = int(df_15m_raw["open_time"].iloc[0])
            latest_15m = int(df_15m_raw["close_time"].iloc[-1])
            # 留出 4h 预热所需的 50 根 4h (约 8 天)
            auto_start = earliest_15m + 50 * 4 * 3600 * 1000
            auto_end = min(latest_15m, auto_start + 180 * 24 * 3600 * 1000)
            if auto_end - auto_start >= 30 * 24 * 3600 * 1000:
                start_ms, end_ms = auto_start, auto_end
                df_1h = df_1h_raw[(df_1h_raw["open_time"] >= start_ms) & (df_1h_raw["close_time"] <= end_ms)].copy()
                df_15m = df_15m_raw[(df_15m_raw["open_time"] >= start_ms) & (df_15m_raw["close_time"] <= end_ms)].copy()

        df_1h["timestamp"] = df_1h["open_time"]
        df_15m["timestamp"] = df_15m["open_time"]

        # 4h 包含目标区间之前至少 42 根预热 (取前 220 根以支持 ATR 预热)
        idx_start_4h = np.searchsorted(df_4h_raw["close_time"].to_numpy(np.int64), start_ms)
        warmup_start_idx = max(0, idx_start_4h - 220)
        df_4h = df_4h_raw.iloc[warmup_start_idx:].copy()
        df_4h = df_4h[df_4h["close_time"] <= end_ms].reset_index(drop=True)

        return df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events

    def run_state_machine_simulation(
        self,
        symbol: str,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        oi_ts: np.ndarray,
        oi_vals: np.ndarray,
        enable_oi_filter: bool = True,
        chunk_size: Optional[int] = None,
        inject_duplicates: bool = False
    ) -> EventStateMachine:
        """运行确定性事件状态机。支持流式逐条、分块批量或注入重复。"""
        sm = EventStateMachine(config=EventTrendConfig(), enable_oi_filter=enable_oi_filter)
        sm.symbol = symbol

        records = df_1h.to_dict("records")
        if inject_duplicates and len(records) > 50:
            dups = [copy.deepcopy(records[50])] * 99
            records = records[:51] + dups + records[51:]

        if chunk_size and chunk_size > 0:
            chunks = [records[i:i + chunk_size] for i in range(0, len(records), chunk_size)]
        else:
            chunks = [records]

        for chunk in chunks:
            for r in chunk:
                t_open = int(r["timestamp"])
                t_close = t_open + 3600 * 1000

                if t_close % (4 * 3600 * 1000) == 0:
                    sub_4h = df_4h[df_4h["close_time"] <= t_close]
                    if len(sub_4h) >= 42:
                        last_42 = sub_4h.tail(42)
                        atr_val = float(sub_4h["atr"].iloc[-1])
                        sm.check_and_form_box(
                            bar_4h_close_ms=t_close,
                            history_42_highs=last_42["high"].to_numpy(np.float64),
                            history_42_lows=last_42["low"].to_numpy(np.float64),
                            history_42_closes=last_42["close"].to_numpy(np.float64),
                            current_atr_a=atr_val
                        )

                sm.process_1h_bar(
                    open_time_ms=t_open,
                    open_p=float(r["open"]),
                    high_p=float(r["high"]),
                    low_p=float(r["low"]),
                    close_p=float(r["close"]),
                    volume_v=float(r["volume"]),
                    oi_timestamps=oi_ts,
                    oi_values=oi_vals
                )

        return sm

    def execute_reconciliation_suite(
        self,
        symbol: str,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        oi_ts: np.ndarray,
        oi_vals: np.ndarray
    ) -> Dict[str, Any]:
        """执行完整四大确定性与抗扰逐字段对账（比对全部导出内部状态字典）。"""
        sm_a = self.run_state_machine_simulation(symbol, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True)
        st_a = sm_a.export_state()

        sm_a_rep = self.run_state_machine_simulation(symbol, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True)
        st_a_rep = sm_a_rep.export_state()
        diff_repeat = (json.dumps(st_a, sort_keys=True) != json.dumps(st_a_rep, sort_keys=True))

        sm_chunked = self.run_state_machine_simulation(symbol, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True, chunk_size=50)
        st_chunked = sm_chunked.export_state()
        diff_chunked = (json.dumps(st_a, sort_keys=True) != json.dumps(st_chunked, sort_keys=True))

        sm_dup = self.run_state_machine_simulation(symbol, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True, inject_duplicates=True)
        st_dup = sm_dup.export_state()
        diff_dup = (json.dumps(st_a, sort_keys=True) != json.dumps(st_dup, sort_keys=True))

        records = df_1h.to_dict("records")
        split_point = len(records) // 2
        sm_part1 = EventStateMachine(config=EventTrendConfig(), enable_oi_filter=True)
        sm_part1.symbol = symbol
        for r in records[:split_point]:
            t_open = int(r["timestamp"])
            t_close = t_open + 3600 * 1000
            if t_close % (4 * 3600 * 1000) == 0:
                sub_4h = df_4h[df_4h["close_time"] <= t_close]
                if len(sub_4h) >= 42:
                    last_42 = sub_4h.tail(42)
                    atr_val = float(sub_4h["atr"].iloc[-1])
                    sm_part1.check_and_form_box(
                        bar_4h_close_ms=t_close,
                        history_42_highs=last_42["high"].to_numpy(np.float64),
                        history_42_lows=last_42["low"].to_numpy(np.float64),
                        history_42_closes=last_42["close"].to_numpy(np.float64),
                        current_atr_a=atr_val
                    )
            sm_part1.process_1h_bar(
                open_time_ms=t_open, open_p=float(r["open"]), high_p=float(r["high"]),
                low_p=float(r["low"]), close_p=float(r["close"]), volume_v=float(r["volume"]),
                oi_timestamps=oi_ts, oi_values=oi_vals
            )
        snapshot = sm_part1.export_state()

        sm_rec = EventStateMachine.restore_state(snapshot)
        for r in records[split_point:]:
            t_open = int(r["timestamp"])
            t_close = t_open + 3600 * 1000
            if t_close % (4 * 3600 * 1000) == 0:
                sub_4h = df_4h[df_4h["close_time"] <= t_close]
                if len(sub_4h) >= 42:
                    last_42 = sub_4h.tail(42)
                    atr_val = float(sub_4h["atr"].iloc[-1])
                    sm_rec.check_and_form_box(
                        bar_4h_close_ms=t_close,
                        history_42_highs=last_42["high"].to_numpy(np.float64),
                        history_42_lows=last_42["low"].to_numpy(np.float64),
                        history_42_closes=last_42["close"].to_numpy(np.float64),
                        current_atr_a=atr_val
                    )
            sm_rec.process_1h_bar(
                open_time_ms=t_open, open_p=float(r["open"]), high_p=float(r["high"]),
                low_p=float(r["low"]), close_p=float(r["close"]), volume_v=float(r["volume"]),
                oi_timestamps=oi_ts, oi_values=oi_vals
            )
        st_rec = sm_rec.export_state()
        diff_recovery = (json.dumps(st_a, sort_keys=True) != json.dumps(st_rec, sort_keys=True))

        return {
            "symbol": symbol,
            "repeatability_pass": not diff_repeat,
            "chunked_batch_pass": not diff_chunked,
            "duplicate_injection_pass": not diff_dup,
            "crash_recovery_pass": not diff_recovery,
            "state_a": sm_a
        }

    # ══════════════════════ 合约规格 ══════════════════════

    def _resolve_spec(self, symbol: str, df_15m: pd.DataFrame,
                      cfg: EventTrendConfig):
        """解析该标的在**本次窗口**的合约规格。

        · 价格样本只用于**证伪**（观察价是否落在 tick 网格上），不用于推断 tick；
        · 规格缺失 / 无法证明历史适用 ⇒ `formal_pass_allowed=False`，
          该 run 只能作为**机制测试**，不得声明真实历史盈亏已修正。
        """
        ts = pd.to_numeric(df_15m["timestamp"], errors="coerce").dropna()
        ws = int(ts.min()) if len(ts) else None
        we = int(ts.max()) + 900_000 if len(ts) else None
        # 均匀抽样 2000 个真实收盘价做证伪检验（够用且不拖慢）
        closes = pd.to_numeric(df_15m["close"], errors="coerce").dropna()
        if len(closes) > 2000:
            step = max(1, len(closes) // 2000)
            closes = closes.iloc[::step]
        observed = [float(x) for x in closes.tolist() if float(x) > 0]
        return resolve_instrument_spec(
            symbol, window_start_ms=ws, window_end_ms=we,
            venue=getattr(cfg, "venue", "binance"),
            market=getattr(cfg, "market", "um"),
            order_type="MARKET", observed_prices=observed,
        )

    # ══════════════════════ 产物装配 ══════════════════════

    def _build_run_files(
        self, *, run_id: str, symbol: str, variant: str, sm_inst: EventStateMachine,
        res_inst: Dict[str, Any], funnel: Dict[str, Any], run_timestamp: int,
        data_summary: Dict[str, Any], fingerprints: Dict[str, Any]
    ) -> Dict[str, str]:
        """装配一个 Run 的**全部**产物文件（内存中），交给原子提交写盘。"""
        engine_result = res_inst["engine_result"]
        orders = res_inst.get("orders", [])
        ledger_events = res_inst.get("ledger_events", [])
        trades = engine_result.get("trades", [])
        equity_curve = engine_result.get("equity_curve", [])

        fills = [e for e in ledger_events if e.get("event_type") == "ORDER_FILLED"]
        opened = [e for e in ledger_events if e.get("event_type") == "POSITION_OPENED"]
        closed = [e for e in ledger_events if e.get("event_type") == "POSITION_CLOSED"]
        rejects = [e for e in ledger_events if e.get("event_type") == "SIGNAL_REJECTED"]
        funding_evs = [e for e in ledger_events if e.get("event_type") == "FUNDING_FEE"]

        inputs_meta = {
            "run_id": run_id,
            "symbol": symbol,
            "strategy_version": "v0.1.0",
            "variant": variant,
            "timestamp_created": run_timestamp,
            "data_summary": data_summary,
            "fingerprints": fingerprints,
            # 成交精度：规格来源/适用性（缺失即 formal_pass_allowed=False）
            "instrument_spec": res_inst.get("instrument_spec"),
            "spec_status": res_inst.get("spec_status"),
        }
        summary_meta = {
            "run_id": run_id, "symbol": symbol, "variant": variant,
            "funnel": funnel,
            "trading_summary": res_inst["summary"],
            "identity": res_inst.get("identity", {}),
            "reconcile": engine_result.get("reconcile", {}),
            "precision": res_inst.get("precision", {}),
            "instrument_spec": res_inst.get("instrument_spec"),
            "spec_status": res_inst.get("spec_status"),
            "formal_pass_allowed": bool(
                (res_inst.get("summary") or {}).get("formal_pass_allowed", False)),
        }
        files: Dict[str, str] = {
            "inputs.json": json.dumps(inputs_meta, indent=2, ensure_ascii=False, sort_keys=True),
            "summary.json": json.dumps(summary_meta, indent=2, ensure_ascii=False, sort_keys=True),
            "events.jsonl": _jsonl_text([ev.to_dict() for ev in
                                         getattr(sm_inst, "completed_events", [])]),
            "candidates.jsonl": _jsonl_text([c.to_dict() for c in
                                             getattr(sm_inst, "candidates", [])]),
            "transitions.jsonl": _jsonl_text([t.to_dict() for t in
                                              getattr(sm_inst, "transitions", [])]),
            "validation_results.jsonl": _jsonl_text([v.to_dict() for v in res_inst["validation_results"]]),
            # ── 全链路逐笔证据 ──
            "orders.jsonl": _jsonl_text(orders),
            "fills.jsonl": _jsonl_text(fills),
            "positions.jsonl": _jsonl_text(opened + closed),
            "trades.jsonl": _jsonl_text(trades),
            "rejects.jsonl": _jsonl_text(rejects),
            "funding.jsonl": _jsonl_text(funding_evs),
            "ledger.jsonl": _jsonl_text(ledger_events),
            "equity_curve.jsonl": _jsonl_text(equity_curve),
            "decisions.jsonl": _jsonl_text(engine_result.get("decisions", [])),
            # ── 成交精度证据（规格来源 / 适用性 / 拒绝明细）──
            "instrument_spec.json": json.dumps(
                {"instrument_spec": res_inst.get("instrument_spec"),
                 "spec_status": res_inst.get("spec_status"),
                 "precision": res_inst.get("precision", {})},
                indent=2, ensure_ascii=False, sort_keys=True),
        }
        return files

    def run_variant_abc(
        self,
        symbol: str,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        df_15m: pd.DataFrame,
        oi_ts: np.ndarray,
        oi_vals: np.ndarray,
        funding_events: List[dict],
        cfg: Optional[EventTrendConfig] = None,
        initial_equity: Optional[float] = None,
        create_variant_runs: bool = False
    ) -> Dict[str, Any]:
        """运行 A/B/C 三版本并**原子提交**完整产物与指纹。

        `initial_equity=None`（默认）⇒ 采用 `EventTrendConfig.initial_equity`。
        本函数**不设**自己的本金默认值，避免同一参数出现两个事实源。
        """
        cfg = cfg or EventTrendConfig()
        # ── 合约规格（tick / step / 最小量 / 最小名义额）──
        # 只认交易所 exchangeInfo 原始证据；窗口内的真实价格仅用于**证伪**。
        # 规格缺失/无法证明适用于该窗口 ⇒ formal_pass_allowed=False（不得当正式回测通过）。
        spec = self._resolve_spec(symbol, df_15m, cfg)
        # A: 完整版 (开 OI)
        sm_a = self.run_state_machine_simulation(symbol, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=True)
        # B: 关 OI 门槛
        sm_b = self.run_state_machine_simulation(symbol, df_4h, df_1h, oi_ts, oi_vals, enable_oi_filter=False)

        res_a = run_event_trend_backtest(
            symbol=symbol, df_1h=df_1h, df_15m=df_15m,
            candidates=sm_a.candidates, funding_events=funding_events,
            df_4h=df_4h, variant="A", initial_equity=initial_equity, config=cfg,
            instrument_spec=spec
        )
        res_b = run_event_trend_backtest(
            symbol=symbol, df_1h=df_1h, df_15m=df_15m,
            candidates=sm_b.candidates, funding_events=funding_events,
            df_4h=df_4h, variant="B", initial_equity=initial_equity, config=cfg,
            instrument_spec=spec
        )
        res_c = run_event_trend_backtest(
            symbol=symbol, df_1h=df_1h, df_15m=df_15m,
            candidates=sm_a.candidates, funding_events=funding_events,
            df_4h=df_4h, variant="C", initial_equity=initial_equity, config=cfg,
            instrument_spec=spec
        )

        funnel_a = {
            "boxes": len(sm_a.completed_events) + (1 if sm_a.active_event else 0),
            "sweeps": sum(1 for e in sm_a.completed_events if e.sweep_time_ms is not None),
            "retests": sum(1 for e in sm_a.completed_events if e.retest_time_ms is not None),
            "breakouts": sum(1 for e in sm_a.completed_events if e.breakout_time_ms is not None),
            "volume_rejected": sum(1 for e in sm_a.completed_events if e.current_state == EventState.VOLUME_REJECTED),
            "oi_rejected": sum(1 for e in sm_a.completed_events if e.current_state == EventState.OI_REJECTED),
            "oi_unavailable": sum(1 for e in sm_a.completed_events if e.current_state == EventState.OI_UNAVAILABLE),
            "expired": sum(1 for e in sm_a.completed_events if e.current_state == EventState.EXPIRED),
            "invalidated": sum(1 for e in sm_a.completed_events if e.current_state == EventState.INVALIDATED),
            "candidates": len(sm_a.candidates),
            "executed_trades": res_a["summary"]["trade_count"]
        }

        funnel_b = {
            "boxes": len(sm_b.completed_events) + (1 if sm_b.active_event else 0),
            "candidates": len(sm_b.candidates),
            "executed_trades": res_b["summary"]["trade_count"]
        }

        data_summary = {
            "bar_4h_count": len(df_4h),
            "bar_1h_count": len(df_1h),
            "bar_15m_count": len(df_15m),
            "oi_records_count": len(oi_ts),
            "funding_records_count": len(funding_events),
            "time_range_1h": {
                "start_ms": int(df_1h["timestamp"].iloc[0]),
                "end_ms": int(df_1h["timestamp"].iloc[-1]) + 3600 * 1000,
                "start_utc": str(pd.to_datetime(int(df_1h["timestamp"].iloc[0]), unit="ms")),
                "end_utc": str(pd.to_datetime(int(df_1h["timestamp"].iloc[-1]) + 3600 * 1000, unit="ms"))
            }
        }

        fingerprints = {
            "rules": self.rules_fingerprint(),
            "code": self.code_fingerprint(),
            "config": self.config_fingerprint(cfg),
            "inputs": self.input_fingerprint(df_4h, df_1h, df_15m, oi_ts, oi_vals, funding_events),
        }

        run_ids: Dict[str, str] = {}
        manifests: Dict[str, dict] = {}
        run_timestamp = int(time.time())
        # Run ID 唯一性：同一秒内重复调用不得覆盖已有 Run（原子提交要求目标不存在）
        while any(os.path.exists(os.path.join(
                self.runs_base, f"run_WX_EVENT_TREND_V0_1_{symbol}_{suf}_{run_timestamp}"))
                for suf in ("A", "B", "C", "")):
            run_timestamp += 1

        plan = [
            ("A", sm_a, res_a, funnel_a),
            ("B", sm_b, res_b, funnel_b),
            ("C", sm_a, res_c, funnel_a),
        ]
        if create_variant_runs:
            for v_name, sm_inst, res_inst, fun_inst in plan:
                run_id = f"run_WX_EVENT_TREND_V0_1_{symbol}_{v_name}_{run_timestamp}"
                run_dir = os.path.join(self.runs_base, run_id)
                files = self._build_run_files(
                    run_id=run_id, symbol=symbol, variant=v_name, sm_inst=sm_inst,
                    res_inst=res_inst, funnel=fun_inst, run_timestamp=run_timestamp,
                    data_summary=data_summary, fingerprints=fingerprints)
                manifests[v_name] = self._commit_run_dir(run_dir, files)
                run_ids[v_name] = run_id

        # 主运行目录：以 Variant A 真实回放为基准，附 A/B/C 三版本汇总
        main_run_id = f"run_WX_EVENT_TREND_V0_1_{symbol}_{run_timestamp}"
        main_run_dir = os.path.join(self.runs_base, main_run_id)
        main_files = self._build_run_files(
            run_id=main_run_id, symbol=symbol, variant="A", sm_inst=sm_a,
            res_inst=res_a, funnel=funnel_a, run_timestamp=run_timestamp,
            data_summary=data_summary, fingerprints=fingerprints)
        main_summary = json.loads(main_files["summary.json"])
        main_summary["summary_variant_a"] = res_a["summary"]
        main_summary["summary_variant_b"] = res_b["summary"]
        main_summary["summary_variant_c"] = res_c["summary"]
        main_summary["funnel_variant_b"] = funnel_b
        main_files["summary.json"] = json.dumps(main_summary, indent=2,
                                                ensure_ascii=False, sort_keys=True)
        main_manifest = self._commit_run_dir(main_run_dir, main_files)

        return {
            "main_run_id": main_run_id,
            "run_ids": run_ids,
            "symbol": symbol,
            "funnel_a": funnel_a,
            "funnel_b": funnel_b,
            "fingerprints": fingerprints,
            "manifests": {"main": main_manifest, **manifests},
            "res_a": res_a,
            "res_b": res_b,
            "res_c": res_c
        }
