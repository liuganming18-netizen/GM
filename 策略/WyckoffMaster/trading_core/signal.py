# -*- coding: utf-8 -*-
"""Shared Trading Core · Signal 契约（施工单 W2-4）。

四时点**必须分开**（此前一个 `timestamp` 混合四种语义）
──────────────────────────────────────────────────────
  signal_time   信号 bar **开盘**时刻（该 bar 开始的时间）
  confirm_time  信号 bar **收盘**时刻 = signal_time + signal_bar_seconds
  available_at  该信号**可被市场参与者知道**的时刻（≥ confirm_time）
  trade_time    该信号**实际被下单成交**的执行 bar 开盘时刻（≥ available_at）

对 OHLCV 驱动的原因（威科夫/唐奇安/ATR），`available_at == confirm_time` 是**可证明**的：
bar 在 confirm_time 闭合，其 OHLCV 在此刻已定。
对 OI 驱动的原因，`available_at` 取决于 OI 的**实际可见时间**（PIT）；
若无法证明，则该原因标记 `oi_pit_proven=False`，不得当作已知（见 W2-5）。

Signal 必须能回答七问（施工单 W2-4）
────────────────────────────────────
  什么时候产生？      signal_time / confirm_time
  为什么产生？        signal_type + conditions
  什么时候可以被知道？ available_at
  产生了什么方向？    direction
  为什么通过/拒绝？   reject_reason（空串 = 通过）
  使用了哪些因子？    factors（wyckoff / oi / oi_divergence / oi_flux）
  当时数据是否完整？  quality + data_quality

纯函数：不读库、不联网、不下单。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .contracts import payload_hash, stable_hash

SIGNAL_CONTRACT_VERSION = "signal_contract_v1.0.0"

# 四时点顺序约束的错误码
TIME_ORDER_VIOLATION = "time_order_violation"


def _f(v) -> Optional[float]:
    """NaN/Inf/None ⇒ None（**不用 0 冒充**）。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _i(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SignalEvent:
    """统一信号事件（Backtest / Paper / Live 三端同一份契约）。"""

    # —— 身份 ——
    signal_id: str
    strategy_key: str
    strategy_version: str
    symbol: str
    timeframe: str

    # —— 内容 ——
    signal_type: str
    direction: int                      # +1 long / -1 short / 0 无
    score: Optional[float] = None
    confidence: Optional[float] = None
    quality: str = ""
    reject_reason: str = ""

    # —— 四时点（毫秒 epoch）——
    signal_time: Optional[int] = None
    confirm_time: Optional[int] = None
    available_at: Optional[int] = None
    trade_time: Optional[int] = None

    # —— 溯源 ——
    source_id: str = ""
    conditions: Dict[str, Any] = field(default_factory=dict)
    factors: Dict[str, Any] = field(default_factory=dict)
    entry_policy: Dict[str, Any] = field(default_factory=dict)
    exit_policy: Dict[str, Any] = field(default_factory=dict)
    data_quality: Dict[str, Any] = field(default_factory=dict)
    exec_bar_open_ms: Optional[int] = None   # 与 trade_time 同源，保留原始值

    # ── 校验 ──
    def time_order_ok(self) -> bool:
        """四时点必须满足 `signal_time <= confirm_time <= available_at <= trade_time`。

        `trade_time` 未定（未进入执行）时不参与；其余三者必须齐备且有序。
        """
        st, ct, at = self.signal_time, self.confirm_time, self.available_at
        if st is None or ct is None or at is None:
            return False
        if not (st <= ct <= at):
            return False
        if self.trade_time is not None and at > self.trade_time:
            return False
        return True

    def identity(self) -> dict:
        """**参与哈希的身份块**（不含 trade_time：那是执行期才确定的派生量）。"""
        return {
            "signal_id": self.signal_id,
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "signal_type": self.signal_type,
            "direction": int(self.direction),
            "score": self.score,
            "quality": self.quality,
            "reject_reason": self.reject_reason,
            "signal_time": self.signal_time,
            "confirm_time": self.confirm_time,
            "available_at": self.available_at,
            "source_id": self.source_id,
            "conditions": self.conditions,
            "factors": self.factors,
        }

    def content_hash(self) -> str:
        return payload_hash(self.identity())

    def to_dict(self) -> dict:
        d = {
            "signal_id": self.signal_id,
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "signal_type": self.signal_type,
            "direction": int(self.direction),
            "signal_time": self.signal_time,
            "confirm_time": self.confirm_time,
            "available_at": self.available_at,
            "trade_time": self.trade_time,
            "source_id": self.source_id,
            "conditions": self.conditions,
            "factors": self.factors,
            "score": self.score,
            "confidence": self.confidence,
            "quality": self.quality,
            "entry_policy": self.entry_policy,
            "exit_policy": self.exit_policy,
            "data_quality": self.data_quality,
            "reject_reason": self.reject_reason,
            "exec_bar_open_ms": self.exec_bar_open_ms,
            "time_order_ok": self.time_order_ok(),
        }
        return d


def make_signal_id(strategy_key: str, symbol: str, timeframe: str,
                   signal_bar_open_ms: int, signal_type: str) -> str:
    """确定性 signal_id（禁止 uuid4 / 时间戳+随机）。"""
    return stable_hash(strategy_key, symbol, timeframe,
                       int(signal_bar_open_ms), signal_type, length=32)


def resolve_trade_time(confirm_time_ms: Optional[int],
                       exec_bar_opens_ms: Optional[Sequence[int]]) -> Optional[int]:
    """`trade_time` = 执行 bar 开盘时刻中**第一个 >= confirm_time** 者。

    与执行引擎的 `searchsorted(exec_opens, confirm, side="left")` **同一规则**，
    保证「信号契约里写的成交时点」与「引擎真实成交时点」不可能各说各话。
    执行 bar 序列缺失 ⇒ 返回 None（不猜）。
    """
    if confirm_time_ms is None or not exec_bar_opens_ms:
        return None
    lo, hi = 0, len(exec_bar_opens_ms)
    c = int(confirm_time_ms)
    while lo < hi:
        mid = (lo + hi) // 2
        if int(exec_bar_opens_ms[mid]) < c:
            lo = mid + 1
        else:
            hi = mid
    return int(exec_bar_opens_ms[lo]) if lo < len(exec_bar_opens_ms) else None


def build_signal_events(sig_frame, *, symbol: str, timeframe: str,
                        strategy_key: str, strategy_version: str,
                        signal_bar_seconds: int,
                        source_id: str = "",
                        entry_policy: Optional[dict] = None,
                        exit_policy: Optional[dict] = None,
                        exec_bar_opens_ms: Optional[Sequence[int]] = None,
                        include_rejected: bool = False,
                        oi_pit_proven: Optional[bool] = None,
                        ) -> List[SignalEvent]:
    """把 `core.compute_signals` 的帧转成**正式 Signal 契约**。

    只产出 `signal != 0` 的事件；`include_rejected=True` 时额外产出
    `reject_reason` 非空的**拒绝事件**（signal_type="none"），用于回答「为什么拒绝」。

    ⚠️ 单位边界：入参帧的 `timestamp` 为**毫秒**，而 `confirm_time` 列为**秒**
    （core 的既有口径，执行引擎按秒消费，不得改）。本函数统一输出**毫秒**。
    """
    ep = dict(entry_policy or {})
    xp = dict(exit_policy or {})
    out: List[SignalEvent] = []
    if sig_frame is None or len(sig_frame) == 0:
        return out

    cols = set(sig_frame.columns)
    has_oi = "oi_resonance_score" in cols

    for _, r in sig_frame.iterrows():
        s = int(r["signal"])
        bar_open = _i(r["timestamp"])
        # 单位边界（必须显式，不得靠启发式阈值猜）：
        #   `core.compute_signals` 的 `confirm_time` 列以 **epoch 秒**给出
        #   （`to_epoch_seconds(ts) + signal_bar_seconds`，执行引擎按秒 searchsorted 消费），
        #   而本契约的四时点**一律毫秒**。因此这里用与 core **逐位等价**的定义重算：
        #       confirm_ms = signal_time(ms) + signal_bar_seconds × 1000
        #   仅在 `timestamp` 缺失时才退回该列，并按「秒 → 毫秒」换算。
        raw_conf = _i(r.get("confirm_time"))
        if bar_open is not None:
            confirm = int(bar_open) + int(signal_bar_seconds) * 1000
        elif raw_conf is not None:
            confirm = int(raw_conf) * 1000
        else:
            confirm = None
        reject = str(r.get("reject_reason") or "")
        if s == 0 and not (include_rejected and reject):
            continue
        name = str(r.get("signal_name") or "none")
        sid = make_signal_id(strategy_key, symbol, timeframe, bar_open or -1,
                             name if s != 0 else f"rejected:{reject}")
        prov = oi_pit_proven
        if prov is None and "oi_pit_proven" in cols:
            try:
                prov = bool(r["oi_pit_proven"])
            except Exception:                            # noqa: BLE001
                prov = None
        factors = {
            "wyckoff": {
                "signal_name": name,
                "dc_high_prev": _f(r.get("dc_high_prev")),
                "dc_low_prev": _f(r.get("dc_low_prev")),
                "exit_low_curr": _f(r.get("exit_low_curr")),
                "exit_high_curr": _f(r.get("exit_high_curr")),
                "atr": _f(r.get("atr")),
            },
            "oi": {
                "score": _f(r.get("oi_resonance_score")) if has_oi else None,
                "level_rank": _f(r.get("f_oi_level_rank")),
                "available": (bool(r.get("oi_resonance_available")) if has_oi else None),
                "pit_proven": prov,
                "quality": (str(r.get("oi_resonance_quality")) if has_oi else None),
                "history_bars": _i(r.get("oi_history_bars")),
                "history_required": _i(r.get("oi_history_required")),
            },
            "oi_divergence": {
                "raw": _f(r.get("f_oi_price_div")),
                "rank": _f(r.get("f_oi_price_div_rank")),
            },
            "oi_flux": {
                "raw": _f(r.get("f_oi_mom_flux")),
                "rank": _f(r.get("f_oi_mom_flux_rank")),
            },
        }
        ev = SignalEvent(
            signal_id=sid, strategy_key=strategy_key, strategy_version=strategy_version,
            symbol=symbol, timeframe=timeframe,
            signal_type=name if s != 0 else "none",
            direction=int(r.get("direction") or 0),
            score=_f(r.get("oi_resonance_score")),
            confidence=_f(r.get("stop_atr_mult")),
            quality=(str(r.get("oi_resonance_quality")) if has_oi else ""),
            reject_reason=reject,
            signal_time=bar_open, confirm_time=confirm,
            available_at=confirm,                       # OHLCV 在收盘即可见（可证明）
            trade_time=resolve_trade_time(confirm, exec_bar_opens_ms),
            source_id=source_id,
            conditions={"stop_price": _f(r.get("stop_price")),
                        "stop_mode": str(r.get("stop_mode") or ""),
                        "structural_ref": _f(r.get("structural_ref"))},
            factors=factors,
            entry_policy=ep, exit_policy=xp,
            data_quality={"time_unit": (sig_frame.attrs or {}).get("time_unit"),
                          "dedup": (sig_frame.attrs or {}).get("dedup_report"),
                          "oi_factor_meta": (sig_frame.attrs or {}).get("oi_factor_meta")},
            exec_bar_open_ms=None,
        )
        out.append(ev)
    return out


__all__ = [
    "SIGNAL_CONTRACT_VERSION", "TIME_ORDER_VIOLATION", "SignalEvent",
    "make_signal_id", "resolve_trade_time", "build_signal_events",
]
