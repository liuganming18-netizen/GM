# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 · 合成组合执行（**仅模拟**，无 IO / 无账户 / 无下单路径）。

相对附件稿的修复（逐条）
────────────────────────
1. **费用率不再硬编码**：附件 `_compute_unrealized_pnl` 里写死 `0.0005`，与 `cfg.fee_rate`
   脱钩（改费率则浮盈估算仍按 0.0005）。本模块一律用 `cfg.fee_rate`。
2. **开盘动作先于 bar 内事件**：同一根 bar 固定相位 ——
   A 开盘：①锁利生效 ②资金费结算 → B 开盘：跳空退出 → C 开盘：新开仓
   → D bar 内：止损检查（**含当根新开仓**） → E 收盘：盯市与权益。
3. **新开仓当根必须检查止损**：相位 D 对**本根刚建立**的仓位同样做 `low <= stop` 判定。
4. **跳空退出不得用未来 low 提前释放资金**：相位 B 只用 `open`；bar 内止损在**开仓之后**才发生，
   因此 bar 内退出释放的现金**不可能**被同一根 bar 的开仓使用（开仓在相位 C 已完成）。
5. **真实成交价 / 取整后再算风险与止损**：`entry_fill = round_buy(open×(1+滑点), step)`，
   止损由 `entry_fill` 推出，风险 `= |entry_fill − 止损成交价| + 双边费用`。
6. **`force_close_at_end=False` 仍返回真实 MTM 权益**：`final_equity = cash + 未实现`，
   且恒等于 `equity_curve[-1]["total_equity"]`。
7. **入场费用只记一次**：开仓时 `cash -= entry_fee`；未实现盈亏只估**出场**费用。
8. **期末强平后对账**：现金 / 毛盈亏 / 净盈亏 / 资金费 / 权益逐项恒等式（见 `reconcile`）。
9. **资金费模型显式**：`assumption_fixed_8h` 是**显式假设模型**（结果恒带
   `funding_is_assumption=True`）；`settlement_events` 消费真实事件 `(time, rate, mark)`，
   缺 rate 或 mark ⇒ 记 `funding_gap`，**不补 0**。默认 `settlement_events`（fail-closed）。
10. **时间单位显式且多币一致**：不接受 `max/median` 推断；多币单位不一致直接拒绝。
11. **组合风险按入场成本本金计**：`long loss=(entry−stop×(1−slip))×qty`、
    `short loss=(stop×(1+slip)−entry)×qty`，**不得**用 `max/min(entry, last_mark)` 把浮盈
    计入本金风险（浮盈越大反而放行更大仓位）。
12. **锁利通道优先取当期列**：`exit_low_curr/exit_high_curr`（不 shift，当根收盘可得），
    缺列才回退 `*_prev`；映射到「信号 bar 收盘确认之后的**下一根 15m**」才生效。

2026-10-16 后端接入轮的最小兼容修复（加性，默认值下逐位兼容）
──────────────────────────────────────────────────────────────
13. **预热段不交易（`trade_start_sec`）**：可选参数；只接受 `confirm_time >= trade_start_sec`
    的信号与其锁利通道。预热 bar 仍参与指标/因子滚动计算（保证窗口起点指标已热），
    但其信号**不得**经 searchsorted 落到窗口首根 15m 而在窗口内开仓。默认 None = 旧行为。
14. **资金费起算点 = 入场时刻**：`OpenPosition.last_funding_ms` 由 `-10**9` 改为入场毫秒。
    旧值会让新仓在入场后第一根 bar 把「从 -1e9 到当前」的**全部历史结算事件**一次性入账
    （给新仓收开仓前资金费）。改后只结算 `(entry_ms, t_ms]` 的真实事件（只持仓区间），
    退出 bar 开盘前的应结事件不丢。
15. **保护止损轨迹实况记录**：`OpenPosition.stop_trace` 在初始化与每次单调锁利上抬/下压时
    追加 `{"ts","stop","reason"}`；平仓记录携带 `stop_trace`。供图表按**引擎实况**取点，
    不重建、不造点。仅新增键，不改既有字段与行为。
16. **资金费口径 = 原始毫秒（不截秒）**：`_funding_events_ms` 把事件规范化为**毫秒**，
    `_settle_funding` 判据为 `pos.last_funding_ms < ev_ms <= t_ms`。
    此前按 `//1000` 截秒比较，会把「开仓后 2ms 的真实结算事件」与开仓时刻判成同一秒而
    **漏计**（币安 `funding_time` 是毫秒，结算时刻常落在整秒后数毫秒）。
    **既有纯 seconds 输入仍兼容**：`time_sec` ⇒ ×1000；`funding_time` 默认按毫秒（币安口径），
    事件自带 `time_unit="s"` 或调用方声明 `funding_time_unit="s"` 时按秒。
17. **入场信号可追溯**：`pend_sig_bar` 记录每个执行 bar 上待开仓信号的**信号 bar 开盘毫秒**，
    开仓决策携带 `signal_bar_open_ms` ⇒ 账本可由 `trade_id` 联结到**原始信号**（非事后猜测）。

