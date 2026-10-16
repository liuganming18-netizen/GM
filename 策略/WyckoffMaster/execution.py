# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 **共用执行核心**（策略窗 ⇄ 回测窗 ⇄ 实时模拟 的唯一成交/风控口径）。

存在意义（为什么必须只有一个核心）
────────────────────────────────────
历史回放（任务三）与实时模拟（任务二）若各写一套成交/风控，必然出现「同一根 bar、
同一份配置，两处得到不同决定」。本模块把**决策**收敛成纯函数：输入 = 已闭合 bar 序列 +
冻结配置 + 账户态快照；输出 = 带**条件实际值 / 阈值 / 时间 / 拒绝原因 / 事件身份**的决定。
两侧都调它，才谈得上「同一策略、风控与成交逻辑」。

覆盖的十项重点修复（逐条对应）
──────────────────────────────
 1. **short 不被改写为 long**：`normalize_direction()` 只认 long/short，其余一律拒绝；
    v3.1 只多 ⇒ short 记 `short_disabled_by_frozen_view_only_long` **显式拒单**，
    绝不静默翻转成多单。决定记录里 `direction` 恒为真实方向。
 2. **参数不被默认值覆盖**：`EXEC_FROZEN` 是唯一真值；`resolve_config()` 对任何
    改动冻结键的尝试**报错拒绝**（`frozen_key_override_rejected`）；视图不可读时
    标注 `config_source=...fallback` + `degraded=True`，**不静默替换**。
 3. **缺结构低点不退回固定止损**：`structural` / `structural_m2b` 模式下
    `struct_low` 缺失/非有限 ⇒ 拒单 `missing_struct_low_for_structural_stop`；
    只有 M0/M1 的 `uniform` 模式才允许无结构低点。绝不把结构止损偷偷降级为 3ATR。
 4. **非法模式被拒绝**：`validate_modes()` 对 entry/stop/trail/rank/factor/direction
    逐项校验，未知值 ⇒ `illegal_*_mode`。
 5. **入场 15 分钟因果时钟**：`order_request_ms = max(信号收盘+1ms, available_at, 真实首次请求)`；
    `earliest_fill_ms = order_request_ms + 15m`；成交价 = 该时刻**首根已闭合 15m bar 的 open**
    （+不利滑点、按步长取整）。**只用该 open**，不用同根 low/close；成交 bar 晚于尝试时钟 ⇒
    `bar_after_attempt_clock_no_lookahead`；错过时刻超容忍 ⇒ `missed_fill_moment_no_backfill`。
 6. **退出事件顺序**：同一根 15m 内固定 4 相位 —— ①真实资金费结算 → ②到期/回撤清仓/预约出场
    → ③新入场 → ④bar 内止损；下一根生效的止损在开盘判断前启用；止损优先。
 7. **台阶止损生效时间**：信号周期收盘评估，`effective_from_ms` = **下一根 15m 开盘**；
    单调不降、不回填（`apply_trail`）。
 8. **720 小时到期**：`expiry_exit_ms()` = 入场时刻 + 720h 之后**首根 15m 的开盘时刻**
    （不是「最后一根已闭合 bar 的收盘」）。
 9. **回撤锁定**：按 15m 收盘权益与峰值回撤 ≥20% ⇒ 停开仓 + 撤未成交挂单 +
    下一根 15m 开盘清仓 + 本轮**持续锁定**（`drawdown_state()`）。
10. **资金费结算**：按真实结算事件窗口 `(entry_ts, exit_ts]`，`qty × 结算 mark × rate`，
    正负原样（负费率收入保留）；缺 rate/mark ⇒ 记 `funding_gap`，**不补 0 冒充**；
    唯一事件身份 `(position, funding_time)` 幂等。

冻结约束（共享账户）
────────────────────
共享 5000 USDT、同时持仓**合计最多 10**、单笔止损风险 0.5% 权益、单币名义 ≤10%、
总多头 ≤80%、现金预留 ≥20%、组合初始止损风险 ≤3%、参与率 ≤上一根 15m quote 的 1%。
旧四账户历史**不由本模块读写**（本模块无 IO）；`allow_new_entries` 与
`maintain_existing` 是**两个独立开关**，旧仓维护（退出/资金费/到期/回撤）不受新开仓开关影响。

边界
────
纯标准库 + numpy（复用 `core`/`series`/`params`/`segments`）。不读库、不联网、不下单、
不持有全局状态；同一输入必得同一输出；只消费**已闭合** bar，追加未来数据不改变过去决定。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, asdict, fields, replace

from . import core, segments as SEG
from .params import FROZEN as PARAMS_FROZEN, MasterParams, ENTRY_MODES, STOP_MODES, TRAIL_MODES
from .params import RANK_MODES, FACTOR_FILTERS

# ══════════════════════════ 版本标识（稳定接口的一部分）══════════════════════════

STRATEGY_VERSION = "3.1"
CORE_VERSION = "exec_core_v3.1.0"
LEDGER_MODEL_VERSION = "paper_exec_v2_shared_5000"

DIRECTIONS = ("long", "short")

# 常量（毫秒）
MIN15_MS = 900_000
HOUR_MS = 3_600_000
MIN_MS = 60_000


# ══════════════════════════ 冻结执行/风控配置 ══════════════════════════