真实下单**恒禁用**：本模块不存在任何下单 / 写库 / 联网路径（`assert_live_disabled()` 为硬闸）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import factors as F
from .config import EXEC_FROZEN, ExecParams, SIGNAL_FROZEN, SignalParams
from .core import SIG_NONE, compute_signals


class LiveOrderForbidden(RuntimeError):
    """本模块为纯合成模拟；任何「真实下单」诉求一律硬拒绝。"""


class ExecutionInputError(ValueError):
    """执行层输入不合法（缺 intrabar bar / 缺时间单位 / 单位不一致）。"""


def assert_live_disabled() -> None:
    """硬闸：真实下单在本包**不存在实现**，调用即抛。"""
    raise LiveOrderForbidden(
        "live_order_forbidden: WX_OI_RESONANCE_V1 只有合成模拟，无任何真实下单路径")


def round_buy_price(px: float, step: float) -> float:
    """买入价向**更贵**取整（step<=0 不取整）。"""
    if not step or step <= 0:
        return float(px)
    return float(step * math.ceil(px / step - 1e-9))


def round_sell_price(px: float, step: float) -> float:
    """卖出价向**更便宜**取整。"""
    if not step or step <= 0:
        return float(px)
    return float(step * math.floor(px / step + 1e-9))


def round_qty_down(qty: float, step: float) -> float:
    if not step or step <= 0:
        return float(qty)
    return float(step * math.floor(qty / step + 1e-9))


# ══════════════════════════ 数据结构 ══════════════════════════

@dataclass
class OpenPosition:
    symbol: str
    direction: int
    signal_type: int
    entry_time: int
    entry_time_sec: int
    entry_price: float
    qty: float
    initial_stop: float
    current_stop: float
    entry_fee: float
    last_mark_price: float
    # 上次资金费结算时刻（**毫秒**；入场时初始化为入场 bar 开盘毫秒）。
    # 判据 `last_funding_ms < ev_ms <= t_ms` ⇒ 只结算**持仓区间内**的真实结算事件。
    last_funding_ms: int = -10 ** 15
    cum_funding_fee: float = 0.0
    funding_gaps: List[dict] = field(default_factory=list)
    entry_slip_loss: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    # 保护止损轨迹（**引擎实况记录**，供图表真实取点；不造点）：
    #   [{"ts": 毫秒, "stop": 保护价, "reason": "init"|"trail"}]
    stop_trace: List[dict] = field(default_factory=list)


# ══════════════════════════ 资产数组装配 ══════════════════════════

def _prep_symbol(sym: str, sig_bars: pd.DataFrame, bar_bars: pd.DataFrame,
                 params: SignalParams, unit: str, price_step: float,
                 trade_start_sec: Optional[int] = None) -> dict:
    """装配单币数组。`sig_bars` **始终**是原始信号周期 bar，信号由本函数内部
    `compute_signals` 现算（不做「已算好帧」的信任捷径，避免绕过输入校验）。

    `trade_start_sec`（可选，默认 None = 旧行为）：只接受 `confirm_time >= trade_start_sec`
    的信号与其锁利通道值。用于「预热段单独喂数据、不交易」——预热 bar 仍参与指标与因子
    的滚动计算（保证窗口起点指标已热），但其信号**不得**在窗口内开仓。缺省 None 时
    行为与加参数前逐位一致。
    """
    if bar_bars is None or len(bar_bars) == 0:
        raise ExecutionInputError(f"missing_intrabar_bars:{sym}")
    for c in ("timestamp", "open", "high", "low", "close"):
        if c not in bar_bars.columns:
            raise ExecutionInputError(f"missing_intrabar_column:{sym}:{c}")

    sig = compute_signals(sig_bars, params, unit, price_step)
    t15_raw = bar_bars["timestamp"].to_numpy(np.int64)
    t15_sec = F.to_epoch_seconds(t15_raw, unit)

    o15 = bar_bars["open"].to_numpy(np.float64)
    h15 = bar_bars["high"].to_numpy(np.float64)
    l15 = bar_bars["low"].to_numpy(np.float64)
    c15 = bar_bars["close"].to_numpy(np.float64)
    if not all(np.all(np.isfinite(x)) for x in (o15, h15, l15, c15)):
        raise ExecutionInputError(f"non_finite_intrabar_ohlc:{sym}")

    n15 = len(t15_raw)
    pend_sig = np.zeros(n15, np.int64)
    pend_dir = np.zeros(n15, np.int64)
    pend_stop = np.full(n15, np.nan, np.float64)
    pend_res = np.full(n15, np.nan, np.float64)
    # 待开仓信号所属**信号 bar 开盘毫秒**（-1 = 无）⇒ 账本可联结回原始信号
    pend_sig_bar = np.full(n15, -1, np.int64)
    conf_exit_low = np.full(n15, np.nan, np.float64)
    conf_exit_high = np.full(n15, np.nan, np.float64)

    if len(sig):
        tsig_confirm = sig["confirm_time"].to_numpy(np.int64)
        sig_bar_ms = sig["timestamp"].to_numpy(np.int64)
        s_arr = sig["signal"].to_numpy(np.int64)
        d_arr = sig["direction"].to_numpy(np.int64)
        st_arr = sig["stop_price"].to_numpy(np.float64)
        rs_arr = sig[F.C_SCORE].to_numpy(np.float64)
        # **优先当期通道**（收盘后可得）；缺列才回退到 prev（严格更早窗口）
        lo_col = "exit_low_curr" if "exit_low_curr" in sig.columns else "exit_low_prev"
        hi_col = "exit_high_curr" if "exit_high_curr" in sig.columns else "exit_high_prev"
        el_arr = sig[lo_col].to_numpy(np.float64)
        eh_arr = sig[hi_col].to_numpy(np.float64)
        idx = np.searchsorted(t15_sec, tsig_confirm, side="left")
        for k in range(len(tsig_confirm)):
            j = int(idx[k])
            if j >= n15:
                continue
            # 预热不交易：`confirm_time < trade_start_sec` 的信号与通道值一律丢弃。
            # 必须在写 pend_*/conf_* 之前 continue，否则预热信号会经 searchsorted 落到
            # 窗口首根 15m（j=0）而在窗口内开仓（前视/越界交易）。
            if trade_start_sec is not None and int(tsig_confirm[k]) < int(trade_start_sec):
                continue
            if s_arr[k] != SIG_NONE:
                pend_sig[j] = s_arr[k]
                pend_dir[j] = d_arr[k]
                pend_stop[j] = st_arr[k]
                pend_res[j] = rs_arr[k]
                pend_sig_bar[j] = int(sig_bar_ms[k])
            conf_exit_low[j] = el_arr[k]
            conf_exit_high[j] = eh_arr[k]

    return {
        "symbol": sym,
        "timestamp": t15_raw,
        "timestamp_sec": t15_sec,
        "open": o15, "high": h15, "low": l15, "close": c15,
        "funding_rate": (bar_bars["funding_rate"].to_numpy(np.float64)
                         if "funding_rate" in bar_bars.columns
                         else np.full(n15, np.nan, np.float64)),
        "pending_sig": pend_sig, "pending_dir": pend_dir,
        "pending_stop": pend_stop, "pending_res": pend_res,
        "pending_sig_bar": pend_sig_bar,
        "exit_low": pd.Series(conf_exit_low).ffill().to_numpy(np.float64),
        "exit_high": pd.Series(conf_exit_high).ffill().to_numpy(np.float64),
        "exit_channel_source": (lo_col if len(sig) else None),
        "signal_frame": sig,
    }


# ══════════════════════════ 盈亏 / 风险 ══════════════════════════

def unrealized_pnl(positions: Dict[str, OpenPosition], fee_rate: float) -> float:
    """未实现盈亏 = Σ[(mark − entry)×qty×dir − 预估**出场**费用]（入场费已在现金中扣过）。"""
    tot = 0.0
    for p in positions.values():
        gross = (p.last_mark_price - p.entry_price) * p.qty * p.direction
        tot += gross - p.last_mark_price * p.qty * fee_rate
    return float(tot)


def unrealized_net_open(positions: Dict[str, OpenPosition], fee_rate: float) -> float:
    """未实现**净**盈亏（再扣一次入场费与已结算资金费），用于权益恒等式对账。"""
    tot = 0.0
    for p in positions.values():
        gross = (p.last_mark_price - p.entry_price) * p.qty * p.direction
        tot += (gross - p.entry_fee - p.cum_funding_fee
                - p.last_mark_price * p.qty * fee_rate)
    return float(tot)


def portfolio_open_risk(positions: Dict[str, OpenPosition], fee_rate: float,
                        slip_rate: float) -> float:
    """组合**本金风险**（入场成本口径）：按「入场价 → 止损价」的损失算，**不含浮盈**。

    long  : exp_exit = stop×(1−slip)；loss = (entry_price − exp_exit) × qty
    short : exp_exit = stop×(1+slip)；loss = (exp_exit − entry_price) × qty
    total += max(0, loss + entry_fee + exp_exit × qty × fee_rate)

    修复：旧稿用 `max(entry, last_mark)`（多）/ `min(...)`（空）作基准，会把**浮盈**算进
    风险预算 ⇒ 浮盈越大反而允许开更多仓（风险随行情放大）。本金风险必须只看**入场成本**，
    与 `last_mark_price` 无关。
    """
    tot = 0.0
    for p in positions.values():
        if p.direction == 1:
            exp_exit = p.current_stop * (1.0 - slip_rate)
            loss = (p.entry_price - exp_exit) * p.qty
        else:
            exp_exit = p.current_stop * (1.0 + slip_rate)
            loss = (exp_exit - p.entry_price) * p.qty
        tot += max(0.0, loss + p.entry_fee + exp_exit * p.qty * fee_rate)
    return float(tot)


# ══════════════════════════ 资金费 ══════════════════════════