@dataclass(frozen=True)
class ExecConfig:
    """共享账户的执行与风控冻结值（v3.1 + 冻结参数视图 v2.2）。

    **只增不改**：任何阈值变更都要走新版本标识，禁止在调用处覆盖。
    """

    # —— 账户（共享）——
    initial_equity: float = 5000.0
    shared_account: bool = True
    max_positions_total: int = 10

    # —— 单笔 / 组合风险 ——
    risk_per_trade_fraction: float = 0.005      # 0.5% 权益
    single_name_notional_cap: float = 0.10      # 单币名义 ≤10% 权益
    gross_long_notional_cap: float = 0.80       # 总多头名义 ≤80% 权益
    cash_reserve_fraction: float = 0.20         # 现金预留 ≥20%
    portfolio_stop_risk_cap: float = 0.03       # 组合初始止损风险 ≤3%
    entry_participation_prev15m_cap: float = 0.01   # ≤上一根完整 15m quote 的 1%
    leverage: float = 1.0
    short_enabled: bool = False                 # v3.1 只多

    # —— 成本 / 时钟 ——
    fee_each_side: float = 0.0005
    slippage_each_side: float = 0.0005
    entry_delay_minutes: int = 15
    exit_delay_minutes: int = 15
    max_holding_hours: int = 720
    max_fill_lag_ms: int = 3_600_000            # 错过成交时刻容忍（1h）

    # —— 回撤 ——
    max_drawdown_fraction: float = 0.20

    def identity(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def hash(self) -> str:
        canon = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()


EXEC_FROZEN = ExecConfig()
FROZEN_EXEC_KEYS = frozenset(f.name for f in fields(ExecConfig))

# 冻结参数视图（只读引用；回测窗与实时窗共用同一份，缺失只降级、不替换）
EFFECTIVE_VIEW = (
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
    "最新唐奇安威科夫接通与收尾_2026-10-11/5000U收尾与回测条件终验6/"
    "10_研究参数生效视图_v2_2_user5000_shared.json"
)

# 视图键 → 本模块冻结键（用于「视图与冻结值不一致」时的**报错**，而不是静默覆盖）
_VIEW_KEYS = {
    "initial_equity_USDT": "initial_equity",
    "max_positions": "max_positions_total",
    "risk_per_trade_equity_fraction": "risk_per_trade_fraction",
    "single_name_notional_equity_cap": "single_name_notional_cap",
    "gross_long_notional_equity_cap": "gross_long_notional_cap",
    "cash_or_free_margin_reserve_fraction": "cash_reserve_fraction",
    "portfolio_initial_stop_risk_cap": "portfolio_stop_risk_cap",
    "entry_participation_previous_15m_quote_cap": "entry_participation_prev15m_cap",
    "leverage": "leverage",
    "short_enabled": "short_enabled",
    "fee_each_side": "fee_each_side",
    "slippage_each_side": "slippage_each_side",
    "maximum_account_drawdown_fraction": "max_drawdown_fraction",
}


def load_exec_config(view_path: str | None = None) -> dict:
    """加载执行配置：以 `EXEC_FROZEN` 为准，读冻结视图**核对**而非覆盖。

    返回 `{config, config_source, degraded, view_sha256, mismatches, missing_view_keys, errors}`。
    · 视图可读且与冻结值**逐项一致** ⇒ `config_source="v3.1_frozen_view_verified"`。
    · 视图可读但某项不一致 ⇒ `mismatches` 列明，**仍以冻结值为准**（degraded=True）。
    · 视图不可读 ⇒ `config_source="v3.1_frozen_view_unreadable"`，degraded=True，**不猜值**。
    """
    cfg = EXEC_FROZEN
    src = "v3.1_frozen_builtin"
    degraded = False
    view_sha = None
    mismatches: list[dict] = []
    missing: list[str] = []
    errors: list[str] = []
    path = view_path or EFFECTIVE_VIEW
    try:
        raw = open(path, "rb").read()
        view_sha = hashlib.sha256(raw).hexdigest()
        view = json.loads(raw.decode("utf-8"))
        flat: dict = {}
        for section in ("execution", "risk", "stops"):
            sec = view.get(section) or {}
            if isinstance(sec, dict):
                flat.update(sec)
        for vk, ck in _VIEW_KEYS.items():
            if vk not in flat:
                missing.append(vk)
                continue
            got, want = flat[vk], getattr(cfg, ck)
            if isinstance(want, bool):
                same = bool(got) == want
            else:
                try:
                    same = abs(float(got) - float(want)) < 1e-12
                except (TypeError, ValueError):
                    same = False
            if not same:
                mismatches.append({"view_key": vk, "config_key": ck,
                                   "view_value": got, "frozen_value": want})
        degraded = bool(mismatches or missing)
        src = ("v3.1_frozen_view_verified" if not degraded
               else "v3.1_frozen_view_partial_mismatch")
    except FileNotFoundError:
        src, degraded = "v3.1_frozen_view_unreadable_missing", True
    except Exception as exc:                                    # noqa: BLE001
        src, degraded = f"v3.1_frozen_view_unreadable_{type(exc).__name__}", True
    if degraded and not errors:
        errors.append("config_view_degraded_frozen_values_still_enforced")
    return {"config": cfg, "config_source": src, "degraded": degraded,
            "view_sha256": view_sha, "mismatches": mismatches,
            "missing_view_keys": missing, "errors": errors,
            "config_hash": cfg.hash(), "core_version": CORE_VERSION,
            "strategy_version": STRATEGY_VERSION}


def resolve_config(overrides: dict | None = None) -> dict:
    """按**冻结键**解析配置；对任何改动冻结值的尝试**报错拒绝**。

    返回 `{config, errors, rejected_overrides, config_hash}`。
    `overrides` 只能提供与冻结值**相同**的值（用于显式确认）；不同 ⇒
    `frozen_key_override_rejected`；未知键 ⇒ `unknown_config_key`。
    """
    errors: list[str] = []
    rejected: list[dict] = []
    cfg = EXEC_FROZEN
    for k, v in (overrides or {}).items():
        if k not in FROZEN_EXEC_KEYS:
            errors.append(f"unknown_config_key:{k}")
            rejected.append({"key": k, "value": v, "reason": "unknown_config_key"})
            continue
        want = getattr(cfg, k)
        try:
            same = (bool(v) == want) if isinstance(want, bool) else abs(float(v) - float(want)) < 1e-12
        except (TypeError, ValueError):
            same = False
        if not same:
            errors.append(f"frozen_key_override_rejected:{k}")
            rejected.append({"key": k, "value": v, "frozen_value": want,
                             "reason": "frozen_key_override_rejected"})
    return {"config": cfg, "errors": errors, "rejected_overrides": rejected,
            "config_hash": cfg.hash()}


# ══════════════════════════ 模式校验（非法模式必须拒绝）══════════════════════════

def validate_modes(*, entry_mode="three_buy_points", stop_mode="structural",
                   trail_mode="none", rank_mode=None, factor_filter=None,
                   direction="long") -> list[str]:
    """逐项校验模式/方向；返回错误列表（空 = 合法）。未知值一律显式拒绝。"""
    errs: list[str] = []
    if entry_mode not in ENTRY_MODES:
        errs.append(f"illegal_entry_mode:{entry_mode}")
    if stop_mode not in STOP_MODES:
        errs.append(f"illegal_stop_mode:{stop_mode}")
    if trail_mode not in TRAIL_MODES:
        errs.append(f"illegal_trail_mode:{trail_mode}")
    if rank_mode is not None and rank_mode not in RANK_MODES:
        errs.append(f"illegal_rank_mode:{rank_mode}")
    if factor_filter is not None and factor_filter not in FACTOR_FILTERS:
        errs.append(f"illegal_factor_filter:{factor_filter}")
    if direction not in DIRECTIONS:
        errs.append(f"illegal_direction:{direction}")
    return errs


def normalize_direction(d) -> str | None:
    """方向归一：只认 long/short；其余返回 None（调用方**必须拒绝**，不得默认成 long）。"""
    if d is None:
        return None
    s = str(d).strip().lower()
    return s if s in DIRECTIONS else None


# ══════════════════════════ 决定记录（可审计的最小单元）══════════════════════════

def decision(*, kind, symbol, exchange="binance", event_identity, direction=None,
             reject_reason=None, reject_detail=None, condition_values=None,
             thresholds=None, times=None, **extra) -> dict:
    """构造一条**决定记录**：条件实际值 / 阈值 / 时间 / 拒绝原因 / 事件身份。

    所有决定（开仓、拒单、出场、锁利、到期、回撤、资金费）都用同一信封，
    便于任务三逐笔核对、任务一直接展示。
    """
    return {
        "core_version": CORE_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "kind": kind,
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "event_identity": event_identity,
        "reject_reason": reject_reason,
        "reject_detail": reject_detail,
        "condition_values": condition_values or {},
        "thresholds": thresholds or {},
        "times": times or {},
        "config_hash": EXEC_FROZEN.hash(),
        **extra,
    }


# ══════════════════════════ 价格/数量取整（与冻结执行层同口径）══════════════════

def round_buy_price(px: float, step: float) -> float:
    """买价向**更贵**方向取整（更保守）。step<=0 不取整。"""
    if not step or step <= 0:
        return px
    return step * math.ceil(px / step - 1e-9)


def round_protect_price(px: float, step: float) -> float:
    """多头保护价向**更低**方向取整（更保守）。step<=0 不取整。"""
    if not step or step <= 0:
        return px
    return step * math.floor(px / step + 1e-9)


def round_qty_down(qty: float, step: float) -> float:
    """数量向**更小**方向按步进取整（不向上凑单）。step<=0 不取整。"""
    if not step or step <= 0:
        return qty
    return step * math.floor(qty / step + 1e-9)


# ══════════════════════════ 开仓决定 ══════════════════════════

def size_position(*, equity, entry_price, stop_dist, prev15m_quote_volume, limits,
                  gross_open_notional=0.0, free_cash=None, portfolio_risk_open=0.0,
                  config: ExecConfig = EXEC_FROZEN) -> dict:
    """按 0.5% 权益风险 / 各上限求数量；任一上限不满足 ⇒ **整单拒单**（不凑单）。

    返回 `{qty, notional, risk_abs, reject, detail}`。`limits` 缺（无合约规格）⇒
    `missing_contract_spec`。所有阈值与实际值都写进 `detail` 供审计。
    """
    out = {"qty": None, "notional": None, "risk_abs": None, "reject": None, "detail": {}}
    if limits is None:
        out["reject"] = "missing_contract_spec"
        return out
    if entry_price is None or not math.isfinite(entry_price) or entry_price <= 0:
        out["reject"] = "nonpositive_entry"
        return out
    if stop_dist is None or not math.isfinite(stop_dist) or stop_dist <= 0:
        out["reject"] = "invalid_stop_reject_trade"
        return out
    if equity is None or not math.isfinite(equity) or equity <= 0:
        out["reject"] = "equity_unavailable"
        return out
    free = float(equity if free_cash is None else free_cash)

    risk_budget = config.risk_per_trade_fraction * equity
    qty_risk = risk_budget / stop_dist
    qty_single = (config.single_name_notional_cap * equity) / entry_price
    qty_gross = max(0.0, (config.gross_long_notional_cap * equity) - gross_open_notional) / entry_price
    need_cash = max(0.0, free - config.cash_reserve_fraction * equity)
    qty_cash = need_cash * config.leverage / entry_price
    qty_port = max(0.0, (config.portfolio_stop_risk_cap * equity) - portfolio_risk_open) / stop_dist
    qty_part = None
    if prev15m_quote_volume is not None and math.isfinite(prev15m_quote_volume):
        qty_part = (config.entry_participation_prev15m_cap * float(prev15m_quote_volume)) / entry_price

    caps = {"risk": qty_risk, "single_name": qty_single, "gross_long": qty_gross,
            "cash_reserve": qty_cash, "portfolio_stop_risk": qty_port}
    if qty_part is not None:
        caps["participation"] = qty_part
    binding = min(caps, key=lambda k: caps[k])
    qty_raw = caps[binding]
    out["detail"] = {
        "risk_budget": risk_budget, "stop_dist": stop_dist,
        "caps_qty": {k: round(v, 12) for k, v in caps.items()},
        "binding_cap": binding, "qty_raw": qty_raw,
        "thresholds": config.identity(),
    }
    if qty_raw <= 0:
        # 哪个上限为 0 就报哪个（顺序即优先级，便于归因）
        for k in ("single_name", "gross_long", "cash_reserve", "portfolio_stop_risk",
                  "participation", "risk"):
            if k in caps and caps[k] <= 0:
                out["reject"] = {
                    "single_name": "single_name_cap_exceeded",
                    "gross_long": "gross_long_cap_exceeded",
                    "cash_reserve": "cash_reserve_breach",
                    "portfolio_stop_risk": "portfolio_stop_risk_cap_exceeded",
                    "participation": "participation_cap_exceeded",
                    "risk": "risk_budget_zero",
                }[k]
                return out
        out["reject"] = "size_zero"
        return out

    step = float(limits.get("quantity_step") or 0.0)
    qty = round_qty_down(qty_raw, step)
    min_qty = float(limits.get("min_quantity") or 0.0)
    max_qty = float(limits.get("max_quantity") or 0.0)
    min_notional = float(limits.get("min_notional") or 0.0)
    if qty <= 0:
        out["reject"] = "qty_below_min_quantity"
        out["detail"]["qty_step"] = step
        return out
    if min_qty > 0 and qty + 1e-12 < min_qty:
        out["reject"] = "qty_below_min_quantity"
        out["detail"].update({"qty": qty, "min_quantity": min_qty})
        return out
    if max_qty > 0 and qty > max_qty + 1e-12:
        out["reject"] = "qty_above_max_quantity"
        out["detail"].update({"qty": qty, "max_quantity": max_qty})
        return out
    notional = qty * entry_price
    if min_notional > 0 and notional + 1e-9 < min_notional:
        out["reject"] = "notional_below_min_notional"
        out["detail"].update({"notional": notional, "min_notional": min_notional})
        return out
    # 取整后**重核**风险预算（取整只会更小 ⇒ 不超）
    risk_abs = qty * stop_dist
    if risk_abs > risk_budget + 1e-9:
        out["reject"] = "risk_budget_exceeded_after_round"
        out["detail"].update({"risk_abs": risk_abs, "risk_budget": risk_budget})
        return out
    out.update({"qty": qty, "notional": notional, "risk_abs": risk_abs})
    out["detail"].update({"qty": qty, "notional": notional, "risk_abs": risk_abs,
                          "min_quantity": min_qty, "max_quantity": max_qty,
                          "min_notional": min_notional})
    return out


def plan_entry(*, symbol, direction, signal_kind, signal_bar_close_time,
               available_at, request_clock_ms, open_15m, atr_val, struct_low=None,
               entry_mode="three_buy_points", stop_mode="structural", trail_mode="none",
               price_step=0.0, limits=None, equity=None, free_cash=None,
               gross_open_notional=0.0, open_risk_abs=0.0, prev15m_quote_volume=None,
               open_positions=0, attempt_clock_ms=None, exchange="binance",
               event_identity=None, config: ExecConfig = EXEC_FROZEN,
               extra_condition_values=None) -> dict:
    """**开仓唯一入口**（历史回放与实时模拟共用）。

    返回 `decision()` 信封；`reject_reason` 为 None 表示可开，并附
    `entry_price / stop_price / stop_dist / target_price / qty / notional / earliest_fill_ms`。
    """
    ident = event_identity or f"{exchange}|{symbol}|{signal_kind}|{signal_bar_close_time}"
    cv = {
        "signal_kind": signal_kind,
        "open_15m": open_15m,
        "atr_signal_bar": atr_val,
        "struct_low": struct_low,
        "equity": equity,
        "free_cash": free_cash,
        "gross_open_notional": gross_open_notional,
        "open_risk_abs": open_risk_abs,
        "prev15m_quote_volume": prev15m_quote_volume,
        "open_positions": open_positions,
    }
    if extra_condition_values:
        cv.update(extra_condition_values)
    th = {"entry_mode": entry_mode, "stop_mode": stop_mode, "trail_mode": trail_mode,
          "entry_delay_minutes": config.entry_delay_minutes,
          "max_positions_total": config.max_positions_total,
          "fee_each_side": config.fee_each_side,
          "slippage_each_side": config.slippage_each_side,
          "price_step": price_step}
    tm = {"signal_bar_close_time": signal_bar_close_time, "available_at": available_at,
          "request_clock_ms": request_clock_ms, "attempt_clock_ms": attempt_clock_ms}

    def _rej(reason, detail=None, **kw):
        return decision(kind="entry", symbol=symbol, exchange=exchange, direction=direction,
                        event_identity=ident, reject_reason=reason, reject_detail=detail,
                        condition_values=cv, thresholds=th, times=tm, **kw)

    # ① 模式与方向
    mode_errs = validate_modes(entry_mode=entry_mode, stop_mode=stop_mode,
                              trail_mode=trail_mode, direction=direction)
    if mode_errs:
        return _rej(mode_errs[0], {"mode_errors": mode_errs})
    if direction == "short" and not config.short_enabled:
        # v3.1 只多：显式拒单，**绝不**翻转成 long
        return _rej("short_disabled_by_frozen_view_only_long",
                    {"requested_direction": direction, "short_enabled": config.short_enabled})
    # ② 共享账户持仓数上限
    if int(open_positions) >= int(config.max_positions_total):
        return _rej("max_positions_total_reached",
                    {"open_positions": open_positions, "cap": config.max_positions_total})
    # ③ 因果时钟
    if signal_bar_close_time is None or request_clock_ms is None:
        return _rej("missing_clock_inputs")
    order_request_ms = int(max(int(signal_bar_close_time) + 1,
                               int(available_at) if available_at is not None else 0,
                               int(request_clock_ms)))
    earliest_fill_ms = order_request_ms + config.entry_delay_minutes * MIN_MS
    tm.update({"order_request_ms": order_request_ms, "earliest_fill_ms": earliest_fill_ms})
    if attempt_clock_ms is not None and int(attempt_clock_ms) < earliest_fill_ms:
        return _rej("pending_future_fill",
                    {"earliest_fill_ms": earliest_fill_ms, "attempt_clock_ms": attempt_clock_ms})
    # ④ 成交 bar（只用 open）
    if open_15m is None or not math.isfinite(open_15m) or open_15m <= 0:
        return _rej("missing_or_nonpositive_open")
    if attempt_clock_ms is not None:
        # 成交 bar 必须已经存在：由调用方保证（open_15m 取自 open_time>=earliest_fill 的首根）
        pass
    # ⑤ 结构低点必需性（**不退回固定止损**）
    uses_struct = stop_mode in ("structural", "structural_m2b")
    if uses_struct and (struct_low is None or not math.isfinite(struct_low)):
        return _rej("missing_struct_low_for_structural_stop",
                    {"stop_mode": stop_mode,
                     "note": "结构止损模式缺结构低点 ⇒ 拒绝，绝不降级为固定 3ATR"})
    # ⑥ 成交价与穿底拒单
    entry_raw = float(open_15m) * (1.0 + config.slippage_each_side)
    entry = round_buy_price(entry_raw, price_step)
    cv.update({"entry_raw": entry_raw, "entry_price": entry})
    if uses_struct and entry <= float(struct_low):
        return _rej("entry_below_struct_low",
                    {"entry_price": entry, "struct_low": float(struct_low)},
                    entry_price=entry)
    # ⑦ 初始止损
    r = core.compute_stop(entry, (float(struct_low) if uses_struct else None), atr_val,
                          PARAMS_FROZEN, stop_mode, price_step)
    if r["reject"]:
        return _rej(r["reject"], {"stop_raw": r.get("raw_stop"),
                                  "dist_after_round": r.get("dist_after_round")},
                    entry_price=entry)
    stop = r["stop_price"]
    if float(open_15m) <= stop:
        return _rej("open_already_through_stop", {"open_15m": open_15m, "stop_price": stop},
                    entry_price=entry)
    stop_dist = r["stop_dist"]
    # ⑧ 数量
    sz = size_position(equity=equity, entry_price=entry, stop_dist=stop_dist,
                       prev15m_quote_volume=prev15m_quote_volume, limits=limits,
                       gross_open_notional=gross_open_notional, free_cash=free_cash,
                       portfolio_risk_open=open_risk_abs, config=config)
    if sz["reject"]:
        return _rej(sz["reject"], sz["detail"], entry_price=entry, stop_price=stop,
                    stop_dist=stop_dist, target_price=r["target_price"])
    return decision(kind="entry", symbol=symbol, exchange=exchange, direction=direction,
                    event_identity=ident, condition_values=cv, thresholds=th, times=tm,
                    entry_price=entry, entry_raw=entry_raw, stop_price=stop,
                    stop_dist=stop_dist, target_price=r["target_price"],
                    stop_atr_mult=r["stop_atr_mult"], qty=sz["qty"],
                    notional=sz["notional"], risk_abs=sz["risk_abs"],
                    sizing_detail=sz["detail"], fill_price_rule="open_of_first_closed_15m_bar_at_or_after(order_request_ms+delay)",
                    ok=True)


# ══════════════════════════ 台阶锁利 / 保本（下一根 15m 生效）══════════════════

def apply_trail(*, closes, lows, current_stop, entry_price, entry_atr,
                evaluate_close_time, next_15m_open_time, mode="step_swing",
                params: MasterParams = PARAMS_FROZEN, upto=None) -> dict:
    """在信号周期收盘评估锁利，返回**下一根 15m 开盘起生效**的止损。

    `next_15m_open_time` = 紧随 `evaluate_close_time` 之后的 15m 开盘时刻
    （= 生效时刻，`effective_from_ms`）。单调不降、不回填由 `core.evaluate_trail_stop` 保证。
    """
    res = core.evaluate_trail_stop(closes, lows, current_stop, entry_price, entry_atr,
                                   params, mode, upto=upto)
    res = dict(res)
    res["mode"] = mode
    res["evaluate_close_time"] = evaluate_close_time
    res["effective_from_ms"] = next_15m_open_time
    res["monotonic_non_decreasing"] = res["stop"] >= current_stop - 1e-12
    res["no_backfill"] = bool(res["effective_from_ms"] is None
                              or evaluate_close_time is None
                              or res["effective_from_ms"] > evaluate_close_time)
    res["core_version"] = CORE_VERSION
    return res


def next_15m_open_at_or_after(ts_ms: int) -> int:
    """给定时刻之后（含）对齐到的 15m 开盘边界。"""
    return int(math.ceil(int(ts_ms) / MIN15_MS) * MIN15_MS)


def expiry_exit_ms(entry_ts_ms: int, max_holding_hours: int = EXEC_FROZEN.max_holding_hours) -> int:
    """720h 到期经济出场时刻 = 入场时刻 + 720h **之后首根 15m 的开盘**。

    修复 D01：原实现取「最后一根已闭合 15m 的收盘」，与冻结规则
    `fill_time_plus_720h_at_detail_open_no_added_delay` 不符。
    """
    return next_15m_open_at_or_after(int(entry_ts_ms) + int(max_holding_hours) * HOUR_MS)


def expiry_exit_from_bars(entry_ts_ms: int, bars_15m, max_holding_hours: int = 720) -> dict:
    """在已闭合 15m 序列里取到期出场 bar；**不用下一段/未来 bar**。"""
    due = expiry_exit_ms(entry_ts_ms, max_holding_hours)
    for b in bars_15m:
        ot = int(b["open_time"])
        if ot >= due:
            return {"ok": True, "due_ms": due, "bar_open_time": ot,
                    "exit_raw_price": float(b["open"]),
                    "bar_close_time": int(b["close_time"])}
    return {"ok": False, "due_ms": due, "reason": "expiry_bar_not_yet_closed"}


# ══════════════════════════ 回撤锁定 ══════════════════════════

def drawdown_state(*, equity, peak_equity, locked_before=False,
                   config: ExecConfig = EXEC_FROZEN) -> dict:
    """按 15m 收盘权益计算回撤与锁定态。

    ≥20% ⇒ 停开仓 + 撤未成交挂单 + 下一根 15m 开盘清仓 + 本轮**持续锁定**。
    缺权益（None）⇒ **停算**（不拿旧值掩盖），`actions=[]` 且 `computable=False`。
    """
    if equity is None or peak_equity is None or not math.isfinite(equity) or peak_equity <= 0:
        return {"computable": False, "drawdown": None, "locked": bool(locked_before),
                "actions": [], "reason": "equity_or_peak_unavailable",
                "thresholds": {"max_drawdown_fraction": config.max_drawdown_fraction}}
    dd = max(0.0, (float(peak_equity) - float(equity)) / float(peak_equity))
    tripped = dd >= config.max_drawdown_fraction
    locked = bool(locked_before or tripped)
    actions = []
    if tripped and not locked_before:
        actions = ["disable_new_entries", "cancel_pending_orders",
                   "liquidate_all_next_15m_open", "lock_for_run_no_resume"]
    return {"computable": True, "drawdown": dd, "locked": locked, "tripped_now": tripped,
            "actions": actions, "peak_equity": peak_equity, "equity": equity,
            "thresholds": {"max_drawdown_fraction": config.max_drawdown_fraction},
            "reason": "drawdown_lock" if tripped else "ok"}


# ══════════════════════════ 资金费结算 ══════════════════════════

def settle_funding_events(*, position_id, qty, entry_ts, exit_ts, events) -> dict:
    """按真实结算事件窗口 `(entry_ts, exit_ts]` 逐笔结算。

    `events`：可迭代 `{funding_time, funding_rate, mark_price}`（真实结算事件）。
    返回 `{charges, gaps, total_expense, n_events}`；`total_expense>0` 表示账户支出。
    缺 rate/mark ⇒ 记 `gap`，**不补 0**；负费率原样保留（账户收入）。
    """
    charges, gaps = [], []
    total = 0.0
    for e in events:
        ft = int(e["funding_time"])
        if not (int(entry_ts) < ft <= int(exit_ts)):
            continue
        rate = e.get("funding_rate")
        mark = e.get("mark_price")
        try:
            rate_f = None if rate is None else float(rate)
            mark_f = None if mark is None else float(mark)
        except (TypeError, ValueError):
            rate_f = mark_f = None
        if rate_f is None or mark_f is None or not math.isfinite(rate_f) or not math.isfinite(mark_f):
            gaps.append({"position_id": position_id, "funding_time": ft,
                         "reason": "missing_real_rate_or_mark",
                         "funding_rate": rate, "mark_price": mark})
            continue
        amt = float(qty) * mark_f * rate_f          # 支出口径：>0 支付、<0 收取
        total += amt
        charges.append({"position_id": position_id, "funding_time": ft, "rate": rate_f,
                        "mark": mark_f, "qty": float(qty), "amount": amt,
                        "event_identity": f"funding|{position_id}|{ft}",
                        "sign_convention": "expense_positive"})
    return {"charges": charges, "gaps": gaps, "total_expense": total,
            "n_events": len(charges), "n_gaps": len(gaps),
            "forbidden_substitutes": list(SEG.EXEC_COST["forbidden_funding_mark_substitutes"])}


# ══════════════════════════ 退出事件顺序（同一根 15m 内）══════════════════════════

PHASE_FUNDING = 1        # ①真实资金费结算（entry < funding_time 且 exit 未发生）
PHASE_LIQUIDATE = 2      # ②到期 / 回撤清仓 / 预约出场
PHASE_ENTRY = 3          # ③新入场
PHASE_STOP = 4           # ④bar 内止损（止损优先于同 bar 其它价量事件）

EXIT_EVENT_ORDER = ("funding_settlement", "expiry_or_drawdown_or_scheduled_exit",
                    "new_entry", "intrabar_stop")


def order_bar_events(*, funding=(), expiries=(), drawdown_liquidations=(),
                     scheduled_exits=(), new_entries=(), intrabar_stops=()) -> list[dict]:
    """把同一根 15m 内的事件排成**固定 4 相位**（回测与实时模拟共用同一顺序）。

    相位 2 内部再按 (到期, 回撤清仓, 预约出场) 排列；相位 4 的止损在开盘判断前
    已启用**下一根生效**的止损（由调用方在相位 2 之前把 `current_stop` 更新到位）。
    """
    out: list[dict] = []
    for e in funding:
        out.append({**e, "phase": PHASE_FUNDING, "event": "funding_settlement"})
    for e in expiries:
        out.append({**e, "phase": PHASE_LIQUIDATE, "event": "expiry"})
    for e in drawdown_liquidations:
        out.append({**e, "phase": PHASE_LIQUIDATE, "event": "drawdown_liquidation"})
    for e in scheduled_exits:
        out.append({**e, "phase": PHASE_LIQUIDATE, "event": "scheduled_exit"})
    for e in new_entries:
        out.append({**e, "phase": PHASE_ENTRY, "event": "new_entry"})
    for e in intrabar_stops:
        out.append({**e, "phase": PHASE_STOP, "event": "intrabar_stop"})
    out.sort(key=lambda x: (x["phase"], x.get("position_id", -1), str(x.get("symbol", ""))))
    return out


def stop_hit_for_bar(*, direction, stop_price, bar_open, bar_high, bar_low) -> dict | None:
    """单根 15m 的止损判定（止损优先；开盘已穿 ⇒ 用更差 open）。

    返回 `{raw_price, gapped, rule}`；未触发 ⇒ None。只读该 bar 自身 OHLC。
    """
    if stop_price is None:
        return None
    sp = float(stop_price)
    op = None if bar_open is None else float(bar_open)
    hi = None if bar_high is None else float(bar_high)
    lo = None if bar_low is None else float(bar_low)
    if direction == "short":
        if hi is None or hi < sp:
            return None
        if op is not None and op >= sp:
            return {"raw_price": op, "gapped": True, "rule": "gap_open_worse_than_stop"}
        return {"raw_price": sp, "gapped": False, "rule": "stop_price_touched"}
    if lo is None or lo > sp:
        return None
    if op is not None and op <= sp:
        return {"raw_price": op, "gapped": True, "rule": "gap_open_worse_than_stop"}
    return {"raw_price": sp, "gapped": False, "rule": "stop_price_touched"}


# ══════════════════════════ 确定性回放/模拟引擎 ══════════════════════════

@dataclass
class SimPosition:
    position_id: int
    symbol: str
    direction: str
    qty: float
    entry_price: float
    entry_ts: int
    stop_price: float
    entry_atr: float
    entry_fee: float
    signal_kind: str
    event_identity: str
    funding_expense: float = 0.0
    last_trail_close_ts: int | None = None


class ReplayEngine:
    """**共用**回放/模拟引擎（历史回放与实时模拟同一实现）。

    输入：已闭合 1h 信号 bar、已闭合 15m 执行 bar、真实结算资金费事件、合约规格、
    账户初始资金与两个**独立开关**：
      · `allow_new_entries` —— 新开仓总闸（默认 False，正式候选未选定/未验收时继续阻断）；
      · `maintain_existing` —— 旧仓维护（退出/资金费/到期/回撤），默认 True，
        **不受**新开仓开关影响。

    纪律：
      · 只处理 `open_time <= now_ms` 的已闭合 bar；未来数据不参与任何过去决定。
      · 状态单调推进（水位线只增），故「追加未来数据不改变过去决定」由构造保证。
      · 所有决定写入 `self.decisions`（同一信封），可用 `decisions_since()` 取前缀比对。
    """

    def __init__(self, *, bars_signal, bars_15m, limits_by_symbol=None,
                 funding_events_by_symbol=None, symbol="SYNTH", exchange="binance",
                 entry_mode="three_buy_points", stop_mode="structural", trail_mode="none",
                 allow_new_entries=False, maintain_existing=True, now_ms=None,
                 contract_spec=None, config: ExecConfig = EXEC_FROZEN):
        self.cfg = config
        self.symbol = symbol
        self.exchange = exchange
        self.entry_mode = entry_mode
        self.stop_mode = stop_mode
        self.trail_mode = trail_mode
        self.allow_new_entries = bool(allow_new_entries)
        self.maintain_existing = bool(maintain_existing)
        self.limits = contract_spec or {"quantity_step": 0.0, "min_quantity": 0.0,
                                        "max_quantity": 0.0, "min_notional": 0.0,
                                        "price_step": 0.0}
        self.bars_15m = sorted((dict(b) for b in bars_15m), key=lambda b: int(b["open_time"]))
        self.funding = sorted((dict(e) for e in (funding_events_by_symbol or [])),
                              key=lambda e: int(e["funding_time"]))
        self.cash = float(config.initial_equity)
        self.peak_equity = float(config.initial_equity)
        self.positions: dict[int, SimPosition] = {}
        self._next_pid = 1
        self.locked = False
        self.pending = []                      # 未成交挂单（撤单由回撤动作触发）
        self.decisions: list[dict] = []
        self.expired_exit: list[dict] = []
        self.funding_charges: list[dict] = []
        self.funding_gaps: list[dict] = []
        self.now_ms = int(now_ms) if now_ms is not None else None

        # 信号：一次性算好（纯函数，前缀无前视）；登记 (close_time, idx) 供因果入场
        self._frame = core.compute_signals(bars_signal, PARAMS_FROZEN,
                                          entry_mode, stop_mode, 0.0) if bars_signal else None
        self._bars_signal = list(bars_signal or [])
        self._signals: list[dict] = []
        if self._frame is not None:
            for i in range(len(self._frame.master_entry)):
                if self._frame.master_entry[i]:
                    ct = int(self._bars_signal[i]["close_time"])
                    self._signals.append({
                        "close_time": ct, "idx": i,
                        "earliest_fill_ms": ct + 1 + self.cfg.entry_delay_minutes * MIN_MS,
                        "consumed": False})
            self._donchian_exits = set()
            for i in range(len(self._bars_signal)):
                c = float(self._bars_signal[i]["close"])
                exit_low = self._frame.don_exit_low[i]
                if math.isfinite(exit_low) and c < exit_low:
                    sig_ct = int(self._bars_signal[i]["close_time"])
                    exit_bar_t = next_15m_open_at_or_after(sig_ct + 1 + self.cfg.exit_delay_minutes * MIN_MS)
                    self._donchian_exits.add(exit_bar_t)
        else:
            self._donchian_exits = set()

    # ── 只读态 ──
    def equity(self) -> float:
        """按当前 15m 已闭合收盘估值（现金 + 浮盈）。"""
        upnl = 0.0
        for p in self.positions.values():
            mark = self._mark(p.symbol)
            if mark is None:
                return float("nan")
            if p.direction == "short":
                upnl += (p.entry_price - mark) * p.qty
            else:
                upnl += (mark - p.entry_price) * p.qty
        return self.cash + upnl

    def _mark(self, symbol):
        last = None
        for b in self.bars_15m:
            if self.now_ms is not None and int(b["close_time"]) > self.now_ms:
                break
            last = b
        return None if last is None else float(last["close"])

    def gross_notional(self) -> float:
        return sum(p.qty * p.entry_price for p in self.positions.values())

    def open_risk_abs(self) -> float:
        return sum(p.qty * abs(p.entry_price - p.stop_price) for p in self.positions.values())

    # ── 主循环 ──
    def run(self, *, upto_ms=None) -> dict:
        """逐根已闭合 15m bar 推进；`upto_ms` 截断（用于「追加未来数据不改过去」核验）。"""
        limit = int(upto_ms) if upto_ms is not None else None
        for b in self.bars_15m:
            ot, ct = int(b["open_time"]), int(b["close_time"])
            if limit is not None and ot > limit:
                break
            self.now_ms = ct                      # 该 bar 已闭合，close 可用
            self._process_bar(b)
        # 段末强平（本段最后 15m 收盘）
        if limit is None and self.maintain_existing and self.bars_15m and self.positions:
            last_b = self.bars_15m[-1]
            last_ot = int(last_b["open_time"])
            last_close = float(last_b["close"])
            for pid, p in list(self.positions.items()):
                self._close(p, pid, raw_price=last_close, exit_ts=last_ot,
                            reason="segment_end_force_close_last_15m_close",
                            detail={"last_bar_open": last_ot})
        return {"decisions": self.decisions, "expired_exit": self.expired_exit,
                "funding_charges": self.funding_charges, "funding_gaps": self.funding_gaps,
                "cash": self.cash, "equity": self.equity(),
                "open_positions": len(self.positions), "locked": self.locked,
                "allow_new_entries": self.allow_new_entries,
                "maintain_existing": self.maintain_existing}

    def _process_bar(self, b):
        ot, ct = int(b["open_time"]), int(b["close_time"])
        # ── 相位 ①：真实资金费结算 ──
        if self.maintain_existing:
            self._phase_funding(ct)
        # ── 相位 ②：到期 / 回撤清仓 / 预约出场 ──
        if self.maintain_existing:
            self._phase_liquidate(b, ot, ct)
        # ── 相位 ③：新入场 ──
        self._phase_entry(b, ot, ct)
        # ── 相位 ④：bar 内止损 ──
        if self.maintain_existing:
            self._phase_stop(b)

    def _phase_funding(self, ct):
        for pid, p in list(self.positions.items()):
            r = settle_funding_events(position_id=pid, qty=p.qty, entry_ts=p.entry_ts,
                                      exit_ts=ct, events=self.funding)
            for c in r["charges"]:
                if any(x["event_identity"] == c["event_identity"] for x in self.funding_charges):
                    continue                          # 幂等：唯一事件身份
                self.funding_charges.append(c)
                self.cash -= c["amount"]              # ΔCash = −amount
                p.funding_expense += c["amount"]
            self.funding_gaps.extend(r["gaps"])
        self._update_peak()

    def _phase_liquidate(self, b, ot, ct):
        # ②-a 到期（720h，首根 15m 开盘）
        for pid, p in list(self.positions.items()):
            due = expiry_exit_ms(p.entry_ts, self.cfg.max_holding_hours)
            if ot >= due:
                self._close(p, pid, raw_price=float(b["open"]), exit_ts=ot,
                            reason="max_holding_720h_at_first_15m_open",
                            detail={"due_ms": due})
                self.expired_exit.append({"position_id": pid, "due_ms": due, "exit_bar_open": ot})
        # ②-b 回撤锁定（每根 15m 收盘**唯一**一次判定）
        eq = self.equity()
        if math.isfinite(eq):
            self.peak_equity = max(self.peak_equity, eq)
        st = drawdown_state(equity=(eq if math.isfinite(eq) else None),
                            peak_equity=self.peak_equity, locked_before=self.locked,
                            config=self.cfg)
        if st["tripped_now"]:
            self.locked = True
            self.pending = []                     # 撤未成交挂单
            self.decisions.append(decision(
                kind="drawdown_lock", symbol=self.symbol, exchange=self.exchange,
                direction=None, event_identity=f"dd|{ct}",
                condition_values={"equity": st["equity"], "peak_equity": st["peak_equity"]},
                thresholds={"max_drawdown_fraction": self.cfg.max_drawdown_fraction},
                times={"bar_open_time": ot, "bar_close_time": ct},
                drawdown=st["drawdown"], actions=st["actions"]))
            nxt = self._next_bar(ot)
            if nxt is not None:
                for pid, p in list(self.positions.items()):
                    self._close(p, pid, raw_price=float(nxt["open"]),
                                exit_ts=int(nxt["open_time"]),
                                reason="drawdown_liquidation_next_15m_open",
                                detail={"drawdown": st["drawdown"]})
        # ②-c 唐奇安预约出场（跌破 10 周期低点，在预约的 15m 开盘平仓）
        if ot in self._donchian_exits:
            for pid, p in list(self.positions.items()):
                self._close(p, pid, raw_price=float(b["open"]), exit_ts=ot,
                            reason="donchian_exit", detail={"exit_bar": ot})

    def _phase_entry(self, b, ot, ct):
        """相位 ③：新入场（受 `allow_new_entries` 与回撤锁定控制）。

        因果：`earliest_fill_ms = 信号收盘+1ms + 15m`；本相位只在
        `bar.open_time >= earliest_fill_ms` 的**首根** bar 上成交，成交价 = 该 bar 的 **open**。
        """
        if not self.allow_new_entries or self.locked:
            return
        for sig in self._signals:
            if sig["consumed"]:
                continue
            if ot < sig["earliest_fill_ms"]:
                break                                  # 信号按 close_time 升序 ⇒ 后面的更晚
            sig["consumed"] = True
            if any(p.symbol == self.symbol for p in self.positions.values()):
                continue
            idx = sig["idx"]
            struct_low = self._frame.struct_low[idx]
            struct_low = None if (struct_low is None or not math.isfinite(struct_low)) else float(struct_low)
            atr_v = self._frame.atr[idx]
            atr_v = None if (atr_v is None or not math.isfinite(atr_v)) else float(atr_v)
            kind = self._frame.signal_kind[idx]
            d = plan_entry(
                symbol=self.symbol, direction="long", signal_kind=kind,
                signal_bar_close_time=sig["close_time"], available_at=sig["close_time"],
                request_clock_ms=sig["close_time"], open_15m=float(b["open"]),
                atr_val=atr_v, struct_low=struct_low, entry_mode=self.entry_mode,
                stop_mode=self.stop_mode, trail_mode=self.trail_mode,
                price_step=float(self.limits.get("price_step") or 0.0), limits=self.limits,
                equity=self.equity(), free_cash=self.cash,
                gross_open_notional=self.gross_notional(),
                open_risk_abs=self.open_risk_abs(),
                prev15m_quote_volume=float(self._prev_quote(ot) or 0.0),
                open_positions=len(self.positions), attempt_clock_ms=ot,
                exchange=self.exchange,
                event_identity=f"{self.exchange}|{self.symbol}|{kind}|{sig['close_time']}",
                config=self.cfg)
            self.decisions.append(d)
            if d.get("reject_reason"):
                continue
            fee = d["notional"] * self.cfg.fee_each_side
            self.cash -= fee
            pid = self._next_pid
            self._next_pid += 1
            self.positions[pid] = SimPosition(
                position_id=pid, symbol=self.symbol, direction="long", qty=d["qty"],
                entry_price=d["entry_price"], entry_ts=ot, stop_price=d["stop_price"],
                entry_atr=atr_v or 0.0, entry_fee=fee, signal_kind=kind,
                event_identity=d["event_identity"])

    def _prev_quote(self, ot):
        """**严格早于** `ot` 的最近一根已闭合 15m 的 quote 成交额（参与率用；无前视）。"""
        prev = None
        for b in self.bars_15m:
            if int(b["open_time"]) >= ot:
                break
            prev = b
        return None if prev is None else prev.get("quote_volume")

    def _phase_stop(self, b):
        for pid, p in list(self.positions.items()):
            hit = stop_hit_for_bar(direction=p.direction, stop_price=p.stop_price,
                                   bar_open=b["open"], bar_high=b["high"], bar_low=b["low"])
            if hit is None:
                continue
            self._close(p, pid, raw_price=hit["raw_price"], exit_ts=int(b["open_time"]),
                        reason="stop_loss", detail=hit)

    # ── 内部工具 ──
    def _next_bar(self, ot):
        for b in self.bars_15m:
            if int(b["open_time"]) > ot:
                return b
        return None

    def _update_peak(self):
        """只更新权益峰值（回撤**判定**只在相位②的 15m 收盘做一次）。"""
        eq = self.equity()
        if math.isfinite(eq):
            self.peak_equity = max(self.peak_equity, eq)
        return self.peak_equity

    def _close(self, p: SimPosition, pid: int, *, raw_price, exit_ts, reason, detail=None):
        raw = float(raw_price)
        tick = float(self.limits.get("price_step") or 0.0)
        if p.direction == "short":
            fill = round_buy_price(raw * (1.0 + self.cfg.slippage_each_side), tick)
            gross = (p.entry_price - fill) * p.qty
        else:
            fill = round_protect_price(raw * (1.0 - self.cfg.slippage_each_side), tick)
            gross = (fill - p.entry_price) * p.qty
        fee = p.qty * fill * self.cfg.fee_each_side
        # 结算窗口内剩余资金费
        r = settle_funding_events(position_id=pid, qty=p.qty, entry_ts=p.entry_ts,
                                  exit_ts=exit_ts, events=self.funding)
        add_fund = 0.0
        for c in r["charges"]:
            if any(x["event_identity"] == c["event_identity"] for x in self.funding_charges):
                continue
            self.funding_charges.append(c)
            add_fund += c["amount"]
        self.funding_gaps.extend(r["gaps"])
        net = gross - p.entry_fee - fee - p.funding_expense - add_fund
        self.cash += gross - fee - add_fund
        del self.positions[pid]
        self.decisions.append(decision(
            kind="exit", symbol=p.symbol, exchange=self.exchange, direction=p.direction,
            event_identity=f"exit|{pid}|{exit_ts}", reject_reason=None,
            condition_values={"raw_price": raw, "fill_price": fill, "qty": p.qty,
                              "entry_price": p.entry_price, "gross_pnl": gross},
            thresholds={"slippage_each_side": self.cfg.slippage_each_side,
                        "fee_each_side": self.cfg.fee_each_side},
            times={"entry_ts": p.entry_ts, "exit_ts": exit_ts},
            position_id=pid, exit_reason=reason, net_pnl=net,
            entry_fee=p.entry_fee, exit_fee=fee, funding_expense=p.funding_expense + add_fund,
            detail=detail or {}))
        self._update_peak()

    # ── 前缀比对（「追加未来数据不改变过去决定」的核验接口）──
    def decisions_since(self, ts_ms) -> list[dict]:
        out = []
        for d in self.decisions:
            t = (d.get("times") or {}).get("bar_close_time") or \
                (d.get("times") or {}).get("signal_bar_close_time")
            if t is None or int(t) <= int(ts_ms):
                out.append(d)
        return out

    def seed_position(self, *, qty, entry_price, entry_ts, stop_price, direction="long",
                      entry_atr=0.0, signal_kind="seeded", event_identity=None,
                      entry_fee=None) -> int:
        """注入一笔**已存在**持仓（用于「旧仓维护 vs 新开仓开关」核验）。

        只用于核验与迁移对账，**不写任何库**；`maintain_existing=False` 时该持仓
        不会被本引擎维护（退出/资金费/到期都不执行），而 `allow_new_entries` 亦与之独立。
        """
        pid = self._next_pid
        self._next_pid += 1
        fee = float(entry_fee) if entry_fee is not None else \
            float(qty) * float(entry_price) * self.cfg.fee_each_side
        self.cash -= fee
        self.positions[pid] = SimPosition(
            position_id=pid, symbol=self.symbol, direction=direction, qty=float(qty),
            entry_price=float(entry_price), entry_ts=int(entry_ts),
            stop_price=float(stop_price), entry_atr=float(entry_atr),
            entry_fee=fee, signal_kind=signal_kind,
            event_identity=event_identity or f"seed|{self.symbol}|{entry_ts}")
        # 峰值保守取初始权益（不借未来 bar 的 mark 虚增峰值）
        self.peak_equity = max(self.peak_equity, float(self.cfg.initial_equity))
        return pid


# ══════════════════════════ 稳定接口与版本标识 ══════════════════════════

def interface() -> dict:
    """供任务三（回测）调用、任务一（展示）引用的**稳定接口清单**。"""
    return {
        "core_version": CORE_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "ledger_model_version": LEDGER_MODEL_VERSION,
        "config_hash": EXEC_FROZEN.hash(),
        "strategy_params_hash": _params_hash(),
        "segments": {k: {"start_ms": v["start_ms"], "end_ms": v["end_ms"]}
                     for k, v in SEG.SEGMENTS.items()},
        "frozen_config": EXEC_FROZEN.identity(),
        "frozen_constraints": {
            "shared_equity_usdt": EXEC_FROZEN.initial_equity,
            "max_positions_total": EXEC_FROZEN.max_positions_total,
            "short_enabled": EXEC_FROZEN.short_enabled,
            "legacy_four_accounts_preserved": True,
            "new_entry_default_blocked": True,
            "maintenance_independent_of_entry_switch": True,
        },
        "exit_event_order": list(EXIT_EVENT_ORDER),
        "entry_functions": {
            "plan_entry": "开仓唯一入口（含因果时钟/结构低点必需/各上限）",
            "size_position": "数量求解（0.5%风险 + 各上限，不凑单）",
            "validate_modes": "模式/方向合法性",
            "normalize_direction": "方向归一（绝不默认 long）",
            "load_exec_config": "配置加载（视图核对而非覆盖）",
            "resolve_config": "冻结键覆盖拒绝",
        },
        "exit_functions": {
            "apply_trail": "台阶锁利（下一根15m生效）",
            "expiry_exit_ms": "720h 到期经济时刻",
            "expiry_exit_from_bars": "到期出场 bar",
            "drawdown_state": "回撤锁定四动作",
            "settle_funding_events": "资金费事件结算",
            "order_bar_events": "同根15m事件顺序",
            "stop_hit_for_bar": "bar内止损（含跳空）",
        },
        "engine": {"ReplayEngine": "确定性回放/模拟引擎（前缀可比对）"},
        "decision_envelope": ["core_version", "strategy_version", "kind", "symbol", "exchange",
                              "direction", "event_identity", "reject_reason", "reject_detail",
                              "condition_values", "thresholds", "times", "config_hash"],
    }


def _params_hash() -> str:
    canon = json.dumps(PARAMS_FROZEN.identity(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def version_block() -> dict:
    return {"core_version": CORE_VERSION, "strategy_version": STRATEGY_VERSION,
            "ledger_model_version": LEDGER_MODEL_VERSION,
            "config_hash": EXEC_FROZEN.hash(), "params_hash": _params_hash(),
            "exec_config": asdict(EXEC_FROZEN)}


__all__ = [
    "CORE_VERSION", "STRATEGY_VERSION", "LEDGER_MODEL_VERSION", "DIRECTIONS",
    "EXEC_FROZEN", "ExecConfig", "FROZEN_EXEC_KEYS",
    "load_exec_config", "resolve_config", "validate_modes", "normalize_direction", "decision",
    "round_buy_price", "round_protect_price", "round_qty_down",
    "size_position", "plan_entry", "apply_trail", "next_15m_open_at_or_after",
    "expiry_exit_ms", "expiry_exit_from_bars", "drawdown_state", "settle_funding_events",
    "order_bar_events", "stop_hit_for_bar", "PHASE_FUNDING", "PHASE_LIQUIDATE",
    "PHASE_ENTRY", "PHASE_STOP", "EXIT_EVENT_ORDER",
    "ReplayEngine", "SimPosition", "interface", "version_block",
]