def _settle_funding(pos: OpenPosition, cash: float, *, model: str, t_ms: int,
                    open_px: float, rate, events: List[dict],
                    funding_is_assumption: bool) -> float:
    """相位 A②：资金费结算。缺 rate/mark ⇒ 记 `funding_gap`，**不补 0**。

    `settlement_events` 模型下的区间判据为 **毫秒**：`pos.last_funding_ms < ev_ms <= t_ms`。
    不截秒 ⇒ 开仓后数毫秒的真实结算事件同样入账（不漏计、不靠延后改时间绕过）。
    """
    if model == "assumption_fixed_8h":
        t_sec = int(t_ms // 1000)
        if t_sec % 28800 != 0 or (t_ms - pos.last_funding_ms) < 28800_000:
            return cash
        if rate is None or not math.isfinite(float(rate)):
            pos.funding_gaps.append({"time_ms": int(t_ms), "time_sec": t_sec,
                                     "reason": "funding_rate_missing_assumption_model"})
            pos.last_funding_ms = int(t_ms)
            return cash
        cost = (open_px * pos.qty) * float(rate) * pos.direction
        pos.cum_funding_fee += cost
        pos.last_funding_ms = int(t_ms)
        return cash - cost

    # settlement_events：消费真实结算事件（毫秒）
    for ev in events or []:
        ev_ms = int(ev.get("time_ms"))
        if not (pos.last_funding_ms < ev_ms <= int(t_ms)):
            continue
        r, m = ev.get("rate"), ev.get("mark_price")
        ok_r = r is not None and isinstance(r, (int, float)) and not isinstance(r, bool) \
            and math.isfinite(float(r))
        ok_m = m is not None and isinstance(m, (int, float)) and not isinstance(m, bool) \
            and math.isfinite(float(m)) and float(m) > 0
        if not (ok_r and ok_m):
            pos.funding_gaps.append({
                "time_ms": ev_ms, "time_sec": ev_ms // 1000,
                "reason": ("funding_event_missing_fields:"
                           + ("rate" if not ok_r else "") + ("mark" if not ok_m else "")),
                "rate": r, "mark_price": m})
            pos.last_funding_ms = ev_ms
            continue
        cost = (float(m) * pos.qty) * float(r) * pos.direction
        pos.cum_funding_fee += cost
        pos.last_funding_ms = ev_ms
        cash -= cost
    return cash


def _apply_trail(pos: OpenPosition, arr: dict, i: int) -> None:
    """把**当期**锁利通道（`exit_low_curr` / `exit_high_curr`，已按「收盘确认 → 下一根 15m」
    映射）作用于持仓止损：多头只上抬、空头只下压（单调，不回填）。

    对**当根新建**仓位同样适用：该通道值在当根开盘前即已确认。
    """
    if pos.direction == 1:
        lvl = float(arr["exit_low"][i])
        if math.isfinite(lvl) and lvl > pos.current_stop:
            pos.current_stop = lvl
            pos.stop_trace.append({"ts": int(arr["timestamp"][i]), "stop": float(lvl),
                                   "reason": "trail"})
    else:
        lvl = float(arr["exit_high"][i])
        if math.isfinite(lvl) and lvl < pos.current_stop:
            pos.current_stop = lvl
            pos.stop_trace.append({"ts": int(arr["timestamp"][i]), "stop": float(lvl),
                                   "reason": "trail"})


def _funding_events_ms(events, default_unit: Optional[str] = None) -> List[dict]:
    """规范化资金费事件 → **毫秒**（保留原始精度，不截秒）。

    接受（按优先级）：
      · `time_ms` / `funding_time_ms`：毫秒，原样保留；
      · `time_sec`：秒（int/float）⇒ ×1000（**既有纯 seconds 输入兼容**）；
      · `funding_time` / `time`：币安口径**毫秒**；仅当事件自带 `time_unit="s"`
        或调用方 `default_unit="s"` 时按秒 ×1000。

    未识别 / 缺时间字段 ⇒ 跳过（**不猜单位、不补 0**）。返回按 `time_ms` 升序。
    """
    out: List[dict] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        unit = ev.get("time_unit") or default_unit
        t_ms: Optional[int] = None
        if ev.get("time_ms") is not None:
            t_ms = int(round(float(ev["time_ms"])))
        elif ev.get("funding_time_ms") is not None:
            t_ms = int(round(float(ev["funding_time_ms"])))
        elif ev.get("time_sec") is not None:
            t_ms = int(round(float(ev["time_sec"]) * 1000.0))
        else:
            for key in ("funding_time", "time"):
                if ev.get(key) is not None:
                    v = float(ev[key])
                    t_ms = int(round(v * 1000.0)) if unit == "s" else int(round(v))
                    break
        if t_ms is None:
            continue
        out.append({"time_ms": t_ms, "time_sec": t_ms // 1000,
                    "rate": ev.get("rate"), "mark_price": ev.get("mark_price")})
    out.sort(key=lambda x: x["time_ms"])
    return out


# 兼容旧名（旧调用方按「秒」语义调用；此处已改为**毫秒**规范化，
# 输出键由 `time_sec` 变为 `time_ms`，旧内部调用点已全部更新）。
_funding_events_by_sec = _funding_events_ms


# ══════════════════════════ 主入口 ══════════════════════════

def run_portfolio_execution(market_data: Dict[str, dict],
                            strategy_params: Optional[SignalParams] = None,
                            exec_cfg: Optional[ExecParams] = None,
                            time_unit=None,
                            trade_start_sec: Optional[int] = None) -> Dict[str, Any]:
    """多币组合合成执行。

    `market_data[sym]` = {
        "signal_bars": DataFrame（信号周期，已收盘）,
        "intrabar_bars": DataFrame（执行周期，需 timestamp/open/high/low/close）,
        "time_unit": 可选，覆盖顶层声明（多币不一致 ⇒ 拒绝）,
        "funding_events": 可选 [{"time_sec"|"time_ms", "rate", "mark_price"}],
    }

    `trade_start_sec`（可选，默认 None = 旧行为）：只接受 `confirm_time >= trade_start_sec`
    的信号。用于「预热段喂数据但不交易」：预热 bar 仍参与指标/因子滚动计算，其信号不开仓。
    缺省 None 时与加参数前行为逐位一致（向后兼容）。
    """
    p = strategy_params or SIGNAL_FROZEN
    cfg = exec_cfg or EXEC_FROZEN
    if cfg.live_order_enabled:
        raise LiveOrderForbidden("live_order_enabled_must_be_false")
    if not market_data:
        return _empty_result(cfg, trade_start_sec)

    # —— 时间单位：显式 + 多币一致 ——
    declared = {s: (d.get("time_unit") or time_unit) for s, d in market_data.items()}
    unit = F.assert_single_time_unit(declared)

    slip = float(cfg.slippage_bps) * 1e-4
    taker_fee = float(getattr(cfg, "taker_fee_rate", cfg.fee_rate))
    maker_fee = float(getattr(cfg, "maker_fee_rate", 0.0002))
    fee = taker_fee
    step = float(p.price_step)

    arrays: Dict[str, dict] = {}
    for sym in sorted(market_data):
        d = market_data[sym]
        if "signal_bars" not in d or d["signal_bars"] is None:
            raise ExecutionInputError(f"missing_signal_bars:{sym}")
        arrays[sym] = _prep_symbol(sym, d["signal_bars"], d.get("intrabar_bars"),
                                   p, unit, step, trade_start_sec)

    timeline = np.unique(np.concatenate([arrays[s]["timestamp"] for s in arrays]))
    cursors = {s: 0 for s in arrays}
    lengths = {s: len(arrays[s]["timestamp"]) for s in arrays}

    cash = float(cfg.initial_equity)
    open_positions: Dict[str, OpenPosition] = {}
    closed: List[dict] = []
    decisions: List[dict] = []
    rejected: List[dict] = []
    equity_curve: List[dict] = []
    funding_gaps_all: List[dict] = []
    sum_gross = sum_entry_fee = sum_exit_fee = 0.0
    funding_is_assumption = (cfg.funding_model == "assumption_fixed_8h")

    for t_now in timeline:
        active = []
        for sym, arr in arrays.items():
            i = cursors[sym]
            if i < lengths[sym] and int(arr["timestamp"][i]) == int(t_now):
                active.append((sym, i))
                cursors[sym] = i + 1
        active.sort()
        if not active:
            continue

        # ── 相位 A：开盘（锁利生效 → 资金费结算）──
        for sym, i in active:
            pos = open_positions.get(sym)
            if pos is None:
                continue
            arr = arrays[sym]
            pos.last_mark_price = float(arr["open"][i])
            if cfg.enable_trailing_exit:
                _apply_trail(pos, arr, i)
            evs = _funding_events_ms(market_data[sym].get("funding_events"),
                                     market_data[sym].get("funding_time_unit"))
            cash = _settle_funding(
                pos, cash, model=cfg.funding_model, t_ms=int(arr["timestamp"][i]),
                open_px=float(arr["open"][i]), rate=arr["funding_rate"][i], events=evs,
                funding_is_assumption=funding_is_assumption)

        # ── 相位 B：跳空退出（**只用 open**）──
        for sym, i in active:
            pos = open_positions.get(sym)
            if pos is None:
                continue
            arr = arrays[sym]
            op = float(arr["open"][i])
            gap = ((pos.direction == 1 and op <= pos.current_stop)
                   or (pos.direction == -1 and op >= pos.current_stop))
            if not gap:
                continue
            fill = round_sell_price(op * (1.0 - slip), step) if pos.direction == 1 \
                else round_buy_price(op * (1.0 + slip), step)
            cash, rec = _close_position(pos, cash, fill, fee, int(t_now),
                                        "gap_open_stop", open_positions, sym, raw_target_px=op)
            closed.append(rec)
            sum_gross += rec["gross_pnl"]
            sum_exit_fee += rec["exit_fee"]        # 入场费已在相位 C 计入一次，此处不得重复
            decisions.append({"timestamp": int(t_now), "symbol": sym, "phase": "B_open_gap",
                              "action": "exit", "reason": "gap_open_stop", "price": fill})

        # ── 相位 C：新开仓（用本 bar 的 open；取整后再算风险/止损）──
        if cfg.allow_new_entries:
            cands = []
            for sym, i in active:
                arr = arrays[sym]
                if arr["pending_sig"][i] != SIG_NONE:
                    r = arr["pending_res"][i]
                    cands.append((-(r if math.isfinite(r) else -1.0), sym, i))
            cands.sort(key=lambda x: (x[0], x[1]))
            for _, sym, i in cands:
                if sym in open_positions:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "duplicate_symbol_active"})
                    continue
                max_trades = int(getattr(cfg, "max_concurrent_trades", 3))
                if len(open_positions) >= max_trades:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "max_concurrent_trades_reached"})
                    continue
                arr = arrays[sym]
                direction = int(arr["pending_dir"][i])
                sig_type = int(arr["pending_sig"][i])
                raw_open = float(arr["open"][i])
                stop_ref = float(arr["pending_stop"][i])
                if not math.isfinite(stop_ref) or stop_ref <= 0:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "invalid_signal_stop"})
                    continue
                entry_fill = (round_buy_price(raw_open * (1.0 + slip), step)
                              if direction == 1
                              else round_sell_price(raw_open * (1.0 - slip), step))
                if (direction == 1 and entry_fill <= stop_ref) or \
                        (direction == -1 and entry_fill >= stop_ref):
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry",
                                     "reason": "open_gapped_beyond_stop"})
                    continue
                mtm = cash + unrealized_pnl(open_positions, fee)
                if mtm <= 0:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "nonpositive_equity"})
                    continue
                stop_fill = (stop_ref * (1.0 - slip) if direction == 1
                              else stop_ref * (1.0 + slip))
                unit_risk = abs(entry_fill - stop_fill) + (entry_fill + stop_fill) * fee
                if unit_risk <= 0:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "zero_unit_risk"})
                    continue
                cur_risk = portfolio_open_risk(open_positions, fee, slip)
                if (cfg.max_portfolio_risk * mtm - cur_risk) < cfg.risk_per_trade * mtm:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry",
                                     "reason": "portfolio_risk_cap_exceeded"})
                    continue
                max_margin_pct = float(getattr(cfg, "max_portfolio_margin_pct", 0.40))
                cur_notional = sum(p.qty * p.last_mark_price for p in open_positions.values())
                max_allowed_notional = mtm * max_margin_pct * cfg.max_leverage_per_symbol
                avail_notional = max(0.0, max_allowed_notional - cur_notional)
                if avail_notional <= 0:
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "portfolio_margin_cap_exceeded"})
                    continue
                qty = min(cfg.risk_per_trade * mtm / unit_risk,
                          mtm * cfg.max_leverage_per_symbol / entry_fill,
                          avail_notional / entry_fill)
                if qty <= 0 or not math.isfinite(qty):
                    rejected.append({"timestamp": int(t_now), "symbol": sym,
                                     "phase": "C_entry", "reason": "nonpositive_qty"})
                    continue
                e_fee = entry_fill * qty * fee
                cash -= e_fee
                sum_entry_fee += e_fee
                entry_slip_loss = abs(entry_fill - raw_open) * qty
                open_positions[sym] = OpenPosition(
                    symbol=sym, direction=direction, signal_type=sig_type,
                    entry_time=int(t_now), entry_time_sec=int(arr["timestamp_sec"][i]),
                    entry_price=entry_fill, qty=qty, initial_stop=stop_ref,
                    current_stop=stop_ref, entry_fee=e_fee, last_mark_price=entry_fill,
                    entry_slip_loss=entry_slip_loss,
                    last_funding_ms=int(arr["timestamp"][i]))
                if cfg.enable_trailing_exit:
                    _apply_trail(open_positions[sym], arr, i)
                open_positions[sym].stop_trace.insert(
                    0, {"ts": int(arr["timestamp"][i]), "stop": float(stop_ref),
                        "reason": "init"})
                decisions.append({"timestamp": int(t_now), "symbol": sym, "phase": "C_entry",
                                  "action": "entry", "signal_type": sig_type,
                                  "direction": direction, "price": entry_fill,
                                  "stop_price": stop_ref, "qty": qty,
                                  "entry_fee": e_fee,
                                  # 原始信号 bar 开盘毫秒（-1 = 无）⇒ 账本可联结回信号
                                  "signal_bar_open_ms": int(arr["pending_sig_bar"][i])})

        # ── 相位 D：bar 内止损（**含当根新开仓**）──
        for sym, i in active:
            pos = open_positions.get(sym)
            if pos is None:
                continue
            arr = arrays[sym]
            hi, lo, cl = float(arr["high"][i]), float(arr["low"][i]), float(arr["close"][i])
            if pos.direction == 1:
                pos.mfe = max(pos.mfe, (hi - pos.entry_price) / pos.entry_price)
                pos.mae = max(pos.mae, (pos.entry_price - lo) / pos.entry_price)
                hit = lo <= pos.current_stop
                fill = round_sell_price(pos.current_stop * (1.0 - slip), step)
            else:
                pos.mfe = max(pos.mfe, (pos.entry_price - lo) / pos.entry_price)
                pos.mae = max(pos.mae, (hi - pos.entry_price) / pos.entry_price)
                hit = hi >= pos.current_stop
                fill = round_buy_price(pos.current_stop * (1.0 + slip), step)
            if hit:
                cash, rec = _close_position(pos, cash, fill, fee, int(t_now),
                                            "intrabar_stop", open_positions, sym, raw_target_px=pos.current_stop)
                closed.append(rec)
                sum_gross += rec["gross_pnl"]
                sum_exit_fee += rec["exit_fee"]    # 入场费已在相位 C 计入一次，此处不得重复
                decisions.append({"timestamp": int(t_now), "symbol": sym,
                                  "phase": "D_intrabar", "action": "exit",
                                  "reason": "intrabar_stop", "price": fill})
            else:
                pos.last_mark_price = cl

        # ── 相位 E：盯市与权益 ──
        un = unrealized_pnl(open_positions, fee)
        equity = cash + un
        equity_curve.append({
            "timestamp": int(t_now), "cash_equity": cash, "unrealized_pnl": un,
            "total_equity": equity, "open_positions_count": len(open_positions),
            "open_risk_pct": (portfolio_open_risk(open_positions, fee, slip) / equity
                              if equity > 0 else 0.0),
        })

    # ── 期末 ──
    last_ts = int(timeline[-1]) if len(timeline) else None
    if cfg.force_close_at_end and open_positions:
        for sym in sorted(list(open_positions)):
            pos = open_positions[sym]
            arr = arrays[sym]
            last_close = float(arr["close"][-1])
            fill = (round_sell_price(last_close * (1.0 - slip), step) if pos.direction == 1
                    else round_buy_price(last_close * (1.0 + slip), step))
            cash, rec = _close_position(pos, cash, fill, fee, int(last_ts),
                                        "force_close_eod", open_positions, sym, raw_target_px=last_close)
            closed.append(rec)
            sum_gross += rec["gross_pnl"]
            sum_exit_fee += rec["exit_fee"]        # 入场费已在相位 C 计入一次，此处不得重复
            decisions.append({"timestamp": int(last_ts), "symbol": sym,
                              "phase": "EOD", "action": "force_close_eod",
                              "reason": "force_close_eod", "price": fill})
        if equity_curve:
            equity_curve[-1].update({"cash_equity": cash, "unrealized_pnl": 0.0,
                                     "total_equity": cash, "open_positions_count": 0,
                                     "open_risk_pct": 0.0})

    un_final = unrealized_pnl(open_positions, fee)
    final_equity = cash + un_final                       # force_close=False 也返回真实 MTM
    if equity_curve:
        equity_curve[-1]["cash_equity"] = cash
        equity_curve[-1]["unrealized_pnl"] = un_final
        equity_curve[-1]["total_equity"] = final_equity
        equity_curve[-1]["open_positions_count"] = len(open_positions)

    for pos in open_positions.values():
        funding_gaps_all.extend({"symbol": pos.symbol, **g} for g in pos.funding_gaps)
    for rec in closed:
        for g in rec.get("funding_gaps") or []:
            funding_gaps_all.append({"symbol": rec["symbol"], **g})

    net_closed = sum(r["net_pnl"] for r in closed)
    un_net_open = unrealized_net_open(open_positions, fee)
    sum_slip = float(sum(r.get("slippage_loss", 0.0) for r in closed))
    recon = {
        "initial_equity": float(cfg.initial_equity),
        "final_cash": float(cash),
        "sum_gross_pnl": float(sum_gross),
        "sum_entry_fee": float(sum_entry_fee),
        "sum_exit_fee": float(sum_exit_fee),
        "sum_fees": float(sum_entry_fee + sum_exit_fee),
        "sum_slippage_loss": sum_slip,
        "sum_funding_fee": float(sum(r["funding_fee"] for r in closed)
                                 + sum(p.cum_funding_fee for p in open_positions.values())),
        "funding_settlement_model": cfg.funding_model,
        "fee_model": {
            "tier": "Binance_VIP0",
            "maker_fee_rate": maker_fee,
            "taker_fee_rate": taker_fee,
        },
        "risk_model": {
            "risk_per_trade": float(cfg.risk_per_trade),
            "max_concurrent_trades": int(getattr(cfg, "max_concurrent_trades", 3)),
            "max_portfolio_margin_pct": float(getattr(cfg, "max_portfolio_margin_pct", 0.40)),
            "max_leverage": float(cfg.max_leverage_per_symbol),
        },
        "sum_net_pnl_closed": float(net_closed),
        "unrealized_net_open": float(un_net_open),
        "final_equity": float(final_equity),
        "expected_cash": float(cfg.initial_equity + sum_gross - sum_entry_fee
                               - sum_exit_fee
                               - (sum(r["funding_fee"] for r in closed)
                                  + sum(p.cum_funding_fee
                                        for p in open_positions.values()))),
        "cash_identity_ok": abs(cash - (cfg.initial_equity + sum_gross - sum_entry_fee
                                        - sum_exit_fee
                                        - (sum(r["funding_fee"] for r in closed)
                                           + sum(p.cum_funding_fee
                                                 for p in open_positions.values())))) < 1e-6,
        "equity_identity_ok": abs((final_equity - cfg.initial_equity)
                                  - (net_closed + un_net_open)) < 1e-6,
        "equity_curve_tail_matches_final": (not equity_curve) or
            abs(equity_curve[-1]["total_equity"] - final_equity) < 1e-9,
    }

    return {
        "strategy_id": "WX_OI_RESONANCE_V1",
        "time_unit": unit,
        "trade_start_sec": (None if trade_start_sec is None else int(trade_start_sec)),
        "funding_model": cfg.funding_model,
        "funding_is_assumption": bool(funding_is_assumption),
        "initial_equity": float(cfg.initial_equity),
        "final_cash": float(cash),
        "final_equity": float(final_equity),
        "open_positions_remaining": sorted(open_positions),
        "net_return_pct": ((final_equity - cfg.initial_equity) / cfg.initial_equity * 100.0
                           if cfg.initial_equity else None),
        "trades_count": len(closed),
        "trades": closed,
        "decisions": decisions,
        "rejected_orders": rejected,
        "equity_curve": equity_curve,
        "funding_gaps": funding_gaps_all,
        "reconcile": recon,
        "max_drawdown_pct": _max_dd(equity_curve),
    }


def _close_position(pos: OpenPosition, cash: float, fill: float, fee: float,
                    ts: int, reason: str, store: Dict[str, OpenPosition],
                    sym: str, raw_target_px: Optional[float] = None) -> Tuple[float, dict]:
    exit_fee = fill * pos.qty * fee
    gross = (fill - pos.entry_price) * pos.qty * pos.direction
    cash += gross - exit_fee
    target_px = raw_target_px if raw_target_px is not None else fill
    exit_slip_loss = abs(fill - target_px) * pos.qty
    total_slip_loss = getattr(pos, "entry_slip_loss", 0.0) + exit_slip_loss
    rec = {
        "symbol": sym, "direction": pos.direction, "signal_type": pos.signal_type,
        "entry_time": int(pos.entry_time), "exit_time": int(ts),
        "entry_price": pos.entry_price, "exit_price": fill, "qty": pos.qty,
        "gross_pnl": gross, "entry_fee": pos.entry_fee, "exit_fee": exit_fee,
        "fees": pos.entry_fee + exit_fee, "funding_fee": pos.cum_funding_fee,
        "slippage_loss": total_slip_loss,
        "net_pnl": gross - pos.entry_fee - exit_fee - pos.cum_funding_fee,
        "mfe": pos.mfe, "mae": pos.mae, "exit_reason": reason,
        "funding_gaps": list(pos.funding_gaps),
        "stop_trace": list(pos.stop_trace),
    }
    del store[sym]
    return cash, rec


def _max_dd(equity_curve) -> float:
    if len(equity_curve) < 2:
        return 0.0
    eq = np.array([p["total_equity"] for p in equity_curve], dtype=np.float64)
    peaks = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - eq) / peaks, 0.0)
    return float(np.max(dd) * 100.0)


def _empty_result(cfg: ExecParams, trade_start_sec: Optional[int] = None) -> dict:
    return {
        "strategy_id": "WX_OI_RESONANCE_V1", "time_unit": None,
        "trade_start_sec": (None if trade_start_sec is None else int(trade_start_sec)),
        "funding_model": cfg.funding_model,
        "funding_is_assumption": (cfg.funding_model == "assumption_fixed_8h"),
        "initial_equity": float(cfg.initial_equity),
        "final_cash": float(cfg.initial_equity),
        "final_equity": float(cfg.initial_equity),
        "open_positions_remaining": [], "net_return_pct": 0.0,
        "trades_count": 0, "trades": [], "decisions": [], "rejected_orders": [],
        "equity_curve": [], "funding_gaps": [],
        "max_drawdown_pct": 0.0,
        "reconcile": {"initial_equity": float(cfg.initial_equity),
                      "final_cash": float(cfg.initial_equity),
                      "final_equity": float(cfg.initial_equity),
                      "cash_identity_ok": True, "equity_identity_ok": True,
                      "equity_curve_tail_matches_final": True},
    }


def version_block() -> dict:
    from .config import CORE_VERSION, EXEC_CORE_VERSION, SIGNAL_VERSION, STRATEGY_ID
    return {"strategy_id": STRATEGY_ID, "signal_version": SIGNAL_VERSION,
            "core_version": CORE_VERSION, "exec_core_version": EXEC_CORE_VERSION,
            "signal_params_hash": SIGNAL_FROZEN.hash(),
            "exec_params_hash": EXEC_FROZEN.hash()}


__all__ = [
    "LiveOrderForbidden", "ExecutionInputError", "assert_live_disabled",
    "round_buy_price", "round_sell_price", "round_qty_down",
    "OpenPosition", "unrealized_pnl", "unrealized_net_open", "portfolio_open_risk",
    "run_portfolio_execution", "version_block",
]
